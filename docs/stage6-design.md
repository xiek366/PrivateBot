# 阶段 6 设计：角色状态系统

## 一、范围边界

### 做
1. **状态文件**：`data/state/{qq}.json`，存 affection / trust / familiarity / valence / arousal
2. **事件表驱动数值变化**：Python 用硬编码事件表（`RELATIONSHIP_EVENTS` + `EMOTION_EVENTS`），根据用户消息关键词匹配事件 → 查表算数值变化
3. **情绪衰减**：每次处理消息前 decay valence 和 arousal，逐渐回归平静基线
4. **关系等级映射**：从 affection 值自动映射 Level 0-5（给 LLM 看）
5. **Prompt 动态注入状态段**：在 Persona 和 Reference Chunks 之间插入 `[当前关系状态]` 和 `[当前情绪]` 两段 system prompt
6. **防数值暴走**：限制每次变化幅度（±5/次上限），防止几轮聊天从陌生人变恋人

### 不做（留后续阶段）
- 不做长期记忆库（4 层记忆 + importance + 检索）→ **Stage 7**
- 不做主动发消息（定时/条件触发 bot 主动发消息）→ **Stage 7**（和长期记忆一起做，两个功能都依赖状态系统）
- 不做 LLM JSON 输出格式（让 LLM 返回 event 名）→ **Stage 7 或 8**
- 不做关系等级行为对照表（Level 0-5 的具体回复示例）→ 在 default.yaml 里逐步补充
- 不做向量数据库（JSON 文件够用）
- 不修改 adapter/onebot.py（协议层不动）

---

## 二、核心设计

### 2.1 状态文件结构

**路径**：`data/state/{qq}.json`（每用户一份，和 `data/sessions/{qq}.json` 平级）

```json
{
  "affection": 35,
  "trust": 28,
  "familiarity": 42,
  "valence": 15,
  "arousal": 12,
  "relationship_level": 1,
  "interaction_count": 47,
  "last_interaction": "2026-09-25T21:30:00",
  "created_at": "2026-09-01T15:00:00",
  "decay_at": "2026-09-25T21:35:00"
}
```

| 字段 | 类型 | 范围 | 说明 |
|---|---|---|---|
| `affection` | float | 0-100 | 好感度（结灯有多喜欢用户） |
| `trust` | float | 0-100 | 信任度（结灯有多相信用户） |
| `familiarity` | float | 0-100 | 熟悉度（相处了多久/互动了多少次） |
| `valence` | float | -100~+100 | 情绪正负（+开心 / -生气难过） |
| `arousal` | float | 0-100 | 情绪激烈程度（0平静 / 100激动） |
| `relationship_level` | int | 0-5 | 关系等级，由 affection 映射（不是独立字段，每次从 affection 算） |
| `interaction_count` | int | ≥0 | 累计互动次数（每次用户发消息 +1） |
| `last_interaction` | ISO 时间 | — | 上次处理消息的时间（用于情绪衰减计算） |
| `created_at` | ISO 时间 | — | 状态创建时间（首次加载时） |
| `decay_at` | ISO 时间 | — | 上次衰减计算时间 |

### 2.2 关系等级映射

**不是独立字段**——每次需要时从 affection 计算：

| affection 范围 | relationship_level | 标签 |
|---|---|---|
| 0-19 | 0 | 刚认识 |
| 20-39 | 1 | 熟悉 |
| 40-59 | 2 | 亲近 |
| 60-79 | 3 | 暧昧 |
| 80-94 | 4 | 恋人 |
| 95-100 | 5 | 深度依赖 |

```python
def calc_relationship_level(affection: float) -> int:
    if affection >= 95: return 5
    if affection >= 80: return 4
    if affection >= 60: return 3
    if affection >= 40: return 2
    if affection >= 20: return 1
    return 0
```

### 2.3 事件表（Python 硬编码）

**核心原则**：程序负责计算，不让 LLM 改数值。事件表是 Python dict，写在代码里。

#### 关系事件表 RELATIONSHIP_EVENTS

