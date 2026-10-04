@echo off
chcp 936 >nul
setlocal EnableExtensions EnableDelayedExpansion
title 直流会馆机器人 快速启动

rem ============================================================
rem  直流会馆 QQ 机器人 快速启动脚本
rem  流程：检查环境 -> 安装/更新 AstrBot -> 部署/更新插件
rem        -> 启动 AstrBot -> 尝试启动 NapCat -> 打开管理面板
rem  说明：首次使用建议先运行 deploy\windows_install.bat
rem ============================================================

set "ROOT=%~dp0"
set "PLUGIN_NAME=astrbot_plugin_dcs_whosin"
set "RUNTIME=%ROOT%deploy\runtime"
set "NAPCAT_DIR="

echo ==========================================================
echo   直流会馆 QQ 机器人 快速启动
echo ==========================================================
echo.

rem ---------- [1/6] 检查 Python ----------
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python。请先安装 Python 3.10 及以上版本：
    echo        https://www.python.org/downloads/
    start "" https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [1/6] Python 检查通过

rem ---------- [2/6] 检查 / 安装 AstrBot ----------
where astrbot >nul 2>nul
if errorlevel 1 (
    echo [提示] 未检测到 AstrBot，需要先安装。
    choice /c YN /m "是否现在自动安装 AstrBot"
    if errorlevel 2 (
        echo 已取消。请先运行 deploy\windows_install.bat 完成安装。
        pause
        exit /b 1
    )
    echo 正在安装 AstrBot（pip）...
    python -m pip install --upgrade astrbot
    if errorlevel 1 (
        echo [警告] 直连安装失败，改用清华镜像重试...
        python -m pip install --upgrade -i https://pypi.tuna.tsinghua.edu.cn/simple astrbot
    )
)
echo [2/6] AstrBot 检查通过

rem ---------- [3/6] 部署 / 更新插件 ----------
if not exist "%RUNTIME%\data\plugins" mkdir "%RUNTIME%\data\plugins"
robocopy "%ROOT%" "%RUNTIME%\data\plugins\%PLUGIN_NAME%" /E /XD deploy installers NapCat __pycache__ .git /XF *.pyc >nul
if errorlevel 8 (
    echo [警告] 插件文件复制异常，请手动检查：%RUNTIME%\data\plugins\%PLUGIN_NAME%
) else (
    echo [3/6] 插件已部署 / 更新
)

rem ---------- [4/6] 启动 AstrBot ----------
echo [4/6] 启动 AstrBot（新窗口）...
start "AstrBot" /d "%RUNTIME%" cmd /k astrbot

rem ---------- [5/6] 尝试启动 NapCat ----------
for %%D in (
    "%ROOT%NapCat"
    "%ROOT%deploy\NapCat"
    "%USERPROFILE%\NapCat"
    "%USERPROFILE%\Desktop\NapCat"
    "C:\NapCat"
) do (
    if exist "%%~D\launcher.bat" set "NAPCAT_DIR=%%~D"
    if exist "%%~D\launcher-user.bat" set "NAPCAT_DIR=%%~D"
    if exist "%%~D\napcat.ps1" set "NAPCAT_DIR=%%~D"
    if exist "%%~D\NapCatWinBootMain.exe" set "NAPCAT_DIR=%%~D"
)

if defined NAPCAT_DIR (
    echo [5/6] 检测到 NapCat：!NAPCAT_DIR!
    if exist "!NAPCAT_DIR!\launcher.bat" (
        start "NapCat" /d "!NAPCAT_DIR!" cmd /k launcher.bat
    ) else if exist "!NAPCAT_DIR!\napcat.ps1" (
        start "NapCat" /d "!NAPCAT_DIR!" powershell -NoExit -ExecutionPolicy Bypass -File napcat.ps1
    ) else if exist "!NAPCAT_DIR!\NapCatWinBootMain.exe" (
        start "NapCat" /d "!NAPCAT_DIR!" cmd /k NapCatWinBootMain.exe
    ) else (
        start "NapCat" /d "!NAPCAT_DIR!" cmd /k launcher-user.bat
    )
) else (
    echo [5/6] 未找到 NapCat（QQ 协议端，首次需要手动安装）：
    echo        下载：https://github.com/NapNeko/NapCatQQ/releases  （下载 NapCat.Shell.zip）
    echo        解压到：%ROOT%NapCat  然后重新运行本脚本即可自动启动
)

rem ---------- [6/6] 状态清单 ----------
echo.
echo ==========================================================
echo   启动完成！请确认以下内容：
echo.
echo   [浏览器面板]
echo     AstrBot：http://localhost:6185   （初始密码见 AstrBot 窗口日志）
echo     NapCat ：http://localhost:6099   （用于扫码登录 QQ）
echo.
echo   [必做配置 - 只需配置一次]
echo     1. NapCat：网络配置 - 新建 - WebSocket 反向
echo        地址：ws://127.0.0.1:6199/ws
echo     2. AstrBot：消息平台 - 新增 aiocqhttp（OneBot v11）适配器
echo     3. AstrBot：插件页确认 %PLUGIN_NAME% 已启用
echo     4. AstrBot：插件配置 - 若查询提示需要登录，请填写站点 Cookie
echo.
echo   [测试] 群聊内发送：/j
echo ==========================================================
start "" http://localhost:6185
pause
