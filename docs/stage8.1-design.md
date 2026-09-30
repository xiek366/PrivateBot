# Stage 8.1 — 记忆强化补丁

## 问题背景

客户反馈：**Bot 经常"忘记"前一天或早一点聊过的内容**。客户部署在个人服务器上，经常重启。

经过代码审查，发现这不是单一 bug，而是**短期记忆 → 摘要压缩 → 长期记忆**三个环节在频繁重启下的配合失效。

---

## 根因分析

### 1. 三重丢失链路

```
昨天用户聊了 N 条 → Bot 因重启滑窗口丢消息
                        ↓
                summary_threshold=30 太高，重启永远凑不够
                        ↓
                摘要没生成 → 旧对话既没在 session、也没在 summary
                        ↓
                长期记忆从 session.history() 抽 → session 里没的也没得抽
                        ↓
                用户今天问"我昨天说过的XX" → Bot 失忆
```

### 2. 三个失效点

| # | 环节 | 原有值 | 问题 |
|---|------|--------|------|
| A | 短期记忆滑窗口 | `MAX_HISTORY=20` | 20 条装不下两天对话，deque 淘汰不回补 |
| B | 摘要压缩触发阈值 | `SUMMARY_THRESHOLD=30` | 每 30 条才压一次。频繁重启 → 永远凑不够 → 永远不触发 |
| C | 无启动补漏 | — | 之前没考虑过"重启前漏掉的摘要，重启后补回来" |

### 3. 为什么重启是关键触发条件

```
不重启：messages 从 0 → 30 → 触发摘要，平滑
频繁重启：每次启动 messages 从磁盘加载（≤20 条），永远到不了 30，永远不摘要
```

---

## 修复方案

### 改动 A：MAX_HISTORY 从 20 → 40

给滑窗口更多容量，两天对话能装得下更多近期上下文。

```python
# config.py
max_history=int(os.getenv("MAX_HISTORY", "40")),  # 原 20
```

### 改动 B：SUMMARY_THRESHOLD 从 30 → 15

降低摘要触发门槛，15 条就压一次。这样即使频繁重启，每次启动时 session 里可能已经存了 10+ 条，再聊几条就触发。

```python
# config.py
summary_threshold=int(os.getenv("SUMMARY_THRESHOLD", "15")),  # 原 30
```

### 改动 C：启动时主动补摘要（新功能）

在 `main.py` 里 SessionStore 创建完、OneBot 连接前，扫描磁盘上所有 session JSON 文件：如果 messages 条数 ≥ 10（= summary_keep + 5，比触发阈值松），立即补做一次摘要。

```
启动流程新增：
  sessions 创建
  → await sessions.scan_and_summarize(llm)   ← 新增
  → bot.run(handler)
```

新增方法 `SessionStore.scan_and_summarize()`：
```python
async def scan_and_summarize(self, llm) -> int:
    """扫描磁盘上所有 session JSON，超过阈值的立即摘要。"""
    target_count = self._summary_threshold - self._summary_keep + 5  # = 10
    for path in self._dir.glob("*.json"):
        if messages ≥ target_count:
            标记 pending=True
            加载进内存
    await self.maybe_summarize(llm)
```

**关键点：扫的是磁盘文件，不是内存。** 刚启动时内存是空的，磁盘上才存着之前的历史。

---

## 改动文件清单

| 文件 | 改动类型 | 改动内容 |
|------|---------|---------|
| `config.py` | 修改 | `MAX_HISTORY` 默认 20→40，`SUMMARY_THRESHOLD` 默认 30→15 |
| `core/session.py` | 修改 | 新增 `scan_and_summarize()` 异步方法（~35 行） |
| `main.py` | 修改 | sessions 创建后、bot.run 前调用 `scan_and_summarize()`（~10 行） |
| `.env.example` | 修改 | 同步默认值 20→40、30→15 |

---

## 不做的事

| 不做 | 原因 |
|------|------|
| ❌ 引入数据库 | JSON 文件够用，保持轻量 |
| ❌ 把 summary 也拆成增量 | 单次 LLM 调用就够了 |
| ❌ 启动时强制抽取长期记忆 | 记忆提取在回复后异步做，启动补摘要优先 |
| ❌ 加启动时的 session 备份 | 不是必须，JSON 本身有人类可读的 fallback |

---

## 验收清单

- [ ] `python main.py` 正常启动，启动时多等 1-2 秒（摘要处理时间）
- [ ] 日志里能看到 `启动扫描：N 个用户的会话需要补摘要` 或 `摘要扫描：无需要补全的会话`
- [ ] 手动把 session.json 的 messages 改成 20+ 条，重启后观察是否自动生成 summary
- [ ] 重启后问 Bot 昨天聊过的内容，能回忆起来
- [ ] `.env` 里覆盖 `MAX_HISTORY` 或 `SUMMARY_THRESHOLD` 时仍能正常工作（配置覆盖默认值）
- [ ] Python 语法检查通过

---

## 客户部署

客户需要的改动只有：**重新打包 + 复制 data/ + .env**。不需要改已有 session 文件，启动扫描会自动补全。

补丁包文件清单：同 Stage 8 覆盖清单（config.py / core/session.py / main.py / .env.example）。
