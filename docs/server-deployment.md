# 服务器部署手册

本文描述 Linux/systemd 主机上的双服务部署。Python Bridge 负责 DWS 群消息监听、历史同步和 SQLite 幂等账本；Node Bug Agent 负责钉钉机器人回调、媒体分析和 TAPD 写入。两者是独立进程，不能把同一个群同时交给两个“自动写 TAPD”入口，否则只能按消息 ID 去重，无法阻止跨进程的语义重复工单。

## 1. 目录和服务

| 组件 | 应用目录 | 运行数据 | systemd 单元 | 端口 |
| --- | --- | --- | --- | --- |
| Python Bridge | `/opt/dingtalk-tapd` | `/var/lib/dingtalk-tapd` | `dingtalk-tapd-listen.service` | 无 |
| Node Bug Agent | `/opt/dingtalk-tapd-bug-bot` | `/var/lib/dingtalk-tapd-bug-bot` | `dingtalk-tapd-bug-bot.service` | `3000` |

环境文件分别为 `/etc/dingtalk-tapd-listen.env` 和 `/etc/dingtalk-tapd-bug-bot.env`。真实 token、密码、AppSecret 和 HMAC 密钥只放环境文件或密钥管理系统，不提交到 Git。

## 2. 前置条件

- Linux 主机已安装 `systemd`、`python3`（3.11+）、Node.js 18+、`git` 和 `dws`。
- DWS CLI 能在服务器访问钉钉，TAPD OAuth 应用拥有目标 workspace 的字段读取、成员读取和 Bug 创建权限。
- 钉钉机器人回调必须使用公网 HTTPS；`10.201.0.151` 是私网地址，不能直接填写到钉钉后台。
- 预先确认只启用一条针对目标群的自动写入链路。使用 Python `listen` 时，Node 端不要再接收同群的自动建单事件；使用 Node Agent 时，Python 只运行 `agent-listen` 或不监听该群。

## 3. 安装应用

在代码仓库根目录执行。脚本默认只安装和启用单元，不会在未填凭据时启动服务；重复运行不会覆盖已有环境文件。

```bash
sudo INSTALL_ROOT=/opt/dingtalk-tapd \
  services/dingtalk-tapd-bridge/install.sh

sudo INSTALL_ROOT=/opt/dingtalk-tapd-bug-bot \
  services/dingtalk-tapd-bug-bot/install.sh
```

如果应用源代码已经位于目标目录，也可以直接从该目录运行对应脚本。需要在配置完成后立即重启服务时显式设置 `START_SERVICE=1`，不要把这个选项用于第一次安装。

脚本完成以下工作：创建 `dingtalkbot` 系统用户和状态目录、创建 Python 虚拟环境并以 editable 模式安装 Bridge、复制 Node 入口和维护脚本、安装 systemd unit、以 0640/0600 权限创建环境模板，并执行 Node 语法检查。

## 4. 配置环境

### Python Bridge

编辑 `/etc/dingtalk-tapd-listen.env`，至少确认以下项目：

```text
DWS_EXECUTABLE=/usr/local/bin/dws
DWS_PROFILE=
TAPD_BACKEND=rest
TAPD_API_BASE_URL=https://api.tapd.cn
TAPD_CLIENT_ID=<TAPD 应用 ID>
TAPD_CLIENT_SECRET=<TAPD 应用密钥>
DINGTALK_TAPD_GROUPS=[{"id":"<openConversationId>","name":"<群名>"}]
DINGTALK_TAPD_WORKSPACE_ID=<workspace ID>
DINGTALK_TAPD_STATE_DB=.dingtalk-tapd/state.sqlite3
DINGTALK_TAPD_ATTACHMENT_DIR=.dingtalk-tapd/attachments
```

`DINGTALK_TAPD_GROUPS` 必须是合法 JSON；ID 必须来自 DWS 的真实返回。`listen` 和 `sync` 不要求消息 @ 机器人，只有正文或 OCR 命中企业知识中心主题时才写入 TAPD。

以服务用户完成 DWS 登录并检查登录态：

```bash
sudo -u dingtalkbot -H env HOME=/var/lib/dingtalk-tapd dws auth status
```

若尚未登录，按当前 DWS CLI 的登录流程在该用户下完成授权，再重复 `auth status`。不要把登录态复制到仓库或其他用户的 HOME。

### Node Bug Agent

编辑 `/etc/dingtalk-tapd-bug-bot.env`，至少配置：

```text
PORT=3000
MEDIA_DIR=/var/lib/dingtalk-tapd-bug-bot/media
TAPD_WORKSPACE_ID=<workspace ID>
TAPD_CLIENT_ID=<TAPD 应用 ID>
TAPD_CLIENT_SECRET=<TAPD 应用密钥>
OPENAI_API_KEY=<模型服务密钥>
OPENAI_BASE_URL=<模型服务地址>/v1
OPENAI_MODEL=<模型服务实际支持的模型 ID>
DINGTALK_CLIENT_SECRET=<钉钉机器人签名密钥>
DINGTALK_FORM_SECRET=<随机生成的 32 位以上密钥>
AGENT_INGEST_SECRET=<与 Bridge 配置相同的随机密钥>
PUBLIC_BASE_URL=https://<公网域名>
```

