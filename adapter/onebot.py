"""OneBot v11 反向 WebSocket 服务端。

NapCat 是客户端，我们是服务端。
职责：连接管理、协议解析、API 调用。不含任何业务判断。

基于 websockets >= 14 的新版 asyncio API。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Awaitable, Callable, Optional

import websockets
from websockets.asyncio.server import ServerConnection

from config import Settings

logger = logging.getLogger(__name__)

EventCallback = Callable[[dict], Awaitable[None]]

CLOSE_AUTH_FAILED = 4001
CLOSE_PATH_MISMATCH = 4004


class OneBotServer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._on_event: Optional[EventCallback] = None
        self._roles: dict[str, ServerConnection] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._pending_ws: dict[str, ServerConnection] = {}
        self._pending_info: dict[str, tuple[str, dict, int]] = {}
        self._retry_queue: list[tuple[str, str, dict, int]] = []
        self._tasks: set[asyncio.Task] = set()
        self._max_retries = 1

    async def run(self, on_event: EventCallback) -> None:
        self._on_event = on_event
        host = self._settings.onebot_ws_host
        port = self._settings.onebot_ws_port
        logger.info("listening on ws://%s:%d%s", host, port, self._settings.onebot_ws_path)
        async with websockets.serve(self._handler, host, port):
            await asyncio.Future()

    async def _handler(self, conn: ServerConnection) -> None:
        request = conn.request
        from urllib.parse import urlparse, parse_qs
        raw_path = request.path
        parsed = urlparse(raw_path)
        path = parsed.path
        if self._settings.onebot_ws_path and path != self._settings.onebot_ws_path:
            await conn.close(code=CLOSE_PATH_MISMATCH, reason="invalid path")
            return
        headers = request.headers
        if self._settings.onebot_access_token:
            token_ok = False
            if headers.get("Authorization") == f"Bearer {self._settings.onebot_access_token}":
                token_ok = True
            qs = parse_qs(parsed.query)
            if qs.get("access_token", [""])[0] == self._settings.onebot_access_token:
                token_ok = True
            if not token_ok:
                logger.warning("auth failed, closing connection")
                await conn.close(code=CLOSE_AUTH_FAILED, reason="auth failed")
                return
        role = headers.get("X-Client-Role") or "Universal"
        self_id = headers.get("X-Self-ID") or "unknown"
        logger.info("client connected role=%s self_id=%s", role, self_id)
        self._roles[role] = conn
        if self._retry_queue:
            self._spawn(self._resend_retry_queue())
        try:
            async for raw in conn:
                if not isinstance(raw, str):
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if payload.get("echo") is not None and payload.get("status") is not None:
                    self._resolve_echo(payload)
                else:
                    self._spawn(self._dispatch_event(payload))
        except websockets.ConnectionClosed:
            pass
        finally:
            logger.info("client disconnected role=%s", role)
            if self._roles.get(role) is conn:
                self._roles.pop(role, None)
            self._fail_pending_for(conn)

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _fail_pending_for(self, conn: ServerConnection) -> None:
        if self._pick_api_conn() is not None:
            return
        for echo, owner in list(self._pending_ws.items()):
            if owner is not conn:
                continue
            self._pending_ws.pop(echo, None)
            info = self._pending_info.pop(echo, None)
            fut = self._pending.pop(echo, None)
            if info is None or fut is None or fut.done():
                continue
            action, params, attempt = info
            if attempt < self._max_retries:
                self._retry_queue.append((echo, action, params, attempt + 1))
            else:
                fut.set_exception(ConnectionError("connection closed, retries exhausted"))

    async def _dispatch_event(self, event: dict) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(event)
        except Exception:
            logger.exception("handler crashed on event: %s", event.get("post_type"))

    async def _resend_retry_queue(self) -> None:
        queued = self._retry_queue[:]
        self._retry_queue.clear()
        for echo, action, params, attempt in queued:
            await asyncio.sleep(0.3)
            conn = self._pick_api_conn()
            if conn is None:
                self._retry_queue.insert(0, (echo, action, params, attempt))
                return
            fut = asyncio.get_running_loop().create_future()
            self._pending[echo] = fut
            self._pending_ws[echo] = conn
            self._pending_info[echo] = (action, params, attempt)
            try:
                await conn.send(json.dumps({"action": action, "params": params, "echo": echo}))
            except Exception:
                self._pending.pop(echo, None)
                self._pending_ws.pop(echo, None)
                self._pending_info.pop(echo, None)
                if not fut.done():
                    fut.set_exception(ConnectionError("resend failed"))

    def _pick_api_conn(self) -> Optional[ServerConnection]:
        for role in ("Universal", "API"):
            conn = self._roles.get(role)
            if conn is not None and conn.state is websockets.State.OPEN:
                return conn
        return None

    async def call_api(self, action: str, params: Optional[dict] = None, timeout: float = 5.0) -> Any:
        conn = self._pick_api_conn()
        if conn is None:
            raise RuntimeError("no available API connection")
        echo = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        self._pending_ws[echo] = conn
        self._pending_info[echo] = (action, params or {}, 0)
        try:
            await conn.send(json.dumps({"action": action, "params": params or {}, "echo": echo}))
            result = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"API call timed out: {action}") from None
        finally:
            self._pending.pop(echo, None)
            self._pending_ws.pop(echo, None)
            self._pending_info.pop(echo, None)
        if result.get("status") != "ok" or result.get("retcode") != 0:
            raise RuntimeError(f"API error: {action} -> {result}")
        return result.get("data")

    def _resolve_echo(self, payload: dict) -> None:
        fut = self._pending.pop(payload.get("echo"), None)
        if fut is not None and not fut.done():
            fut.set_result(payload)

    async def send_private_msg(self, user_id: int, text: str) -> None:
        await self.call_api(
            "send_private_msg",
            {"user_id": user_id, "message": [{"type": "text", "data": {"text": text}}]},
        )

    async def set_friend_add_request(self, flag: str, approve: bool = True) -> None:
        await self.call_api("set_friend_add_request", {"flag": flag, "approve": approve})

    @staticmethod
    def extract_private_content(event: dict) -> Optional[tuple[int, str, list[dict]]]:
        """提取 (user_id, 拼接文本, image_blocks)。"""
        if event.get("post_type") != "message" or event.get("message_type") != "private":
            return None
        user_id = event.get("user_id")
        if not isinstance(user_id, int):
            return None
        image_blocks: list[dict] = []
        text_parts: list[str] = []
        has_any_image = False
        for seg in event.get("message", []):
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            sdata = seg.get("data", {}) or {}
            if stype == "text":
                text_parts.append(sdata.get("text", ""))
            elif stype == "image":
                has_any_image = True
                url = sdata.get("url", "") or ""
                local_file = sdata.get("file", "") or ""
                image_blocks.append({"url": url, "file": local_file})
        text = "".join(text_parts).strip()
        if not text and not has_any_image:
            return None
        return (user_id, text, image_blocks)

    @staticmethod
    def extract_private_text(event: dict) -> Optional[tuple[int, str]]:
        """保持兼容：内部转发给 extract_private_content。"""
        result = OneBotServer.extract_private_content(event)
        if result is None:
            return None
        user_id, text, _images = result
        if not text:
            return None
        return (user_id, text)

    @staticmethod
    def extract_friend_request(event: dict) -> Optional[tuple[int, str]]:
        if event.get("post_type") != "request" or event.get("request_type") != "friend":
            return None
        user_id, flag = event.get("user_id"), event.get("flag")
        if not isinstance(user_id, int) or not isinstance(flag, str):
            return None
        return user_id, flag
