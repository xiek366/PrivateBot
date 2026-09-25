# PrivateBot

基于 **OneBot v11 反向 WebSocket** 的本地私有 QQ 私聊机器人。支持 YAML 人设、参考语料风格模仿、数值化角色状态、长期记忆与主动发消息。

设计目标是**轻量**：纯 Python，无重框架（不用 NoneBot / LangChain / FastAPI），存储全部用 JSON 文件，不引入数据库和向量库。

---

## 特性

| 阶段 | 能力 |
|---|---|
| 1 | OneBot v11 反向 WS 服务端 + QQ 白名单 + 好友请求自动同意 |
| 2 | OpenAI 兼容 LLM 接入（默认 DeepSeek）+ 滑窗口上下文 + JSON 持久化 |
| 3 | YAML 人设系统（身份/性格/说话风格/禁忌/示例）+ 热重载 + few-shot 注入 |
| 4 | 断线重连 + 消息丢弃策略（同用户只留最新一条）+ 自动摘要压缩 |
| 5 | 参考语料轻量检索（2-gram + difflib，Top 3 注入），模仿指定角色语气 |
| 6 | 角色状态系统（好感度/信任度/熟悉度/情绪正负/情绪激烈 + 事件表 + 时间衰减 + Prompt 动态注入） |
| 7 | 长期记忆库（4 类记忆 + importance + 语义去重 + 检索注入）+ 主动发消息调度器 |

**热重载**：改 `data/personas/default.yaml` 或 `data/style/reference.txt` 存盘即生效，无需重启。

---

## 架构

三层解耦，从下到上：

```
OneBot 协议层   adapter/onebot.py     只负责 WS 收发、鉴权、事件解析
      ↓
业务处理层      core/handler.py       白名单过滤、消息丢弃、组装 Prompt、调用 LLM
      ↓
能力层          core/llm.py            LLM 调用（对话 / 摘要 / 记忆提取）
                core/session.py        对话历史与摘要
                core/state.py          角色状态数值
                core/memory.py         长期记忆
                core/proactive.py      主动发消息
                persona/loader.py      人设与参考语料
```

**核心设计原则**：**程序负责计算，LLM 负责表达**。角色状态的数值变化由 Python 事件表计算，不让 LLM 直接改数值。

### Prompt 组装顺序

```
1. system: 人设（身份→性格→说话风格→禁忌→固定约束）
2. system: 当前关系状态 + 当前情绪
3. system: 参考片段（动态检索 Top 3）
4. system: 对话摘要（可能为空）
5. system: 长期记忆（自然语言，不暴露"这是记忆"）
6. few-shot 示例
7. 历史滑窗口 + 本次用户消息
```

---

## 环境要求

- **Python 3.11+**（实测环境 3.11.5）
- **NapCat**（QQ 客户端，提供 OneBot v11 实现）—— 负责登录 QQ 并主动连接本服务
- 一个 **OpenAI 兼容协议**的 LLM API（默认 DeepSeek）

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

依赖只有四个：`websockets>=14.0`、`httpx>=0.27.0`、`python-dotenv>=1.0.0`、`PyYAML>=6.0`。

