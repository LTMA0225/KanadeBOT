@echo off
chcp 936 >nul
setlocal EnableExtensions
title 直流会馆机器人 一键部署（Windows）

rem ============================================================
rem  直流会馆 QQ 机器人一键部署脚本（Windows）
rem  自动完成：安装 AstrBot、部署插件、下载桌面客户端安装包（可选）
rem  依赖：Python 3.10+ / 网络可访问 PyPI 与 GitHub
rem ============================================================

set "SCRIPT_DIR=%~dp0"
set "PLUGIN_ROOT=%SCRIPT_DIR%.."
set "RUNTIME=%SCRIPT_DIR%runtime"
set "PLUGIN_NAME=astrbot_plugin_dcs_whosin"

echo ==========================================================
echo   直流会馆 QQ 机器人一键部署（AstrBot + NapCat）
echo ==========================================================
echo.

rem ---------- [1/5] 检查 Python ----------
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10 及以上版本：
    echo        https://www.python.org/downloads/
    start "" https://www.python.org/downloads/
    pause
    exit /b 1
)
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo [1/5] 已检测到 Python %PYVER%

rem ---------- [2/5] 安装 / 更新 AstrBot ----------
echo [2/5] 安装 / 更新 AstrBot（pip 方式）...
python -m pip install --upgrade pip >nul 2>nul
python -m pip install --upgrade astrbot
if errorlevel 1 (
    echo [警告] 直连安装失败，尝试使用清华镜像重试...
    python -m pip install --upgrade -i https://pypi.tuna.tsinghua.edu.cn/simple astrbot
)

rem ---------- [3/5] 准备运行时目录并部署插件 ----------
echo [3/5] 部署插件到运行时目录...
if not exist "%RUNTIME%\data\plugins" mkdir "%RUNTIME%\data\plugins"
robocopy "%PLUGIN_ROOT%" "%RUNTIME%\data\plugins\%PLUGIN_NAME%" /E /XD deploy runtime NapCat __pycache__ .git /XF *.pyc >nul
if errorlevel 8 (
    echo [警告] 复制插件时出现问题，请手动将插件目录复制到：
    echo        %RUNTIME%\data\plugins\%PLUGIN_NAME%
)

rem ---------- [4/5] 下载 AstrBot 桌面客户端安装包（可选） ----------
echo [4/5] 尝试下载 AstrBot 桌面客户端安装包...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-RestMethod -Uri 'https://api.github.com/repos/AstrBotDevs/AstrBot-desktop/releases/latest' -Headers @{ 'User-Agent' = 'dcs-bot-installer' }; $a = $r.assets | Where-Object { $_.name -match '(?i)win.*\.(exe|msi)$' } | Select-Object -First 1; if ($a) { New-Item -ItemType Directory -Force -Path '%SCRIPT_DIR%installers' | Out-Null; Invoke-WebRequest -Uri $a.browser_download_url -OutFile ('%SCRIPT_DIR%installers\' + $a.name); Write-Host ('已下载桌面客户端：' + $a.name) } else { Write-Host '未找到 Windows 安装包，请前往 releases 页面手动下载' } } catch { Write-Host '桌面客户端下载失败（网络原因），可稍后手动下载' }"
echo        发布页：https://github.com/AstrBotDevs/AstrBot-desktop/releases

rem ---------- [5/5] 输出后续步骤 ----------
echo [5/5] 部署完成！后续步骤：
echo.
echo   1. 启动 AstrBot（在下方询问时选择 Y，或手动执行）：
echo        cd /d "%RUNTIME%"
echo        astrbot
echo      浏览器打开管理面板：http://localhost:6185
echo.
echo   2. 安装并启动 NapCat（QQ 协议端），登录机器人 QQ：
echo        https://github.com/NapNeko/NapCatQQ/releases
echo.
echo   3. NapCat WebUI（默认 http://localhost:6099）：
echo        网络配置 - 新建 - WebSocket 反向
echo        URL 填写：ws://127.0.0.1:6199/ws
echo.
echo   4. AstrBot WebUI：
echo        消息平台 - 新增 aiocqhttp（OneBot v11）适配器（反向 WS 端口 6199）
echo        插件页确认 %PLUGIN_NAME% 已加载并启用
echo.
echo   5. 群聊内发送 /j 测试（唤醒前缀可在 AstrBot 配置中调整）
echo.
choice /c YN /m "是否现在启动 AstrBot"
if errorlevel 2 goto :end
start "AstrBot" cmd /k "cd /d "%RUNTIME%" && astrbot"
:end
echo 脚本执行完毕。
pause
