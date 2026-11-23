# 配置说明

配置分两处：

- **平台配置**：AstrBot `data/cmd_config.json` 的 `platform` 列表中类型为 `native_wechat` 的一项，决定连哪个微信号、回复哪些群和好友。修改前先停止 AstrBot。
- **插件配置**：AstrBot WebUI「插件 → AstrBot for Personal WeChat → 配置」，管理群人格、掉线提醒和回复风格。保存后插件自动重载。

完整示例见 [examples/platform.example.json](../examples/platform.example.json)，其中的值都是占位数据。

## 平台配置

### 连接与范围

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `id` | — | 平台 ID，例如 `wechat_personal`。会话 ID 和配置路由都以它开头，部署后不要随意修改 |
| `type` | — | 固定为 `native_wechat` |
| `enable` | — | 是否启用 |
| `server_url` | `http://127.0.0.1:6174` | 桥接地址。只接受这个本机地址 |
| `account_id` | — | 桥接里的账号 ID（`account-...`），用 `wx accounts list` 查询 |
| `token_file` | — | 桥接 API 令牌文件的绝对路径 |
| `state_file` | — | 事件游标和已处理消息的状态文件；同目录会生成 `.health.json` |
| `allowed_groups` | `[]` | 回复的群 ID 列表（数字加 `@chatroom`） |
| `chat_provider_id` | — | 必填。为群和私聊新建独立配置时使用的完整提供商 ID |

### 群聊

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `context_messages` | `false` | 把白名单群里没有 @ 的消息也交给 AstrBot 作为群聊上下文。这些消息不会唤醒模型，也不会被回复 |
| `context_skip_prefixes` | `["/"]` | 以这些前缀开头的未 @ 消息不转发，群成员不能绕过 @ 执行指令 |
| `resolve_member_names` | `true` | 用群昵称作为发送者名称，查不到时退回微信 ID；缓存一小时 |
| `show_member_ids` | `true` | 发送者显示为「昵称(微信 ID)」；消息 @ 了其他成员时附注被 @ 者 |
| `text_mentions` | `false` | 手打的「@机器人群昵称」也算 @。微信只有从成员列表选择时才附带 @ 信息 |
| `mention_sender` | `false` | 群里的回复 @ 提问者 |
| `strip_markdown` | `true` | 去掉微信会原样显示的 Markdown 标记 |

插件首次遇到一个白名单群时，会为它建立独立的 AstrBot 配置（名称为「微信 · 群名」），使用 `chat_provider_id`，取消额外的唤醒前缀。若该配置继承了 AstrBot 的 ID 白名单，会把群会话加进去，否则 AstrBot 会静默拦截所有消息；开启 `context_messages` 时还会关闭「主动回复」、打开「识别发送者」。已经绑定其他配置的群保持不变。

### 好友私聊

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `friend_whitelist` | `false` | `false` 时回复所有好友；`true` 时只回复 `allowed_friends` |
| `allowed_friends` | `[]` | 允许私聊的微信 ID。`friend_whitelist: true` 且列表为空即关闭私聊 |

私聊路由到插件建立的「微信 · 私聊」配置（`<平台 ID>:FriendMessage:*`），新建时关闭它从原配置继承的 ID 白名单。需要语音转文字时在这个配置里开启。也可以在 AstrBot 的会话路由里把个别好友指向别的配置。

### 图片、语音与文件

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `send_images` | `true` | 发送回复中的图片（桥接 1.3 及以上） |
| `receive_media` | `true` | 接收图片、语音、动画表情和普通文件。群里的媒体还需要开启 `context_messages` |
| `attach_recent_image_seconds` | `120` | 提问者在这段时间内发的最后一张图，会在其 @ 机器人时附给模型（只附一次） |
| `attach_recent_file_seconds` | `120` | 同上，用于文件 |

图片优先下载标准尺寸，原图仍在下载时最多等 15 秒，再退回缩略图。文件收发上限 50 MiB，最多等待下载 60 秒，失败时显示「文件未能获取」。下载的文件放在 AstrBot 临时目录，保留一小时。引用回复时，插件会在该会话最近 1000 条消息里查找被引用的文件，重启后也能找到。

### 主动消息

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `proactive_messages` | `false` | 允许任何主动发送（定时任务、插件、`send_message_to_user`）到白名单群和私聊，不限次数。开启后 AstrBot 会把 `send_message_to_user` 提供给模型 |
| `proactive_window_seconds` | `120` | 未开启 `proactive_messages` 时，只在该会话最近一次 @ 或私聊后的这段时间内放行主动发送，例如表情包插件的补发 |

定时提醒需要在对应的 AstrBot 配置里开启 `provider_settings.proactive_capability.add_cron_tools`。群成员创建的提醒，发出的第一段文字会 @ 创建者。AstrBot 只投递经 `send_message_to_user` 发出的定时任务内容；模型若只写了最终回复，插件会把这条回复补发出去。

## 插件配置

| 项 | 说明 |
| --- | --- |
| 微信群人格 | 自动列出白名单里的群，每个群选择人格；「切换人格时开始新对话」默认开启。在这里添加卡片不会授权新群 |
| 掉线提醒发送到（`alert_sessions`） | AstrBot 会话 ID 列表，在目标会话发送 `/sid` 获取，例如 `qq:FriendMessage:12345`。留空则不提醒 |
| 离线多久后提醒（`alert_after_seconds`） | 默认 120 秒，最小 30 秒；短暂重连不提醒 |
| 微信回复风格提示（`reply_style_hint`） | 追加到微信群的模型请求：不用 Markdown、可以用微信表情代码、以微信 ID 判断身份等。留空则不追加 |
| 微信私聊回复风格提示（`private_reply_style_hint`） | 私聊使用的风格提示 |

## 管理面板

管理面板没有单独的配置。它读取上面的平台配置，桥接令牌只在服务器端使用；浏览器只拿到汇总数据和二维码图片。测试消息只能发给机器人本来就会回复的会话，最长 500 字。插件日志只保存在内存里（最近 500 条），插件重载后清空。
