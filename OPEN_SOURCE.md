# 构建来源、依赖与开源说明

核验日期：2026-10-28。本项目不是从零实现微信通信协议，而是在现有项目提供的接口上实现 AstrBot 平台适配。下文区分实际依赖、接口参考和开发工具。

## 1. 实际运行依赖

| 项目 | 在本项目中的作用 | 版本 / 参考点 | 许可说明 |
| --- | --- | --- | --- |
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | 提供插件加载、消息事件、平台注册、模型调用、人设、上下文和群配置路由 | 本地实测 4.29.3 | 本地发行包 `LICENSE` 为 GNU AGPL v3；上游许可见 [LICENSE](https://github.com/AstrBotDevs/AstrBot/blob/master/LICENSE) |
| [agent-wechat](https://github.com/thisnick/agent-wechat) | 提供微信扫码登录、消息同步、群信息、原生收发及本机 HTTP API | npm `@agent-wechat/cli@1.1.0`；发布清单指向提交 [`12c3c335d1a180b12b035be8844ed6f896b0cc46`](https://github.com/thisnick/agent-wechat/tree/12c3c335d1a180b12b035be8844ed6f896b0cc46) | 该提交未发现仓库根目录许可证；npm 清单未声明 license；GitHub 未识别仓库级许可证。应向上游确认使用与再分发条款 |
| [aiohttp](https://github.com/aio-libs/aiohttp) | 插件的异步 HTTP 客户端，连接本机桥接服务 | 本地实测 3.14.5 | Apache-2.0，见上游 [LICENSE.txt](https://github.com/aio-libs/aiohttp/blob/master/LICENSE.txt) |
| [Python](https://www.python.org/) | 插件及配置脚本运行环境；使用 asyncio、json、pathlib、unittest 等标准库 | 实测 3.12；仓库代码要求 3.11+ | PSF 许可，见 [Python 许可证](https://docs.python.org/3/license.html) |
| [Node.js](https://github.com/nodejs/node) / [npm CLI](https://github.com/npm/cli) | 安装与启动上游发布的 CLI；插件自身不使用 Node.js 调用模型 | 上游 CLI 声明 Node.js >=24.16；本机安装时使用 Node.js 24.22.1 | 各自及其捆绑组件遵循上游许可证，本仓库不分发运行时 |

> agent-wechat 是公开源码的第三方项目，但本次未核实其整体获得何种开源许可。这里不会把“GitHub 上可见”当作“拥有明确的开源授权”。

## 2. 构建与实现时参考的项目

### AstrBot 平台接口

阅读了本地 AstrBot 的平台基类、消息事件、平台注册装饰器和配置管理器；实现围绕 `Platform`、`AstrMessageEvent`、`AstrBotMessage`、`register_platform_adapter` 及 `AstrBotConfigManager` 编写。事件发送方式参考其内置适配器的接口用法。插件不是 AstrBot 核心代码的替代品。

### agent-wechat 原生 API 契约

参考上述固定提交中的：

- [`packages/shared/src/native-client.ts`](https://github.com/thisnick/agent-wechat/blob/12c3c335d1a180b12b035be8844ed6f896b0cc46/packages/shared/src/native-client.ts)：账户、事件、消息和发送接口。
- [`packages/shared/src/native.generated.ts`](https://github.com/thisnick/agent-wechat/blob/12c3c335d1a180b12b035be8844ed6f896b0cc46/packages/shared/src/native.generated.ts)：消息字段、@元数据和发送回执结构。
- [`packages/openclaw-extension/src/native-monitor.ts`](https://github.com/thisnick/agent-wechat/blob/12c3c335d1a180b12b035be8844ed6f896b0cc46/packages/openclaw-extension/src/native-monitor.ts)：`message.committed` 与 `origin=live` 的语义。
- [`packages/openclaw-extension/src/native-send.ts`](https://github.com/thisnick/agent-wechat/blob/12c3c335d1a180b12b035be8844ed6f896b0cc46/packages/openclaw-extension/src/native-send.ts)：幂等键和发送状态确认流程。

本仓库使用 Python 调用其接口，没有复制其 TypeScript/Rust 实现、协议实现或发布二进制。

### 社区 AstrBot 微信插件

前期评估过 [JackZhaoooooo/astrbot-plugin-agent-wechat](https://github.com/JackZhaoooooo/astrbot-plugin-agent-wechat)，查看版本为 [`84ac8d4c62ca768ee5cd311871e1c07dabb1554a`](https://github.com/JackZhaoooooo/astrbot-plugin-agent-wechat/tree/84ac8d4c62ca768ee5cd311871e1c07dabb1554a)。

它展示了通过 agent-wechat 接入 AstrBot 的思路，但该版本使用旧 `/api` 接口，事件继承、平台构造方法和导入方式也与本次环境不匹配。因此实际部署使用本仓库的新 Python 适配器，没有直接安装或打包该社区插件。核验时未发现可确认的仓库级许可证；本仓库不对它进行重新授权。

## 3. 上游桥接自身的第三方来源

agent-wechat 的 [`THIRD_PARTY_NOTICES.txt`](https://github.com/thisnick/agent-wechat/blob/12c3c335d1a180b12b035be8844ed6f896b0cc46/packages/cli/THIRD_PARTY_NOTICES.txt) 还列出了其自身使用的第三方内容，例如：

- [Citizen Lab / wechat-security-report](https://github.com/citizenlab/wechat-security-report)：MMTLS 测试向量，通知中标注 MIT 许可。
- [kn007 / silk-v3-decoder](https://github.com/kn007/silk-v3-decoder)：SILK 相关源码，通知区分 MIT 包装项目与保留的 SDK 许可。

这些属于桥接的间接来源，不是本插件直接实现或导入的功能。本插件收发图片、语音和文件时只调用桥接的 HTTP 接口，不包含这些编解码实现；上游通知也不能替代其整个项目的许可证。完整依赖与许可应查看上游发布物，不把本文件当作其完整软件物料清单。

## 4. 开发工具与外部服务

- [Ruff](https://github.com/astral-sh/ruff)：本项目使用 0.17.3 做格式化和静态检查；MIT 许可；不是运行依赖。
- Git / GitHub：用于版本管理与项目发布。
- 阿里云百炼 `qwen-flash`：本次验证使用的在线模型服务，不是本仓库包含的开源模型或代码。模型账号、费用和服务条款由相应服务商管理。
- 腾讯微信客户端与微信服务：不包含在本仓库，不因本仓库的 AGPL 许可证而变成开源软件。

## 5. 本仓库的发布范围

采用 AGPL-3.0-or-later 发布本仓库的适配器、辅助脚本和文档；完整文本见 [LICENSE](LICENSE)。选择与宿主 AstrBot 相容的许可，不改变任何上游权利。

仓库不包含实际账号 ID、群 ID、聊天记录、登录二维码、认证令牌、API 密钥、完整 AstrBot 配置、微信数据库或下载的桥接程序。示例里的账号、群和路径仅为占位符。
