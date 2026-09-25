# 阶段 5 设计：参考语料轻量检索（方案 C）

## 一、范围边界

### 做
1. **用户提供参考语料**：用户在 `data/style/reference.txt` 放一段纯文本（角色台词、聊天记录、语录、百科都行）
2. **启动时 chunk 预处理**：按段落拆成若干 chunk，存 JSON（不需要向量库）
3. **每次对话时轻量检索**：用 `difflib` 文本重叠度 + 关键词匹配，找最相关的 Top 3 片段
4. **动态注入 messages**：把检索到的参考片段拼进 system prompt，让 LLM 能精确引用角色的具体台词/设定
5. **热重载**：reference.txt 改了存盘 → 自动重新 chunk

### 不做
- 不做完整 RAG（Chroma/FAISS + Embedding）——违反轻量原则
- 不做从用户自己的消息里自动学习风格（后续再加）
- 不做多人设切换、图片/语音、管理后台

---

## 二、核心流程

### 2.1 用户放参考语料

```
data/style/reference.txt

（散兵的台词截图文字、原神 wiki 散兵词条、散兵百科...随便放）

哼，一群不知天高地厚的家伙。
你以为你是什么东西？
...无聊。
愚人众的执行官，散兵，见过吗？
风魔龙？呵，不过是一条被锁链束缚的可怜虫罢了。
...
```

### 2.2 启动时 chunk 预处理

```python
def _chunk_reference(self, text: str) -> list[str]:
    """按段落切分，每段 100-300 字，超过就再拆。"""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks = []
    for p in paragraphs:
        if len(p) <= 300:
            chunks.append(p)
        else:
            # 长段落按句号/换行再拆
            for part in re.split(r'[。！？\n]', p):
                if part.strip():
                    chunks.append(part.strip())
    return chunks

# 存到内存里（不存磁盘——每次启动重新预处理也就几十毫秒）
self._chunks: list[str] = [...]
self._chunk_signature = (mtime, size)
```

### 2.3 每次对话时检索

**检索时机**：handler 调 `persona.get_reference_chunks(user_text)` 时。
**检索方法**：文本重叠度（`difflib.SequenceMatcher`）+ 关键词匹配（去停用词后算词频重叠），取两者的加权和排序。
**返回**：Top 3 chunk 原文。如果没有匹配（用户的问题和 reference.txt 完全无关），返回空列表。

```python
import difflib
import re

def find_top_chunks(self, query: str, top_k: int = 3) -> list[str]:
    if not self._chunks:
        return []
    
    # 去掉中英文标点做关键词提取
    query_words = re.findall(r'[\w\u4e00-\u9fff]+', query.lower())
    if not query_words:
        return []
    
    scored = []
    for chunk in self._chunks:
        # 文本整体相似度（适合短 chunk）
        sim = difflib.SequenceMatcher(None, query.lower(), chunk.lower()).ratio()
        # 关键词重叠度（适合长 chunk）
        chunk_words = set(re.findall(r'[\w\u4e00-\u9fff]+', chunk.lower()))
        overlap = sum(1 for w in query_words if w in chunk_words)
        word_score = overlap / max(len(query_words), 1)
        
        combined = sim * 0.4 + word_score * 0.6
        if combined > 0.15:  # 阈值：太低就不注入，避免噪音
            scored.append((combined, chunk))
    
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored[:top_k]]
```

**阈值 0.15** 是经验值——可以调整。如果太低，不相关的片段也会被注入；如果太高，用户换个说法就匹配不到了。先保守一点，后面根据效果调。

### 2.4 动态注入 messages

**问题**：当前 `persona.get_system_prompt()` 是同步的、无参数的，handler 调它时还不知道这次用户发了什么消息。所以检索必须在 handler 里做，**persona 只负责提供 chunks 列表**，handler 负责组装。

