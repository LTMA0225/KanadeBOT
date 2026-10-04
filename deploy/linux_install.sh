#!/usr/bin/env bash
# ============================================================
#  直流会馆 QQ 机器人一键部署脚本（Linux / macOS）
#  自动完成：安装 AstrBot、部署插件；并输出 NapCat 对接步骤
#  依赖：Python 3.10+ / 网络可访问 PyPI 与 GitHub
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME="$SCRIPT_DIR/runtime"
PLUGIN_NAME="astrbot_plugin_dcs_whosin"

echo "=========================================================="
echo "  直流会馆 QQ 机器人一键部署（AstrBot + NapCat）"
echo "=========================================================="

# ---------- [1/4] 检查 Python ----------
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[错误] 未检测到 Python，请先安装 Python 3.10 及以上版本"
  exit 1
fi
echo "[1/4] 使用 Python：$($PY --version 2>&1)"

# ---------- [2/4] 安装 / 更新 AstrBot ----------
echo "[2/4] 安装 / 更新 AstrBot（pip 方式）..."
$PY -m pip install --upgrade pip >/dev/null 2>&1 || true
if ! $PY -m pip install --upgrade astrbot; then
  echo "[警告] 直连安装失败，尝试使用清华镜像重试..."
  $PY -m pip install --upgrade -i https://pypi.tuna.tsinghua.edu.cn/simple astrbot
fi

# ---------- [3/4] 准备运行时目录并部署插件 ----------
echo "[3/4] 部署插件到运行时目录..."
mkdir -p "$RUNTIME/data/plugins"
rm -rf "$RUNTIME/data/plugins/$PLUGIN_NAME"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --exclude deploy --exclude runtime --exclude NapCat --exclude __pycache__ --exclude .git \
    "$PLUGIN_ROOT/" "$RUNTIME/data/plugins/$PLUGIN_NAME/"
else
  cp -r "$PLUGIN_ROOT" "$RUNTIME/data/plugins/$PLUGIN_NAME"
  rm -rf "$RUNTIME/data/plugins/$PLUGIN_NAME/deploy" \
         "$RUNTIME/data/plugins/$PLUGIN_NAME/runtime" \
         "$RUNTIME/data/plugins/$PLUGIN_NAME/NapCat" \
         "$RUNTIME/data/plugins/$PLUGIN_NAME/__pycache__"
fi

# ---------- [4/4] 输出后续步骤 ----------
cat <<'EOF'
[4/4] 部署完成！后续步骤：

  1. 启动 AstrBot：
       cd <本脚本目录>/runtime && astrbot
     浏览器打开管理面板：http://localhost:6185

  2. 安装并启动 NapCat（QQ 协议端），登录机器人 QQ：
       https://github.com/NapNeko/NapCatQQ/releases

  3. NapCat WebUI（默认 http://localhost:6099）：
       网络配置 - 新建 - WebSocket 反向
       URL 填写：ws://127.0.0.1:6199/ws

  4. AstrBot WebUI：
       消息平台 - 新增 aiocqhttp（OneBot v11）适配器（反向 WS 端口 6199）
       插件页确认 astrbot_plugin_dcs_whosin 已加载并启用

  5. 群聊内发送 /j 测试（唤醒前缀可在 AstrBot 配置中调整）
EOF
