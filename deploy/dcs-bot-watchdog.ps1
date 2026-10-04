# 直流会馆机器人看门狗
# 每 5 分钟检查一次：
#   1. AstrBot 管理面板（6185）是否在线，掉线则拉起桌面客户端
#   2. NapCat（6099）是否在线，掉线则拉起启动器
#   3. NapCat 的 QQ 是否在线，掉线则尝试快速登录
#   4. NapCat 与 AstrBot 的反向连接（6199）是否存在，僵死则重启 NapCat 触发重连
# 依赖：PowerShell 5.1，无需管理员权限（普通计划任务即可运行）

$ErrorActionPreference = "SilentlyContinue"

$AstrBotExe = Join-Path $env:LOCALAPPDATA "astrbot\astrbot-desktop-tauri.exe"
$NapCatDir = Join-Path (Split-Path $PSScriptRoot -Parent) "NapCat"
if (-not (Test-Path $NapCatDir)) {
    $NapCatDir = "C:\Users\Yoga-Slim-Pro-X\Documents\Default Project\astrbot_plugin_dcs_whosin\NapCat"
}
$QQAccount = "3033615073"
$LogFile = Join-Path $PSScriptRoot "watchdog.log"

function Write-Log([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    if ((Get-Item $LogFile).Length -gt 1048576) {
        $tail = Get-Content $LogFile -Tail 200
        Set-Content -Path $LogFile -Value $tail -Encoding UTF8
    }
}

function Test-Url([string]$Url) {
    try {
        $resp = Invoke-WebRequest -Uri $Url -TimeoutSec 6 -UseBasicParsing
        return ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 500)
    } catch {
        return $false
    }
}

# ---------- 1. AstrBot ----------
$astrBotOk = Test-Url "http://127.0.0.1:6185"
if (-not $astrBotOk) {
    Write-Log "AstrBot 未响应，正在启动桌面客户端..."
    if (Test-Path $AstrBotExe) {
        Start-Process -FilePath $AstrBotExe
        Start-Sleep -Seconds 25
    } else {
        Write-Log "未找到 AstrBot 桌面客户端：$AstrBotExe"
    }
}

# ---------- 2. NapCat ----------
$napcatOk = Test-Url "http://127.0.0.1:6099"
if (-not $napcatOk) {
    Write-Log "NapCat 未响应，正在启动..."
    $launcher = Join-Path $NapCatDir "launcher-user.bat"
    if (Test-Path $launcher) {
        Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "launcher-user.bat" -WorkingDirectory $NapCatDir -WindowStyle Minimized
        Start-Sleep -Seconds 25
    } else {
        Write-Log "未找到 NapCat 启动器：$launcher"
    }
}

# ---------- 3/4. NapCat 内部检查（QQ 登录 + 反向连接） ----------
$webuiConfig = Join-Path $NapCatDir "config\webui.json"
if (Test-Path $webuiConfig) {
    $token = (Get-Content $webuiConfig -Raw -Encoding UTF8 | ConvertFrom-Json).token
    if ($token) {
        try {
            $sha = [System.Security.Cryptography.SHA256]::Create()
            $hash = (($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes("$token.napcat"))) | ForEach-Object { $_.ToString("x2") }) -join ""
            $login = Invoke-RestMethod -Uri "http://127.0.0.1:6099/api/auth/login" -Method Post -Body (@{hash=$hash; totpCode=$null} | ConvertTo-Json) -ContentType "application/json" -TimeoutSec 8
            $credential = $login.data.Credential
            if ($credential) {
                $headers = @{ Authorization = "Bearer $credential" }
                $status = (Invoke-RestMethod -Uri "http://127.0.0.1:6099/api/QQLogin/CheckLoginStatus" -Method Post -Headers $headers -TimeoutSec 8).data

                # 3. QQ 未登录 → 快速登录
                if ($status -and -not $status.isLogin) {
                    Write-Log "QQ 未登录（阶段：$($status.loginPhase)），尝试快速登录 $QQAccount ..."
                    Invoke-RestMethod -Uri "http://127.0.0.1:6099/api/QQLogin/SetQuickLogin" -Method Post -Headers $headers -Body (@{uin=$QQAccount} | ConvertTo-Json) -ContentType "application/json" -TimeoutSec 15 | Out-Null
                    Start-Sleep -Seconds 8
                    $status2 = (Invoke-RestMethod -Uri "http://127.0.0.1:6099/api/QQLogin/CheckLoginStatus" -Method Post -Headers $headers -TimeoutSec 8).data
                    if ($status2 -and $status2.isLogin) {
                        Write-Log "快速登录成功。"
                    } else {
                        Write-Log "快速登录未成功，可能需要手动扫码。"
                    }
                }

                # 4. 反向连接僵死检测：双端在线且 QQ 已登录但 6199 无 ESTABLISHED → 重启 NapCat 触发重连
                $astrBotAlive = Test-Url "http://127.0.0.1:6185"
                $napcatAlive = Test-Url "http://127.0.0.1:6099"
                $qqLoggedIn = $false
                try {
                    $statusNow = (Invoke-RestMethod -Uri "http://127.0.0.1:6099/api/QQLogin/CheckLoginStatus" -Method Post -Headers $headers -TimeoutSec 8).data
                    $qqLoggedIn = [bool]($statusNow -and $statusNow.isLogin)
                } catch { $qqLoggedIn = $false }
                if ($astrBotAlive -and $napcatAlive -and $qqLoggedIn) {
                    $conn = netstat -ano | Select-String ":6199\s.*ESTABLISHED"
                    if (-not $conn) {
                        Write-Log "检测到 6199 反向连接缺失，重启 NapCat 触发重连..."
                        try {
                            Invoke-RestMethod -Uri "http://127.0.0.1:6099/api/QQLogin/RestartNapCat" -Method Post -Headers $headers -TimeoutSec 20 | Out-Null
                            Write-Log "已发送重启指令。"
                        } catch {
                            Write-Log "NapCat 重启失败：$($_.Exception.Message)"
                        }
                    }
                } elseif ($astrBotAlive -and $napcatAlive -and -not $qqLoggedIn) {
                    Write-Log "QQ 未登录（阶段：$($statusNow.loginPhase)），等待手动扫码或快速登录恢复。"
                }
            }
        } catch {
            Write-Log "NapCat 内部检查失败：$($_.Exception.Message)"
        }
    }
}
