# 阶段 3 设计：人设系统（手写人设 + prompt 拼装）

## 一、范围边界

### 做
- YAML 人设文件：身份 / 性格 / 说话风格 / 禁忌 / 示例对话
- 启动加载人设，拼装 system prompt，few-shot 注入 messages
- 热重载：改完存盘即生效，无需重启（mtime + size 判断）
- 缺失或语法错误时回退到 `.env` 的 `SYSTEM_PROMPT`，保留上一份可用人设

### 不做
- 不做聊天记录风格自动学习（往后放）
- 不做多个人设切换（全局统一一个人设）
- 不做长期摘要压缩（推迟到阶段 5）

## 二、协议契约

### 2.1 人设文件格式（`data/personas/default.yaml`）

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `name` | 是 | str | 人设名，仅用于日志 |
| `identity` | 是 | str | 身份背景，多行；缺失视为无效人设 |
| `personality` | 否 | str | 性格 |
| `speaking_style` | 否 | str 或 list | 说话风格 |
| `taboos` | 否 | list[str] | 禁忌 |
| `examples` | 否 | list[{user, assistant}] | few-shot 示例，建议 2-5 组 |

### 2.2 system prompt 拼装顺序

`【身份】` → `【性格】` → `【说话风格】` → `【禁忌】` → 固定角色约束句。
空字段自动跳过，不产生空标题。

### 2.3 messages 组装顺序

system（上述拼装结果）→ examples（user/assistant 交替）→ 历史滑窗（末尾即本次用户消息）

### 2.4 热重载与回退

- 每次生成回复前比较 `(mtime, size)`，变化则重载
- 加载失败：保留上一份可用人设，记 warning，并记录新签名避免同一坏文件反复报错
- 文件不存在：回退 `SYSTEM_PROMPT`，仅警告一次
- 回退链：人设文件 → `SYSTEM_PROMPT` → 内置兜底文案

## 三、文件清单与职责

| 文件 | 动作 | 职责 |
|---|---|---|
| `persona/__init__.py` | 新建 | 包声明 |
| `persona/loader.py` | 新建 | `PersonaLoader`：加载、校验、拼装、热重载 |
| `data/personas/default.yaml` | 新建 | 默认人设 |
| `config.py` | 修改 | 新增 `persona_file` 字段 |
| `core/handler.py` | 修改 | 使用人设 prompt 与 few-shot；修正心跳日志与失败落盘 |
| `main.py` | 修改 | 组装并注入 `PersonaLoader` |
| `requirements.txt` | 修改 | 新增 `PyYAML>=6.0` |
| `.env.example` | 修改 | 新增 `PERSONA_FILE` |
| `.gitignore` | 修改 | `data/*` 且放行 `data/personas/` |

## 四、配置项（新增）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PERSONA_FILE` | `data/personas/default.yaml` | 相对项目根目录；留空则只用 `SYSTEM_PROMPT` |

## 五、验收清单

1. 启动日志出现 `persona loaded: 小柚`，无 warning
2. 问「你是谁」，回复符合 `identity`，不自称 AI
3. 问「用 Markdown 列个表」，回复仍遵守 `speaking_style`
4. **不重启**修改 `identity`，再发消息 → 风格立刻变化
5. 改成非法 YAML → warning，仍能正常回复
6. 删除 `default.yaml` → 同样回退，不崩
7. `examples` 生效：问示例里出现过的话题，语气接近示例

## 六、已知边界

- `persona/__init__.py` 可省（Python 3.3+ 命名空间包），补上仅为与 `adapter/`、`core/` 保持一致
- 热重载在同一秒内连续保存两次且文件大小不变时，可能漏检一次；重发一条消息即可