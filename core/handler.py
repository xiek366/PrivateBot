"""私聊消息业务处理。

职责：白名单过滤、调用 LLM 生成回复、好友请求自动同意。
Stage 4 新增：同用户消息丢弃中间只保留最新一条；自动摘要触发。
Stage 8 新增：多模态图片处理（下载/读本地 -> base64 -> chat_with_images）。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any

import httpx

from adapter.onebot import OneBotServer
from config import Settings
from core.llm import LLMClient
from core.memory import MemoryStore, memories_to_prompt
from core.session import SessionStore
from core.state import CharacterStateManager
from persona.loader import PersonaLoader

logger = logging.getLogger(__name__)

FALLBACK_REPLY = "抱歉，我这边出了点问题，稍后再试。"

_IMAGE_MAX_BYTES_PER_MB = 1024 * 1024


def _guess_mime(url: str = "", local_file: str = "") -> str:
    candidate = url or local_file or ""
    _, ext = os.path.splitext(candidate)
    mime, _ = mimetypes.guess_type(candidate)
    if mime and mime.startswith("image/"):
        return mime
    ext_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }
    return ext_map.get(ext.lower(), "image/jpeg")


async def _download_image(url: str, max_bytes: int) -> bytes | None:
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
            if len(data) > max_bytes:
                logger.info("图片过大 (%d bytes) 跳过: %s", len(data), url[:80])
                return None
            return data
    except Exception as exc:
        logger.warning("下载图片失败 %s: %s", url[:80], exc)
        return None


def _read_local_image(local_file: str, max_bytes: int) -> bytes | None:
    try:
        p = Path(local_file)
        if not p.exists():
            for root in [Path(os.getcwd()) / "cache", Path(os.environ.get("TEMP", ""))]:
                alt = root / p.name
                if alt.exists():
                    p = alt
                    break
            else:
                return None
        if p.stat().st_size > max_bytes:
            logger.info("本地图片过大 (%d bytes) 跳过: %s", p.stat().st_size, p)
            return None
        return p.read_bytes()
    except Exception as exc:
        logger.warning("读本地图片失败 %s: %s", local_file, exc)
        return None


async def _image_blocks_to_data_urls(
    image_blocks: list[dict],
    max_images: int,
    max_bytes: int,
) -> tuple[list[str], int]:
    urls: list[str] = []
    tried = 0
    for block in image_blocks[:max_images]:
        tried += 1
        url = block.get("url", "") or ""
        local_file = block.get("file", "") or ""
        data: bytes | None = None
        if url:
            data = await _download_image(url, max_bytes)
        if data is None and local_file:
            data = _read_local_image(local_file, max_bytes)
        if data is None:
            continue
        mime = _guess_mime(url=url, local_file=local_file)
        b64 = base64.b64encode(data).decode("ascii")
        urls.append(f"data:{mime};base64,{b64}")
    return urls, tried


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
        self._locks: dict[int, asyncio.Lock] = {}
        self._heartbeat_logged = False
        self._pending_msg: dict[int, str] = {}

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    async def on_event(self, event: dict) -> None:
        post_type = event.get("post_type")
        if post_type == "message":
            await self._handle_message(event)
        elif post_type == "request":
            await self._handle_request(event)
        elif post_type == "meta_event":
            self._log_meta_event(event)

    def _log_meta_event(self, event: dict) -> None:
        if event.get("meta_event_type") == "heartbeat" and not self._heartbeat_logged:
            self._heartbeat_logged = True
            logger.info("heartbeat ok，连接存活")
        else:
            logger.debug("meta_event: %s", event.get("meta_event_type"))

    async def _handle_message(self, event: dict) -> None:
        result = OneBotServer.extract_private_content(event)
        if result is None:
            return
        user_id, text, image_blocks = result
        if user_id not in self._settings.allowed_qq:
            logger.info("ignored non-whitelisted user_id=%d", user_id)
            return
        lock = self._lock_for(user_id)
        if lock.locked() and self._settings.max_pending_per_user > 0:
            self._pending_msg[user_id] = text or "[图片]"
            logger.debug("user_id=%d 正在处理中，消息已暂存", user_id)
            return
        async with lock:
            await self._process_one(user_id, text, image_blocks)
            while True:
                queued = self._pending_msg.pop(user_id, None)
                if queued is None:
                    break
                await self._process_one(user_id, queued, [])

    async def _process_one(self, user_id: int, text: str, image_blocks: list[dict] | None = None) -> None:
        image_blocks = image_blocks or []
        try:
            reply = await self._generate_reply(user_id, text, image_blocks)
        except Exception:
            logger.exception("生成回复失败 user_id=%d", user_id)
            reply = FALLBACK_REPLY
        try:
            await self._bot.send_private_msg(user_id, reply)
        except Exception:
            logger.exception("发送失败 user_id=%d", user_id)

    async def _generate_reply(self, user_id: int, text: str, image_blocks: list[dict] | None = None) -> str:
        image_blocks = image_blocks or []
        state = self._state_mgr.load(user_id)
        if self._state_mgr.enabled:
            self._state_mgr.decay(state)
            events = self._state_mgr.match_events(text)
            self._state_mgr.apply_events(state, events)
        session_text = text
        if image_blocks:
            session_text = (text + " [图片]" if text else "[图片]").strip()
        self._sessions.append(user_id, "user", session_text)
        self._sessions.save(user_id)
        messages = [{"role": "system", "content": self._persona.get_system_prompt()}]
        if self._state_mgr.enabled:
            state_prompt = self._state_mgr.state_to_prompt(state)
            messages.append({"role": "system", "content": state_prompt})
        ref_chunks = self._persona.find_top_chunks(session_text)
        if ref_chunks:
            ref_text = "\n---\n".join(ref_chunks)
            messages.append({"role": "system", "content": f"参考台词:\n---\n{ref_text}\n---"})
        summary_text = self._sessions.summary(user_id)
        if summary_text:
            messages.append({"role": "system", "content": f"之前对话摘要:\n{summary_text}"})
        if self._memory and self._memory.enabled:
            entries = self._memory.retrieve(user_id, session_text, top_k=5)
            mem_prompt = memories_to_prompt(entries)
            if mem_prompt:
                messages.append({"role": "system", "content": mem_prompt})
        messages.extend(self._persona.get_examples())
        full_messages = messages + self._sessions.history(user_id)
        use_images = bool(image_blocks) and self._settings.vision_enabled
        if use_images:
            max_bytes = self._settings.vision_max_mb * _IMAGE_MAX_BYTES_PER_MB
            data_urls, tried = await _image_blocks_to_data_urls(
                image_blocks,
                max_images=self._settings.vision_max_images,
                max_bytes=max_bytes,
            )
            logger.info("识图：user_id=%d 收到 %d 张，成功转 base64 %d 张", user_id, tried, len(data_urls))
            if data_urls:
                reply = await self._llm.chat_with_images(
                    text_messages=full_messages[:-1],
                    image_data_urls=data_urls,
                    user_text=text,
                    detail=self._settings.vision_detail,
                )
            else:
                logger.warning("图片全部下载失败，降级纯文本 user_id=%d", user_id)
                reply = await self._llm.chat(full_messages)
        else:
            reply = await self._llm.chat(full_messages)
        self._sessions.append(user_id, "assistant", reply)
        self._sessions.save(user_id)
        logger.info("replied to %d: %s", user_id, reply[:80])
        if self._state_mgr.enabled:
            self._state_mgr.mark_interaction(state)
            self._state_mgr.save(user_id, state)
        asyncio.create_task(self._sessions.maybe_summarize(self._llm))
        asyncio.create_task(self._async_extract_memories(user_id, text or session_text, reply))
        return reply

    async def _async_extract_memories(self, user_id: int, user_msg: str, bot_reply: str) -> None:
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

    async def _handle_request(self, event: dict) -> None:
        result = OneBotServer.extract_friend_request(event)
        if result is None:
            return
        user_id, flag = result
        if user_id not in self._settings.allowed_qq:
            logger.info("ignored friend request from non-whitelisted user_id=%d", user_id)
            return
        if not self._settings.auto_accept_friend:
            return
        try:
            await self._bot.set_friend_add_request(flag, approve=True)
            logger.info("accepted friend request from %d", user_id)
        except Exception:
            logger.exception("接受好友请求失败 user_id=%d", user_id)
