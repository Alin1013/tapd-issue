#!/usr/bin/env bash
set -euo pipefail

# 在 systemd 主机上安装 Node Bug Agent；脚本保留已有环境文件，不把密钥写入应用目录。

APP_USER="${APP_USER:-dingtalkbot}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/dingtalk-tapd-bug-bot}"
STATE_DIR="${STATE_DIR:-/var/lib/dingtalk-tapd-bug-bot}"
ENV_FILE="${ENV_FILE:-/etc/dingtalk-tapd-bug-bot.env}"
SERVICE_NAME="${SERVICE_NAME:-dingtalk-tapd-bug-bot.service}"
START_SERVICE="${START_SERVICE:-0}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
NODE_BIN="${NODE_BIN:-$(command -v node || true)}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo '请使用 root 运行安装脚本。' >&2
  exit 1
fi
if [[ -z "$NODE_BIN" || ! -x "$NODE_BIN" ]]; then
  echo '未找到 node；请先安装 Node.js 18 或更高版本。' >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo '当前系统没有 systemctl，Node Bug Agent 安装脚本只支持 systemd 主机。' >&2
  exit 1
fi
NODE_MAJOR="$($NODE_BIN -p 'process.versions.node.split(".")[0]')"
if (( NODE_MAJOR < 18 )); then
  echo "Node.js 版本过低：$NODE_MAJOR；需要 18 或更高版本。" >&2
  exit 1
fi

# 两个服务可以共用系统用户，但必须使用不同的状态目录和 systemd 单元。
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$STATE_DIR" --create-home \
    --shell /usr/sbin/nologin "$APP_USER"
fi
APP_GROUP="$(id -gn "$APP_USER")"
install -d -o root -g root -m 0750 "$INSTALL_ROOT"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$STATE_DIR"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$STATE_DIR/media"

# Node 服务没有第三方 npm 依赖，只复制入口、清单和维护脚本即可完成升级。
# 源码已经在目标目录时跳过自复制，避免 GNU install 把同一文件判定为目标冲突。
if [[ "$SCRIPT_DIR" != "$INSTALL_ROOT" ]]; then
  install -o root -g root -m 0644 "$SCRIPT_DIR/server.js" "$SCRIPT_DIR/package.json" "$INSTALL_ROOT/"
  for helper in configure-bug-agent.sh configure-interactive-card.sh configure-tapd-credentials.sh; do
    install -o root -g root -m 0750 "$SCRIPT_DIR/$helper" "$INSTALL_ROOT/$helper"
  done
fi
"$NODE_BIN" --check "$INSTALL_ROOT/server.js"
install -o root -g root -m 0644 \
  "$SCRIPT_DIR/dingtalk-tapd-bug-bot.service.example" \
  "/etc/systemd/system/$SERVICE_NAME"

# 首次安装生成模板；后续运行只修正权限，绝不覆盖生产凭据。
if [[ ! -e "$ENV_FILE" ]]; then
  install -o root -g root -m 0600 "$SCRIPT_DIR/.env.example" "$ENV_FILE"
else
  chown root:root "$ENV_FILE"
  chmod 0600 "$ENV_FILE"
fi

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
if [[ "$START_SERVICE" == "1" ]]; then
  systemctl restart "$SERVICE_NAME"
  systemctl is-active --quiet "$SERVICE_NAME"
  echo "已启动 $SERVICE_NAME。"
else
  echo "已安装 $SERVICE_NAME；填好 $ENV_FILE 后执行："
  echo "  systemctl enable --now $SERVICE_NAME"
fi
