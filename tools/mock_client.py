"""OneBot 客户端模拟器：本地验证 传输→接收→发送 闭环。

用法（先在另一个终端运行 python main.py）：
    python tools/mock_client.py                     # 白名单内消息，期望收到回复
    python tools/mock_client.py --case reject       # 白名单外消息，期望被丢弃
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import websockets


async def run_case(
    url: str, token: str, case: str, user_id: int, text: str, timeout: float
) -> bool:
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Self-ID": "99999",
        "X-Client-Role": "Universal",
    }

    async with websockets.connect(url, additional_headers=headers) as ws:
        event = {
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "user_id": user_id,
            "raw_message": text,
            "message": [{"type": "text", "data": {"text": text}}],
            "sender": {"nickname": "tester"},
            "self_id": 99999,
        }

        print(f"[tx] -> user_id={user_id} text={text!r}")
        await ws.send(json.dumps(event))

        # 白名单外的消息预期无任何回包，只需短暂等待
        wait = 2.0 if case == "reject" else timeout
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=wait)
        except asyncio.TimeoutError:
            if case == "reject":
                print("[PASS] 白名单外消息被丢弃，未产生任何 API 调用")
                return True
            print(f"[FAIL] {wait:.0f} 秒内未收到任何 API 调用")
            return False

        payload = json.loads(raw)
        if "action" not in payload:
            print(f"[FAIL] 收到非 API 调用报文: {raw[:200]}")
            return False

        print(f"[rx] <- action={payload['action']}")

        if case == "reject":
            print(f"[FAIL] 白名单外消息不应触发 API 调用，实际 {payload['action']}")
            return False
        if payload["action"] != "send_private_msg":
            print(f"[FAIL] 期望 send_private_msg，实际 {payload['action']}")
            return False

        sent = "".join(
            seg.get("data", {}).get("text", "")
            for seg in payload["params"].get("message", [])
            if seg.get("type") == "text"
        )
        print(f"[rx] <- 回复内容: {sent!r}")

        # 回执，让服务端结束 call_api 的等待
        await ws.send(
            json.dumps(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": 1},
                    "echo": payload.get("echo"),
                }
            )
        )

        print("[PASS] 传输→接收→发送 闭环成功")
        return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8080/onebot")
    parser.add_argument("--token", default="my-token-change-me")
    parser.add_argument("--case", choices=["echo", "reject"], default="echo")
    parser.add_argument("--user", type=int, default=0, help="留空则按 case 取默认值")
    parser.add_argument("--text", default="你好")
    parser.add_argument("--timeout", type=float, default=60.0, help="等待 LLM 回复的秒数")
    args = parser.parse_args()

    user_id = args.user or (10001 if args.case == "echo" else 99999999)

    try:
        ok = asyncio.run(
            run_case(args.url, args.token, args.case, user_id, args.text, args.timeout)
        )
    except OSError as exc:
        print(f"[FAIL] 无法连接 {args.url}: {exc}")
        print("       请先在另一个终端运行 python main.py")
        return 1

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
