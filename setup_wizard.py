"""配置向导（Stage 9）：tkinter 图形化生成 .env。

设计要点：
- 只问 4 个必填项，其余配置沿用 .env.example 里的默认值（向导每多一个字段，
  零基础用户放弃的概率就上升一分）
- 写入时保留模板的注释与分组，只替换对应键的值，方便用户之后手动编辑
- 先写临时文件再原子替换，避免写一半崩溃把配置写坏
- 编码固定 UTF-8 无 BOM（Windows 记事本另存为可能带 BOM，会导致首行键名解析失败）
"""
from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

import paths
import selfcheck

try:  # 极简 Python 发行版可能不带 tkinter
    import tkinter as tk
    from tkinter import messagebox, ttk

    _TK_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于运行环境
    tk = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    _TK_AVAILABLE = False

logger = logging.getLogger(__name__)

# 向导覆盖的 4 个必填项：(键名, 中文标签, 填写说明)
WIZARD_FIELDS = (
    (
        "LLM_API_KEY",
        "DeepSeek 密钥",
        "在 DeepSeek 官网「API Keys」页面创建，形如 sk-xxxxxxxx 的一串字符",
    ),
    (
        "ALLOWED_QQ",
        "允许聊天的 QQ 号",
        "谁会跟机器人聊天就填谁的 QQ 号（注意：不是机器人自己的号）；多个用英文逗号分隔",
    ),
    (
        "ONEBOT_ACCESS_TOKEN",
        "OneBot 访问令牌",
        "必须和 NapCat 里填的 Token 完全一致，两边不一致就连不上",
    ),
    (
        "ONEBOT_WS_PORT",
        "OneBot 端口",
        "默认 8080，必须和 NapCat 的端口一致；端口被占用时才需要改",
    ),
)

# .env.example 缺失时的兜底模板（所有键在 config.py 里都有代码默认值，
# 所以这里只需保证必填项齐全）
FALLBACK_TEMPLATE = """# ---- OneBot 反向 WebSocket ----
# PrivateBot 作为服务端监听，NapCat 作为客户端连过来
ONEBOT_WS_HOST=127.0.0.1
ONEBOT_WS_PORT=8080
ONEBOT_WS_PATH=/onebot
ONEBOT_ACCESS_TOKEN=my-token-change-me

# ---- 权限 ----
# 允许使用机器人的 QQ 号（能跟机器人对话的账号，不是机器人自己的号），多个用逗号分隔
ALLOWED_QQ=10001
AUTO_ACCEPT_FRIEND=true

# ---- LLM ----
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
LLM_TIMEOUT=60
MAX_HISTORY=20
"""

_ENV_KEY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*)=(.*)$")


def available() -> bool:
    """当前环境能否显示配置窗口。"""
    return _TK_AVAILABLE


# ---------- .env 读写 ----------

def _load_template() -> str:
    if paths.ENV_EXAMPLE_PATH.exists():
        try:
            return paths.ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("读取 .env.example 失败，改用内置模板: %s", exc)
    return FALLBACK_TEMPLATE


def render_env(updates: dict[str, str], base_text: str) -> str:
    """按模板渲染 .env：命中键名则替换值，未命中的追加到文件末尾。"""
    remaining = dict(updates)
    out: list[str] = []
    for line in base_text.splitlines():
        match = _ENV_KEY_RE.match(line)
        if match and match.group(2) in remaining:
            key = match.group(2)
            out.append(f"{match.group(1)}{key}={remaining.pop(key)}")
        else:
            out.append(line)
    if remaining:
        out.append("")
        out.append("# ---- 配置向导补充 ----")
        for key, value in remaining.items():
            out.append(f"{key}={value}")
    return "\n".join(out).rstrip("\n") + "\n"


def save_env(updates: dict[str, str]) -> Path:
    """写入 .env（UTF-8 无 BOM，先写临时文件再原子替换）。"""
    text = render_env(updates, _load_template())
    target = paths.ENV_PATH
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, target)

    # 让同一进程后续读取立即生效
    for key, value in updates.items():
        os.environ[key] = value
    logger.info("配置已写入 %s", target)
    return target


# ---------- 窗口 ----------

