# Stage 8.2 — 记忆检索与注入强化

## 一、范围边界

### 做什么

1. **回忆意图检测**：用户消息含回忆类关键词时，切换为增强检索（top_k 5→15），并使用回忆专用注入文案
2. **检索命中率修复**：`retrieve()` 的关键词匹配从「整句匹配」改为「2-gram 匹配」（与 Stage 5 语料检索同一思路），归一化计分
3. **记忆注入位置后移**：从 prompt 第 5/7 位（summary 之后）挪到 history 紧前方（examples 之后），利用 LLM 对结尾的注意力偏置
4. **摘要保真**：`summarize()` prompt 加硬性保留清单（人名/数字/时间/约定/承诺/未完成话题），禁止模糊概括

### 不做什么

| 不做 | 原因 |
|------|------|
| ❌ 向量检索 / embedding | 违反轻量约束，2-gram + difflib 够用 |
| ❌ 记忆使用强化（importance 自动 +） | 会造成重要度通胀，影响淘汰机制；留作后续可选 |
| ❌ top_k 默认值 5→8 | 普通消息捞 8 条会注入噪音，等 A 方案按需增强更精准 |
| ❌ 改记忆提取逻辑（extract_memories） | Stage 7 已验收，提取质量不是本次瓶颈 |
| ❌ 改 JSON 存储结构 | MemoryEntry 字段不变，无迁移成本 |

---

## 二、根因回顾（为什么还会忘）

### 盲区 1：检索命中失败（memory.py:166-174）

```python
# 现状：抓整段中文
query_keywords = set(re.findall(r"[\u4e00-\u9fff]+", query))
kw_hit = sum(1 for kw in query_keywords if kw in e.content)
```

`query` 是用户整条消息（如「我上次跟你说的那只猫怎么样来着」），`re.findall` 抓出的是
`["我上次跟你说的那只猫怎么样来着"]`——**整句不可能作为子串出现在记忆内容里**，
`kw_hit` 恒为 0，`kw_score` 恒为 0。检索实际退化为纯 difflib 相似度，
而用户复述旧事时的措辞和记忆原文几乎必然不同 → 检索失败。

### 盲区 2：无回忆意图通道

用户问「你还记得吗」「我之前说过…」时，与普通消息走同一条 top_k=5 的检索路径。
这类消息恰恰是最需要记忆的场景。

### 盲区 3：注入位置被稀释（handler.py）

当前注入顺序：

```
① persona → ② state → ③ 参考语料 → ④ summary → ⑤ 记忆 → ⑥ few-shot → ⑦ history
```

记忆被夹在中间，LLM 注意力集中在一头一尾，⑤ 的权重被 ⑦ 的长历史稀释。

### 盲区 4：摘要失真（llm.py summarize）

现 prompt 只说「保留人名、日期、偏好、事件」，无负面约束。LLM 倾向输出
「两人聊了一些日常话题」式的空洞摘要，细节在压缩中静默丢失。

---

## 三、协议契约

内部模块改动，**不涉及任何对外协议**（OneBot 消息格式、LLM API 调用格式均不变）。

### 3.1 新增内部接口

```python
# handler.py 模块级
RECALL_KEYWORDS: frozenset[str]  # 回忆意图关键词表

def _is_recall_intent(text: str) -> bool:
    """用户消息是否含回忆意图。命中任一关键词即 True。"""
```

```python
# memory.py
def memories_to_prompt(memories: list[MemoryEntry], recall_mode: bool = False) -> str:
    """recall_mode=True 时使用回忆专用引导文案。"""
```

`MemoryStore.retrieve()` 签名不变（`top_k` 参数已存在），内部计分逻辑变更。

### 3.2 关键词表（硬编码，不进配置）

```
记得, 忘了, 忘记, 上次, 之前, 前几天, 说过, 提过, 讲过, 答应,
约定, 说好, 那只, 那个时候, 昨天说, 还记得吗
```

匹配方式：`any(kw in text for kw in RECALL_KEYWORDS)`，仅对文本消息生效（图片消息 text 为空自然不触发）。

---

## 四、文件清单与职责

| 文件 | 改动 | 职责说明 |
|------|------|---------|
| `core/handler.py` | 修改 ~15 行 | ① 新增 `RECALL_KEYWORDS` + `_is_recall_intent()`；② `_generate_reply` 中按意图切换 `top_k`（普通 5 / 回忆 15）；③ 记忆注入块从 summary 之后挪到 examples 之后；④ recall 时给 `memories_to_prompt` 传 `recall_mode=True` |
| `core/memory.py` | 修改 ~20 行 | ① `retrieve()` 关键词匹配改 2-gram + 归一化计分；② `memories_to_prompt()` 加 `recall_mode` 参数与对应文案 |
| `core/llm.py` | 修改 ~6 行 | `summarize()` prompt 重写：硬性保留清单 + 负面约束，字数上限 200→300 |
| `config.py` | 修改 1 行 | 新增 `recall_top_k` 配置（默认 15） |
| `.env.example` | 修改 ~2 行 | 新增 `RECALL_TOP_K=15` 及注释 |
| `docs/stage8.2-design.md` | 新增 | 本文档 |

