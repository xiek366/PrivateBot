"""环境自检（Stage 9）。

把"启动失败"从 Python traceback 变成人话版提示：控制台与配置向导共用同一份结果。

两类检查：
- `check_preflight()`：配置加载前的检查（.env 存在、必填项齐全、目录可写）
- `check_runtime()`：配置加载后的检查（端口可 bind、LLM 接口可用）

注意：本项目里 PrivateBot 是 OneBot 反向 WebSocket 的**服务端**，NapCat 是客户端。
所以端口检查是"本程序能否占用该端口"，而不是"能否连上别人"。
"""
from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

import httpx
from dotenv import dotenv_values

import paths

logger = logging.getLogger(__name__)

# 与 config.load_settings 的必填项保持一致
REQUIRED_KEYS = (
    "ONEBOT_WS_HOST",
    "ONEBOT_WS_PORT",
    "ONEBOT_WS_PATH",
    "ALLOWED_QQ",
    "LLM_API_KEY",
)

KEY_LABELS = {
    "ONEBOT_WS_HOST": "监听地址（ONEBOT_WS_HOST）",
    "ONEBOT_WS_PORT": "OneBot 端口（ONEBOT_WS_PORT）",
    "ONEBOT_WS_PATH": "连接路径（ONEBOT_WS_PATH）",
    "ALLOWED_QQ": "允许聊天的 QQ 号（ALLOWED_QQ）",
    "LLM_API_KEY": "DeepSeek 密钥（LLM_API_KEY）",
}

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"


@dataclass
class CheckResult:
    """单项自检结果。hint 是给用户看的人话版修复建议。"""

    name: str
    ok: bool
    detail: str = ""
    hint: str = ""

    def line(self) -> str:
        mark = "OK  " if self.ok else "失败"
        text = f"[{mark}] {self.name}"
        if not self.ok and self.hint:
            text += f" —— {self.hint}"
        return text


# ---------- 读取配置 ----------

def read_env() -> dict[str, str]:
    """读取 .env，并用真实环境变量覆盖（与 load_dotenv 的优先级一致）。"""
    values: dict[str, str] = {}
    if paths.ENV_PATH.exists():
        try:
            values.update(
                {k: v for k, v in dotenv_values(paths.ENV_PATH).items() if v is not None}
            )
        except OSError as exc:
            logger.warning("读取 .env 失败: %s", exc)
    for key in list(values) + [k for k in REQUIRED_KEYS if k not in values]:
        env_value = os.getenv(key)
        if env_value:
            values[key] = env_value
    return values


def check_env_file() -> CheckResult:
    if paths.ENV_PATH.exists():
        return CheckResult("配置文件 .env", True, detail=str(paths.ENV_PATH))
    return CheckResult(
        "配置文件 .env",
        False,
        detail=str(paths.ENV_PATH),
        hint="还没生成配置文件，请在配置向导里填写",
    )


def check_required(env: Optional[dict[str, str]] = None) -> CheckResult:
    values = read_env() if env is None else env
    missing = [key for key in REQUIRED_KEYS if not (values.get(key) or "").strip()]
    if not missing:
        return CheckResult("必填配置项", True)
    labels = "、".join(KEY_LABELS.get(key, key) for key in missing)
    return CheckResult(
        "必填配置项",
        False,
        detail=f"missing={missing}",
        hint=f"缺少这些配置：{labels}，请在配置向导里填写",
    )


def check_dirs_writable() -> CheckResult:
    reason = paths.check_writable()
    if reason is None:
        return CheckResult("程序目录可写", True, detail=str(paths.APP_ROOT))
    return CheckResult(
        "程序目录可写",
        False,
        detail=reason,
        hint="程序所在目录不能写入，请把整个文件夹挪到非系统盘（例如 D 盘）后重试",
    )


def check_preflight() -> list[CheckResult]:
    """配置加载前的检查。全部通过才说明可以正常启动。"""
    return [check_env_file(), check_required(), check_dirs_writable()]


# ---------- 端口检查 ----------

def check_port_bindable(host: str, port) -> CheckResult:
    """检查本程序能否占用该端口（本项目是服务端）。"""
    name = f"端口 {host}:{port} 可占用"
    try:
        port_num = int(port)
    except (TypeError, ValueError):
        return CheckResult(
            name,
            False,
            detail=f"port={port!r}",
            hint="端口不是合法数字，请在配置向导里填写 1024-65535 之间的数字",
        )

    probe_host = "127.0.0.1" if (host or "").strip() in ("", "0.0.0.0", "::") else host.strip()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((probe_host, port_num))
    except OSError as exc:
        return CheckResult(
            name,
            False,
            detail=str(exc),
            hint=(
                f"端口 {port_num} 已被其它程序占用。请先关掉可能重复运行的 PrivateBot，"
                f"或在配置向导里换一个端口（同时要把 NapCat 的端口改成一样的）"
            ),
        )
    finally:
        sock.close()
    return CheckResult(name, True)


# ---------- LLM 检查 ----------

def test_llm(
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    timeout: float = 20.0,
) -> CheckResult:
    """发一次最小请求验证密钥是否可用。同步调用，向导在后台线程里跑它。"""
    name = "DeepSeek 接口可用"
    api_key = (api_key or "").strip()
    if not api_key:
        return CheckResult(name, False, hint="请先填写 DeepSeek 密钥")

    payload = {
        "model": (model or DEFAULT_MODEL).strip(),
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    url = (base_url or DEFAULT_BASE_URL).strip()

    try:
        with httpx.Client(base_url=url, timeout=timeout, headers=headers) as client:
            resp = client.post("/chat/completions", json=payload)
    except httpx.HTTPError as exc:
        return CheckResult(
            name,
            False,
            detail=str(exc),
            hint=f"连不上 DeepSeek 服务器，请检查网络或代理设置（{exc}）",
        )

    if resp.status_code == 200:
        return CheckResult(name, True)
    if resp.status_code in (401, 403):
        return CheckResult(
            name,
            False,
            detail=f"HTTP {resp.status_code}",
            hint="密钥无效。请到 DeepSeek 官网重新复制一次，注意不要多带空格或换行",
        )
    if resp.status_code == 402:
        return CheckResult(
            name,
            False,
            detail=f"HTTP {resp.status_code}",
            hint="账户余额不足。请到 DeepSeek 官网充值后再试",
        )
    if resp.status_code == 404:
        return CheckResult(
            name,
            False,
            detail=f"HTTP {resp.status_code}",
            hint=f"模型 {payload['model']} 不存在，请检查配置里的 LLM_MODEL",
        )
    return CheckResult(
        name,
        False,
        detail=f"HTTP {resp.status_code}: {resp.text[:200]}",
        hint=f"接口返回了异常状态（HTTP {resp.status_code}），请把 logs 目录里的日志发给开发者",
    )


def check_runtime(settings) -> list[CheckResult]:
    """配置就绪后的检查。"""
    return [
        check_port_bindable(settings.onebot_ws_host, settings.onebot_ws_port),
        test_llm(
            settings.llm_api_key,
            settings.llm_base_url,
            settings.llm_model,
            timeout=min(float(settings.llm_timeout), 20.0),
        ),
    ]


# ---------- 输出 ----------

def format_report(results: Sequence[CheckResult]) -> str:
    return "\n".join(result.line() for result in results)


def all_ok(results: Iterable[CheckResult]) -> bool:
    return all(result.ok for result in results)