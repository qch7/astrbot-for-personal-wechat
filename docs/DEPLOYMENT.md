# Ubuntu 部署与运行

本指南面向单机源码运行的 AstrBot，实测版本为 4.29.3 / Python 3.12。桥接固定运行在宿主机 `127.0.0.1:6174`。Docker 的 `127.0.0.1` 指向容器自己，本版本没有提供容器跨网络部署方案。

## 1. 准备 AstrBot 和模型

先按 [AstrBot 官方项目](https://github.com/AstrBotDevs/AstrBot) 安装，确认 WebUI 可用，配置一个能实际回复的聊天模型。记录模型在 AstrBot 中的**完整提供商 ID**，例如 `你的提供商名称/qwen-flash`；不能只写模型名。

本插件要求 `agent_runner.runner_type` 为 `local`，用 AstrBot 自己调用模型。模型凭据仍由 AstrBot 管理，不要写进本仓库。

## 2. 下载本项目并安装桥接

```bash
git clone https://github.com/shuizhaodemumu/astrbot-for-personal-wechat.git
cd astrbot-for-personal-wechat

node --version
npm --version
ldd --version
```

上游 CLI 1.1.0 要求 Node.js >=24.16；其 Linux GNU 发布物要求 glibc >=2.36。请先阅读 [来源与许可说明](../OPEN_SOURCE.md)，再从上游独立获取桥接。以下命令下载 npm 包，并由上游安装脚本下载、校验原生可执行文件：

```bash
npm install --prefix "$PWD/.runtime" @agent-wechat/cli@1.1.0
python3 scripts/setup_bridge.py
bash scripts/wx --json daemon start
bash scripts/wx --json daemon status
```

`.runtime/` 被 Git 忽略。配置脚本以私有权限创建 `server.toml`、`client.toml` 和 `private/`，不会覆盖已有配置，也不会接管桌面微信资料。API 令牌由桥接启动时创建。

如自定义运行目录，配置与后续脚本必须一致：

```bash
export WECHAT_BRIDGE_RUNTIME=/absolute/path/to/bridge-runtime
python3 scripts/setup_bridge.py --runtime-dir "$WECHAT_BRIDGE_RUNTIME"
# npm --prefix 也应使用同一个目录。
```

## 3. 创建独立账号并扫码

```bash
bash scripts/wx --json accounts create --alias astrbot-test
bash scripts/wx --account astrbot-test --json auth login \
  --detach --strategy qr --qr-refresh manual --timeout 300
```

保存返回的登录操作 `id`，用它导出二维码；将下方 `LOGIN_OPERATION_ID` 替换成真实值：

```bash
bash scripts/wx --account astrbot-test --json auth qr LOGIN_OPERATION_ID \
  --output "$PWD/.runtime/private/login.png"
```

在本机打开图片，用准备充当机器人的测试微信号扫码，并在手机确认。然后检查：

```bash
bash scripts/wx --account astrbot-test --json auth attempts get LOGIN_OPERATION_ID
bash scripts/wx --account astrbot-test --json account status
```

登录操作应为 `succeeded`；账号应为 `authenticated: true`、`connection: online`。不要把二维码、令牌或整个 `.runtime` 目录上传到 GitHub。

## 4. 加入群并查询标识

手动把测试号加入准备使用的群，然后执行：

```bash
bash scripts/wx --json accounts list
bash scripts/wx --account astrbot-test --json groups list
```

记录账户 `id`（通常是 `account-...`）和目标群 `id`（数字加 `@chatroom`）。群名不唯一，安装时使用 ID。刚入群可能需要等待同步后再查询。

## 5. 安装到 AstrBot

**先在启动器或原运行终端停止 AstrBot**，避免其运行中的配置保存覆盖安装结果。

`--astrbot-root` 指向包含 `data/cmd_config.json` 的运行根目录。启动器管理的源码实例通常是该实例的 `core` 目录。以下均为占位值：

```bash
python3 scripts/install_plugin.py \
  --astrbot-root /absolute/path/to/AstrBot \
  --account-id account-REPLACE_ME \
  --group-id '123456789@chatroom' \
  --provider-id '你的提供商名称/qwen-flash'
```

脚本会备份 `cmd_config.json`，复制插件，并追加一个启用的平台。备份位于 `.runtime/private/backups/`。脚本拒绝覆盖已有插件或同名平台；它不能自动确认 AstrBot 已停止，操作者需要自行完成停止步骤。

多群可重复传入 `--group-id`；每个群都只在明确 @机器人时回复。本仓库默认不启用任何真实群。

重新启动 AstrBot。插件首次加载时，给未绑定独立配置的目标群创建独立配置，清空该群额外的模型唤醒前缀并设置指定模型。如果目标群已匹配其他配置，插件会保留它；请在 AstrBot WebUI 中检查该配置的模型和唤醒设置。

手动安装时，把根目录的 `main.py`、`panel.py`、`metadata.yaml`、`requirements.txt`、`_conf_schema.json` 以及 `views/`、`.astrbot-plugin/` 两个目录放进 `data/plugins/astrbot_plugin_native_wechat/`，再按 [示例](../examples/platform.example.json) 追加平台配置。`chat_provider_id` 是必填项。

## 6. 验证收发

用另一个微信号在目标群中，从成员选择器选中 @测试号，发送一句简短问题。不要只手打昵称，也不要用测试号自己发送。

可检查 `.runtime/private/wechat_personal-state.health.json` 是否持续更新、`connected` 是否为 `online`。状态文件随 `state_file` 的文件名变化；它表示桥接轮询状态，不保证模型调用和发送都成功。最终以群内收到回复和发送回执为准。

本项目实测时使用 `qwen-flash`；任何能在 AstrBot 正常工作的文字聊天提供商都可以自行验证。回复风格仍受 AstrBot 人设影响。

## 启动、停止、升级和移除

电脑重启后先启动桥接，再启动 AstrBot：

```bash
bash scripts/wx --json daemon start
bash scripts/wx --account astrbot-test --json account status
```

如登录已失效，再按扫码步骤重新登录；不要假设登录状态永久有效。本项目没有设置 systemd 或开机自启。

停用时先停止 AstrBot，再执行 `bash scripts/wx --json daemon stop`。升级前备份插件、AstrBot 配置和桥接私有目录；核对接口兼容后再替换插件文件并重启。不要把升级当作登录资料迁移工具。

移除时，在 AstrBot 中删除对应平台和不再需要的独立群配置/路由，停止 AstrBot 后移走插件目录。运行数据和配置备份由操作者保管；安装脚本不会自动删除它们。

## 0.2.0 升级与群人格设置

备份现有插件目录，将 `main.py`、`panel.py`、`metadata.yaml`、`requirements.txt`、`_conf_schema.json` 和 `views/`、`.astrbot-plugin/` 目录（0.9 起的管理面板）一并复制到插件目录，然后重启 AstrBot 并刷新管理页面。运行配置和密钥不放入插件源码。

在“插件 → AstrBot for Personal WeChat → 配置 → 微信群人格”中按群名选择人格。保留“切换人格时开始新对话”可避免旧上下文影响，旧会话仍保留。日常保存会自动重载插件。群名在启动或保存时刷新，暂时读取失败不影响既有绑定。详细步骤见 [README](../README.md#按群选择人格020)。