---

## 五、核心设计

### 5.1 回忆意图 → 增强检索（handler.py）

```python
# _generate_reply 内，Stage 7 记忆检索段改为：
if self._memory and self._memory.enabled:
    recall = _is_recall_intent(session_text)
    top_k = self._settings.recall_top_k if recall else 5
    entries = self._memory.retrieve(user_id, session_text, top_k=top_k)
    mem_prompt = memories_to_prompt(entries, recall_mode=recall)
    if mem_prompt:
        messages.append({"role": "system", "content": mem_prompt})
```

### 5.2 注入位置调整（handler.py）

新顺序（记忆从 ⑤ 挪到 ⑥'，紧贴 history）：

```
① persona → ② state → ③ 参考语料 → ④ summary → ⑤ few-shot → ⑥ 记忆 → ⑦ history
```

### 5.3 2-gram 关键词匹配（memory.py retrieve）

```python
# 旧：
query_keywords = set(re.findall(r"[\u4e00-\u9fff]+", query))

# 新：从每个中文连续段提取 2-gram（段长 <2 时保留整段，照顾单字称呼），
#     命中率归一化，上限 0.4 不变
segments = re.findall(r"[\u4e00-\u9fff]+", query)
grams: set[str] = set()
for seg in segments:
    if len(seg) < 2:
        grams.add(seg)
    else:
        grams.update(seg[i:i+2] for i in range(len(seg) - 1))

kw_hit = sum(1 for g in grams if g in e.content)
kw_score = (kw_hit / len(grams)) * 0.4 if grams else 0.0
relevance = max(sim, kw_score)
```

示例：「我上次跟你说的那只猫怎么样来着」→ grams 含「那只/只猫/猫怎/怎么/么样…」，
记忆「用户家的猫叫咪咪」命中「的猫」→ kw_score > 0，能被捞出。

### 5.4 回忆专用注入文案（memory.py memories_to_prompt）

```
recall_mode=True:
「用户正在向你提起过去聊过的事，请认真回忆以下内容并自然回应，
 不要说'我不记得了'：」

recall_mode=False（原文案不变）:
「以下是你对这个用户的了解，请在对话中自然地体现出来
（不要说'根据我的记忆'，要像真的记得一样）：」
```

### 5.5 摘要保真（llm.py summarize）

新 prompt 要点：

```
- 字数上限 200 → 300
- 必须保留：人名/称呼、数字（日期/时间/数量）、地点、
  双方的约定或承诺、未聊完的话题、明确的情绪事件
- 禁止出现「聊了一些日常」「闲聊了几句」这类无信息量概括；
  若对话确实无实质内容，输出「(无实质内容)」
- 保留用户消息中的原话关键词（引号内的内容原样保留）
```

---

## 六、配置项

| 配置 | 默认 | 说明 |
|------|------|------|
| `RECALL_TOP_K` | `15` | 回忆意图触发时的检索条数。普通消息固定 top_k=5 不进配置（避免配置膨胀） |

无新增环境变量分类，`RECALL_TOP_K` 放在 Stage 7 段落之后。

---

## 七、验收清单

- [ ] 语法检查：`py_compile` 三个改动文件通过
- [ ] 普通消息（无回忆关键词）：行为与旧版一致——top_k=5，注入顺序变化（记忆在 few-shot 后）
- [ ] 发送「你还记得我上次说的那只猫吗」：日志可见检索条数 > 5（接近 15），回复中体现相关记忆
- [ ] 发送普通问候「早上好」：不触发增强检索
- [ ] 2-gram 计分：构造「query 含『猫』但记忆原文无整句匹配」的用例，验证 kw_score > 0
- [ ] 摘要：手动触发 `_do_summarize`，检查摘要含具体信息，无「聊了一些日常」式空话
- [ ] `RECALL_TOP_K=10` 覆盖默认值后生效
- [ ] 回归：Stage 8 识图、Stage 8.1 启动补摘要不受影响

---

## 八、客户部署

与 Stage 8.1 相同：重新打包 + 客户复制 `data/` + `.env`。无数据迁移。

完成后更新 AGENTS.md 阶段表：Stage 8 行改为「8 已验收；8.1/8.2 已验收」。
