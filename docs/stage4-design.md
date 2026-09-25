# 阶段 4 设计：稳定性补强 + 长期记忆

## 一、范围边界

### 做
1. **断线重连恢复**：NapCat 断线重连后，自动恢复 pending 的 API 调用（单次重试）
2. **消息限流/丢弃策略**：同一用户在 LLM 处理中又发新消息时，丢弃中间消息、只保留最新一条
3. **自动摘要压缩**：滑窗口满阈值时，把最旧的对话片段压成摘要，滑窗口同时维护「摘要 + 近期对话」

### 不做
- 不做多人设切换（Stage 5）
- 不做从用户历史消息学习风格（Stage 5）
- 不做图片/语音消息处理（Stage 5，需要多模态模型）
- 不做 RAG / 向量检索（当前对话量级不需要）
- 不做管理后台（纯文件配置够用）

---

## 二、协议契约

### 2.1 断线重连恢复

**背景**：当前 OneBotServer 断连时，`_fail_pending_for()` 会直接把属于该连接的所有 pending Future 置为失败，调用方收到 ConnectionError。NapCat 自己会自动重连，但重连期间的 API 调用全部丢失。

**新逻辑**：
- 断连时，不是直接失败 pending，而是记录为「待重试」状态：`_retry_queue: list[(echo, action, params, attempt_count)]`
- 新连接建立时，按 FIFO 顺序重发待重试请求，最多再试 1 次
- 如果重试也超时，最终才失败
- 限制：只重试还没被其他连接成功处理过的请求（防止重复发送消息）

**状态流转**：
```
call_api() → pending[echo] = Future
                │
                ├─ 正常收到 echo 回执 → Future resolve → pending 清理
                │
                └─ 连接断开
                    │
                    ├─ attempt_count < 1 → 移入 retry_queue → 等新连接建立后重发
                    └─ attempt_count >= 1 → Future set_exception(ConnectionError)
```

**边界**：如果断连的是 Universal 连接且此时有 Event 连接在线，pending 请求不失败，而是等到 Universal 或 API 连接可用时继续（因为 call_api 用的是 `_pick_api_conn()` 优先级）。这种情况当前代码已经天然支持——只是当前实现是断连就 cancel，现在改成断连后如果有其他可用连接就继续等。

### 2.2 消息限流/丢弃

**背景**：当前 handler 用 `asyncio.Lock` 串行处理同一用户的消息。如果 LLM 一条要 5 秒，用户连发 5 条就是 25 秒延迟。Lock 是队列式的，所有消息都排队等处理——体验差 + 浪费 LLM API 额度。

**新策略**：**合并 + 丢弃中间**。

```
用户连发消息到达顺序：
  msg1 → msg2 → msg3 → msg4(此时 msg1 还在调 LLM)

  当前行为：Lock 队列依次处理 msg1→msg2→msg3→msg4，全部发给 LLM
  新行为：msg1 正常处理；msg2、msg3 标记为"待处理"；msg4 到来时，msg2、msg3 被丢弃，只保留 msg1 + msg4
```

**实现方式**：用 `deque` 或简单的标记位，在 `_lock_for()` 旁边加一个 `_pending_msg: dict[int, str]`。Lock 释放时检查是否有待处理消息，如果有就处理最新一条，同时再检查一轮（防止 Lock 释放到重新获取期间又有新消息）。

**最大排队深度**：设为 1，即"只保留最新一条待处理"。这是最简单且最符合直觉的策略。

**可配置项**：`MAX_PENDING_PER_USER=1`，留着以后可能想改成保留多条。

**日志**：丢弃时打 `logger.info("dropped %d pending messages for user_id=%d", count, user_id)`，方便你知道用户发了多少条被吞了。

### 2.3 自动摘要压缩

**背景**：当前 SessionStore 用 `deque(maxlen=20)`，满了就丢弃最旧的。聊到第 21 轮时，第 1 轮的上下文就丢了——但早期对话里的信息（比如用户说过"我叫张三"）以后可能还需要。

**新策略**：滑窗口满时，自动生成摘要。

```
SessionStore 内部状态变化（以 max_history=20 为例）：

阶段 1（前 20 条）：messages = [m1, m2, ..., m20]       直接 deque
阶段 2（到第 21 条）：触发摘要 → summarize([m1..m10])  → summary = "用户是张三，28岁，程序员..."
                      messages 变成：summary + [m11..m21]   ← 滑窗口里保留摘要 + 近期对话
阶段 3（再到第 21 条新增 → 累计 31 条）：
                      旧摘要 + [m11..m20] → 再次摘要 → new_summary = "之前：...；近期：..."
                      messages 变成：new_summary + [m21..m31]
```