```python
# handler._generate_reply()
async def _generate_reply(self, user_id, text):
    self._sessions.append(user_id, "user", text)
    self._sessions.save(user_id)

    # 组装 messages
    messages = [{"role": "system", "content": self._persona.get_system_prompt()}]
    
    # Stage 5：检索参考片段并注入
    reference_chunks = self._persona.find_top_chunks(text)
    if reference_chunks:
        ref_text = "\n---\n".join(reference_chunks)
        messages.append({
            "role": "system",
            "content": f"以下是你可能需要参考的角色原始台词/设定，请你在对话中参考这些内容，让回复更贴合角色：\n---\n{ref_text}\n---"
        })
    
    messages.extend(self._persona.get_examples())
    messages.extend(self._sessions.history(user_id))
    
    reply = await self._llm.chat(messages)
    ...
```

**和 Stage 5 前几版的差异**：风格特征（3-5 条"语气高傲..."那段）**不做了**——因为检索到的是原始台词本身，LLM 看到散兵的原话自然就会学那个语气，不需要额外描述。而且原始台词更灵活：用户问风魔龙的事，注入的就是散兵说风魔龙的那句，自然就用那个语气来回答。

**messages 组装顺序（Stage 5 后的完整版本）**：
```
1. system: 【人设拼装结果】身份→性格→说话风格→禁忌→固定角色约束
2. system: 【参考片段】（动态检索，可能没有）
3. system: 【之前对话摘要】（Stage 4，可能没有）
4. few-shot examples（人设文件里的）
5. 历史滑窗口 messages
6. 本次 user message（在滑窗口末尾）
```

### 2.5 热重载

检测 `data/style/reference.txt` 的 mtime+size 变化 → 自动重新 chunk。和人设热重载是独立的两套监测。

---

## 三、文件清单与职责

| 文件 | 动作 | 职责 |
|---|---|---|
| `persona/loader.py` | **修改** | 新增 `style_dir` 参数；chunk 预处理逻辑；`find_top_chunks(query)` 检索方法；reference.txt 热重载 |
| `core/handler.py` | **修改** | `_generate_reply()` 里调 `find_top_chunks(text)` 并把结果注入 messages |
| `config.py` | **修改** | 新增 `STYLE_DIR` 配置项 |
| `.env.example` + `.env` | **修改** | 新增 `STYLE_DIR` |
| `main.py` | **修改** | PersonaLoader 构造时传 style_dir |
| `data/style/` | **新建目录** | 用户放 reference.txt 的地方 |

### 不需要改的文件
- `core/llm.py`——这次**不需要 LLM 参与分析**（纯文本匹配，零 API 费用）
- `core/session.py`——风格和参考片段是全局的，不存 per-session
- `adapter/onebot.py`——协议层不变

### PersonaLoader 构造变化

```python
# Stage 3/4
PersonaLoader(path: Optional[Path], fallback_prompt: str)

# Stage 5：新增 style_dir，不需要 llm 参数（纯文本匹配）
PersonaLoader(
    persona_path: Optional[Path],
    fallback_prompt: str,
    style_dir: Optional[Path] = None,
)
```

### PersonaLoader 新增字段

```python
self._style_dir = style_dir
self._chunks: list[str] = []                       # 当前 chunk 列表
self._chunk_signature: Optional[tuple[float, int]] = None   # reference.txt 签名
self._chunk_failed_signature: Optional[tuple[float, int]] = None
```

### PersonaLoader 新增方法

```python
def find_top_chunks(self, query: str, top_k: int = 3) -> list[str]:
    """检索与 query 最相关的参考片段。返回空列表表示没匹配到。"""
    
def _chunk_reference(self, text: str) -> list[str]:
    """把 reference.txt 按段落拆成 chunks。"""
    
def _refresh_chunks(self) -> None:
    """检查 reference.txt 是否变化，变了就重新 chunk。"""
```

### handler 改动幅度

```python
async def _generate_reply(self, user_id: int, text: str) -> str:
    ...
    messages = [{"role": "system", "content": self._persona.get_system_prompt()}]
    
    # 新增：参考片段注入
    ref_chunks = self._persona.find_top_chunks(text)
    if ref_chunks:
        messages.append({"role": "system", "content": ...})
    
    # 之前的摘要注入 + examples + history 不变
    summary_text = self._sessions.summary(user_id)
    ...
```

