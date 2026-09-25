# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（Stage 9）。

- 单目录（onedir）模式：启动快，且 .env / data 就在 exe 旁边，用户看得见、能备份
- console=True：保留黑窗口实时显示日志，出问题时有迹可循
- datas 留空：data/ 不内嵌，由 build.bat 在构建后复制，用户可直接编辑人设与语料

用法：python -m PyInstaller --noconfirm --clean PrivateBot.spec
"""

import sys
from pathlib import Path

# Anaconda 把 Tcl/Tk 动态库放在 Library\bin（而非 DLLs 目录），
# PyInstaller 因此解析不到 _tkinter.pyd 的依赖，打包后配置向导会直接失效。
# 这里显式收集，保证 exe 里的 tkinter 可用。
_tk_binaries: list = []
_prefix = Path(sys.base_prefix)
for _dir in (_prefix / "DLLs", _prefix / "Library" / "bin", _prefix / "lib"):
    for _name in ("tcl86t.dll", "tk86t.dll"):
        _path = _dir / _name
        if _path.exists():
            _tk_binaries.append((str(_path), "."))

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=_tk_binaries,
    datas=[],
    hiddenimports=[
        # websockets 内部有惰性导入，显式声明避免打包后运行时报缺失
        "websockets.asyncio.server",
        "websockets.asyncio.client",
        "websockets.legacy.server",
        "dotenv",
        "yaml",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PrivateBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PrivateBot",
)