> `websockets` 必须 **14.0 及以上**，本项目使用新版 asyncio API，旧版不兼容。

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env`，至少填这三项：

- `LLM_API_KEY`：你的 DeepSeek 密钥
- `ALLOWED_QQ`：允许跟机器人对话的 QQ 号（**是使用者的号，不是机器人的号**），多个用逗号分隔
- `ONEBOT_ACCESS_TOKEN`：自定义一个口令，稍后要填到 NapCat 里，两边必须一致

### 3. 准备人设与参考语料

- `data/personas/default.yaml`：人设定义
- `data/style/reference.txt`：参考语料（纯文本，一段一条，用来模仿语气）

两者都可留空——人设缺失时回退到 `.env` 里的 `SYSTEM_PROMPT`。

### 4. 启动

```bash
python main.py
```

正常启动会打印：

```
白名单: [10001]
人设文件: .../data/personas/default.yaml
角色状态系统: True
长期记忆库: True
主动消息调度器: True
listening on ws://127.0.0.1:8080/onebot
```

### 5. 配置 NapCat

打开 NapCat WebUI → **网络配置** → 新建 **WebSocket 客户端**，URL 填：

```
ws://127.0.0.1:8080/onebot?access_token=你的ONEBOT_ACCESS_TOKEN
```

连上后 PrivateBot 会打印：

```
client connected role=Universal self_id=机器人QQ号
heartbeat ok，连接存活
```

> **路径和口令两边必须完全一致**，否则会被服务端直接断开（日志表现为连接建立后立刻 `auth failed` 或 `unexpected path`）。
> 服务端同时支持 `Authorization: Bearer <token>` 请求头和 `?access_token=` 查询参数两种传法。

### 6. 联调（可选，不需要真实 QQ）

`tools/mock_client.py` 可以模拟 NapCat 客户端，配一个本地模拟 LLM 服务即可全链路验证，无需真实 QQ 与真实 API Key。

---

## 配置项

全部通过 `.env` 管理，敏感信息不硬编码。

### 基础

| 变量 | 默认 | 说明 |
|---|---|---|
| `ONEBOT_WS_HOST` / `ONEBOT_WS_PORT` | `127.0.0.1` / `8080` | 反向 WS 监听地址 |
| `ONEBOT_WS_PATH` | `/onebot` | 路径校验，不匹配直接拒绝 |
| `ONEBOT_ACCESS_TOKEN` | — | 鉴权口令，空则不校验 |
| `ALLOWED_QQ` | — | **必填**，白名单，非白名单消息直接丢弃 |
| `AUTO_ACCEPT_FRIEND` | `true` | 是否自动同意白名单用户的好友请求 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

### LLM

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_API_KEY` | — | **必填** |
| `LLM_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `LLM_MODEL` | `deepseek-chat` | 模型名（`.env.example` 模板里给的是 `deepseek-flash`） |
| `LLM_TIMEOUT` | `60` | 请求超时（秒） |
| `MAX_HISTORY` | `20` | 滑窗口保留的对话条数 |
| `SYSTEM_PROMPT` | — | 人设缺失时的兜底提示词 |
| `PERSONA_FILE` | `data/personas/default.yaml` | 人设文件路径，留空则只用 `SYSTEM_PROMPT` |

### 稳定性与摘要（阶段 4）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_PENDING_PER_USER` | `1` | 同用户在处理中时，只保留最新一条待处理消息；`0` 关闭丢弃 |
| `SUMMARY_THRESHOLD` | `30` | 消息累计到此条数触发摘要压缩 |
| `SUMMARY_KEEP` | `10` | 压缩后保留的近期对话条数 |
| `LLM_SUMMARY_TEMP` | `0.3` | 摘要生成的 temperature |

### 人设与语料（阶段 3 / 5）

| 变量 | 默认 | 说明 |
|---|---|---|
| `STYLE_DIR` | `data/style` | 参考语料目录（放 `reference.txt`）；留空或目录不存在则跳过 |

### 角色状态（阶段 6）

| 变量 | 默认 | 说明 |
|---|---|---|
| `STATE_DIR` | `data/state` | 状态目录；留空或目录不存在则跳过整个状态系统 |

### 长期记忆与主动消息（阶段 7）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEMORY_DIR` | `data/memories` | 记忆目录；留空或目录不存在则跳过 |
| `ENABLE_PROACTIVE` | `true` | 主动发消息开关（需 `STATE_DIR` 同时启用） |
| `PROACTIVE_MIN_HOURS` | `12` | 距上次聊天最少隔多少小时才主动发 |
| `PROACTIVE_MIN_AFFECTION` | `30` | 最低好感度（约 Level 2 亲近以上） |
| `PROACTIVE_WINDOW_START` / `_END` | `08:00` / `23:00` | 允许主动发的时间窗口，凌晨不打扰 |

**主动发消息的 5 个触发条件（全部满足才发）**：

1. `affection ≥ PROACTIVE_MIN_AFFECTION`
2. 当前时间在允许窗口内
3. 24 小时内没主动发过
4. 情绪不极端（`valence ≥ -50` 且 `arousal ≤ 60`）
5. 距上次聊天 ≥ `PROACTIVE_MIN_HOURS`

调度器每 **10 分钟**检查一次，且启动后**先等 10 分钟**才做第一次检查。

---

## 数据文件

运行时数据全部是 JSON，每用户一份，可以直接打开查看或删除（删除后自动重建）。

| 路径 | 内容 |
|---|---|
| `data/sessions/{qq}.json` | 对话历史 + 摘要 |
| `data/state/{qq}.json` | 角色状态数值（affection / trust / familiarity / valence / arousal / interaction_count / last_interaction / decay_at） |
| `data/memories/{qq}.json` | 长期记忆条目（category / content / importance / created_at / last_used / source_msg） |
| `data/personas/default.yaml` | 人设（属于配置，需要入库） |
| `data/style/reference.txt` | 参考语料（属于配置） |

### 角色状态

| 字段 | 范围 | 说明 |
|---|---|---|
| `affection` | 0-100 | 好感度 |
| `trust` | 0-100 | 信任度 |
| `familiarity` | 0-100 | 熟悉度 |
| `valence` | -100 ~ +100 | 情绪正负 |
| `arousal` | 0-100 | 情绪激烈程度 |

