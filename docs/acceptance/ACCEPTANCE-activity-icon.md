# 2026-10-27 两行活动解析与原创图标验收

范围：沿用用户已完成的监听和仪表盘实现，只修正活动正文解析和图标。
气泡保持 `Agent · 工作区 · 状态` 首行、固定两行活动、底部一行目标。
命令、搜索词和思考取开头；尾部省略，不滚动、不轮播。目标、description、
工具输出不冒充当前活动；缺失正文时只显示明确状态。

## 自动化结果

实现全部完成后，Windows / Python 3.12.14 集中执行一次完整套件：
**919 tests / 67.758s / OK**。没有重复运行完整套件或额外的零散测试批次。
新增场景以表驱动和扩展现有用例为主；删除旧图标绘制的过时签名断言。

四项既有 blocking benchmark 全部通过：Monitor（5000 ticks）、Desktop
sources（5000 ticks）、Presentation、UI architecture。`compileall` 和
`git diff --check` 通过。日志与报告位于 `.test-artifacts/activity-icon-*`。
本轮没有构建或发布 EXE，不把源码测试当成编译产物验收。

回归覆盖实际命令/路径/查询、流式思考开头稳定、shell 通配符与分组语法、
工具结果隔离、并行工具选择、未知输入降级及 ZCode 本地/WSL 精确归属一致性。
只读 SQL 仍采用固定字段、行数和文本上限，使用既有事实刷新时机；没有新增
轮询、Hook、控制连接或向 Agent 数据写入。

## Codex dead-process follow-up — 2026-10-29

补充修复了 ChatGPT/Codex 宿主退出后遗留 `codex app-server --remote-control`
被下一次 census 重新提升为终端 Agent 的问题。孤立 app-server 现在保持在
Desktop data host 数据面，维护进程和 WSL app-server 服务不会创建 CLI watcher
目标；Desktop source 仍按 originator、rollout 活跃证据和有界 orphan TTL 决定
是否显示会话。

本次集中复验：完整套件 **923 tests / 97.501s / OK**；Desktop sources
5000 ticks **16.4s / BENCHMARK OK**，Monitor 5000 ticks **10.8s / BENCHMARK OK**；
discovery/Codex Desktop/WindowsExitWatcher **123 项**聚焦回归通过；compileall 与
`git diff --check` 通过。新增回归覆盖 GUI 退出后孤立服务、独立 daemon host、
维护进程、WSL 服务过滤、dead process 行、缺失 PID 退出候选、内部 exec-server
过滤、大型 rollout 首轮尾部读取，以及 `codex-app-server` originator 的只读
threads 读取。

## 上游字段证据

- [Codex protocol](https://github.com/openai/codex/blob/f276b82185f021e7b8d532a3851413bc79a67594/codex-rs/protocol/src/protocol.rs)：reasoning delta 的 item/channel 与 WebSearchEnd；[models](https://github.com/openai/codex/blob/f276b82185f021e7b8d532a3851413bc79a67594/codex-rs/protocol/src/models.rs)：reasoning summary、WebSearchAction；[app-server daemon](https://github.com/openai/codex/blob/f276b82185f021e7b8d532a3851413bc79a67594/codex-rs/app-server-daemon/README.md)：PID/start-time 生命周期。

### CLI 残留与 Desktop 首轮读取补正

- Windows census 现在丢弃 `dead`/`zombie`/`terminated` 进程行；watcher 句柄注册若遇到进程退出竞态，只在只读 PID+创建时间复核明确确认该代次消失时发出一次退出候选，仍由 Monitor 做 exact key/token 核对。
- Codex Desktop 的大 rollout 首轮从有界尾部读取，避免从文件头回放多轮后才到达当前事件，导致活动被误判为 crash orphan；`exec-server --remote` 内部服务子进程也不再创建 CLI target。

回归覆盖：discovery/Codex Desktop/WindowsExitWatcher **123 tests / OK**；其中含大型 live rollout 首轮读取、dead process 行、内部 exec-server 过滤、缺失 PID 退出候选和重复事件检查。
- [Claude 官方文档](https://code.claude.com/docs/en/hooks)：transcript 路径与工具输入。只读已有会话，不安装 Hook。
- [Kimi message](https://github.com/MoonshotAI/kimi-code/blob/68a283894163204e93b072cef20db164015a5cad/packages/kosong/src/message.ts)：ThinkPart.think 追加；[loop fold](https://github.com/MoonshotAI/kimi-code/blob/68a283894163204e93b072cef20db164015a5cad/packages/agent-core-v2/src/agent/contextMemory/loopEventFold.ts)：content.part、tool.call.args。
- [pi types](https://github.com/badlogic/pi-mono/blob/842c73170a3658d4e3b39fcc84690211ed503e09/packages/ai/src/types.ts)：ThinkingContent.thinking、ToolCall.arguments、ToolResultMessage.content。
- [ZCode contract](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/apps/zcode-cli/packages/contracts/src/interfaces/session-store.port.ts)：ToolPart.state.input、ReasoningPart.text；[schema](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/apps/zcode-cli/packages/adapters/src/storage/session-store/schema.baseline.sql)：message/part 与 running usage 的精确消息/调用关系。
- [OpenCode schema](https://github.com/anomalyco/opencode/blob/115113931c7a6957ca4325a37fbcdd2936edde79/packages/schema/src/session-message.ts)：assistant.content 的 text/reasoning/tool 及 shell.command。
- [DSH events](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/core/session/src/types.ts)：tool/call.arguments、assistant/message；[content types](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/llm/llm/src/types.ts)：text/reasoning/tool-call。

## 图标和界面

内置 imagegen 生成原创猫形标志，资源及完整提示词见
[图标说明](../assets/brand/README.md)。仪表盘品牌标记、窗口和托盘使用同一作品，
ICO 包含 16–256px 九种尺寸；保留托盘缓存自愈和 HICON 释放契约。

使用纯合成数据检查七种 Agent 的固定两行卡片和仪表盘品牌位置，截图为
`.test-artifacts/activity-icon-preview.png`、`brand-dashboard-preview.png`。
截图只捕获合成窗口自身的原生绘制，不采样其他应用内容。这些合成截图和
自动化结果不替代各个真实 Agent/远程连接的完整交互验收。
