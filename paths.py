"""统一的运行根目录与路径解析（Stage 9）。

打包后程序运行在 PyInstaller 的解压目录中，`Path(__file__).parent` 不再等于
用户看到的目录，因此全项目的路径基准必须收敛到这里。

- 打包模式（sys.frozen）：基准 = exe 所在目录
- 源码模式：基准 = 项目根目录

约定：除本模块外，其它模块不得再用 `Path(__file__).resolve().parent` 推导基准，
只接收上层传入的 Path 参数。
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _detect_app_root() -> Path:
    """打包模式取 exe 所在目录，源码模式取项目根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_ROOT: Path = _detect_app_root()

# ---- 配置 ----
ENV_PATH: Path = APP_ROOT / ".env"
ENV_EXAMPLE_PATH: Path = APP_ROOT / ".env.example"

# ---- 数据 ----
DATA_DIR: Path = APP_ROOT / "data"
SESSIONS_DIR: Path = DATA_DIR / "sessions"
STATE_DIR: Path = DATA_DIR / "state"
MEMORIES_DIR: Path = DATA_DIR / "memories"
PERSONA_DIR: Path = DATA_DIR / "personas"
STYLE_DIR: Path = DATA_DIR / "style"

# ---- 日志 ----
LOG_DIR: Path = APP_ROOT / "logs"
LOG_FILE: Path = LOG_DIR / "privatebot.log"

# 随包分发、运行时需要存在的数据目录
DISTRIBUTED_DIRS = (PERSONA_DIR, STYLE_DIR)


def resolve_path(raw: str) -> Optional[Path]:
    """相对路径按 APP_ROOT 解析，绝对路径原样返回；空字符串返回 None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else APP_ROOT / path


def ensure_runtime_dirs() -> None:
    """创建运行时必需目录（各数据目录由各自的 Store 自行创建）。"""
    for directory in (DATA_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def check_writable() -> Optional[str]:
    """探测 APP_ROOT 是否可写。可写返回 None，否则返回人话版原因。"""
    try:
        APP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=APP_ROOT, prefix=".write_test_", delete=True):
            pass
    except OSError as exc:
        return f"程序所在目录没有写入权限（{APP_ROOT}）：{exc}"
    return None