class SetupWizard:
    """首次运行时的配置窗口。只做"填 4 项 → 校验 → 保存"。"""

    def __init__(self) -> None:
        self._root = tk.Tk()
        self._root.title("PrivateBot 首次配置")
        self._root.resizable(False, False)
        self._vars: dict[str, tk.StringVar] = {}
        self._status = tk.StringVar(value="填好后可以点「测试连接」先验证密钥是否可用。")
        self._saved = False
        self._build()

    # ---- 界面 ----

    def _build(self) -> None:
        current = selfcheck.read_env()
        frame = ttk.Frame(self._root, padding=16)
        frame.grid(row=0, column=0, sticky="nsew")

        ttk.Label(frame, text="第一次使用，先填这 4 项", font=("", 12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(
            frame,
            text="其它设置都有默认值。以后想改，可以直接用记事本打开同目录下的 .env 文件。",
            wraplength=520,
            justify="left",
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 12))

        row = 2
        for key, label, tip in WIZARD_FIELDS:
            ttk.Label(frame, text=f"{label}：").grid(
                row=row, column=0, sticky="ne", padx=(0, 8), pady=(6, 0)
            )
            var = tk.StringVar(value=current.get(key, ""))
            ttk.Entry(frame, textvariable=var, width=48).grid(
                row=row, column=1, sticky="we", pady=(6, 0)
            )
            ttk.Label(frame, text=tip, wraplength=520, justify="left", foreground="#777777").grid(
                row=row + 1, column=1, sticky="w", pady=(0, 2)
            )
            self._vars[key] = var
            row += 2

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="we", pady=(16, 0))
        self._test_btn = ttk.Button(buttons, text="测试连接", command=self._on_test)
        self._test_btn.pack(side="left")
        ttk.Button(buttons, text="取消", command=self._on_cancel).pack(side="right")
        self._save_btn = ttk.Button(buttons, text="保存并启动", command=self._on_save)
        self._save_btn.pack(side="right", padx=(0, 8))

        ttk.Label(frame, textvariable=self._status, wraplength=520, justify="left").grid(
            row=row + 1, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )

        self._root.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self._root.update_idletasks()
        width = self._root.winfo_width()
        height = self._root.winfo_height()
        x = max((self._root.winfo_screenwidth() - width) // 2, 0)
        y = max((self._root.winfo_screenheight() - height) // 3, 0)
        self._root.geometry(f"+{x}+{y}")
        self._root.lift()
        self._root.attributes("-topmost", True)
        self._root.after(500, lambda: self._root.attributes("-topmost", False))

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self._test_btn.config(state=state)
        self._save_btn.config(state=state)

    # ---- 取值与校验 ----

    def _collect(self) -> dict[str, str]:
        return {key: var.get().strip() for key, var in self._vars.items()}

    def _validate(self, values: dict[str, str]) -> Optional[str]:
        if not values["LLM_API_KEY"]:
            return "请填写 DeepSeek 密钥（LLM_API_KEY）"
        if not values["ALLOWED_QQ"]:
            return "请填写允许聊天的 QQ 号（ALLOWED_QQ）"
        parts = [p.strip() for p in values["ALLOWED_QQ"].split(",") if p.strip()]
        if not parts or any(not p.isdigit() for p in parts):
            return "QQ 号只能是数字，多个用英文逗号分隔，例如：123456,234567"
        if not values["ONEBOT_ACCESS_TOKEN"]:
            return "请填写 OneBot 访问令牌（ONEBOT_ACCESS_TOKEN），需与 NapCat 保持一致"
        port = values["ONEBOT_WS_PORT"]
        if not port.isdigit() or not (1 <= int(port) <= 65535):
            return "端口必须是 1-65535 之间的数字，一般保持默认的 8080"
        return None

    # ---- 事件 ----

    def _on_test(self) -> None:
        values = self._collect()
        if not values["LLM_API_KEY"]:
            self._status.set("请先填写 DeepSeek 密钥，再点「测试连接」。")
            return
        current = selfcheck.read_env()
        base_url = values.get("LLM_BASE_URL") or current.get("LLM_BASE_URL", selfcheck.DEFAULT_BASE_URL)
        model = current.get("LLM_MODEL", selfcheck.DEFAULT_MODEL)
        self._status.set("正在测试，请稍候……")
        self._set_busy(True)
        threading.Thread(
            target=self._test_worker,
            args=(values["LLM_API_KEY"], base_url, model),
            daemon=True,
        ).start()

    def _test_worker(self, api_key: str, base_url: str, model: str) -> None:
        result = selfcheck.test_llm(api_key, base_url, model)
        self._root.after(0, self._test_done, result)

    def _test_done(self, result) -> None:
        self._set_busy(False)
        if result.ok:
            self._status.set("[通过] 密钥可用，可以点「保存并启动」了。")
        else:
            self._status.set(f"[失败] {result.hint}")

    def _on_save(self) -> None:
        values = self._collect()
        error = self._validate(values)
        if error:
            self._status.set(f"[失败] {error}")
            messagebox.showwarning("配置还没填完", error, parent=self._root)
            return
        try:
            save_env(values)
        except OSError as exc:
            messagebox.showerror(
                "保存失败",
                f"无法写入配置文件：\n{exc}\n\n请把整个文件夹挪到非系统盘（例如 D 盘）后重试。",
                parent=self._root,
            )
            return
        self._saved = True
        self._root.destroy()

    def _on_cancel(self) -> None:
        self._saved = False
        self._root.destroy()

    # ---- 对外 ----

    def run(self) -> bool:
        """显示窗口，返回用户是否保存了配置。"""
        self._root.mainloop()
        return self._saved


def run() -> bool:
    """弹出配置向导。环境不支持图形界面时抛 RuntimeError。"""
    if not available():
        raise RuntimeError("当前环境没有 tkinter，无法显示配置窗口")
    try:
        wizard = SetupWizard()
    except tk.TclError as exc:
        raise RuntimeError(f"无法创建配置窗口：{exc}") from exc
    return wizard.run()