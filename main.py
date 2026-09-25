"""PrivateBot 入口。"""
from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import TimedRotatingFileHandler

import paths
import selfcheck
from adapter.onebot import OneBotServer
from config import load_settings
from core.handler import PrivateChatHandler
from core.llm import LLMClient
from core.memory import MemoryStore
from core.proactive import ProactiveScheduler
from core.session import SessionStore
from core.state import CharacterStateManager
from persona.loader import PersonaLoader

# 文件日志保留天数
LOG_BACKUP_DAYS = 7


def setup_logging(level: str) -> None:
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    handlers: list[logging.Handler] = [console]

    file_error: str | None = None
    try:
        paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
        # 用带 BOM 的 UTF-8：零基础用户会用记事本双击打开日志，
        # 无 BOM 的 UTF-8 在旧版记事本里会显示成乱码。
        file_handler = TimedRotatingFileHandler(
            str(paths.LOG_FILE),
            when="midnight",
            backupCount=LOG_BACKUP_DAYS,
            encoding="utf-8-sig",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except OSError as exc:
        file_error = str(exc)

    logging.basicConfig(level=getattr(logging, level, logging.INFO), handlers=handlers)
    if file_error:
        logging.getLogger("main").warning("日志文件不可用（关掉窗口后日志会丢失）: %s", file_error)


def ensure_ready() -> bool:
    """启动前自检；配置不完整时弹出配置向导。返回是否可以继续启动。"""
    preflight = selfcheck.check_preflight()
    if selfcheck.all_ok(preflight):
        return True

    print(selfcheck.format_report(preflight), flush=True)

    import setup_wizard

    if not setup_wizard.available():
        print("\n配置不完整，当前环境又无法显示配置窗口。")
        print(f"请用记事本手动编辑 {paths.ENV_PATH}")
        print(f"（可以参考同目录下的 {paths.ENV_EXAMPLE_PATH.name}）")
        return False

    print("\n正在打开配置窗口……\n", flush=True)
    try:
        saved = setup_wizard.run()
    except RuntimeError as exc:
        print(f"无法打开配置窗口：{exc}")
        return False

    if not saved:
        print("已取消配置，程序退出。")
        return False

    remaining = [result for result in selfcheck.check_preflight() if not result.ok]
    if remaining:
        print(selfcheck.format_report(remaining), flush=True)
        print("\n配置仍不完整，程序退出。")
        return False
    return True


def run_runtime_checks(settings) -> bool:
    """配置就绪后的检查。端口被占用属于致命问题，LLM 不可用只告警。"""
    logger = logging.getLogger("main")

    port_result = selfcheck.check_port_bindable(
        settings.onebot_ws_host, settings.onebot_ws_port
    )
    if not port_result.ok:
        logger.error("%s", port_result.line())
        print(f"\n启动失败：{port_result.hint}")
        return False

    llm_result = selfcheck.test_llm(
        settings.llm_api_key,
        settings.llm_base_url,
        settings.llm_model,
        timeout=min(float(settings.llm_timeout), 20.0),
    )
    if not llm_result.ok:
        logger.warning("%s", llm_result.line())
        print(f"\n提示：{llm_result.hint}\n机器人仍会启动，但可能无法回复消息。")
    return True


async def main() -> None:
    if not ensure_ready():
        raise SystemExit(1)

    settings = load_settings()
    setup_logging(settings.log_level)
    paths.ensure_runtime_dirs()

    logger = logging.getLogger("main")
    logger.info("运行目录: %s", paths.APP_ROOT)
    if not run_runtime_checks(settings):
        raise SystemExit(1)

    bot = OneBotServer(settings)
    llm = LLMClient(settings)
    sessions = SessionStore(
        paths.SESSIONS_DIR,
        settings.max_history,
        summary_threshold=settings.summary_threshold,
        summary_keep=settings.summary_keep,
    )
    persona = PersonaLoader(settings.persona_file, settings.system_prompt, settings.style_dir)
    state_mgr = CharacterStateManager(settings.state_dir)
    memory = MemoryStore(settings.memory_dir)
    handler = PrivateChatHandler(settings, bot, llm, sessions, persona, state_mgr, memory)

    logger.info("白名单: %s", sorted(settings.allowed_qq))
    logger.info("人设文件: %s", settings.persona_file or "(未启用)")
    logger.info("角色状态系统: %s", state_mgr.enabled)
    logger.info("长期记忆库: %s", memory.enabled)
    logger.info("主动消息调度器: %s", settings.enable_proactive)
    logger.info("日志文件: %s", paths.LOG_FILE)
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
    except SystemExit:
        sys.exit(1)