```python
RELATIONSHIP_EVENTS: dict[str, dict[str, float]] = {
    # 用户行为 → 数值变化
    "user_greeted":      {"affection": 0.5, "familiarity": 0.5},       # "你好" "在吗"
    "user_chat_normally": {"affection": 0.3, "familiarity": 0.3},      # 普通闲聊
    "user_shared_life":   {"affection": 1.0, "familiarity": 0.5},      # "今天发生了一件事..."
    "user_cares_about_her": {"affection": 2.0, "trust": 1.5},          # "你今天怎么样" "有没有吃饭"
    "user_praises_her":   {"affection": 1.0, "trust": 0.5},            # "你真可爱" "结灯好棒"
    "user_says_he_likes_her": {"affection": 2.0, "trust": 1.0},        # "我喜欢你"
    "user_comforted_her": {"affection": 2.5, "trust": 2.0},            # 安慰她
    "user_listens_patiently": {"trust": 2.0, "familiarity": 1.0},      # 认真倾听她倾诉
    "user_shares_secret": {"affection": 1.5, "trust": 3.0},            # 分享隐私/秘密
    "user_completed_goal": {"affection": 1.5, "trust": 1.0},           # "我终于通过了考试"
    "user_needs_comfort": {"affection": 0.5},                          # 用户自己遇到困难（结灯会关心，affection微涨）
    
    # 负向事件
    "user_ignored_long": {"affection": -0.5},    # 未实现（Stage 6 延后，见 §2.6）：24h 无互动
    "user_misled_her":   {"affection": -3.0, "trust": -6.0},   # 欺骗
    "user_hurt_her":     {"affection": -5.0, "trust": -10.0},  # 侮辱/严重伤害
    "user_apologizes":   {"trust": 3.0, "affection": 1.0},    # 真诚道歉
}
```

#### 情绪事件表 EMOTION_EVENTS

```python
EMOTION_EVENTS: dict[str, dict[str, float]] = {
    # 用户说什么 → 结灯情绪变化
    "user_greeted":      {"valence": 5,  "arousal": 3},
    "user_chat_normally": {"valence": 2,  "arousal": 1},
    "user_shared_life":   {"valence": 8,  "arousal": 5},
    "user_cares_about_her": {"valence": 15, "arousal": 12},
    "user_praises_her":   {"valence": 18, "arousal": 25},   # 害羞：正向 + 激烈
    "user_says_he_likes_her": {"valence": 20, "arousal": 35},  # 害羞大
    "user_comforted_her": {"valence": 25, "arousal": 10},   # 温暖：正向大 + 激烈小
    "user_listens_patiently": {"valence": 12, "arousal": 5},
    "user_shares_secret": {"valence": 15, "arousal": 8},
    "user_completed_goal": {"valence": 15, "arousal": 10},  # 替他开心
    "user_needs_comfort": {"valence": -5, "arousal": 8},    # 担心：负向 + 略激动
    "user_long_absent":   {"valence": -10, "arousal": 5},   # 未实现（Stage 6 延后，见 §2.6）：长时间不联系 → 孤单
    "user_misled_her":    {"valence": -25, "arousal": 30},  # 生气
    "user_hurt_her":      {"valence": -35, "arousal": 45},  # 很生气
    "user_apologizes":    {"valence": 10, "arousal": -5},   # 缓和
}
```

### 2.4 事件匹配：关键词触发

事件表是"最终数值变化"，但**谁来判断发生了什么事件**？

方案：用**关键词正则匹配**，在 handler 收到用户消息时先扫一遍：

```python
import re

EVENT_PATTERNS: dict[str, list[str]] = {
    "user_greeted":           [r"早上好", r"早安", r"中午好", r"下午好", r"晚上好", r"晚安", r"你好", r"在吗", r"在么", r"嗨"],
    "user_says_he_likes_her": [r"我喜欢你", r"我爱你", r"喜欢你", r"爱你"],
    "user_cares_about_her":   [r"你.*怎么样", r"你.*还好吗", r"有没有吃饭", r"累不累", r"还好吗", r"在干嘛"],
    "user_praises_her":       [r"你真可爱", r"你好可爱", r"好漂亮", r"真棒", r"你好美", r"你真好看"],
    "user_comforted_her":     [r"别难过", r"别伤心", r"我在这", r"我陪着你", r"不要哭", r"没事的"],
    "user_needs_comfort":     [r"好累啊?", r"好难过", r"好累", r"被骂", r"搞砸了", r"失败了", r"好烦"],
    "user_shared_life":       [r"我今天", r"我昨天", r"我刚才", r"我刚刚", r"我跟你说", r"我跟你讲"],
    "user_listens_patiently": [r"你继续说", r"继续说", r"我在听", r"你讲吧", r"你说吧", r"然后呢"],
    "user_shares_secret":     [r"告诉你个?秘密", r"我只跟你说", r"别告诉别人", r"这是秘密", r"我从来没跟.*说过"],
    "user_apologizes":        [r"对不起", r"抱歉", r"我错了", r"原谅我", r"是我不好"],
    "user_completed_goal":    [r"通过了", r"过了", r"完成了", r"做到了", r"成功了", r"拿到了"],
    "user_hurt_her":          [r"滚[！！]?", r"闭嘴[！！]?", r"烦不烦", r"你算什么"],  # 故意侮辱类
    "user_misled_her":        [r"我骗了你", r"我刚才是骗你的", r"我撒谎了", r"我一直在骗你"],
}
```

