# Docker 部署（AstrBot 官方镜像 + agent-wechat 原生镜像）

上游文档只覆盖源码运行的 AstrBot。插件写死了 `http://127.0.0.1:6174`，而 `wx serve` 也只接受回环地址监听，所以在 Docker 中让桥接容器**共用 AstrBot 容器的网络命名空间**即可，不需要改代码、也不对外开放端口。以下配置在 AstrBot v4.29.4（ARM64）上实测：文字收发先在 agent-wechat v1.1.0、后在 v1.5.1 验证；发送图片需要 v1.3 及以上（实测 v1.5.1，原地升级保留登录态，升级前请备份 `wechat/data`）。

## 1. 目录与配置

在 compose 目录（含 AstrBot 的 `./data`）下：

```bash
sudo install -d -o 10001 -g 10001 -m 700 wechat/data wechat/cache data/wechat_bridge
sudo install -d -m 700 data/wechat_personal
# 桥接要求显式 tokenFile 事先存在：64 位十六进制 + 换行，属主 10001，权限 600
sudo sh -c 'umask 077; openssl rand -hex 32 > data/wechat_bridge/api-token'
sudo chown 10001:10001 data/wechat_bridge/api-token
```

`wechat/server.toml`：

```toml
schemaVersion = 1
listen = "127.0.0.1:6174"
dataDir = "/var/lib/agent-wechat"
tokenFile = "/run/agent-wechat/api-token"
```

## 2. compose 服务

```yaml
  wechat-bridge:
    image: ghcr.io/thisnick/agent-wechat-native:v1.5.1
    restart: always
    network_mode: "service:astrbot"   # 与 AstrBot 共用 127.0.0.1
    user: "10001:10001"
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    tmpfs: [/tmp:rw,noexec,nosuid,size=64m,mode=1777]
    volumes:
      - ./wechat/data:/var/lib/agent-wechat        # 登录态与消息库
      - ./wechat/cache:/var/cache/agent-wechat
      - ./wechat/server.toml:/etc/agent-wechat/server/config.toml:ro
      - ./data/wechat_bridge:/run/agent-wechat     # AstrBot 以 /AstrBot/data/wechat_bridge/api-token 读取
    stop_grace_period: 30s
    depends_on:
      astrbot:
        condition: service_started
        restart: true
```

**注意：** AstrBot 容器每次重启都会得到新的网络命名空间，桥接容器必须随后重启（`docker compose restart wechat-bridge`），否则插件报 `endpoint_unavailable`/连接被拒。`depends_on.restart` 只对 compose 命令生效；崩溃或宿主机重启时可用一个每分钟比较两个容器 `/proc/<pid>/ns/net` 的 systemd timer 自动处理。桥接自身重启后会自动重新登录在线。

## 3. 登录与 CLI

在容器内执行 CLI 时用 `--server`/`--token-file`，**不要用 `--config` 指向只读挂载的文件**（登录回执会写到配置文件所在目录，报 `Read-only file system`）：

```bash
WX="docker exec wechat-bridge wx --server http://127.0.0.1:6174 --token-file /run/agent-wechat/api-token"
$WX --json accounts create --alias bot
$WX --account bot --json auth login --detach --strategy qr --qr-refresh manual --timeout 300
$WX --account bot --json auth qr OPERATION_ID --output /var/cache/agent-wechat/login.png
$WX --account bot --json account status
```

首次扫码成功后若仍显示 `disconnected`，执行一次 `$WX --account bot --json account connect`。

微信的电脑类登录依附手机：手机退出或切换走这个小号，服务器会把桥接踢下线（事件 `account.connection`，`serviceCode -13`）。重新登录先试 `auth login --strategy saved-device`（手机确认即可）；若返回 `needs_login`，说明已保存的设备凭证被作废，需要重新扫码。建议用安卓「应用分身」或备用机让小号常驻登录。

## 4. 平台配置

`data/cmd_config.json` 的 `platform` 中追加（容器内路径）：

```json
{
  "id": "wechat_personal",
  "type": "native_wechat",
  "enable": true,
  "server_url": "http://127.0.0.1:6174",
  "account_id": "account-REPLACE_ME",
  "token_file": "/AstrBot/data/wechat_bridge/api-token",
  "state_file": "/AstrBot/data/wechat_personal/wechat_personal-state.json",
  "allowed_groups": ["123456789@chatroom"],
  "chat_provider_id": "你的提供商/模型",
  "context_messages": true
}
```

编辑前先停止 AstrBot 容器，启动后再重启桥接容器。