关系等级由 `affection` 映射：0-19 刚认识 / 20-39 熟悉 / 40-59 亲近 / 60-79 暧昧 / 80-94 恋人 / 95+ 深度依赖。

**数值变化由事件表驱动**：用户消息经过正则匹配触发事件（如"我喜欢你"→ affection +2.0），单次单字段变化上限 ±5.0 防暴走；`valence` / `arousal` 每 30 分钟向平静基线衰减一次。

### 长期记忆

4 个分类：`preference`（喜好）/ `habit`（习惯）/ `profile`（基本信息）/ `relationship`（共同经历）。

- **提取**：每轮回复后异步用 LLM 提取，要求只提取关于用户的客观事实，禁止提取角色自身状态和瞬时状态
- **去重**：两道关卡——LLM 侧语义去重（把已有记忆清单喂回去）+ Python 侧文本去重（相似度 > 0.8，或短句被长句完整包含）
- **检索**：`importance × 0.6 + 相关性 × 0.3 + 时间新鲜度 × 0.1` 排序取 Top 5 注入
- **生命周期**：同分类上限 50 条，超出淘汰 importance 最低的

---

## 项目结构

```
PrivateBot/
├── main.py                      入口：装配组件 + 启动后台调度器
├── config.py                    配置加载与校验
├── requirements.txt
├── .env.example                 配置模板
├── AGENTS.md                    研发规则（阶段流程约束）
├── adapter/
│   └── onebot.py                OneBot v11 反向 WS 服务端
├── core/
│   ├── handler.py               私聊消息业务处理
│   ├── llm.py                   LLM 客户端 + 记忆提取
│   ├── session.py               对话历史滑窗口 + 摘要压缩
│   ├── state.py                 角色状态（数值 + 事件表 + 衰减）
│   ├── memory.py                长期记忆库
│   └── proactive.py             主动发消息调度器
├── persona/
│   └── loader.py                人设加载 + 热重载 + 参考语料检索
├── data/
│   ├── personas/default.yaml    人设（热重载）
│   ├── style/reference.txt      参考语料（热重载）
│   ├── sessions/                运行时生成
│   ├── state/                   运行时生成
│   └── memories/                运行时生成
├── docs/                        各阶段设计文档
└── tools/
    └── mock_client.py           本地模拟 NapCat 客户端
```

---

## 设计文档

每个阶段都有独立的设计文档，含范围边界、协议契约、验收清单：

- [阶段 1：OneBot 反向 WS 服务端 + 白名单](docs/stage1-design.md)
- [阶段 2：DeepSeek 接入 + 上下文 + 持久化](docs/stage2-design.md)
- [阶段 3：YAML 人设系统](docs/stage3-design.md)
- [阶段 4：稳定性 + 自动摘要](docs/stage4-design.md)
- [阶段 5：参考语料轻量检索](docs/stage5-design.md)
- [阶段 6：角色状态系统](docs/stage6-design.md)
- [阶段 7：长期记忆库 + 主动发消息](docs/stage7-design.md)

---

## 已知限制

- **LLM 跨用户剧情脑补**：回复偶尔会引用与该用户无关的剧情（人设/语料驱动）。已确认状态与记忆本身是**按用户隔离**的，属于 LLM 行为，当前接受此设计。
- **记忆去重有上限**：LLM 侧语义去重只覆盖**最近 30 条**记忆；更早的条目仅靠文本相似度兜底，极端改写仍可能重复。
- **主动消息的 24 小时频率限制不持久化**：依据只存在内存中，进程重启会重新计时。
- **事件匹配是纯文本正则**：存在固有误判（如"你好烦"会命中问候），这是关键词匹配的局限，非 bug。
- **参考语料本身不随包分发**：`data/*` 除 `data/personas/` 外均被 `.gitignore` 排除，因为角色台词可能涉及版权，请自行准备。

---

## 安全提示

`.env` 内含 API 密钥和 QQ 号，**已在 `.gitignore` 中排除**。

但要注意：**如果是直接压缩整个文件夹上传，`.env` 和 `data/` 会被一起打包**。上传前请确认：

- 已删除或清空 `.env`（用 `.env.example` 代替）
- 已清理 `data/sessions/`、`data/state/`、`data/memories/` 中的个人聊天记录
- 若密钥曾随包泄露，请到服务商处**立即吊销并重新生成**

---

## 开发说明

项目遵循「**先写文档 → 用户确认 → 写代码 → 自测验收**」的阶段流程，规则见 [AGENTS.md](AGENTS.md)。调整需求时先改对应阶段的设计文档，再改代码。