**覆盖原则**：`RELATIONSHIP_EVENTS` / `EMOTION_EVENTS` 里定义的事件，除兜底 `user_chat_normally` 外，**都必须在这里有对应的 pattern 组**，否则就是永不触发的"死事件"。当前 13 组 pattern 覆盖 14 条事件中的 13 条，剩 1 条为兜底。

**匹配方式**：全部用 `re.search` 不加锚点（沿用项目既有风格）。好处是 `你在吗`、`刚上号，晚上好` 这类非句首问候也能命中；代价是 `你好烦` 会误判为问候——这是纯文本匹配的固有局限，见「风险点与回退」。

**多条可以同时匹配**——用户说"你今天怎么样，有没有吃饭"会同时匹配 `user_cares_about_her`（两个 pattern 都命中）。

**匹配优先级**：一个事件匹配到就执行一次查表。如果多个事件都匹配，**累加**数值变化。

**兜底事件**：如果什么 pattern 都没匹配到，默认 `user_chat_normally`（affection +0.3, familiarity +0.3, valence +2, arousal +1）。

### 2.5 数值变化防暴走

每次事件查表后，对每个数值字段做**±5 上限裁剪**：

```python
MAX_DELTA_PER_FIELD = 5.0

def apply_event(state, event_key):
    delta = RELATIONSHIP_EVENTS.get(event_key, {})
    delta.update(EMOTION_EVENTS.get(event_key, {}))
    
    for field, value in delta.items():
        # 裁剪单次变化幅度
        clipped = max(-MAX_DELTA_PER_FIELD, min(MAX_DELTA_PER_FIELD, value))
        
        # 边界保护
        if field in ("affection", "trust", "familiarity", "arousal"):
            new_val = getattr(state, field) + clipped
            setattr(state, field, max(0.0, min(100.0, new_val)))
        elif field == "valence":
            new_val = state.valence + clipped
            state.valence = max(-100.0, min(100.0, new_val))
```

为什么 ±5？因为 `user_comforted_her` 在 affection 和 trust 上各加 2.5，已经超过单次聊天的合理上限。如果让 `user_hurt_her` 直接扣 affection -5 + trust -10（表值），但 trust 的 ±5 裁剪会把 -10 变成 -5，这样一次侮辱不会让 trust 直接归零。

**特殊规则**：`familiarity` 不裁剪（它是单调递增的时间积累，不会爆），但封顶 100。

### 2.6 情绪衰减

**时机**：每次处理用户消息前（或者更精确，每次计算状态变化前）。

**公式**：

```python
BASE_VALENCE = 20.0    # 平静时的 valence 基线（微正）
BASE_AROUSAL = 15.0    # 平静时的 arousal 基线（低激动）
DECAY_RATE = 0.1       # 每单位时间衰减系数
TIME_SCALE_MINUTES = 30  # 30 分钟衰减一次完整步骤

def decay_emotion(state, now: datetime):
    # 计算距上次衰减的分钟数
    elapsed = (now - state.decay_at).total_seconds() / 60.0
    if elapsed < 5:
        return  # 不到 5 分钟不衰减，避免频繁波动
    
    steps = int(elapsed / TIME_SCALE_MINUTES)
    for _ in range(steps):
        state.valence += (BASE_VALENCE - state.valence) * DECAY_RATE
        state.arousal += (BASE_AROUSAL - state.arousal) * DECAY_RATE
    
    state.decay_at = now
```

