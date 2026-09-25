@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "VENV_DIR=.build-venv"
set "PY=%VENV_DIR%\Scripts\python.exe"

echo ==========================================
echo   PrivateBot 打包脚本（Windows x64）
echo ==========================================
echo.

echo [1/6] 检查 Python ...
python --version >nul 2>nul
if errorlevel 1 (
    echo [错误] 找不到可用的 python 命令。
    echo        请先安装 Python 3.11 或更高版本，安装时勾选 "Add python.exe to PATH"。
    goto :fail
)

echo [2/6] 准备独立构建环境 %VENV_DIR% ...
rem 用独立环境构建，避免宿主环境里的杂包（例如过时的 pathlib backport）干扰打包
set "DEPS_OK=0"
if exist "%PY%" (
    "%PY%" -c "import PyInstaller" >nul 2>nul
    if not errorlevel 1 set "DEPS_OK=1"
)
if "%DEPS_OK%"=="1" goto :deps_ready

echo     正在创建/补齐构建环境，首次运行会慢一些 ...
if not exist "%PY%" (
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [错误] 创建构建环境失败。
        goto :fail
    )
)
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt -r requirements-build.txt
if errorlevel 1 (
    echo [错误] 安装构建依赖失败，请检查网络后重试。
    goto :fail
)

:deps_ready
echo [3/6] 清理旧产物 ...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [4/6] 开始打包 ...
"%PY%" -m PyInstaller --noconfirm --clean PrivateBot.spec
if errorlevel 1 (
    echo [错误] PyInstaller 打包失败，请查看上面的报错信息。
    goto :fail
)

echo [5/6] 复制随包数据 ...
xcopy /e /i /y "data\personas" "dist\PrivateBot\data\personas" >nul
if errorlevel 1 (
    echo [错误] 复制 data\personas 失败。
    goto :fail
)
xcopy /e /i /y "data\style" "dist\PrivateBot\data\style" >nul
if errorlevel 1 (
    echo [错误] 复制 data\style 失败。
    goto :fail
)
copy /y ".env.example" "dist\PrivateBot\.env.example" >nul

echo [6/6] 生成压缩包 ...
if exist "PrivateBot-win64.zip" del /q "PrivateBot-win64.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\PrivateBot' -DestinationPath 'PrivateBot-win64.zip' -Force"
if errorlevel 1 (
    echo [错误] 压缩失败。
    goto :fail
)

echo.
echo [完成] 目录产物：dist\PrivateBot\
echo        压缩包：PrivateBot-win64.zip
echo.
echo 分发时只需把 PrivateBot-win64.zip 发给对方。
goto :eof

:fail
echo.
echo 打包中断，请按上面的提示处理后重试。
exit /b 1