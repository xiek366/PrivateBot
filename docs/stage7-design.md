# 阶段 7 设计：长期记忆库 + 主动发消息

## 一、范围边界

### 做
1. **长期记忆库** `data/memories/{qq}.json`：4 类记忆 + importance + 生命周期管理
2. **自动提取记忆**：每次对话后异步用 LLM 从本轮对话中提取记忆候选 → 评分 → 入库
3. **记忆自然注入**：每次组装 Prompt 时，把"最相关 + 最高重要性"的记忆注入 system prompt（用自然语言形式，不暴露"这是 AI 记忆"）
4. **主动发消息调度器**：独立的 asyncio 定时任务，根据状态数值（affection/上次聊天时间/当前时间）决定要不要主动发消息
5. **主动消息生成**：主动消息也是 LLM 生成的，输入包含"为什么现在要发"的原因（如"24 小时没聊天 + 好感度 45"）

### 不做（留后续阶段）
- 不做 LLM JSON 输出格式（让 LLM 返回结构化记忆候选）→ **Stage 8 优化**（初期用 Prompt 引导 LLM 返回特定格式）
- 不做关系等级行为对照表（Level 0-5 的具体回复示例）→ 在 default.yaml 里逐步补充
- 不做向量数据库（记忆检索用文本匹配 + importance 排序，JSON 文件够用）
- 不做图片/语音

---

## 二、核心设计

### 2.1 长期记忆库数据结构

**路径**：`data/memories/{qq}.json`（每用户一份，和 sessions/state 平级）

