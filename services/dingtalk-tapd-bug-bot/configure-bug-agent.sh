#!/usr/bin/env bash
set -euo pipefail

# 交互式写入 Bug Agent 凭据，所有密钥只落到 root-only 的 systemd 环境文件。

ENV_FILE=/etc/dingtalk-tapd-bug-bot.env
TMP_FILE=$(mktemp /tmp/dingtalk-tapd-bug-bot.env.XXXXXX)
trap 'rm -f "$TMP_FILE"' EXIT

printf 'OpenAI API Key（输入时不显示，必填）: '
read -r -s OPENAI_KEY
printf '\n钉钉 AppKey（可见，必填）: '
read -r DINGTALK_KEY
printf '钉钉 RobotCode（可见；不知道可先回车）: '
read -r DINGTALK_ROBOT
printf '新的 TAPD 应用密钥（建议填写；输入时不显示，用于自动刷新 Token）: '
read -r -s TAPD_SECRET
printf '\n新的 TAPD access_token（可选；如果填写应用密钥则不需要）: '
read -r -s TAPD_TOKEN
printf '\n中转站 Base URL（例如 https://relay.example.com/v1；直连官方可回车）: '
read -r OPENAI_BASE
printf '模型名（例如 gpt-5.6 或中转站分配的名称，回车默认 gpt-5.6）: '
read -r OPENAI_MODEL_VALUE
printf '接口模式（auto / responses / chat_completions，回车默认 auto）: '
read -r OPENAI_MODE
printf '\n'

if [[ -z "$OPENAI_KEY" || -z "$DINGTALK_KEY" ]]; then
  echo 'OPENAI_API_KEY 和 DINGTALK_APP_KEY 不能为空。' >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "配置文件不存在：$ENV_FILE" >&2
  exit 1
fi

OPENAI_VALUE="$OPENAI_KEY"
DINGTALK_KEY_VALUE="$DINGTALK_KEY"
DINGTALK_ROBOT_VALUE="$DINGTALK_ROBOT"
TAPD_SECRET_VALUE="$TAPD_SECRET"
TAPD_TOKEN_VALUE="$TAPD_TOKEN"
OPENAI_BASE_VALUE="${OPENAI_BASE:-https://api.openai.com/v1}"
OPENAI_MODEL_VALUE="${OPENAI_MODEL_VALUE:-gpt-5.6}"
OPENAI_MODE_VALUE="${OPENAI_MODE:-auto}"

while IFS= read -r line || [[ -n "$line" ]]; do
  case "$line" in
    OPENAI_API_KEY=*) printf 'OPENAI_API_KEY=%s\n' "$OPENAI_VALUE" ;;
    OPENAI_BASE_URL=*) printf 'OPENAI_BASE_URL=%s\n' "$OPENAI_BASE_VALUE" ;;
    OPENAI_MODEL=*) printf 'OPENAI_MODEL=%s\n' "$OPENAI_MODEL_VALUE" ;;
    OPENAI_API_MODE=*) printf 'OPENAI_API_MODE=%s\n' "$OPENAI_MODE_VALUE" ;;
    DINGTALK_APP_KEY=*) printf 'DINGTALK_APP_KEY=%s\n' "$DINGTALK_KEY_VALUE" ;;
    DINGTALK_ROBOT_CODE=*) if [[ -n "$DINGTALK_ROBOT_VALUE" ]]; then printf 'DINGTALK_ROBOT_CODE=%s\n' "$DINGTALK_ROBOT_VALUE"; else printf '%s\n' "$line"; fi ;;
    TAPD_CLIENT_ID=*) if [[ -n "$TAPD_SECRET_VALUE" ]]; then printf 'TAPD_CLIENT_ID=tapd-app-0409b1\n'; else printf '%s\n' "$line"; fi ;;
    TAPD_CLIENT_SECRET=*) if [[ -n "$TAPD_SECRET_VALUE" ]]; then printf 'TAPD_CLIENT_SECRET=%s\n' "$TAPD_SECRET_VALUE"; else printf '%s\n' "$line"; fi ;;
    TAPD_ACCESS_TOKEN=*) if [[ -n "$TAPD_TOKEN_VALUE" ]]; then printf 'TAPD_ACCESS_TOKEN=%s\n' "$TAPD_TOKEN_VALUE"; else printf '%s\n' "$line"; fi ;;
    *) printf '%s\n' "$line" ;;
  esac
done < "$ENV_FILE" > "$TMP_FILE"

grep -q '^OPENAI_API_KEY=' "$TMP_FILE" || printf 'OPENAI_API_KEY=%s\n' "$OPENAI_VALUE" >> "$TMP_FILE"
grep -q '^OPENAI_BASE_URL=' "$TMP_FILE" || printf 'OPENAI_BASE_URL=%s\n' "$OPENAI_BASE_VALUE" >> "$TMP_FILE"
grep -q '^OPENAI_MODEL=' "$TMP_FILE" || printf 'OPENAI_MODEL=%s\n' "$OPENAI_MODEL_VALUE" >> "$TMP_FILE"
grep -q '^OPENAI_API_MODE=' "$TMP_FILE" || printf 'OPENAI_API_MODE=%s\n' "$OPENAI_MODE_VALUE" >> "$TMP_FILE"
grep -q '^DINGTALK_APP_KEY=' "$TMP_FILE" || printf 'DINGTALK_APP_KEY=%s\n' "$DINGTALK_KEY_VALUE" >> "$TMP_FILE"
grep -q '^DINGTALK_ROBOT_CODE=' "$TMP_FILE" || printf 'DINGTALK_ROBOT_CODE=%s\n' "$DINGTALK_ROBOT_VALUE" >> "$TMP_FILE"
if [[ -n "$TAPD_SECRET_VALUE" ]]; then
  grep -q '^TAPD_CLIENT_ID=' "$TMP_FILE" || printf 'TAPD_CLIENT_ID=tapd-app-0409b1\n' >> "$TMP_FILE"
  grep -q '^TAPD_CLIENT_SECRET=' "$TMP_FILE" || printf 'TAPD_CLIENT_SECRET=%s\n' "$TAPD_SECRET_VALUE" >> "$TMP_FILE"
fi
if [[ -n "$TAPD_TOKEN_VALUE" ]]; then
  grep -q '^TAPD_ACCESS_TOKEN=' "$TMP_FILE" || printf 'TAPD_ACCESS_TOKEN=%s\n' "$TAPD_TOKEN_VALUE" >> "$TMP_FILE"
fi

install -o root -g root -m 0600 "$TMP_FILE" "$ENV_FILE"
systemctl restart dingtalk-tapd-bug-bot.service
sleep 2
systemctl is-active --quiet dingtalk-tapd-bug-bot.service
echo 'Bug Agent 配置已更新，服务已重启。'
curl -fsSL --max-time 10 http://127.0.0.1:3000/api/agent/status
