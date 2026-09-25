"""OneBot v11 反向 WebSocket 服务端。

NapCat 是客户端，我们是服务端。
职责：连接管理、协议解析、API 调用。不含任何业务判断。

基于 websockets >= 14 的新版 asyncio API（handler 收到 ServerConnection）。
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

# 关闭码（应用自定义区间 4000-4999）
CLOSE_AUTH_FAILED = 4001
CLOSE_PATH_MISMATCH = 4004


class OneBotServer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._on_event: Optional[EventCallback] = None
        # 角色 -> 连接（Universal / Event / API）
        self._roles: dict[str, ServerConnection] = {}
        # echo -> Future
        self._pending: dict[str, asyncio.Future] = {}
        # echo -> 归属连接，断连时只失败该连接自己的请求
        self._pending_ws: dict[str, ServerConnection] = {}
        # echo -> (action, params, attempt_count)，断连重试需要知道原始请求内容
        self._pending_info: dict[str, tuple[str, dict, int]] = {}
        # 待重试队列：(echo, action, params, attempt_count)
        self._retry_queue: list[tuple[str, str, dict, int]] = []
        # 事件处理任务，保持强引用防止被 GC 回收
        self._tasks: set[asyncio.Task] = set()
        # 每个请求最多重连重试次数（不含首次发送）
        self._max_retries = 1

    # ---------- 服务启动 ----------

    async def run(self, on_event: EventCallback) -> None:
        self._on_event = on_event
        host = self._settings.onebot_ws_host
        port = self._settings.onebot_ws_port
        logger.info(
            "listening on ws://%s:%d%s", host, port, self._settings.onebot_ws_path
        )

        async with websockets.serve(self._handler, host, port):
            await asyncio.Future()  # 永久运行，由外部取消

    # ---------- 连接处理 ----------

    async def _handler(self, conn: ServerConnection) -> None:
        request = conn.request
        from urllib.parse import urlparse, parse_qs

        # 1. 路径校验（拆出纯 path，忽略 query string）
        raw_path = request.path
        parsed = urlparse(raw_path)
        path = parsed.path
        if self._settings.onebot_ws_path and path != self._settings.onebot_ws_path:
            logger.warning("unexpected path %r (raw=%r), closing connection", path, raw_path)
            await conn.close(code=CLOSE_PATH_MISMATCH, reason="invalid path")
            return

        headers = request.headers

        # 2. 鉴权（同时支持 HTTP Header 和 URL query 参数两种方式）
        if self._settings.onebot_access_token:
            token_ok = False
            # 方式 1：HTTP Header Authorization: Bearer xxx
            if headers.get("Authorization") == f"Bearer {self._settings.onebot_access_token}":
                token_ok = True
            # 方式 2：URL query 参数 ws://host/path?access_token=xxx
            qs = parse_qs(parsed.query)
            if qs.get("access_token", [""])[0] == self._settings.onebot_access_token:
                token_ok = True
            if not token_ok:
                logger.warning("auth failed (header or query token mismatch), closing connection")
                await conn.close(code=CLOSE_AUTH_FAILED, reason="auth failed")
                return

        # 3. 读取角色与 self_id
        role = headers.get("X-Client-Role") or "Universal"
        self_id = headers.get("X-Self-ID") or "unknown"
        logger.info("client connected role=%s self_id=%s", role, self_id)
        self._roles[role] = conn

        # 新连接建立后，重发待重试队列里的请求
        # 用 create_task 避免阻塞 _handler 的接收循环
        if self._retry_queue:
            self._spawn(self._resend_retry_queue())

        try:
            async for raw in conn:
                if not isinstance(raw, str):
                    logger.debug("忽略二进制帧 %d 字节", len(raw))
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("invalid JSON: %s", raw[:200])
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
        """事件处理必须异步执行，否则会阻塞接收循环，导致 echo 回执读不到而超时。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _fail_pending_for(self, conn: ServerConnection) -> None:
        """连接断开时的处理：
        - 如果还有其他可用连接，不失败，让 call_api 等超时（当前实现的 timeout 会处理）
        - 否则：入重试队列（未超次数）或直接失败（超次数）
        """
        # 还有其他可用连接吗？
        if self._pick_api_conn() is not None:
            # 有，pending 请求留在原地，call_api 会等 echo 回执直到超时
            return

        # 没有了，逐个判断是重试还是彻底失败
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
                # 入重试队列，等新连接建立后重发
                self._retry_queue.append((echo, action, params, attempt + 1))
                logger.info("pending API 入重试队列 echo=%s action=%s (第 %d 次重试)",
                            echo[:8], action, attempt + 1)
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
        """新连接建立后，按 FIFO 重发待重试的请求。

        设计约束：重试请求已经没有对应的 Future（断连时可能已丢失），
        所以这里重新构造 Future 并注册到 pending 中，正常走 echo 匹配流程。
        """
        # 拷贝队列再清空，避免重发过程中 _fail_pending_for 再次入队造成无限循环
        queued = self._retry_queue[:]
        self._retry_queue.clear()

        for echo, action, params, attempt in queued:
            await asyncio.sleep(0.3)  # 稍微间隔，避免一次性塞爆 NapCat

            conn = self._pick_api_conn()
            if conn is None:
                # 连接又断了，放回队列（attempt 不变，因为还没真的重发）
                self._retry_queue.insert(0, (echo, action, params, attempt))
                logger.warning("重试时无可用连接，放回队列")
                return

            fut = asyncio.get_running_loop().create_future()
            self._pending[echo] = fut
            self._pending_ws[echo] = conn
            self._pending_info[echo] = (action, params, attempt)

            try:
                await conn.send(json.dumps({"action": action, "params": params, "echo": echo}))
                logger.info("已重发 echo=%s action=%s", echo[:8], action)
            except Exception as exc:
                logger.warning("重发失败 echo=%s: %s", echo[:8], exc)
                self._pending.pop(echo, None)
                self._pending_ws.pop(echo, None)
                self._pending_info.pop(echo, None)
                # 重发也失败了，不再重试
                if not fut.done():
                    fut.set_exception(ConnectionError(f"resend failed: {exc}"))

    # ---------- API 调用 ----------

    def _pick_api_conn(self) -> Optional[ServerConnection]:
        """优先 Universal，其次 API。"""
        for role in ("Universal", "API"):
            conn = self._roles.get(role)
            if conn is not None and conn.state is websockets.State.OPEN:
                return conn
        return None

    async def call_api(
        self, action: str, params: Optional[dict] = None, timeout: float = 5.0
    ) -> Any:
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

    # ---------- 便捷封装 ----------

    async def send_private_msg(self, user_id: int, text: str) -> None:
        await self.call_api(
            "send_private_msg",
            {"user_id": user_id, "message": [{"type": "text", "data": {"text": text}}]},
        )

    async def set_friend_add_request(self, flag: str, approve: bool = True) -> None:
        await self.call_api("set_friend_add_request", {"flag": flag, "approve": approve})

    # ---------- 事件提取工具（纯函数） ----------

    @staticmethod
    def extract_private_text(event: dict) -> Optional[tuple[int, str]]:
        """提取 (user_id, text)；非私聊文本消息返回 None。"""
        if event.get("post_type") != "message" or event.get("message_type") != "private":
            return None

        user_id = event.get("user_id")
        if not isinstance(user_id, int):
            return None

        parts = [
            seg.get("data", {}).get("text", "")
            for seg in event.get("message", [])
            if isinstance(seg, dict) and seg.get("type") == "text"
        ]
        text = "".join(parts).strip() or (event.get("raw_message") or "").strip()
        return (user_id, text) if text else None

    @staticmethod
    def extract_friend_request(event: dict) -> Optional[tuple[int, str]]:
        """提取 (user_id, flag)；非好友请求返回 None。"""
        if event.get("post_type") != "request" or event.get("request_type") != "friend":
            return None

        user_id, flag = event.get("user_id"), event.get("flag")
        if not isinstance(user_id, int) or not isinstance(flag, str):
            return None
        return user_id, flag
