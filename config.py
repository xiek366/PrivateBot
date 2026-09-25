"""配置加载与校验。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set

from dotenv import load_dotenv

import paths


@dataclass
class Settings:
    # OneBot
    onebot_ws_host: str
    onebot_ws_port: int
    onebot_ws_path: str
    onebot_access_token: str
    # 权限
    allowed_qq: Set[int]
    auto_accept_friend: bool
    # 日志
    log_level: str
    # LLM
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    llm_timeout: float
    # 对话
    max_history: int
    system_prompt: str
    # 人设
    persona_file: Optional[Path]
    # 限流：同用户最多保留 1 条待处理消息
    max_pending_per_user: int
    # 自动摘要：累计到此条数时触发摘要
    summary_threshold: int
    # 自动摘要：摘要后滑窗口保留多少条近期对话
    summary_keep: int
    # 自动摘要：生成 temperature（越低越稳定）
    llm_summary_temp: float
    # 参考语料目录（Stage 5 轻量检索）
    style_dir: Optional[Path]
    # 角色状态目录（Stage 6）
    state_dir: Optional[Path]
    # 长期记忆目录（Stage 7）
    memory_dir: Optional[Path]
    # 主动发消息开关（Stage 7）
    enable_proactive: bool
    # 主动发消息：最少隔多少小时才发
    proactive_min_hours: float
    # 主动发消息：最低好感度阈值
    proactive_min_affection: float
    # 主动发消息：时间窗口
    proactive_window_start: str
    proactive_window_end: str


def _parse_qq_list(raw: str) -> Set[int]:
    result: Set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError as exc:
            raise ValueError(f"非法的 QQ 号: {part!r}") from exc
    return result


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_persona_path(raw: str) -> Optional[Path]:
    """相对路径按 APP_ROOT 解析，避免依赖当前工作目录。"""
    return paths.resolve_path(raw)


def _parse_dir(raw: str) -> Optional[Path]:
    """目录路径解析（同 _parse_persona_path，但语义不同）。"""
    return paths.resolve_path(raw)


_ENV_PATH = paths.ENV_PATH


def load_settings() -> Settings:
    load_dotenv(_ENV_PATH)

    required = ["ONEBOT_WS_HOST", "ONEBOT_WS_PORT", "ONEBOT_WS_PATH", "ALLOWED_QQ", "LLM_API_KEY"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"缺少必填配置: {', '.join(missing)}，请在 .env 中填写")

    return Settings(
        onebot_ws_host=os.getenv("ONEBOT_WS_HOST", "127.0.0.1"),
        onebot_ws_port=int(os.getenv("ONEBOT_WS_PORT", "8080")),
        onebot_ws_path=os.getenv("ONEBOT_WS_PATH", "/onebot"),
        onebot_access_token=os.getenv("ONEBOT_ACCESS_TOKEN", ""),
        allowed_qq=_parse_qq_list(os.getenv("ALLOWED_QQ", "")),
        auto_accept_friend=_as_bool(os.getenv("AUTO_ACCEPT_FRIEND", "true")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
        llm_model=os.getenv("LLM_MODEL", "deepseek-flash"),
        llm_timeout=float(os.getenv("LLM_TIMEOUT", "60")),
        max_history=int(os.getenv("MAX_HISTORY", "20")),
        system_prompt=os.getenv("SYSTEM_PROMPT", "你是一个乐于助人的中文聊天伙伴，回答简洁自然。"),
        persona_file=_parse_persona_path(os.getenv("PERSONA_FILE", "data/personas/default.yaml")),
        max_pending_per_user=int(os.getenv("MAX_PENDING_PER_USER", "1")),
        summary_threshold=int(os.getenv("SUMMARY_THRESHOLD", "30")),
        summary_keep=int(os.getenv("SUMMARY_KEEP", "10")),
        llm_summary_temp=float(os.getenv("LLM_SUMMARY_TEMP", "0.3")),
        style_dir=_parse_dir(os.getenv("STYLE_DIR", "data/style")),
        state_dir=_parse_dir(os.getenv("STATE_DIR", "data/state")),
        memory_dir=_parse_dir(os.getenv("MEMORY_DIR", "data/memories")),
        enable_proactive=_as_bool(os.getenv("ENABLE_PROACTIVE", "true")),
        proactive_min_hours=float(os.getenv("PROACTIVE_MIN_HOURS", "12")),
        proactive_min_affection=float(os.getenv("PROACTIVE_MIN_AFFECTION", "30")),
        proactive_window_start=os.getenv("PROACTIVE_WINDOW_START", "08:00"),
        proactive_window_end=os.getenv("PROACTIVE_WINDOW_END", "23:00"),
    )