如果只使用 Python `listen`，Node 的 `AGENT_INGEST_SECRET` 仍建议配置，但不要让 Node 和 Python 同时监听同一群。`TAPD_AUTO_CREATE_BUGS=false` 才会进入草稿/人工确认模式；生产自动建单保持默认 `true` 前，应先用单条测试消息验收。

## 5. 启动和验收

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dingtalk-tapd-listen.service
sudo systemctl enable --now dingtalk-tapd-bug-bot.service

sudo systemctl is-active dingtalk-tapd-listen.service
sudo systemctl is-active dingtalk-tapd-bug-bot.service
curl -fsS http://127.0.0.1:3000/healthz
curl -fsS http://127.0.0.1:3000/api/agent/status
```

首次启动先看日志：

```bash
sudo journalctl -u dingtalk-tapd-listen.service -n 100 --no-pager
sudo journalctl -u dingtalk-tapd-bug-bot.service -n 100 --no-pager
```

Bridge 至少应出现两个目标群的 `event ready`；Node 的健康检查只证明 HTTP 进程存活，不代表 TAPD、模型或钉钉回调凭据已配置完成。

## 6. 更新发布

更新前先备份 Bridge 的 SQLite 账本和环境文件；账本是消息级幂等依据，不能随代码发布删除。

```bash
sudo install -D -m 0640 -o root -g dingtalkbot \
  /etc/dingtalk-tapd-listen.env \
  /var/backups/dingtalk-tapd/listen.env.$(date +%Y%m%d%H%M%S)
sudo install -D -m 0600 -o root -g root \
  /etc/dingtalk-tapd-bug-bot.env \
  /var/backups/dingtalk-tapd/bug-bot.env.$(date +%Y%m%d%H%M%S)
sudo cp -a /var/lib/dingtalk-tapd/.dingtalk-tapd/state.sqlite3 \
  /var/backups/dingtalk-tapd/state.sqlite3.$(date +%Y%m%d%H%M%S)
```

拉取新代码后重复运行两个安装脚本。脚本会保留环境文件，随后按顺序重启：

```bash
sudo systemctl restart dingtalk-tapd-listen.service
sudo systemctl restart dingtalk-tapd-bug-bot.service
sudo systemctl status --no-pager dingtalk-tapd-listen.service dingtalk-tapd-bug-bot.service
```

如果环境变量发生变化，先完成文件校验和权限检查，再重启对应单元。不要用 `systemctl edit` 把密钥写入 drop-in，避免凭据散落在多个位置。

## 7. 历史同步与完整性

历史读取默认拒绝 `partial` 结果。DWS 某些后端在跨日或高消息量范围中可能无法返回可靠分页元数据，建议按自然日执行并逐次检查 `complete`、`hasMore` 和 `failures`：

```bash
sudo -u dingtalkbot -H env HOME=/var/lib/dingtalk-tapd \
  /opt/dingtalk-tapd/.venv/bin/dingtalk-tapd sync \
  --start '2026-09-05T00:00:00+08:00' \
  --end '2026-09-06T00:00:00+08:00' \
  --order asc
```

只有确认某个窗口的分页完整，或明确接受漏数风险时，才追加 `--allow-partial`。已处理消息由 SQLite 账本去重；未知 TAPD 写入结果不要直接重试，先按消息 ID、标题和 TAPD 现有 Bug 核对是否已经成功。

## 8. 停止、切换和故障排查

```bash
sudo systemctl stop dingtalk-tapd-listen.service
sudo systemctl stop dingtalk-tapd-bug-bot.service
sudo journalctl -fu dingtalk-tapd-listen.service
sudo journalctl -fu dingtalk-tapd-bug-bot.service
```

- `event ready` 缺失：检查 `dws auth status`、`DWS_PROFILE`、目标群 ID 和服务用户的 `HOME`。
- `partial` 或 `pagination_error`：缩小到自然日或更短窗口，保存失败 ledger，不要把空结果当作完整历史。
- TAPD 401/403：检查 OAuth client、workspace 权限和服务时间；不要把 token 写入日志。
- Node 端口占用：检查 `ss -ltnp | grep ':3000'`，确认只有一个 Node 单元监听。
- Bug 已创建但附件失败：保留 TAPD Bug ID，单独检查媒体目录权限和 TAPD 附件权限，不回滚已创建工单。

完整配置项和业务默认值参见 [配置说明](configuration.md)；日常操作参见 [操作手册](operations.md)。
