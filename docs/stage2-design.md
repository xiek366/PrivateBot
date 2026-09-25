# 阶段 2 设计：接入 DeepSeek，实现真实对话

## 一、范围边界

### 做
- 接入 DeepSeek（OpenAI 兼容 `/chat/completions`）
- 按 QQ 号维护上下文滑动窗口（默认最近 20 条）
- 上下文持久化到 `data/sessions/{user_id}.json`
- 同一用户消息串行处理，防止上下文错乱
- LLM 失败时返回兜底话术，不让机器人沉默

### 不做
- 不读人设文件（阶段 3，本阶段用 `.env` 里的 `SYSTEM_PROMPT` 占位）
- 不做长期摘要压缩（阶段 3 与人设一起做）
- 不做流式输出（QQ 消息本身不支持流式编辑）
- 不做图片/语音消息（暂不需要）

## 二、协议契约

### 2.1 请求

POST `{LLM_BASE_URL}/chat/completions`，Header `Authorization: Bearer {LLM_API_KEY}`

```json
{
  "model": "deepseek-chat",
  "messages": [
    {"role": "system", "content": "人设/系统提示"},
    {"role": "user", "content": "历史用户消息"},
    {"role": "assistant", "content": "历史机器人回复"},
    {"role": "user", "content": "本次用户消息"}
  ],
  "temperature": 0.9,
  "stream": false
}
```

### 2.2 响应

取 `choices[0].message.content`，`strip()` 后作为回复。

### 2.3 失败处理

- 网络/HTTP 错误：重试 2 次，退避 1.5s / 3s
- 全部失败：记 error 日志，回复兜底话术「抱歉，我这边出了点问题，稍后再试。」
- 兜底话术**不写入上下文**，避免污染历史

## 三、上下文策略

- 存储结构：`deque(maxlen=MAX_HISTORY)`，超出自动丢弃最旧的
- 组装顺序：`system` → 历史（时间正序，末尾即本次 user 消息）
- 落盘时机：每次成功生成回复后写一次
- 并发控制：每个 `user_id` 一把 `asyncio.Lock`，保证同一用户的消息顺序处理

## 四、文件清单

| 文件 | 动作 | 职责 |
|---|---|---|
| `config.py` | 新增字段 | LLM 配置、上下文长度、系统提示 |
| `core/llm.py` | 新建 | httpx 异步客户端 + 重试 |
| `core/session.py` | 新建 | 上下文读写与持久化 |
| `core/handler.py` | 改写 | 回显改为调 LLM |
| `main.py` | 改写 | 组装 LLM 与 SessionStore |
| `requirements.txt` | 新增 | httpx |
| `.env.example` | 新增字段 | LLM 相关配置 |
| `tools/mock_client.py` | 新建 | 本地验收：模拟 NapCat 验证闭环 |

## 五、配置项（新增）

| 变量 | 示例 | 说明 |
|---|---|---|
| `LLM_API_KEY` | `sk-xxx` | DeepSeek 密钥，必填 |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | 兼容其他 OpenAI 协议服务 |
| `LLM_MODEL` | `deepseek-chat` | 模型名 |
| `LLM_TIMEOUT` | `60` | 单次请求超时（秒） |
| `MAX_HISTORY` | `20` | 上下文保留条数 |
| `SYSTEM_PROMPT` | `你是一个...` | 阶段 3 会被人设文件取代 |

## 六、验收清单

1. 启动无报错，日志出现 `listening on ws://...`
2. 私聊发「你好」，收到自然语言回复（不再是 `[echo]`）
3. 连发 3 轮，第 3 轮问「我第一条说了什么」，能答对 → 证明上下文生效
4. 检查 `data/sessions/{你的QQ}.json` 存在且含 6 条消息（3 轮问答）
5. 把 `LLM_API_KEY` 改错，收到兜底话术，且日志有 error
6. 非白名单号发消息，仍无任何反应
