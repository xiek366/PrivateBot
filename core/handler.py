"""私聊消息业务处理。

职责：白名单过滤、调用 LLM 生成回复、好友请求自动同意。
Stage 4 新增：同用户消息丢弃中间只保留最新一条；自动摘要触发。
"""
from __future__ import annotations

import asyncio
import logging

from adapter.onebot import OneBotServer
from config import Settings
from core.llm import LLMClient
from core.memory import MemoryStore, memories_to_prompt
from core.session import SessionStore
from core.state import CharacterStateManager
from persona.loader import PersonaLoader

logger = logging.getLogger(__name__)

FALLBACK_REPLY = "抱歉，我这边出了点问题，稍后再试。"


class PrivateChatHandler:
    def __init__(
        self,
        settings: Settings,
        bot: OneBotServer,
        llm: LLMClient,
        sessions: SessionStore,
        persona: PersonaLoader,
        state_mgr: CharacterStateManager,
        memory: MemoryStore | None = None,
    ) -> None:
        self._settings = settings
        self._bot = bot
        self._llm = llm
        self._sessions = sessions
        self._persona = persona
        self._state_mgr = state_mgr
        self._memory = memory
        # 每个用户一把锁，保证同一用户的消息串行处理
        self._locks: dict[int, asyncio.Lock] = {}
        # 心跳只提示一次，避免刷屏
        self._heartbeat_logged = False
        # 每个用户一条"最新待处理消息"，用于消息丢弃
        self._pending_msg: dict[int, str] = {}

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    # ---------- 事件分派 ----------

    async def on_event(self, event: dict) -> None:
        post_type = event.get("post_type")
        if post_type == "message":
            await self._handle_message(event)
        elif post_type == "request":
            await self._handle_request(event)
        elif post_type == "meta_event":
            self._log_meta_event(event)

    def _log_meta_event(self, event: dict) -> None:
        """首次心跳用 INFO 确认连接存活，之后降到 DEBUG 避免刷屏。"""
        if event.get("meta_event_type") == "heartbeat" and not self._heartbeat_logged:
            self._heartbeat_logged = True
            logger.info("heartbeat ok，连接存活")
        else:
            logger.debug("meta_event: %s", event.get("meta_event_type"))

    # ---------- 私聊消息（带丢弃策略） ----------

    async def _handle_message(self, event: dict) -> None:
        result = OneBotServer.extract_private_text(event)
        if result is None:
            return

        user_id, text = result
        if user_id not in self._settings.allowed_qq:
            logger.info("ignored non-whitelisted user_id=%d", user_id)
            return

        # 丢弃策略：如果 Lock 正在被持有（上一条消息还在处理），
        # 把当前消息暂存为"最新一条待处理"，让上一条处理完后处理这一条。
        # 中间的其他消息会不断覆盖这个值，最终只保留最新一条。
        lock = self._lock_for(user_id)
        if lock.locked() and self._settings.max_pending_per_user > 0:
            # 已经有消息在处理了 → 暂存/覆盖最新
            self._pending_msg[user_id] = text
            logger.debug("user_id=%d 正在处理中，消息已暂存", user_id)
            return

        # 拿到锁，先处理自身
        async with lock:
            await self._process_one(user_id, text)
            # 如果暂存区还有消息，继续处理（直到清空）
            while True:
                queued = self._pending_msg.pop(user_id, None)
                if queued is None:
                    break
                await self._process_one(user_id, queued)

    async def _process_one(self, user_id: int, text: str) -> None:
        """处理一条具体的消息。"""
        try:
            reply = await self._generate_reply(user_id, text)
        except Exception:
            logger.exception("生成回复失败 user_id=%d", user_id)
            reply = FALLBACK_REPLY

        try:
            await self._bot.send_private_msg(user_id, reply)
        except Exception:
            logger.exception("发送失败 user_id=%d", user_id)

    async def _generate_reply(self, user_id: int, text: str) -> str:
        # Stage 6：加载状态 → 衰减 → 匹配事件 → 应用变化
        state = self._state_mgr.load(user_id)
        if self._state_mgr.enabled:
            self._state_mgr.decay(state)
            events = self._state_mgr.match_events(text)
            self._state_mgr.apply_events(state, events)

        self._sessions.append(user_id, "user", text)
        self._sessions.save(user_id)

        # 组装 messages
        messages = [{"role": "system", "content": self._persona.get_system_prompt()}]

        # Stage 6：动态注入状态段（人设之后，参考片段之前）
        if self._state_mgr.enabled:
            state_prompt = self._state_mgr.state_to_prompt(state)
            messages.append({"role": "system", "content": state_prompt})

        # Stage 5：参考语料轻量检索
        ref_chunks = self._persona.find_top_chunks(text)
        if ref_chunks:
            ref_text = "\n---\n".join(ref_chunks)
            messages.append({
                "role": "system",
                "content": f"以下是你可能需要参考的角色原始台词/设定，请你在对话中参考这些内容，让回复更贴合角色：\n---\n{ref_text}\n---",
            })

        # Stage 4：对话摘要
        summary_text = self._sessions.summary(user_id)
        if summary_text:
            messages.append({
                "role": "system",
                "content": f"以下是之前对话的摘要，供你参考：\n{summary_text}",
            })

        # Stage 7：长期记忆检索 + 注入
        if self._memory and self._memory.enabled:
            entries = self._memory.retrieve(user_id, text, top_k=5)
            mem_prompt = memories_to_prompt(entries)
            if mem_prompt:
                messages.append({"role": "system", "content": mem_prompt})

        messages.extend(self._persona.get_examples())
        messages.extend(self._sessions.history(user_id))

        reply = await self._llm.chat(messages)

        self._sessions.append(user_id, "assistant", reply)
        self._sessions.save(user_id)
        logger.info("replied to %d: %s", user_id, reply[:80])

        # Stage 6：保存状态（mark_interaction 同时记录 last_interaction，供 Stage 7 主动消息判断）
        if self._state_mgr.enabled:
            self._state_mgr.mark_interaction(state)
            self._state_mgr.save(user_id, state)

        # 异步触发自动摘要（不阻塞回复）
        asyncio.create_task(self._sessions.maybe_summarize(self._llm))

        # Stage 7：异步触发长期记忆提取
        asyncio.create_task(self._async_extract_memories(user_id, text, reply))

        return reply

    async def _async_extract_memories(self, user_id: int, user_msg: str, bot_reply: str) -> None:
        """从本轮对话中提取记忆候选并入库。异步，失败静默。"""
        if not (self._memory and self._memory.enabled):
            return
        try:
            history = self._sessions.history(user_id)
            known = [e.content for e in self._memory.load(user_id)]
            candidates = await self._llm.extract_memories(user_msg, bot_reply, history, known)
            if candidates:
                added = self._memory.add_batch(user_id, candidates)
                logger.debug("记忆提取 user_id=%d: 已有 %d 条, %d 条候选, %d 条入库",
                             user_id, len(known), len(candidates), added)
        except Exception as exc:
            logger.warning("异步记忆提取失败 user_id=%d: %s", user_id, exc)

    # ---------- 好友请求 ----------

    async def _handle_request(self, event: dict) -> None:
        result = OneBotServer.extract_friend_request(event)
        if result is None:
            return

        user_id, flag = result
        if user_id not in self._settings.allowed_qq:
            logger.info("ignored friend request from non-whitelisted user_id=%d", user_id)
            return
        if not self._settings.auto_accept_friend:
            logger.info("friend request from %d ignored (auto_accept disabled)", user_id)
            return

        try:
            await self._bot.set_friend_add_request(flag, approve=True)
            logger.info("accepted friend request from %d", user_id)
        except Exception:
            logger.exception("接受好友请求失败 user_id=%d", user_id)