```json
{
  "user_id": 10001,
  "memories": [
    {
      "id": "m_001",
      "category": "preference",
      "content": "用户喜欢玩 RPG 游戏，最近在玩《原神》",
      "importance": 0.8,
      "created_at": "2026-09-25T22:30:00",
      "last_used": "2026-09-25T22:48:00",
      "source_msg": "我最近在玩原神..."
    },
    {
      "id": "m_002",
      "category": "profile",
      "content": "用户在科技公司做程序员，工作很忙经常加班",
      "importance": 0.7,
      "created_at": "2026-09-25T22:35:00",
      "last_used": null,
      "source_msg": "今天又加班到十点..."
    },
    {
      "id": "m_003",
      "category": "relationship",
      "content": "用户第一次向结灯表白（连续说10次'我喜欢你'），结灯答应明天等他",
      "importance": 0.95,
      "created_at": "2026-09-25T22:48:00",
      "last_used": "2026-09-25T22:50:00",
      "source_msg": "我喜欢你"
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | str | 唯一标识，`m_` + 数字自增（单调递增，重新加载文件时不回退） |
| `category` | str | 4 类之一：`preference`（喜好）/ `habit`（习惯）/ `profile`（基本信息）/ `relationship`（共同经历）|
| `content` | str | 记忆内容，自然语言一句话 |
| `importance` | float | 0.0-1.0 重要性分数，越高越优先保留和检索 |
| `created_at` | ISO 时间 | 记忆创建时间 |
| `last_used` | ISO 时间 / null | 上次被注入 Prompt 的时间（null 表示从没被用过） |
| `source_msg` | str | 触发这条记忆的原始用户消息（短片段） |

### 2.2 记忆提取流程

**触发时机**：每次回复用户后，`asyncio.create_task` 异步执行（不阻塞回复）

**输入**：本轮对话（user_msg + bot_reply）+ 近期 3-5 轮历史 + **已有记忆的内容清单**（用于语义去重，见下）

**做法**：用 LLM 以特定 Prompt 引导返回 JSON 格式的记忆候选：

```python
EXTRACT_MEMORY_SYSTEM_PROMPT = """你是一个记忆提取器，负责从对话中提取【关于用户】的客观事实。

写作要求（每条记忆都必须满足）：
1. 主语是「用户」，不是「我」、不是「结灯」
2. 是客观事实，脱离本次对话也能独立成立
3. 一条只写一个事实，不要把多件事揉成一句
4. 像记笔记，不要像写故事，不要心理描写

category 四选一：
- preference: 用户的喜好、兴趣、厌恶（如"用户喜欢 RPG 游戏"）
- habit: 用户的习惯、作息、行为规律（如"用户经常加班到十点"）
- profile: 用户的基本信息（职业、年龄、城市、称呼等）
- relationship: 用户和结灯共同经历的事件（如"用户向结灯表白"）

importance 0.0-1.0：
- 0.9+: 关键关系事件
- 0.7-0.89: 重要个人信息
- 0.4-0.69: 一般小事
- <0.4: 忽略（不要返回）

禁止提取（重要）：
- 结灯的台词、心理活动、情绪、期待、状态（如"结灯一直在等他""结灯很害羞"）
  —— 这些是角色状态，由系统自动管理
- 对结灯内心或剧情的推测、脑补
- 好感度 / 信任度等数值
- 瞬时/临时状态（如"用户现在还没睡""用户今天在线"）
  —— 只提取明天、下个月依然成立的事实
- 与「已经记住的内容」清单里语义重复的事实（措辞不同也算重复）

示例：
输入："刚上号，你怎么知道"
输出：[{"category":"habit","content":"用户经常在晚上才上号","importance":0.5}]
错误示范："用户只在晚上上号找结灯，结灯一直在等他"
  → 错在①掺入了结灯的状态 ②两个事实揉成一句

输入："我最近在玩原神"
输出：[{"category":"preference","content":"用户最近在玩《原神》","importance":0.6}]

输入："我喜欢你"
输出：[{"category":"relationship","content":"用户向结灯说了「我喜欢你」","importance":0.9}]

没有值得记住的内容时返回 []。
严格返回 JSON 数组，不要加任何其他文字。"""
```

（注：此 Prompt 在实现中作为 system message，与包含对话历史/本轮对话的 user message 配合使用，见 `core/llm.py` 的 `EXTRACT_MEMORY_SYSTEM_PROMPT` 与 `extract_memories()`。）

```python
EXTRACT_MEMORY_USER_TEMPLATE = """对话历史：
{history_text}

本轮对话：
用户：{user_msg}
结灯：{bot_reply}

{known_block}请返回 JSON 数组。"""
```

其中 `{known_block}` 由已有记忆清单拼成；没有已有记忆时为空字符串：

```python
KNOWN_MEMORIES_BLOCK = """已经记住的内容（措辞不同但语义相同的事实，不要重复返回）：
{items}

"""
```

**上限**：`MAX_KNOWN_MEMORIES = 30`，只取**最近 30 条**（`known[-30:]`）塞进清单，防止记忆库变大后 Prompt 无限膨胀。

**评分过滤**：两道关卡。

**第一道在 LLM 侧**——提取时把已有记忆清单一起喂进去，要求它不要返回语义重复的候选。这是治「同义改写」的关键：同一个事实换个说法（如 `用户在玩《鸣潮》这款游戏` vs `用户最近一直在玩《鸣潮》`，difflib 相似度仅 0.67、且无包含关系）文本匹配抓不到，但 LLM 能判断语义等价。

**第二道在 Python 侧**——LLM 返回后二次过滤：
- importance < 0.4 的直接丢弃
- 文本级重复跳过，判据为「满足任一」：
  - difflib 相似度 > 0.8
  - 短句被长句完整包含，且短句长度 ≥ 6 字。用于覆盖「原句 + 补充说明」这种形态——当长句恰为短句 1.5 倍长时，相似度精确等于 0.8，单靠 `>` 判据会漏检
- 同 category 下最多保留 50 条（防爆炸）

### 2.3 记忆检索与注入

**时机**：每次组装 Prompt 时（handler._generate_reply 内）

**做法**：
1. 取当前用户消息 + 状态数值 + 参考语料 chunks 的关键词
2. 用 difflib 算每条记忆的相关性分数
3. 综合排序：`final_score = importance * 0.6 + relevance * 0.3 + recency * 0.1`
4. 取 Top 5，importance >= 0.7 的必选 + relevance 高的补充
5. 转成自然语言注入 system prompt：

```python
def memories_to_prompt(memories: list[dict]) -> str:
    if not memories:
        return ""
    lines = ["以下是你对这个用户的了解，请在对话中自然地体现出来（不要说'根据我的记忆'，要像真的记得一样）："]
    for m in memories:
        lines.append(f"- {m['content']}")
    return "\n".join(lines)
