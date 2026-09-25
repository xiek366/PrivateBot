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

# 传给提取 Prompt 的已有记忆上限（防 Prompt 无限膨胀）
MAX_KNOWN_MEMORIES = 30

# 已有记忆清单块：让 LLM 自己判断"同义改写"造成的重复
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

    async def chat(
        self, messages: Sequence[dict], temperature: float = 0.9, retries: int = 2
    ) -> str:
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

    async def summarize(
        self, messages: list[dict], temperature: float = 0.3
    ) -> str:
        """把一段对话压成 200 字以内的中文摘要。失败返回空字符串（不 raise）。"""
        prompt = (
            "请将以下对话摘要成 200 字以内的中文，"
            "保留人名、日期、偏好、事件等关键信息。"
            "只输出摘要内容，不要加「摘要：」等前缀。\n\n"
        )
        # 把原始对话拼成一段纯文本
        raw_parts = []
        for m in messages:
            role = m.get("role", "?")
            content = m.get("content", "")
            role_label = "用户" if role == "user" else "机器人"
            raw_parts.append(f"{role_label}: {content}")
        prompt += "\n".join(raw_parts)

        try:
            return await self.chat(
                [{"role": "user", "content": prompt}],
                temperature=temperature,
                retries=1,
            )
        except Exception as exc:
            logger.warning("摘要生成失败: %s", exc)
            return ""

    # ---------- Stage 7：长期记忆提取 ----------

    EXTRACT_MEMORY_SYSTEM_PROMPT = """你是一个记忆提取器，负责从对话中提取【关于用户】的客观事实。

写作要求（每条记忆都必须满足）：
1. 主语是「用户」，不是「我」、不是「结灯」
2. 是客观事实，脱离本次对话也能独立成立
3. 一条只写一个事实，不要把多件事揉成一句
4. 像记笔记，不要像写故事，不要心理描写

category 四选一：
- preference: 用户的喜好、兴趣、厌恶（如"用户喜欢 RPG 游戏"）
- habit: 用户的习惯、作息、行为规律（如"用户经常加班到十点"）
- profile: 用户的基本信息（职业、年龄、城市、称呼等）
- relationship: 用户和结灯共同经历的事件（如"用户向结灯表白"）

importance 0.0-1.0：
- 0.9+: 关键关系事件
- 0.7-0.89: 重要个人信息
- 0.4-0.69: 一般小事
- <0.4: 忽略（不要返回）

禁止提取（重要）：
- 结灯的台词、心理活动、情绪、期待、状态（如"结灯一直在等他""结灯很害羞"）
  —— 这些是角色状态，由系统自动管理
- 对结灯内心或剧情的推测、脑补
- 好感度 / 信任度等数值
- 瞬时/临时状态（如"用户现在还没睡""用户今天在线"）
  —— 只提取明天、下个月依然成立的事实
- 与「已经记住的内容」清单里语义重复的事实（措辞不同也算重复）

示例：
输入："刚上号，你怎么知道"
输出：[{"category":"habit","content":"用户经常在晚上才上号","importance":0.5}]
错误示范："用户只在晚上上号找结灯，结灯一直在等他"
  → 错在①掺入了结灯的状态 ②两个事实揉成一句

输入："我最近在玩原神"
输出：[{"category":"preference","content":"用户最近在玩《原神》","importance":0.6}]

输入："我喜欢你"
输出：[{"category":"relationship","content":"用户向结灯说了「我喜欢你」","importance":0.9}]

没有值得记住的内容时返回 []。
严格返回 JSON 数组，不要加任何其他文字。"""

    async def extract_memories(
        self,
        user_msg: str,
        bot_reply: str,
        history: list[dict] | None = None,
        known: list[str] | None = None,
        temperature: float = 0.3,
    ) -> list[dict]:
        """从对话中提取记忆候选。返回 list[dict]，每项有 category/content/importance。

        known 是已有记忆的内容清单，一并喂给 LLM 做语义去重
        （"同义改写"造成的重复，文本相似度抓不到，LLM 能判断）。
        """
        # 拼接历史（最近 4 轮）
        history_text = ""
        if history:
            recent = history[-8:]  # 最近 4 轮
            parts = []
            for m in recent:
                label = "用户" if m.get("role") == "user" else "结灯"
                parts.append(f"{label}：{m.get('content', '')}")
            history_text = "\n".join(parts)

        # 已有记忆清单（只取最近 MAX_KNOWN_MEMORIES 条，防 Prompt 膨胀）
        known_block = ""
        if known:
            items = "\n".join(f"- {c}" for c in known[-MAX_KNOWN_MEMORIES:])
            known_block = KNOWN_MEMORIES_BLOCK.format(items=items)

        user_prompt = f"""对话历史：
{history_text or "(无)"}

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
        """尝试从 LLM 输出里解析 JSON 数组。"""
        # LLM 可能返回带 ```json 包裹或前导文字
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