**关键设计点**：

1. **什么时候触发摘要？**
   - 选项 A：滑窗口刚满时（第 20 条）就触发
   - 选项 B：滑窗口满 + 再累加 10 条时（第 30 条）才触发，把最旧的 10 条压成摘要
   - **选 B**。因为满了就压太频繁（每 10 轮对话就压一次），等到累计 30 条时压最旧的 10 条比较合适。

2. **摘要用谁来生成？**
   - 用 LLM 自己生成。发一个专门的 prompt："请将以下对话摘要成 200 字以内的中文，保留人名、日期、偏好、事件等关键信息"。
   - **重试 1 次**，失败就跳过（不阻塞主流程，不用摘要也能继续聊）。
   - **单独用一个轻量模型或同一个模型**？先用同一个模型，摘要请求加个 `temperature=0.3` 让输出更稳定。

3. **摘要放在 messages 的哪个位置？**
   - **放在 system prompt 之后、滑窗口对话之前**。LLM 会先看到角色设定，再看到"这段对话的历史摘要"，最后看到近期对话 + 本次用户消息。
   - 格式：`{"role": "system", "content": "以下是之前对话的摘要，供你参考：..."}`

4. **持久化格式变化**：
   ```json
   {
     "user_id": 10001,
     "summary": "用户是张三，28岁...",
     "messages": [
       {"role": "user", "content": "今天天气真好"},
       {"role": "assistant", "content": "是啊，适合出去走走"}
     ]
   }
   ```
   （旧版本只有 `messages` 字段，新增 `summary` 字段，读的时候兼容旧格式）

5. **触发频率上限**：每次摘要后，滑窗口里近期对话条数重置（比如重置到保留最近 10 条），这样下一次摘要需要再积累 10 条才会触发，不会形成"每发一条消息都触发摘要"的雪崩。

---

## 三、文件清单与职责

| 文件 | 动作 | 职责 |
|---|---|---|
| `adapter/onebot.py` | **修改** | 新增 `_retry_queue`；断连时判断是标记失败还是入重试队列；新连接建立时重发 |
| `core/handler.py` | **修改** | 新增 `_pending_msg` dict；Lock 释放后检查待处理消息；丢弃中间消息 |
| `core/session.py` | **修改** | 新增 `summary` 字段；`history()` 返回摘要 + 滑窗口；`append()` 里检查是否触发摘要；新增 `summarize()` 方法 |
| `core/llm.py` | **修改** | 新增一个可选的轻量摘要生成方法（复用现有 chat，只是 prompt 不同） |
| `config.py` | **修改** | 新增 `MAX_PENDING_PER_USER`、`SUMMARY_THRESHOLD`、`SUMMARY_KEEP` 等配置 |
| `.env.example` | **修改** | 新增对应环境变量 |
| `main.py` | **修改** | SessionStore 需要注入 LLMClient 的引用才能生成摘要（LLM 是 async 的，摘要不能同步生成） |

### 注意：文件变动关系

```
config.py  ← 新增配置项
    ↓
adapter/onebot.py  ← 断连重试
    ↓
core/session.py  ← 摘要逻辑（需要 LLMClient 做摘要）
    ↓
core/llm.py  ← 摘要 prompt
    ↓
core/handler.py  ← 消息丢弃 + 注入 LLM 给 SessionStore
    ↓
main.py  ← 组装顺序微调（SessionStore 构造时需要可选的 LLMClient）
```

---

## 四、配置项（新增）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_PENDING_PER_USER` | `1` | 同用户最多保留 1 条待处理消息（最新一条）；设为 0 则关闭丢弃 |
| `SUMMARY_THRESHOLD` | `30` | messages 累计到此条数时触发摘要 |
| `SUMMARY_KEEP` | `10` | 摘要后滑窗口保留多少条近期对话 |
| `LLM_SUMMARY_TEMP` | `0.3` | 摘要生成的 temperature（越低越稳定） |

---

## 五、验收清单

### 断线重连恢复
1. NapCat 连接上 → 触发一次 `send_private_msg` → 在发送过程中手动断开 NapCat → 重新连接 → **消息应该在重连后自动重发**（或至少不丢失）
2. 如果断连 + 重连 + 重试也失败 → 最终失败时有清晰日志，不崩

### 消息限流/丢弃
3. 在 LLM 处理中给机器人连发 3 条消息 → 应该只处理第 1 条和第 3 条，中间那条被丢弃（日志可查）
4. 不连发（正常单条发）→ 行为和之前一样，不受影响