**效果**：生气（valence=-35, arousal=45）→ 30 分钟后 → valence=-23, arousal=34 → 2 小时后 → valence=-5, arousal=21 → 一天后 → valence=17, arousal=16（回到接近平静）。

**不衰减的**：affection / trust / familiarity。这些是长期积累，不会因为时间自动降。（等后面加"长时间不联系衰减"再做，Stage 6 先不做）

### 2.7 状态 → Prompt 动态转换

Python 内部算完数值后，在 Prompt 组装时转成**自然语言描述**给 LLM：

```python
def state_to_prompt(state) -> str:
    level_names = ["刚认识", "熟悉", "亲近", "暧昧", "恋人", "深度依赖"]
    level = calc_relationship_level(state.affection)
    
    # 情绪标签（从 valence + arousal 推断）
    if state.valence > 30 and state.arousal > 20:
        mood = "开心"
    elif state.valence > 30 and state.arousal <= 20:
        mood = "平静但有暖意"
    elif state.valence > 0 and state.arousal > 30:
        mood = "害羞（情绪正向但激动）"
    elif state.valence < -20 and state.arousal > 20:
        mood = "生气或不安"
    elif state.valence < -10:
        mood = "低落"
    else:
        mood = "平静"
    
    return f"""[当前关系状态]
关系等级：{level}（{level_names[level]}）
好感度：{state.affection:.0f}/100
信任度：{state.trust:.0f}/100
熟悉度：{state.familiarity:.0f}/100

[当前情绪]
情绪倾向：{mood}
情绪正负：{state.valence:.0f}（-100 极度负面 ←→ +100 极度正面）
情绪激烈：{state.arousal:.0f}/100"""
```

### 2.8 Prompt 组装新顺序（Stage 6 后）

```
1. system: 【人设拼装结果】身份→性格→说话风格→禁忌→固定角色约束       ← Stage 3
2. system: 【当前关系状态 + 当前情绪】（动态，每次计算）                 ← Stage 6 新增
3. system: 【参考片段】（动态检索 Top 3，可能没有）                     ← Stage 5
4. system: 【对话摘要】（可能没有）                                     ← Stage 4
5. few-shot examples                                                    ← Stage 3
6. 历史滑窗口 + 本次 user message
```

关系状态放在人设之后、参考片段之前。因为人设告诉 LLM"你是谁"，状态告诉 LLM"你和这个用户现在什么关系、什么情绪"——LLM 基于这两段决定用什么语气回复。

---

## 三、文件清单与职责

| 文件 | 动作 | 职责 |
|---|---|---|
| `core/state.py` | **新建** | 状态数据类 `CharacterState` + 事件表 + 衰减逻辑 + 事件匹配 + 持久化 |
| `core/handler.py` | **修改** | `_generate_reply()` 前先 decay + 匹配事件 + 应用数值变化；Prompt 组装加状态段 |
| `main.py` | **修改** | 启动时 `data/state/` 目录初始化；`SessionStore` 旁边多传一个 `StateStore` 给 handler |
| `config.py` | **修改** | 新增 `STATE_DIR` 配置 |
| `.env` + `.env.example` | **修改** | 新增 `STATE_DIR` |

### 改动幅度估算

**core/state.py（新建，约 250 行）**：
- `CharacterState` dataclass（10 个字段 + 序列化）
- `RELATIONSHIP_EVENTS` dict（15 条，其中 `user_ignored_long` 未实现 → 实际生效 14 条）
- `EMOTION_EVENTS` dict（15 条，其中 `user_long_absent` 未实现 → 实际生效 14 条）
- `EVENT_PATTERNS` dict（13 个 pattern 组）
- `CharacterStateManager` 类：`load(user_id)` / `save()` / `match_events(text)` / `apply_events(events)` / `decay()` / `to_prompt()`

**core/handler.py 修改（约 30 行）**：
- `_generate_reply()` 入口：先 `state.decay()` → `events = state.match_events(text)` → `state.apply_events(events)` → 然后才组装 Prompt（加状态段）
- 最后 `state.interaction_count += 1` + `state.save()`

### 不需要改的文件
- `adapter/onebot.py`（协议层不动）
- `persona/loader.py`（人设层不动，状态注入在 handler 里做）
- `core/session.py`（session 管对话，state 管数值，两个独立）
- `core/llm.py`（LLM 调用不变）

