# 阶段 1 设计：骨架 + OneBot 反向 WS 接入

## 一、范围边界

### 做
- 项目骨架与配置加载
- OneBot v11 反向 WebSocket 服务端（我们是服务端，NapCat 是客户端）
- 私聊消息回显（带 `[echo]` 前缀）
- 好友请求自动同意（仅白名单内）
- QQ 号白名单过滤

### 不做
- 不接 LLM（阶段 2）
- 不存上下文（阶段 2）
- 不读人设文件（阶段 3）
- 不做断线重连、限流（阶段 5）
- 不处理群聊消息（本项目定位为私聊机器人）

## 二、协议契约

### 2.1 连接关系

```
NapCatQQ (WS Client)  ──反向连接──►  PrivateBot (WS Server)
                                       ws://127.0.0.1:8080/onebot
```

NapCat 握手时的 HTTP 头：

| 头 | 用途 |
|---|---|
| `Authorization: Bearer <token>` | 鉴权，需与 `.env` 中 `ONEBOT_ACCESS_TOKEN` 一致 |
| `X-Self-ID` | 机器人 QQ 号 |
| `X-Client-Role` | `Event` / `API` / `Universal`，决定连接能干什么 |

> **websockets 版本要求：>= 14.0**（已按 17.1 实测）。
> 新版 asyncio API 与旧版差异：
> - handler 收到的是 `ServerConnection`（非 `WebSocketServerProtocol`）
> - `serve()` 不再接受 `path` 参数，路径需在 handler 内自行校验
> - 请求头通过 `conn.request.headers` 读取（非 `conn.request_headers`）
> - 客户端连接用 `additional_headers=`（非 `extra_headers=`）

### 2.2 连接角色处理

- 一条 `Universal` 连接：既能收事件也能调 API
- 若 NapCat 分别建立 `Event` 和 `API` 两条连接：按角色分开存储
- 调 API 时优先级：`Universal` > `API`

### 2.3 API 调用与 echo 匹配

通过 WS 下发指令，响应靠 `echo` 字段回关。

下发：
```json
{
  "action": "send_private_msg",
  "params": {"user_id": 10001, "message": [{"type": "text", "data": {"text": "hi"}}]},
  "echo": "a1b2c3"
}
```

回包：
```json
{"status": "ok", "retcode": 0, "data": {"message_id": 456}, "echo": "a1b2c3"}
```

实现：`pending: dict[echo, Future]`，超时 5 秒；用 `pending_ws` 记录归属连接，断连时只失败该连接自己的请求。

### 2.4 消息格式

一律用「消息段数组」格式，不用 CQ 码字符串，避免转义坑。

### 2.5 关键事件结构

**私聊消息事件**：
```json
{
  "post_type": "message",
  "message_type": "private",
  "sub_type": "friend",
  "user_id": 10001,
  "raw_message": "hi",
  "message": [{"type": "text", "data": {"text": "hi"}}],
  "sender": {"nickname": "xxx"},
  "self_id": 99999
}
```

**好友请求事件**：
```json
{
  "post_type": "request",
  "request_type": "friend",
  "user_id": 10001,
  "comment": "我是 xxx",
  "flag": "xxx"
}
```

对应调用 `set_friend_add_request`，参数 `{"flag": "...", "approve": true}`。
注意：`flag` 是一次性的，不能缓存复用。

### 2.6 心跳

`post_type: "meta_event"` + `meta_event_type: "heartbeat"`，默认 5 秒一次。
阶段 1 只记日志，用于肉眼确认连接存活。

## 三、文件清单与职责

```text
PrivateBot/
├─ main.py                 组装 + 启动 + 优雅退出
├─ config.py               配置读取与校验
├─ requirements.txt        依赖清单
├─ .env.example            配置模板
├─ .gitignore              忽略规则
├─ adapter/
│  └─ onebot.py            WS 服务端 + 协议解析 + API 调用
└─ core/
   └─ handler.py           业务分派（白名单 / 回显 / 好友请求）
```