---

## 四、配置项（新增）

| 变量 | 默认 | 说明 |
|---|---|---|
| `STYLE_DIR` | `data/style` | 参考语料目录；留空或不存在则跳过参考片段检索 |

**不新增 STYLE_TOP_K、STYLE_THRESHOLD 等**——默认值在代码里写死（top_k=3, threshold=0.15），够用了。以后需要调再加配置。

---

## 五、验收清单

### 基础功能
1. 启动日志出现 `参考语料加载成功: reference.txt (1200 字, 45 个 chunk)`
2. `data/style/reference.txt` 不存在 → 启动正常，无 warning（静默跳过），对话不受影响
3. reference.txt 为空文件 → 同上，静默跳过

### 检索正确性
4. 参考语料有散兵的风魔龙台词，用户问"风魔龙" → 回复里用到了那段台词的内容/语气
5. 参考语料里没有"吃饭"相关内容，用户问"你吃饭了吗" → 回复正常但**不注入参考片段**（不硬凑）
6. 用户问和角色完全无关的话题（比如"今天星期几"）→ 参考片段为空，回复按正常人设走

### 热重载
7. **不重启**改 reference.txt 加一段新台词，立刻发一个相关问题 → 新台词被检索到并注入
8. reference.txt 改成乱码垃圾文本 → warning chunk 失败，保留上一份可用 chunks（或空）

---

## 六、风险点与回退

| 风险 | 影响 | 应对 |
|---|---|---|
| 参考语料很短（< 100 字）→ chunk 太少 → 检索效果差 | 角色模仿不够像 | 启动时 warning 提示"参考语料建议 500+ 字"，但不阻塞 |
| 用户的问题和参考语料完全无关 | 注入了不相关的片段，反而带偏 LLM | 设了阈值 0.15，低于阈值就不注入 |
| 长 reference.txt（> 2 万字）→ difflib 变慢 | 每次对话多几百毫秒延迟 | chunk 数一般不会超过 200，difflib 对 200 × 平均 query 的匹配 < 10ms，忽略。如果以后真有问题加缓存 |
| 纯文本匹配不如 embedding 准（"风元素巨龙"匹配不到"风魔龙"） | 部分相关片段漏掉 | 这是方案 C 的固有局限。如果以后不够准，再升级到完整 RAG（但违反轻量原则，先忍着） |

---

## 七、与现有代码的关系

### 改动依赖链

```
config.py (STYLE_DIR)
  ↓
main.py (传 style_dir 给 PersonaLoader)
  ↓
persona/loader.py (新增 chunk + 检索 + 热重载)
  ↓
core/handler.py (调 find_top_chunks() + 注入 messages)
```

### 启动流程变化

```python
async def main():
    ...
    persona = PersonaLoader(persona_path, system_prompt, style_dir)
    # 不再需要 await persona.analyze_style() —— 纯文本匹配是同步的
    ...
```

### 热重载触发

在 `_refresh()`（人设热重载的那个方法）末尾加一段：

```python
def _refresh(self) -> None:
    # ...现有人设热重载逻辑...
    
    # Stage 5：reference.txt 变化检测
    if self._style_dir:
        ref_path = self._style_dir / "reference.txt"
        if ref_path.exists():
            sig = (ref_path.stat().st_mtime, ref_path.stat().st_size)
            if sig != self._chunk_signature and sig != self._chunk_failed_signature:
                self._refresh_chunks()
        elif self._chunk_signature is not None:
            # 文件被删了
            self._chunks = []
            self._chunk_signature = None
```

### 静态依赖（Python 标准库）

- `re`（正则分词）—— 标准库
- `difflib`（文本相似度）—— 标准库
- `pathlib`（文件路径）—— 已经在用

**零新依赖**，不需要改 `requirements.txt`。
