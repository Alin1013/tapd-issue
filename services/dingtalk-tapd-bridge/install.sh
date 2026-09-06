#!/usr/bin/env bash
set -euo pipefail

# 在 systemd 主机上安装 Python Bridge；脚本只创建缺失配置，不覆盖已有密钥。

APP_USER="${APP_USER:-dingtalkbot}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/dingtalk-tapd}"
STATE_DIR="${STATE_DIR:-/var/lib/dingtalk-tapd}"
ENV_FILE="${ENV_FILE:-/etc/dingtalk-tapd-listen.env}"
SERVICE_NAME="${SERVICE_NAME:-dingtalk-tapd-listen.service}"
START_SERVICE="${START_SERVICE:-0}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo '请使用 root 运行安装脚本。' >&2
  exit 1
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo '未找到 python3；请先安装 Python 3.11 或更高版本。' >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo '当前系统没有 systemctl，Bridge 安装脚本只支持 systemd 主机。' >&2
  exit 1
fi

# 独立系统用户承载 DWS 登录态和运行数据，避免服务直接使用 root 凭据。
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$STATE_DIR" --create-home \
    --shell /usr/sbin/nologin "$APP_USER"
fi
APP_GROUP="$(id -gn "$APP_USER")"
install -d -o root -g root -m 0750 "$INSTALL_ROOT"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$STATE_DIR"

# 从仓库部署到固定目录时只复制运行所需源文件，避免把本地密钥、虚拟环境和状态库带上服务器。
if [[ "$REPO_ROOT" != "$INSTALL_ROOT" ]]; then
  install -d -o root -g root -m 0750 "$INSTALL_ROOT/src"
  cp -a "$REPO_ROOT/src/." "$INSTALL_ROOT/src/"
  install -o root -g root -m 0644 "$REPO_ROOT/pyproject.toml" "$INSTALL_ROOT/pyproject.toml"
  install -o root -g root -m 0644 "$REPO_ROOT/README.md" "$INSTALL_ROOT/README.md"
fi

if [[ ! -x "$INSTALL_ROOT/.venv/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$INSTALL_ROOT/.venv"
fi
"$INSTALL_ROOT/.venv/bin/python" -m pip install --disable-pip-version-check --no-input --editable "$INSTALL_ROOT"
install -o root -g root -m 0644 \
  "$REPO_ROOT/services/dingtalk-tapd-bridge/dingtalk-tapd-listen.service.example" \
  "/etc/systemd/system/$SERVICE_NAME"

# 环境文件是唯一需要人工填入凭据的文件；重复部署时保留现有内容，防止误覆盖线上配置。
if [[ ! -e "$ENV_FILE" ]]; then
  install -o root -g "$APP_GROUP" -m 0640 \
    "$REPO_ROOT/services/dingtalk-tapd-bridge/.env.example" "$ENV_FILE"
else
  chown root:"$APP_GROUP" "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
fi

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
if [[ "$START_SERVICE" == "1" ]]; then
  systemctl restart "$SERVICE_NAME"
  systemctl is-active --quiet "$SERVICE_NAME"
  echo "已启动 $SERVICE_NAME。"
else
  echo "已安装 $SERVICE_NAME；填好 $ENV_FILE 并完成 DWS 登录后执行："
  echo "  systemctl enable --now $SERVICE_NAME"
fi