### 自动摘要
5. 用 mock 或手动调 SessionStore 累计 30 条消息 → 自动触发摘要 → messages 里同时有摘要和近期对话
6. 删光 session 重开 → 前 10 轮对话正常（没到摘要阈值），和之前行为一致
7. LLM 返回摘要后，下一次 `history()` 调用能返回 `[{"role":"system","content":"以下是之前对话的摘要..."}, ...]`
8. LLM 生成摘要失败 → 打 warning，session 不受影响，继续正常对话

---

## 六、风险点与回退策略

| 风险 | 影响 | 回退 |
|---|---|---|
| 摘要生成让 LLM API 用量翻倍（每次长对话要额外调一次 LLM） | 费用增加 | 摘要可以设得更稀（SUMMARY_THRESHOLD 调大），或用便宜的模型（比如 deepseek-light）做摘要 |
| 断连重试导致重复发送消息（NapCat 其实收到了但没回 echo） | 用户收到重复消息 | echo 是唯一匹配的，如果上一次已经收到 echo，重试队列里应该移除。重试前检查是否已经有成功回执 |
| 消息丢弃太激进导致用户困惑（"我发了 3 条怎么只回了 2 条"） | 用户体验 | 丢弃时日志清晰，必要时可以给用户一个"消息太多了，我只处理了最新一条"的提示（但这会增加复杂度，暂不做） |

---

## 七、与现有代码的关系

### adapter/onebot.py 的改动范围

**当前 `_fail_pending_for`**：
```python
def _fail_pending_for(self, conn):
    for echo, owner in list(self._pending_ws.items()):
        if owner is not conn: continue
        self._pending_ws.pop(echo, None)
        fut = self._pending.pop(echo, None)
        if fut is not None and not fut.done():
            fut.set_exception(ConnectionError("connection closed"))
```

**改后**：
```python
def _fail_pending_for(self, conn):
    # 检查是否还有其他可用连接
    if self._pick_api_conn() is not None:
        return  # 还有其他连接，不失败，等它们处理
    
    # 没有其他连接了，尝试入重试队列
    for echo, owner in list(self._pending_ws.items()):
        if owner is not conn: continue
        action, params, attempt = self._pending_info.pop(echo, ...)
        if attempt < self._max_retries:
            self._retry_queue.append((echo, action, params, attempt + 1))
        else:
            fut = self._pending.pop(echo)
            fut.set_exception(ConnectionError(...))
        self._pending_ws.pop(echo, None)
```

注意需要新增 `_pending_info: dict[str, tuple[str, dict, int]]` 存 action/params/attempt_count（当前只存了 Future 和 ws）。

### SessionStore 需要 LLMClient 引用

SessionStore 自己不会生成摘要——它只是管理 messages。生成摘要需要 LLM，所以 `summarize()` 方法是一个 async 方法，handler 在 `_generate_reply` 里调用它：

```python
async def _generate_reply(self, user_id, text):
    self._sessions.append(user_id, "user", text)
    self._sessions.save(user_id)
    
    # 触发摘要（如果需要）——非阻塞，先继续调 LLM
    await self._sessions.maybe_summarize(self._llm)
    
    messages = [...]
    reply = await self._llm.chat(messages)
    ...
```

`maybe_summarize(llm)` 是 async 方法，里面调 LLM 生成摘要。如果失败直接 warning，不 raise。这样不会阻塞主流程太久。

### handler 消息丢弃的实现

```python
async def _handle_message(self, event):
    user_id, text = extract...
    if user_id not in self._settings.allowed_qq: return
    
    # 记录最新一条待处理消息
    prev_pending = self._pending_msg.get(user_id)
    self._pending_msg[user_id] = text
    if prev_pending is not None and prev_pending != text:
        logger.info("dropped pending message for user_id=%d", user_id)
    
    async with self._lock_for(user_id):
        # 取出当前最新的那条
        current_text = self._pending_msg.pop(user_id, None)
        if current_text is None:
            return  # 被后续消息清掉了
        
        try:
            reply = await self._generate_reply(user_id, current_text)
        except Exception:
            reply = FALLBACK_REPLY
        
        await self._bot.send_private_msg(user_id, reply)
```

这个实现比我之前想的更简单——不用队列，只用一个 dict 存"当前最新待处理"。Lock 释放后如果有新消息进来，自然会被取出来处理（因为它进 `_pending_msg` 时如果 Lock 正被占着，说明有消息正在处理，新消息就暂存着）。