```

### 2.4 记忆生命周期

| 规则 | 做法 |
|---|---|
| **创建** | LLM 提取 + 评分过滤后入库 |
| **检索** | 每次对话时按综合排序取 Top 5 |
| **衰减** | 暂不做（importance 固定）—— 等 Stage 8 看效果再说 |
| **删除** | 同 category 超过 50 条时删 importance 最低的旧条目 |
| **手动清理** | 用户可以直接删 `data/memories/{qq}.json` 重启 |

### 2.5 主动发消息调度器

**核心**：一个独立的 asyncio 定时任务，每 10 分钟检查一次所有白名单用户的状态，判断是否该主动发消息。

#### 触发条件（全部满足才发）

| 条件 | 计算方式 | 阈值 |
|---|---|---|
| 上次聊天时间够久 | `now - state.last_interaction` | ≥ 12 小时（可调） |
| 好感度够高 | `state.affection` | ≥ 30（Level 2 亲近以上才会主动） |
| 不在凌晨 | `hour(now)` | 8:00 - 23:00 之间 |
| 情绪不极端 | `state.valence` | > -50（太生气/太难过时不打扰；正向愉快不拦） |
| 同用户最近没主动发过 | `now - last_proactive_msg` | ≥ 24 小时 |

#### 主动消息生成

```python
PROACTIVE_MEMORY_PROMPT = """你是{character_name}，现在你想主动给{user_nickname}发一条消息。

原因：距离上次聊天已经过了{hours_since:.1f}小时，你们的好感度是{affection:.0f}/100（关系等级 {level_name}）。
当前时间是{current_time}，你的情绪是{mood}（valence {valence:.0f}, arousal {arousal:.0f}）。

{memory_text}

请发一条自然的、符合你性格的消息。
要求：
- 不要说"我想你"这么直白，要别扭一点、害羞一点
- 1-2 句话，100 字以内
- 符合你的说话风格（用……，短句，偶尔带动作描写）
- 内容要和触发原因相关（很久没聊可以说"没什么，就是突然想起来而已"；好感度高可以稍微亲近一点）

直接返回回复内容，不要其他任何格式。"""
```

#### 调度器实现位置

在 `main.py` 里启动，作为后台 asyncio task：

```python
# main.py
if settings.enable_proactive and state_mgr.enabled:
    scheduler = ProactiveScheduler(settings, state_mgr, llm, bot, memory)
    asyncio.create_task(scheduler.run())
```

**注意**：`run()` 先 `sleep 600s` 才做第一次检查，所以启动后要等约 10 分钟才会看到第一条主动消息。

**已知限制**：`_last_sent`（24 小时频率限制的依据）只存在内存中，进程重启会清空，即重启后 24 小时限制重新计时。

### 2.6 Prompt 完整组装顺序（Stage 7 后最终版）

```
1. system: 人设（身份→性格→说话风格→禁忌→固定约束）            ← Stage 3
2. system: 当前关系状态 + 当前情绪                              ← Stage 6
3. system: 参考片段（动态检索 Top 3）                          ← Stage 5
4. system: 对话摘要（可能没有）                                ← Stage 4
5. system: 长期记忆（用户画像+共同经历，自然语言形式）            ← Stage 7 新增
6. few-shot examples                                          ← Stage 3
7. 历史滑窗口 + 本次 user message
```

（注：状态段紧跟人设——"你是谁"和"你现在什么关系、什么情绪"是 LLM 决定语气的两个前提，放最前面；长期记忆放在参考片段和摘要之后，作为"关于用户的事实"补充。此顺序与 `core/handler.py` 的 `_generate_reply()` 及 `docs/stage6-design.md` §2.8 一致）

---

## 三、文件清单与职责

| 文件 | 动作 | 职责 |
|---|---|---|
| `core/memory.py` | **新建**（~200 行） | MemoryStore：记忆加载/保存/检索/相似度去重/LLM 提取封装 |
| `core/proactive.py` | **新建**（~150 行） | 主动消息调度器：触发条件判断 + Prompt 模板 + LLM 生成 + 发送 |
| `core/llm.py` | **修改** | `extract_memories()`（含已有记忆清单，用于语义去重）+ `_parse_memory_json()`；主动消息由 `proactive.py` 直接调 `chat()`，没有 `generate_proactive()` |
| `core/handler.py` | **修改** | Prompt 组装加记忆段；回复后异步触发记忆提取（并把已有记忆内容传给 LLM 做语义去重） |
| `main.py` | **修改** | 启动 MemoryStore；启动 ProactiveScheduler（后台 asyncio task） |
| `config.py` + `.env*` | **修改** | 新增 6 个配置项 |

### 不需要改的文件
- `adapter/onebot.py`（协议层不动）
- `persona/loader.py`（人设层不动）
- `core/state.py`（状态层不动）
- `core/session.py`（session 管对话历史，memory 管长期事实，两个独立）

### core/memory.py 类图

```
模块常量：`MAX_PER_CATEGORY=50` / `DEDUP_THRESHOLD=0.8` / `DEDUP_MIN_CONTAIN_LEN=6` / `MIN_IMPORTANCE=0.4`

MemoryEntry (dataclass)
  ├─ id / category / content / importance
  ├─ created_at / last_used / source_msg
  ├─ to_json() → dict
  └─ from_json(data) → MemoryEntry            # classmethod