### core/state.py 类图

```
CharacterState (dataclass)
  ├─ affection: float
  ├─ trust: float
  ├─ familiarity: float
  ├─ valence: float
  ├─ arousal: float
  ├─ interaction_count: int
  ├─ last_interaction: str (ISO)
  ├─ created_at: str (ISO)
  └─ decay_at: str (ISO)
  ├─ to_json() → dict
  └─ from_json(dict) → CharacterState

CharacterStateManager
  ├─ __init__(state_dir: Path)
  ├─ load(user_id: int) → CharacterState   # 文件不存在则创建默认状态
  ├─ save(state, user_id: int)
  ├─ match_events(user_text: str) → list[str]   # 匹配到的事件 key 列表
  ├─ apply_events(state, event_keys: list[str])  # 查表 + 防暴走裁剪
  ├─ decay(state)                                # 情绪衰减
  ├─ calc_relationship_level(affection) → int    # 静态方法
  └─ state_to_prompt(state) → str                # 转自然语言给 LLM
```

---

## 四、配置项（新增）

| 变量 | 默认 | 说明 |
|---|---|---|
| `STATE_DIR` | `data/state` | 角色状态目录；留空或不存在则跳过状态系统 |

**防暴走常量**（写死在 `core/state.py` 里，不改 config）：
- `MAX_DELTA_PER_FIELD = 5.0`（单次事件单字段最大变化）
- `BASE_VALENCE = 20.0`
- `BASE_AROUSAL = 15.0`
- `DECAY_RATE = 0.1`
- `TIME_SCALE_MINUTES = 30`

---

## 五、验收清单

### 状态文件与持久化
1. 首次启动自动创建 `data/state/{qq}.json`，默认 affection=20 / trust=15 / familiarity=20（Level 1 刚认识）
2. 重启后数值保留，不归零

### 事件匹配与数值变化
3. 发 "我喜欢你" → affection +2, trust +1, valence +20, arousal +35（害羞大）
4. 发 "你今天怎么样" → affection +2, trust +1.5, valence +15, arousal +12
5. 发普通闲聊 "今天天气不错" → affection +0.3, familiarity +0.3, valence +2, arousal +1
6. 发恶意侮辱类 → affection 和 trust 扣（但单次不超过 -5）
7. 同时匹配多个事件（如 "你今天怎么样，有没有吃饭"）→ 数值累加

### 防暴走
8. 连续发 10 次 "我喜欢你" → affection 不会一次跳满（每轮最多 +5）
9. 发 3 次侮辱 → trust 不会直接归零（每次最多 -5）

### 情绪衰减
10. 发一次让结灯生气的消息（valence -35），等待 2 小时后看日志 → valence 应该接近 -5 或正数
11. 正常对话后 valence +15，等 30 分钟后 → 至少衰减了一部分

### Prompt 动态注入
12. system prompt 里出现 `[当前关系状态]` 和 `[当前情绪]` 段
13. 不同 affection 值 → relationship_level 正确映射（39→1, 40→2, 80→4）

### 无状态文件时的回退
14. 删掉 `data/state/` 目录重启 → 自动创建，对话不受影响（不会崩）

---

## 六、风险点与回退

| 风险 | 影响 | 应对 |
|---|---|---|
| 关键词匹配不准（"你好"可能是打招呼也可能是反讽） | 数值涨得不对 | 这是纯文本匹配的固有局限。Stage 6 先做最简版，以后可以让 LLM 返回 event 名（Stage 7） |
| 初始状态（affection=20, Level 1）让老用户体验变差 | 第一次聊天机器人太"生" | 可以在 .env 里加 `INITIAL_AFFECTION` 让用户调；或者 Stage 7 做"从已有 sessions 推断初始状态" |
| 数值长时间不涨（几轮聊天看不出变化） | 用户觉得没效果 | 事件表的数值是经过设计的——正常聊天 50 轮左右 affection 从 20 → 35（Level 1 → 1.75），不会跳太快也不会太慢 |
| 情绪衰减让生气消失太快 | 用户觉得角色"没记性" | 30 分钟衰减一次完整步骤，2 小时后还没完全消，一天回到平静。如果嫌快可以把 `DECAY_RATE` 改成 0.05 |
