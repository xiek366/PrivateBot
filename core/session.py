"""按用户维护对话上下文，并持久化到 JSON 文件。

Stage 4 新增：自动摘要压缩。当 messages 累计超过阈值时，
把最旧的一段对话压成摘要，滑窗口里同时维护「摘要 + 近期对话」。
"""
from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Deque, Optional

if TYPE_CHECKING:
    from core.llm import LLMClient

logger = logging.getLogger(__name__)


class SessionStore:
    def __init__(
        self,
        data_dir: Path,
        max_history: int,
        summary_threshold: int = 30,
        summary_keep: int = 10,
    ) -> None:
        self._dir = data_dir
        self._max = max_history
        self._summary_threshold = summary_threshold
        self._summary_keep = summary_keep
        # user_id -> (messages, summary, pending_summarize)
        #   messages: Deque[dict] — 近期对话
        #   summary: str — 之前的摘要（空字符串表示还没生成过）
        #   pending_summarize: bool — 本轮对话刚累计到阈值，下一次 maybe_summarize 要处理
        self._sessions: dict[int, tuple[Deque[dict], str, bool]] = {}
        self._dir.mkdir(parents=True, exist_ok=True)

    # ---------- 内部数据访问 ----------

    def _path(self, user_id: int) -> Path:
        return self._dir / f"{user_id}.json"

    def _ensure(self, user_id: int) -> tuple[Deque[dict], str, bool]:
        """懒加载：首次访问时从 JSON 读入内存。"""
        entry = self._sessions.get(user_id)
        if entry is not None:
            return entry

        messages: Deque[dict] = deque(maxlen=self._max)
        summary = ""
        path = self._path(user_id)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                # 兼容旧格式（只有 messages 字段，没有 summary）
                summary = str(raw.get("summary", "") or "")
                for item in raw.get("messages", [])[-self._max :]:
                    if isinstance(item, dict) and "role" in item and "content" in item:
                        messages.append({"role": item["role"], "content": item["content"]})
            except (json.JSONDecodeError, OSError, AttributeError) as exc:
                logger.warning("读取会话失败 %s: %s", path, exc)

        entry = (messages, summary, False)
        self._sessions[user_id] = entry
        return entry

    # ---------- 对外接口 ----------

    def history(self, user_id: int) -> list[dict]:
        """返回历史 messages（不含摘要，摘要由 handler 单独处理）。"""
        messages, _summary, _pending = self._ensure(user_id)
        return list(messages)

    def summary(self, user_id: int) -> str:
        _, summary, _ = self._ensure(user_id)
        return summary

    def append(self, user_id: int, role: str, content: str) -> None:
        entry = self._ensure(user_id)
        messages, summary, _pending = entry
        messages.append({"role": role, "content": content})

        # 触发条件：messages 里实际存的条数 >= summary_threshold
        new_pending = (len(messages) >= self._summary_threshold)
        if new_pending != _pending:
            self._sessions[user_id] = (messages, summary, new_pending)
        else:
            # 只更新 messages 引用（deque 是可变的，不需要重建 tuple）
            pass

    def save(self, user_id: int) -> None:
        messages, summary, _pending = self._ensure(user_id)
        payload = {
            "user_id": user_id,
            "summary": summary,
            "messages": list(messages),
        }
        try:
            self._path(user_id).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("保存会话失败 user_id=%d: %s", user_id, exc)

    async def maybe_summarize(self, llm: Optional["LLMClient"]) -> None:
        """对所有 pending 的用户触发自动摘要。

        这个方法应该在 handler 调完 LLM 后立即调用，
        这样摘要可以在下一次用户发消息前完成。如果失败直接跳过，不 raise。
        """
        if llm is None:
            return

        targets = [uid for uid, (_m, _s, pending) in self._sessions.items() if pending]
        for uid in targets:
            await self._do_summarize(uid, llm)

    async def _do_summarize(self, user_id: int, llm: "LLMClient") -> None:
        """对单个用户执行摘要：取最旧的 messages 段 → LLM 压缩 → 合并进 summary → 重置滑窗口。"""
        messages, old_summary, _pending = self._ensure(user_id)

        # 取最旧的一段：把 messages 从 summary_keep 后面的都压掉
        # 例如：summary_threshold=30, summary_keep=10, messages 有 30 条
        #       → 压最旧的 20 条（30-10），保留最近 10 条
        old_count = len(messages) - self._summary_keep
        if old_count <= 0:
            # 还没到该压的时候（可能因为 max_history 比较小）
            self._sessions[user_id] = (messages, old_summary, False)
            return

        old_messages = [messages.popleft() for _ in range(old_count)]
        new_messages = list(messages)  # 剩下的近期对话

        # 调 LLM 生成摘要
        logger.info("触发自动摘要 user_id=%d, 压缩 %d 条消息", user_id, old_count)
        extra = await llm.summarize(old_messages, temperature=0.3)

        if not extra:
            # 摘要生成失败，把消息放回去，下次再试
            logger.warning("自动摘要失败 user_id=%d，保留原消息", user_id)
            for m in old_messages:
                messages.appendleft(m)
            self._sessions[user_id] = (messages, old_summary, True)  # 仍然 pending
            return

        # 合并摘要
        if old_summary:
            merged = f"{old_summary}\n\n近期对话摘要：{extra}"
        else:
            merged = extra

        # 重置 messages 为滑窗口（deque 有 maxlen，多余的自动淘汰）
        new_deque: Deque[dict] = deque(maxlen=self._max)
        for m in new_messages:
            new_deque.append(m)

        self._sessions[user_id] = (new_deque, merged, False)
        self.save(user_id)
        logger.info("自动摘要完成 user_id=%d, 新摘要长度 %d", user_id, len(merged))
