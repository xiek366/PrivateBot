"""主动发消息调度器（Stage 7）。

每 10 分钟检查一次所有白名单用户，根据状态数值和时间窗口决定是否主动发消息。
主动消息由 LLM 生成，输入包含触发原因和当前状态。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import Settings
from core.state import CharacterStateManager, CharacterState
from core.llm import LLMClient

logger = logging.getLogger(__name__)


# ---------- 主动消息 Prompt ----------
PROACTIVE_MEMORY_PROMPT = """你是{character_name}，现在你想主动给{user_nickname}发一条消息。

原因：距离上次聊天已经过了{hours_since:.1f}小时，你们的好感度是{affection:.0f}/100（关系等级 {level_name}）。
当前时间是{current_time}，你的情绪是{mood}（valence {valence:.0f}, arousal {arousal:.0f}）。

{memory_text}

请发一条自然的、符合你性格的消息。
要求：
- 不要说"我想你"这么直白，要别扭一点、害羞一点
- 1-2 句话，100 字以内
- 符合你的说话风格（用……，短句，偶尔带动作描写）
- 内容要和触发原因相关（很久没聊可以说"没什么，就是突然想起来而已"；好感度高可以稍微亲近一点）

直接返回回复内容，不要其他任何格式。"""


class ProactiveScheduler:
    """主动发消息后台调度器。"""

    def __init__(
        self,
        settings: Settings,
        state_mgr: CharacterStateManager,
        llm: LLMClient,
        bot,
        memory_store=None,
    ) -> None:
        self._settings = settings
        self._state_mgr = state_mgr
        self._llm = llm
        self._bot = bot
        self._memory = memory_store
        # 记录每用户上次主动发消息时间
        self._last_sent: dict[int, datetime] = {}
        self._interval = 600  # 10 分钟检查一次

    async def run(self) -> None:
        """主循环。"""
        logger.info("主动消息调度器启动，检查间隔 %ds", self._interval)
        await asyncio.sleep(self._interval)  # 等系统先稳定

        while True:
            try:
                await self._check_all()
            except Exception as exc:
                logger.error("主动消息调度器异常: %s", exc, exc_info=True)
            await asyncio.sleep(self._interval)

    async def _check_all(self) -> None:
        for user_id in self._settings.allowed_qq:
            try:
                state = self._state_mgr.load(user_id)
                msg = await self._maybe_generate(user_id, state)
                if msg:
                    await self._send(user_id, msg)
            except Exception as exc:
                logger.warning("主动消息检查 user_id=%d 异常: %s", user_id, exc)

    async def _maybe_generate(self, user_id: int, state: CharacterState) -> Optional[str]:
        """判断是否该发，是则生成主动消息内容。"""
        # 条件 1：好感度够高
        if state.affection < self._settings.proactive_min_affection:
            return None

        # 条件 2：时间窗口
        now = datetime.now()
        if not self._in_window(now):
            return None

        # 条件 3：上次主动发消息够久
        last_sent = self._last_sent.get(user_id)
        if last_sent and (now - last_sent).total_seconds() < 24 * 3600:
            return None

        # 条件 4：情绪不极端（只拦负面极端，正向愉快不拦）
        if state.valence < -50 or state.arousal > 60:
            return None  # 太生气/太难过/太激动时不打扰

        # 条件 5：上次聊天够久
        hours_since = self._hours_since_last(state)
        if hours_since < self._settings.proactive_min_hours:
            return None

        # 全部满足 → 生成主动消息
        return await self._generate(user_id, state, hours_since)

    async def _generate(self, user_id: int, state: CharacterState, hours_since: float) -> Optional[str]:
        """调 LLM 生成主动消息。"""
        try:
            # 拿相关记忆
            memory_text = ""
            if self._memory and self._memory.enabled:
                memories = self._memory.retrieve(user_id, "最近怎么样", top_k=3)
                if memories:
                    memory_lines = [f"（你记得：{m.content}）" for m in memories]
                    memory_text = "相关记忆：\n" + "\n".join(memory_lines)

            level = self._state_mgr.calc_relationship_level(state.affection)
            mood = self._state_mgr.state_to_prompt(state)
            # 简化 mood 描述
            if state.valence > 30 and state.arousal > 20:
                mood_desc = "开心"
            elif state.valence > 0 and state.arousal > 25:
                mood_desc = "害羞"
            elif state.valence < -20:
                mood_desc = "有点低落"
            else:
                mood_desc = "平静"

            prompt = PROACTIVE_MEMORY_PROMPT.format(
                character_name="黑姬结灯",
                user_nickname="你",
                hours_since=hours_since,
                affection=state.affection,
                level_name=self._state_mgr.LEVEL_NAMES[level],
                current_time=now(),
                mood=mood_desc,
                valence=state.valence,
                arousal=state.arousal,
                memory_text=memory_text,
            )

            reply = await self._llm.chat([{"role": "system", "content": prompt}])
            if reply and len(reply) <= 200:
                logger.info("主动消息 user_id=%d: %s", user_id, reply[:80])
                return reply
            return None
        except Exception as exc:
            logger.warning("生成主动消息失败 user_id=%d: %s", user_id, exc)
            return None

    async def _send(self, user_id: int, msg: str) -> None:
        """发送主动消息并记录时间。"""
        try:
            await self._bot.send_private_msg(user_id, msg)
            self._last_sent[user_id] = datetime.now()
            logger.info("主动消息已发送 user_id=%d", user_id)
        except Exception as exc:
            logger.warning("主动消息发送失败 user_id=%d: %s", user_id, exc)

    def _in_window(self, now: datetime) -> bool:
        """检查当前时间是否在允许窗口内。"""
        try:
            start_h, start_m = map(int, self._settings.proactive_window_start.split(":"))
            end_h, end_m = map(int, self._settings.proactive_window_end.split(":"))
            start = start_h * 60 + start_m
            end = end_h * 60 + end_m
            current = now.hour * 60 + now.minute
            if start <= end:
                return start <= current <= end
            else:  # 跨午夜
                return current >= start or current <= end
        except (ValueError, AttributeError):
            return True  # 配置错了就不限制

    def _hours_since_last(self, state: CharacterState) -> float:
        """距上次聊天过了多少小时。"""
        if not state.last_interaction:
            return 24.0  # 没记录的话当作很久以前
        try:
            last = datetime.fromisoformat(state.last_interaction)
            return (datetime.now() - last).total_seconds() / 3600.0
        except (ValueError, TypeError):
            return 24.0


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
