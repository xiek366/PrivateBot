"""LLM 客户端（OpenAI 兼容协议）。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Sequence

import httpx

from config import Settings

logger = logging.getLogger(__name__)

MAX_KNOWN_MEMORIES = 30

KNOWN_MEMORIES_BLOCK = """已经记住的内容（措辞不同但语义相同的事实，不要重复返回）：
{items}

"""


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )

    async def chat(self, messages: Sequence[dict], temperature: float = 0.9, retries: int = 2) -> str:
        payload: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": list(messages),
            "temperature": temperature,
            "stream": False,
        }
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self._client.post("/chat/completions", json=payload)
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
            except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
                last_error = exc
                logger.warning("LLM 调用失败(第 %d 次): %s", attempt + 1, exc)
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"LLM 调用失败: {last_error}")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat_with_images(
        self,
        text_messages: list[dict],
        image_data_urls: list[str],
        user_text: str,
        temperature: float = 0.9,
        retries: int = 2,
        detail: str = "low",
    ) -> str:
        """多模态调用：在最后一条 user 消息里附上图片。"""
        content_blocks: list[dict] = []
        for url in image_data_urls:
            content_blocks.append({
                "type": "image_url",
                "image_url": {"url": url, "detail": detail},
            })
        content_blocks.append({"type": "text", "text": user_text or "（用户只发了一张图片）"})
        messages = list(text_messages) + [{"role": "user", "content": content_blocks}]
        payload: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self._client.post("/chat/completions", json=payload)
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
            except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
                last_error = exc
                logger.warning("多模态 LLM 调用失败(第 %d 次): %s", attempt + 1, exc)
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"多模态 LLM 调用失败: {last_error}")

    async def summarize(self, messages: list[dict], temperature: float = 0.3) -> str:
        prompt = (
            "请将以下对话摘要成 200 字以内的中文，"
            "保留人名、日期、偏好、事件等关键信息。"
            "只输出摘要内容，不要加「摘要：」等前缀。\n\n"
        )
        raw_parts = []
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content", "")
            role_label = "用户" if role == "user" else "机器人"
            raw_parts.append(f"{role_label}: {content}")
        prompt += "\n".join(raw_parts)
        try:
            return await self.chat([{"role": "user", "content": prompt}], temperature=temperature, retries=1)
        except Exception as exc:
            logger.warning("摘要生成失败: %s", exc)
            return ""

    EXTRACT_MEMORY_SYSTEM_PROMPT = """你是一个记忆提取器，负责从对话中提取【关于用户】的客观事实。

写作要求：
1. 主语是「用户」
2. 是客观事实，脱离本次对话也能独立成立
3. 一条只写一个事实
4. 像记笔记，不要写故事

category 四选一：preference / habit / profile / relationship
importance 0.0-1.0

禁止提取：结灯的状态、对结灯的脑补、好感度数值、瞬时状态。

没有值得记住的内容时返回 []。严格返回 JSON 数组。"""

    async def extract_memories(
        self,
        user_msg: str,
        bot_reply: str,
        history: list[dict] | None = None,
        known: list[str] | None = None,
        temperature: float = 0.3,
    ) -> list[dict]:
        history_text = ""
        if history:
            recent = history[-8:]
            parts = []
            for m in recent:
                label = "用户" if m.get("role") == "user" else "结灯"
                parts.append(f"{label}：{m.get('content', '')}")
            history_text = "\n".join(parts)
        known_block = ""
        if known:
            items = "\n".join(f"- {c}" for c in known[-MAX_KNOWN_MEMORIES:])
            known_block = KNOWN_MEMORIES_BLOCK.format(items=items)
        user_prompt = f"""对话历史：
{history_text or '(无)'}

本轮对话：
用户：{user_msg}
结灯：{bot_reply}

{known_block}请返回 JSON 数组。"""
        try:
            raw = await self.chat(
                [
                    {"role": "system", "content": self.EXTRACT_MEMORY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                retries=1,
            )
            return self._parse_memory_json(raw, user_msg)
        except Exception as exc:
            logger.warning("记忆提取失败: %s", exc)
            return []

    def _parse_memory_json(self, raw: str, source_msg: str) -> list[dict]:
        json_match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not json_match:
            return []
        try:
            arr = json.loads(json_match.group())
        except json.JSONDecodeError:
            return []
        result = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            try:
                cat = item.get("category", "").strip()
                content = item.get("content", "").strip()
                imp = float(item.get("importance", 0.5))
                if cat not in ("preference", "habit", "profile", "relationship"):
                    continue
                if not content:
                    continue
                result.append({
                    "category": cat,
                    "content": content,
                    "importance": max(0.0, min(1.0, imp)),
                    "source_msg": source_msg[:100],
                })
            except (ValueError, TypeError):
                continue
        if result:
            logger.debug("LLM 提取记忆 %d 条: %s", len(result), [r["content"][:30] for r in result])
        return result
