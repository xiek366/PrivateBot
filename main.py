"""PrivateBot 入口。"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from adapter.onebot import OneBotServer
from config import load_settings
from core.handler import PrivateChatHandler
from core.llm import LLMClient
from core.memory import MemoryStore
from core.proactive import ProactiveScheduler
from core.session import SessionStore
from core.state import CharacterStateManager
from persona.loader import PersonaLoader

BASE_DIR = Path(__file__).resolve().parent
SESSION_DIR = BASE_DIR / "data" / "sessions"


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)

    bot = OneBotServer(settings)
    llm = LLMClient(settings)
    sessions = SessionStore(
        SESSION_DIR,
        settings.max_history,
        summary_threshold=settings.summary_threshold,
        summary_keep=settings.summary_keep,
    )
    persona = PersonaLoader(settings.persona_file, settings.system_prompt, settings.style_dir)
    state_mgr = CharacterStateManager(settings.state_dir)
    memory = MemoryStore(settings.memory_dir)
    handler = PrivateChatHandler(settings, bot, llm, sessions, persona, state_mgr, memory)

    logger = logging.getLogger("main")
    logger.info("白名单: %s", sorted(settings.allowed_qq))
    logger.info("人设文件: %s", settings.persona_file or "(未启用)")
    logger.info("角色状态系统: %s", state_mgr.enabled)
    logger.info("长期记忆库: %s", memory.enabled)
    logger.info("主动消息调度器: %s", settings.enable_proactive)
    persona.get_system_prompt()  # 启动时预加载，尽早暴露人设配置问题

    # 启动主动消息调度器（后台 task，不阻塞主流程）
    if settings.enable_proactive and state_mgr.enabled:
        scheduler = ProactiveScheduler(settings, state_mgr, llm, bot, memory)
        asyncio.create_task(scheduler.run())

    try:
        await bot.run(handler.on_event)
    finally:
        await llm.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger("main").info("已退出")
