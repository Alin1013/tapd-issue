#!/usr/bin/env bash
set -euo pipefail

# 更新 TAPD OAuth 凭据时使用临时文件，避免半写入配置被服务读取。

ENV_FILE=/etc/dingtalk-tapd-bug-bot.env
TMP_FILE=$(mktemp /tmp/dingtalk-tapd-tapd-env.XXXXXX)
trap 'rm -f "$TMP_FILE"' EXIT

if [[ ! -f "$ENV_FILE" ]]; then
  echo "配置文件不存在：$ENV_FILE" >&2
  exit 1
fi

printf '请输入 TAPD 应用密钥（输入时不显示）: '
read -r -s TAPD_SECRET
printf '\n'
if [[ -z "$TAPD_SECRET" ]]; then
  echo '应用密钥不能为空。' >&2
  exit 1
fi

TAPD_CLIENT_SECRET="$TAPD_SECRET" python3 - <<'PY' "$ENV_FILE" "$TMP_FILE"
from pathlib import Path
import os
import sys

env_file = Path(sys.argv[1])
tmp_file = Path(sys.argv[2])
secret = os.environ["TAPD_CLIENT_SECRET"]
values = {
    "TAPD_CLIENT_ID": "tapd-app-0409b1",
    "TAPD_CLIENT_SECRET": secret,
    "TAPD_ACCESS_TOKEN": "",
}
lines = env_file.read_text().splitlines()
for key, value in values.items():
    for index, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[index] = key + "=" + value
            break
    else:
        lines.append(key + "=" + value)
tmp_file.write_text("\n".join(lines) + "\n")
tmp_file.chmod(0o600)
PY

unset TAPD_SECRET TAPD_CLIENT_SECRET
install -o root -g root -m 0600 "$TMP_FILE" "$ENV_FILE"
systemctl restart dingtalk-tapd-bug-bot.service
sleep 2
systemctl is-active --quiet dingtalk-tapd-bug-bot.service
echo 'TAPD 自动刷新配置完成，服务已重启。'
curl -fsSL --max-time 10 http://127.0.0.1:3000/api/agent/status
