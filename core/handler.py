"""私聊消息业务处理。

职责：白名单过滤、调用 LLM 生成回复、好友请求自动同意。
Stage 4 新增：同用户消息丢弃中间只保留最新一条；自动摘要触发。
Stage 8 新增：多模态图片处理（下载/读本地 → base64 → chat_with_images）。
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

# 图片大小安全上限（MB → 字节），超过直接跳过
_IMAGE_MAX_BYTES_PER_MB = 1024 * 1024

# Stage 8.2：回忆意图关键词（命中任一即视为用户在提起过去的事）
RECALL_KEYWORDS = frozenset({
    "记得", "忘了", "忘记", "上次", "之前", "前几天", "说过", "提过",
    "讲过", "答应", "约定", "说好", "那只", "那个时候", "昨天说",
})


def _is_recall_intent(text: str) -> bool:
    """用户消息是否含回忆意图。命中任一关键词即 True。"""
    if not text:
        return False
    return any(kw in text for kw in RECALL_KEYWORDS)


def _guess_mime(url: str = "", local_file: str = "") -> str:
    """从 URL 或本地文件名推断 MIME type。"""
    # mimetypes 覆盖常见图片格式（image/jpeg, image/png, image/gif, image/webp）
    candidate = url or local_file or ""
    _, ext = os.path.splitext(candidate)
    mime, _ = mimetypes.guess_type(candidate)
    if mime and mime.startswith("image/"):
        return mime
    # 兜底：按扩展名
    ext_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }
    return ext_map.get(ext.lower(), "image/jpeg")


async def _download_image(url: str, max_bytes: int) -> bytes | None:
    """异步下载图片，超过 max_bytes 或失败返回 None。"""
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
    """读本地图片文件，超过 max_bytes 或文件不存在返回 None。"""
    try:
        p = Path(local_file)
        if not p.exists():
            # NapCat 有时候会给相对路径，尝试补几个常见根
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
    """把 image_blocks 转成 base64 data URL 列表。

    返回 (urls, success_count)。success_count 是跳过前成功处理的数量。
    """
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
        result = OneBotServer.extract_private_content(event)
        if result is None:
            return

        user_id, text, image_blocks = result
        if user_id not in self._settings.allowed_qq:
            logger.info("ignored non-whitelisted user_id=%d", user_id)
            return

        # 丢弃策略：如果 Lock 正在被持有（上一条消息还在处理），
        # 把当前消息暂存为"最新一条待处理"，让上一条处理完后处理这一条。
        # 中间的其他消息会不断覆盖这个值，最终只保留最新一条。
        lock = self._lock_for(user_id)
        if lock.locked() and self._settings.max_pending_per_user > 0:
            # 已经有消息在处理了 → 暂存/覆盖最新
            # 注意：暂存无法完整保存图片 blocks（没有序列化必要），简单处理：
            # 如果是带图片的新消息来了，直接当"需要重处理" → 存入标记让调用方知道有图
            self._pending_msg[user_id] = text or "[图片]"
            logger.debug("user_id=%d 正在处理中，消息已暂存", user_id)
            return

        # 拿到锁，先处理自身
        async with lock:
            await self._process_one(user_id, text, image_blocks)
            # 如果暂存区还有消息，继续处理（直到清空）
            while True:
                queued = self._pending_msg.pop(user_id, None)
                if queued is None:
                    break
                # 暂存消息丢失了图片上下文，降级为纯文本处理
                await self._process_one(user_id, queued, [])

    async def _process_one(self, user_id: int, text: str, image_blocks: list[dict] | None = None) -> None:
        """处理一条具体的消息。"""
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

        # Stage 6：加载状态 → 衰减 → 匹配事件 → 应用变化
        state = self._state_mgr.load(user_id)
        if self._state_mgr.enabled:
            self._state_mgr.decay(state)
            events = self._state_mgr.match_events(text)
            self._state_mgr.apply_events(state, events)

        # Stage 8：session 里存 [图片] 占位符（不存 base64，防膨胀）
        session_text = text
        if image_blocks:
            # 把图片放在文字前面或后面？一般 QQ 消息是文字+图片混合，
            # 用一个简洁占位符表示"本条消息带图片"
            session_text = (text + " [图片]" if text else "[图片]").strip()

        self._sessions.append(user_id, "user", session_text)
        self._sessions.save(user_id)

        # 组装 messages（不含最后一条 user 消息，图片分支自己构造）
        messages = [{"role": "system", "content": self._persona.get_system_prompt()}]

        # Stage 6：动态注入状态段（人设之后，参考片段之前）
        if self._state_mgr.enabled:
            state_prompt = self._state_mgr.state_to_prompt(state)
            messages.append({"role": "system", "content": state_prompt})

        # Stage 5：参考语料轻量检索（用用户文字 + 占位符一起做检索，命中更准）
        ref_chunks = self._persona.find_top_chunks(session_text)
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

        messages.extend(self._persona.get_examples())

        # Stage 7 + 8.2：长期记忆检索 + 注入
        # 位置在 history 紧前方（LLM 对 prompt 结尾注意力最强）；
        # 回忆意图时增强检索并使用回忆专用引导文案
        if self._memory and self._memory.enabled:
            recall = _is_recall_intent(session_text)
            top_k = self._settings.recall_top_k if recall else 5
            entries = self._memory.retrieve(user_id, session_text, top_k=top_k)
            mem_prompt = memories_to_prompt(entries, recall_mode=recall)
            if mem_prompt:
                messages.append({"role": "system", "content": mem_prompt})
        # 注意：history 里已经包含了我们刚 append 的 "[图片]" 占位符那条 user 消息，
        # chat_with_images 会自己构造最后一条（带图片的 block 数组），
        # 所以这里传给 chat_with_images 时要先去掉最后一条
        # 或者：我们让 messages 不含最后 user（把 messages 和 history 分开传）
        # 简单方案：直接把 history 也拼到 messages，chat 就接这个；chat_with_images 会替换最后一条

        full_messages = messages + self._sessions.history(user_id)

        # Stage 8：图片处理分支
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
                # 从 full_messages 里剥离最后一条 user 消息，chat_with_images 自己构造带图片的最后一条
                # 历史里最后一条就是我们刚 append 的 session_text
                reply = await self._llm.chat_with_images(
                    text_messages=full_messages[:-1],
                    image_data_urls=data_urls,
                    user_text=text,
                    detail=self._settings.vision_detail,
                )
            else:
                # 图片全挂了，降级纯文本 + 给 LLM 一个提示
                logger.warning("图片全部下载失败，降级纯文本 user_id=%d", user_id)
                reply = await self._llm.chat(full_messages)
        else:
            reply = await self._llm.chat(full_messages)

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
        asyncio.create_task(self._async_extract_memories(user_id, text or session_text, reply))

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
