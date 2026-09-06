#!/usr/bin/env bash
set -euo pipefail

# 配置互动卡片模板和回调参数，并重启线上 systemd 服务。

ENV_FILE=/etc/dingtalk-tapd-bug-bot.env
TMP_FILE=$(mktemp /tmp/dingtalk-tapd-card-env.XXXXXX)
trap 'rm -f "$TMP_FILE"' EXIT

printf '互动卡片模板 ID: '
read -r TEMPLATE_ID
printf '互动卡片回调 RouteKey: '
read -r ROUTE_KEY
printf '互动卡片回调 Secret（输入时不显示）: '
read -r -s CALLBACK_SECRET
printf '\n'
printf '公网 HTTPS 地址（必填，用于钉钉回调和草稿链接）: '
read -r PUBLIC_URL

if [[ -z "$TEMPLATE_ID" || -z "$ROUTE_KEY" || -z "$CALLBACK_SECRET" ]]; then
  echo '模板 ID、RouteKey、回调 Secret 不能为空。' >&2
  exit 1
fi
if [[ ! "$PUBLIC_URL" =~ ^https://[^[:space:]]+$ ]]; then
  echo '公网地址必须是 HTTPS URL。' >&2
  exit 1
fi

CARD_TEMPLATE_VALUE="$TEMPLATE_ID" CARD_ROUTE_VALUE="$ROUTE_KEY" CARD_SECRET_VALUE="$CALLBACK_SECRET" PUBLIC_URL_VALUE="$PUBLIC_URL" python3 - "$ENV_FILE" "$TMP_FILE" <<'PY'
from pathlib import Path
import os
import sys

env_file = Path(sys.argv[1])
tmp_file = Path(sys.argv[2])
values = {
    "DINGTALK_CARD_TEMPLATE_ID": os.environ["CARD_TEMPLATE_VALUE"],
    "DINGTALK_CARD_CALLBACK_ROUTE_KEY": os.environ["CARD_ROUTE_VALUE"],
    "DINGTALK_CARD_CALLBACK_SECRET": os.environ["CARD_SECRET_VALUE"],
    "PUBLIC_BASE_URL": os.environ["PUBLIC_URL_VALUE"],
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

CARD_CALLBACK_SECRET_VALUE="$CALLBACK_SECRET"
unset CARD_TEMPLATE_VALUE CARD_ROUTE_VALUE CARD_SECRET_VALUE PUBLIC_URL_VALUE
install -o root -g root -m 0600 "$TMP_FILE" "$ENV_FILE"

set -a
. "$ENV_FILE"
set +a

if [[ -z "${DINGTALK_APP_KEY:-}" || -z "${DINGTALK_CLIENT_SECRET:-}" ]]; then
  echo '服务器缺少 DINGTALK_APP_KEY 或 DINGTALK_CLIENT_SECRET，无法注册回调。' >&2
  exit 1
fi

TOKEN_FILE=$(mktemp /tmp/dingtalk-access-token.XXXXXX)
REGISTER_FILE=$(mktemp /tmp/dingtalk-card-register.XXXXXX)
trap 'rm -f "$TMP_FILE" "$TOKEN_FILE" "$REGISTER_FILE"' EXIT

curl -fsSL --max-time 30 -o "$TOKEN_FILE" -X POST https://api.dingtalk.com/v1.0/oauth2/accessToken \
  -H 'content-type: application/json' \
  --data "{\"appKey\":\"$DINGTALK_APP_KEY\",\"appSecret\":\"$DINGTALK_CLIENT_SECRET\"}"
DT_ACCESS_TOKEN=$(python3 - "$TOKEN_FILE" <<'PY'
import json, sys
data=json.load(open(sys.argv[1]))
if not data.get('accessToken'):
    raise SystemExit('钉钉未返回 accessToken: ' + str(data.get('message', 'unknown error')))
print(data['accessToken'])
PY
)

curl -fsSL --max-time 30 -o "$REGISTER_FILE" -X POST https://api.dingtalk.com/v1.0/card/callbacks/register \
  -H "x-acs-dingtalk-access-token: $DT_ACCESS_TOKEN" \
  -H 'content-type: application/json' \
  --data "{\"apiSecret\":\"$CARD_CALLBACK_SECRET_VALUE\",\"callbackUrl\":\"${PUBLIC_URL}/dingtalk/card-callback\",\"callbackRouteKey\":\"$ROUTE_KEY\",\"forceUpdate\":true}"

rm -f "$TOKEN_FILE" "$REGISTER_FILE"
unset DT_ACCESS_TOKEN DINGTALK_APP_SECRET CALLBACK_SECRET CARD_CALLBACK_SECRET_VALUE
systemctl restart dingtalk-tapd-bug-bot.service
sleep 2
systemctl is-active --quiet dingtalk-tapd-bug-bot.service
echo '互动卡片配置和回调注册完成。'
curl -fsSL --max-time 10 http://127.0.0.1:3000/api/agent/status