### `config.py`
- `Settings` dataclass
- 字段见下方配置表
- `ALLOWED_QQ` 从逗号字符串解析成 `set[int]`
- 缺失必填项时抛清晰错误（非 `KeyError`）

### `adapter/onebot.py`
- `OneBotServer(settings)` —— 纯协议层，不含业务判断
- `async run(on_event)`：`websockets.serve()` 监听 + 常驻，回调通过参数注入
- `async _handler(conn: ServerConnection)`：
  - 校验路径（新版 `serve()` 无 `path` 参数，需比较 `conn.request.path`）
  - 校验 token（若配置）
  - 读取 `X-Self-ID` / `X-Client-Role`
  - 注册连接到 `self._roles[role]`
  - 循环收消息 → 区分 API 回包 / 事件
  - **事件用 `asyncio.create_task` 并发分发，绝不阻塞接收循环**（否则 echo 回执读不到，`call_api` 必然超时）
  - `finally` 中移除连接引用，并只失败属于该连接的 pending 请求
- `async call_api(action, params, timeout=5)`：
  - 生成 echo
  - 选中可用连接（Universal > API）
  - 发 JSON，`await` Future
  - 超时抛 `TimeoutError`
- `async send_private_msg(user_id, text)` / `async set_friend_add_request(flag, approve)`：薄封装
- `extract_private_text(event)` / `extract_friend_request(event)`：静态纯函数，不匹配返回 `None`

### `core/handler.py`
- `PrivateChatHandler(settings, bot, ...)` —— 依赖 `bot` 的公开方法，不碰 websocket
- `async on_event(event)`：按 `post_type` 分派
- 白名单未命中直接 `return`，不产生任何网络请求

### `main.py`
- 配置日志
- 组装 `Settings` / `OneBotServer` / `PrivateChatHandler`
- `OneBotServer.run(handler.on_event)`
- `KeyboardInterrupt` 优雅关闭

## 四、配置项

| 变量 | 示例 | 说明 |
|---|---|---|
| `ONEBOT_WS_HOST` | `127.0.0.1` | 监听地址 |
| `ONEBOT_WS_PORT` | `8080` | 监听端口 |
| `ONEBOT_WS_PATH` | `/onebot` | 路径，需与 NapCat 一致 |
| `ONEBOT_ACCESS_TOKEN` | `my-token` | 与 NapCat 一致；留空则跳过鉴权 |
| `ALLOWED_QQ` | `10001,10002` | 白名单，逗号分隔 |
| `AUTO_ACCEPT_FRIEND` | `true` | 仅自动同意白名单内的好友请求 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 五、NapCat 配置步骤

1. 安装 NapCatQQ（Windows 一键包），启动后打开 WebUI（默认 `http://localhost:6099`）
2. 扫码登录 **QQ 小号**（别用主号）
3. 进「网络配置」→ 新建 **WebSocket 客户端**（注意不是「WebSocket 服务器」）
   - URL：`ws://127.0.0.1:8080/onebot`
   - Token：与 `.env` 的 `ONEBOT_ACCESS_TOKEN` 一致
   - 消息格式：`array`
   - 心跳间隔：`5000`
4. 上报事件勾选 **消息** 和 **请求**
5. 保存并启用

> 顺序：先启动我们的程序，再启用 NapCat 连接。若顺序反了，NapCat 会自动重试。

## 六、验收清单

1. 启动 `python main.py`，日志出现 `listening on ws://127.0.0.1:8080/onebot`
2. NapCat 启用连接后，日志出现 `client connected role=Universal self_id=xxx`
3. 每 5 秒能刷到一条 heartbeat 日志
4. 白名单内的号私聊发 `hi`，收到 `[echo] hi`（阶段 2 后改为自然语言回复）
5. 非白名单号私聊，日志记录 `ignored non-whitelisted user_id=xxx`，无回包
6. 白名单内的号发起好友请求，自动通过；非白名单号发起，忽略