MemoryStore
  ├─ __init__(memory_dir: Optional[Path])
  ├─ enabled → bool                           # property，memory_dir 为 None 时关闭
  ├─ load(user_id) → list[MemoryEntry]        # 文件不存在返回空列表
  ├─ save(user_id, entries)
  ├─ add(user_id, entry) → bool               # 是否真的入库（去重 / importance 过滤可能拒绝）
  ├─ add_batch(user_id, candidates) → int     # 返回实际入库条数
  ├─ retrieve(user_id, query, top_k=5) → list[MemoryEntry]   # 综合排序 + 回写 last_used
  └─ 内部方法
      ├─ _next_id(user_id) → str              # m_0001 起递增，重载不回退
      ├─ _is_similar(content, entries) → bool
      ├─ _is_duplicate(a, b) → bool           # 相似度超阈值，或短句被长句完整包含
      ├─ _enforce_category_limit(entries)     # 同 category 超 50 条淘汰低 importance
      └─ _recency_score(created_at) → float

模块级函数
  └─ memories_to_prompt(memories) → str       # 转自然语言注入 system prompt
```

### core/proactive.py 设计

```
ProactiveScheduler
  ├─ __init__(settings, state_mgr, llm, bot, memory)
  ├─ should_send(state, last_proactive_time) → bool   # 触发条件判断
  ├─ generate_message(state, memory) → str            # LLM 生成主动消息
  └─ run()  → asyncio while True, sleep 600s          # 主循环
```

---

## 四、配置项（新增 6 个）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEMORY_DIR` | `data/memories` | 长期记忆目录 |
| `ENABLE_PROACTIVE` | `true` | 是否启用主动发消息调度器 |
| `PROACTIVE_MIN_HOURS` | `12` | 最少隔多久才主动发（小时） |
| `PROACTIVE_MIN_AFFECTION` | `30` | 最低好感度才会主动发 |
| `PROACTIVE_WINDOW_START` | `08:00` | 允许主动发的时间窗口开始 |
| `PROACTIVE_WINDOW_END` | `23:00` | 允许主动发的时间窗口结束 |

---

## 五、验收清单

### 长期记忆库
1. **首次对话后自动创建** `data/memories/{qq}.json`，提取到的记忆写入
2. **检索相关性**：用户说"我最近玩了新游戏"，记忆里的"用户喜欢玩 RPG"会被注入
3. **记忆注入**：组装 Prompt 时相关记忆被注入（判断依据：`data/memories/{qq}.json` 里对应条目的 `last_used` 被更新为当次时间）。**不要求回复显式提及**——黑姬结灯的"别扭"人设允许它装傻回避（如"你玩什么，我怎么会知道"），这属于角色表现，不算失败
4. **去重**：相似内容不会重复入库（LLM 连续说两次"我玩原神"只存一条）
5. **生命周期**：同 category 超过 50 条时自动删 importance 最低的

### 主动发消息
6. **不满足条件时不发**：好感度 20、刚聊完、凌晨 3 点，都不会主动发
7. **满足条件时会发**：好感度 ≥30、12 小时没聊、在 8-23 点之间、情绪正常
8. **主动消息风格符合角色**：不直白"想你"，要别扭害羞
9. **频率限制**：同用户 24 小时内最多主动发 1 条
10. **重启恢复**：重启后记忆库保留、主动调度器继续运行

---

## 六、风险点与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| LLM 提取记忆格式不稳定 | JSON 解析失败，记忆入库失败 | 失败时静默跳过（logger warning），不阻塞主流程；Prompt 里加格式示例 |
| 记忆库越来越大 | JSON 文件膨胀 | importance 排序 + 同 category 上限 50 条 + 低 importance 淘汰 |
| 主动消息骚扰用户 | 用户关掉机器人 | `ENABLE_PROACTIVE=true` 默认打开，设为 `false` 即可关闭；阈值可调 |
| 凌晨主动发消息 | 打扰用户 | 时间窗口硬限制 8:00-23:00 |
| 主动消息 LLM 生成不合适 | 角色崩坏或发不该发的话 | 加 valence/arousal 过滤（情绪极端时不发）+ Prompt 模板约束 |

## 七、改动幅度估算

| 模块 | 新增/修改 | 行数 |
|---|---|---|
| core/memory.py | 新建 | ~200 行 |
| core/proactive.py | 新建 | ~150 行 |
| core/llm.py | 修改 | +40 行（extract_memories / generate_proactive） |
| core/handler.py | 修改 | +30 行（记忆提取触发 + Prompt 注入） |
| main.py | 修改 | +25 行（初始化 + 后台 task） |
| config.py + .env* | 修改 | +8 行（配置项） |

**总计**：新建 2 个文件（~350 行），修改 4 个文件（~100 行增量）。零新依赖。
