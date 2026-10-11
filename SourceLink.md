# DeskPet 4.1.0 — SourceLink

> Research snapshot: 2026-09-13  
> Main implementation commit: `44305feffe59b0495046a978fbbd01a9ee94d83b`  
> Purpose: record the external contracts and implementation evidence used by DeskPet's passive Windows/WSL/Desktop Agent monitoring.  
> Trust labels: **Official contract**, **Upstream implementation**, **Empirical issue**, **Third-party reverse evidence**.

This file records evidence, not promises made by third-party products. Private paths, process names and database schemas are implementation observations and must remain capability-detected and fail closed. DeskPet's own safety contract is defined by the repository code, README and AGENTS.md.

## DeskPet 4.1.0 implementation

The following links pin the accepted ZCode WSL implementation on `main`. The implementation first landed in the immutable `44305fe` product commit; the later 4.1.0 promotion corrects the SemVer and release metadata without changing this data-plane architecture.

- [agents/models.py @ `44305fe`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/44305feffe59b0495046a978fbbd01a9ee94d83b/agents/models.py) — `RemoteRuntimeContext` and three-state `SourceProbeSnapshot.remote_runtimes`.
- [agents/discovery.py @ `44305fe`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/44305feffe59b0495046a978fbbd01a9ee94d83b/agents/discovery.py) — fresh WSL running-distro census, ZCode remote-runtime detection, uid/user/HOME metadata, authoritative/stale/tombstone semantics.
- [agents/paths.py @ `44305fe`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/44305feffe59b0495046a978fbbd01a9ee94d83b/agents/paths.py) — pure Linux-path helpers for `~/.zcode/cli` and `~/.zcode/server`; production remote DB access does not use WSL UNC.
- [agents/zcode_remote.py @ `44305fe`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/44305feffe59b0495046a978fbbd01a9ee94d83b/agents/zcode_remote.py) — bounded direct-argv WSL transport and read-only `node:sqlite` facts reader.
- [agents/zcode_desktop.py @ `44305fe`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/44305feffe59b0495046a978fbbd01a9ee94d83b/agents/zcode_desktop.py) — local Windows + remote WSL plane projection with one global admission cap.
- [agents/monitor.py @ `44305fe`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/44305feffe59b0495046a978fbbd01a9ee94d83b/agents/monitor.py) — remote reads are owned by the existing `ProcessProbeWorker`; Monitor/UI polling never enters WSL.
- [tests/test_zcode_wsl_remote.py @ `44305fe`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/44305feffe59b0495046a978fbbd01a9ee94d83b/tests/test_zcode_wsl_remote.py) — path, discovery, transport, last-good, projection, cap and worker-gating regression coverage.
- [tools/desktop_source_probe.py @ `44305fe`](https://github.com/invincible-summer/Deskpet-For-Agents/blob/44305feffe59b0495046a978fbbd01a9ee94d83b/tools/desktop_source_probe.py) — sanitized real-machine structure probe; no prompt/transcript/tool-argument/token output.

### 4.1.0 architecture conclusions

- Windows `ZCode.exe` remains the Desktop host and activation target. WSL remote runtime is a separate observation/data plane and never becomes a Terminal Agent target.
- A remote data plane is authorized only by the current fresh WSL process census. Failed WSL enumeration retains last-good state as non-authoritative; an authoritative stopped-distro result removes the plane.
- The remote database engine runs inside the same WSL environment as the database file. Windows does not open a live WSL SQLite/WAL file through `\\wsl.localhost`.
- ZCode's private DB schema, runtime filenames and filesystem layout are feature-detected implementation evidence, not treated as stable public API.
- Approval state is projected only from explicit session-scoped DB facts (`approval_status=requested` on a running tool). DeskPet does not synthesize approval from silence and does not approve automatically.

## ZCode — product contracts

- **Official contract** — [Remote Development](https://zcode.z.ai/en/docs/remote-development) — Remote Development supports WSL on the Windows desktop client. After connection, file reads, terminal commands, Git operations and ZCode Agent execution occur in the selected target environment, while the desktop client continues to provide account/model/task UI. The WSL flow can select a specific Linux user and prepares remote-side components on first connection.
- **Official contract** — [Safety Confirmation](https://zcode.z.ai/en/docs/safety-confirm) — permission requests pause the affected task and remain task-scoped; this supports DeskPet's requirement that WAITING attribution be session-specific rather than globally guessed.
- **Official contract** — [Hooks](https://zcode.z.ai/en/docs/hooks) — hooks are explicitly configured behavior and can participate in permissions. DeskPet therefore excludes hooks from the zero-configuration monitoring path.
- **Official contract** — [Subagents](https://zcode.z.ai/en/docs/subagents) — ZCode may run subagents as work inside a task. DeskPet folds known child-session activity into the root task rather than creating a separate pet for each subagent.
- **Official contract** — [Plugin](https://zcode.z.ai/en/docs/plugin) — plugins can bundle hooks/MCP/subagents. DeskPet monitoring does not require a plugin.
- **Official contract** — [Usage Stats](https://zcode.z.ai/en/docs/usage-stats) — ZCode exposes usage derived from local session records, supporting the general existence of a persistent local session data plane; it does not make the private schema a public API.

## ZCode — observed Remote Development implementation

These issue logs are empirical evidence of current runtime layout. They are deliberately matched narrowly and are covered by fail-closed tests because upstream may change them.

- **Empirical issue** — [zai-org/feedback #162](https://github.com/zai-org/feedback/issues/162) — ZCode 3.4 Windows/WSL connection logs show `server/zcode-server.cjs` uploaded to `~/.zcode/server/zcode-server.cjs` and the server installed in the WSL environment.
- **Empirical issue** — [zai-org/feedback #195](https://github.com/zai-org/feedback/issues/195) — remote-workspace failure logs reference the server path and an agent entry under `~/.zcode/server/agents/glm/zcode-agent`. DeskPet 4.1.0 accepts this exact current entry in addition to `zcode.cjs`.
- **Empirical issue** — [zai-org/feedback #302](https://github.com/zai-org/feedback/issues/302) — ZCode 3.7.7 remote runtime/log evidence references `zcode.cjs` under the remote `~/.zcode/server/agents/glm/` tree and remote `~/.zcode/cli/log` activity.
- **Empirical issue** — [zai-org/feedback #28](https://github.com/zai-org/feedback/issues/28) — Windows 11 + WSL2 logs independently show the remote ZCode CLI log root under `~/.zcode/cli/log/`.

## ZCode — private data-plane reverse evidence

These are not official compatibility contracts. They are retained because public ZCode documentation does not specify the internal DB schema or bundled runtime executable path.

- **Third-party reverse evidence** — [yiyanwannian/zcode-monitor @ `fe8857f`](https://github.com/yiyanwannian/zcode-monitor/tree/fe8857f23708e7b45b6bbb00a075021ddb799984) — independent read-only monitoring evidence for ZCode session/model/tool/turn records and approval-related fields.
- **Third-party reverse evidence** — [xhwxt/zcode-token-usage-statusbar @ `42b64bc`](https://github.com/xhwxt/zcode-token-usage-statusbar/tree/42b64bc25a0aceaa473e70b791d0c11d4f9e953b) — evidence that current ZCode session data is read from `~/.zcode/cli/db/db.sqlite`, including remote environments.
- **Third-party reverse evidence** — [windviki/zcode-webui @ `a9abc61`](https://github.com/windviki/zcode-webui/tree/a9abc6177e24bc94f97f4d6828f18e709d8b3d2a) — evidence for the bundled remote `~/.zcode/server/node` runtime and ZCode remote-agent layout.

DeskPet never treats these repositories as authority over ZCode. Their evidence is used only to define conservative capability probes and is corroborated with official/empirical runtime evidence where possible.

## Node.js / SQLite — remote read-only transport

- **Official contract** — [Node.js `node:sqlite`](https://nodejs.org/api/sqlite.html) — `DatabaseSync` was introduced in Node 22.5.0; it supports a read-only connection option, a bounded busy `timeout`, and extension loading disabled by default. DeskPet explicitly requests `readOnly: true`, `timeout: 40`, and `allowExtension: false` in ZCode's bundled WSL Node process.
- **Official contract** — [SQLite WAL](https://sqlite.org/wal.html) — WAL requires shared-memory coordination among processes using the database and is not designed for ordinary network-filesystem access between different hosts.
- **Official contract** — [SQLite Over a Network](https://sqlite.org/useovernet.html) — SQLite recommends keeping the database engine on the same machine as the database file when a network boundary exists. DeskPet follows this by executing `node:sqlite` inside WSL and sending only bounded JSON facts back to Windows.

The 4.1.0 remote reader issues only a fixed schema probe and fixed `SELECT`/`WITH` facts queries. It performs no `INSERT`, `UPDATE`, `DELETE`, DDL, WAL checkpoint, repair, extension loading, or database migration.

## Microsoft WSL — passive running-distro boundary

- **Official contract** — [WSL interop](https://learn.microsoft.com/en-us/windows/dev-environment/wsl-interop) — Windows can access a distro through `\\wsl$` / `\\wsl.localhost`; Microsoft documents that `\\wsl.localhost` can auto-start a distro on Windows 11. This is why DeskPet never uses a stale cached distro name or a UNC file probe as authorization to inspect a stopped distribution.
- **Official contract** — [WSL basic commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands) — `wsl --list --running` is the host-side inventory used as the fresh gate before any distro-specific `--exec` operation.

DeskPet's existing WSL discovery therefore keeps the three-state distinction: authoritative presence, authoritative absence, and non-authoritative read failure. “Could not read” is never converted into “the Agent disappeared.”

## Codex / Windows sources retained by the project

The ZCode 4.1.0 change does not alter the existing Codex Desktop/CLI safety boundary; these references remain part of the project evidence set.

- **Official contract** — [Introducing the Codex app](https://openai.com/index/introducing-the-codex-app/) — product-level multi-agent desktop behavior and Windows availability.
- **Official contract** — [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server) — thread/turn/item and approval-server-request concepts. DeskPet's current passive Desktop source does not attach to this live control plane.
- **Upstream implementation** — [openai/codex state migrations](https://github.com/openai/codex/tree/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/state/migrations) — pinned implementation evidence used for capability-detected Codex Desktop state DB reads.
- **Upstream implementation** — [Codex rollout persistence policy](https://github.com/openai/codex/blob/654b0a77d0d2f81aa21f61caf7af4be88fe550bb/codex-rs/rollout/src/policy.rs) — distinguishes durable rollout evidence from transient runtime events; DeskPet does not infer hidden approvals from rollout silence.
- **Empirical issue** — [Windows Terminal #19783](https://github.com/microsoft/terminal/issues/19783) — illustrates the lack of a stable external mapping from an arbitrary process/session to an exact already-open Windows Terminal tab; DeskPet keeps terminal activation conservative.

## Evidence interpretation and safety rules

1. **Official contract** may justify product/platform behavior, but it does not turn undocumented internal file layouts into APIs.
2. **Upstream implementation** is pinned to a commit where possible and must be capability-detected if consumed.
3. **Empirical issue** proves an observed runtime shape or failure mode, not a permanent guarantee.
4. **Third-party reverse evidence** is used only to corroborate private implementation details and never as sole authority for a destructive/action path.
5. Monitoring must remain passive: no hooks/plugin installation, no Agent config mutation, no input injection, no automatic approval, no private control-plane navigation, and no Agent-database writes.
6. Runtime identity is bounded and ephemeral. Distro/user/session/runtime facts are not persisted as long-lived user configuration.
7. A schema/runtime mismatch degrades to non-authoritative/UNKNOWN/last-good behavior instead of guessing.

## Acceptance boundary

The 4.1.0 automated acceptance covers byte-compile, the full Windows unit suite, fresh/stale/tombstone discovery semantics, direct-argv read-only remote transport, state isolation, global session/plane bounds, and the existing Monitor/Desktop-source/Presentation/UI architecture benchmarks. It does **not** claim that GitHub-hosted CI is a real user's ZCode + WSL Remote Development machine. `tools/desktop_source_probe.py` exists for the final environment-specific read-only smoke check without exposing prompts, transcripts, tool arguments, credentials, or other sensitive payloads.

---

## 2026-10-11 — OpenCode / DeepSeek Harness 纯监听调研（替代之前的审批集成草案）

> **审计状态**：仅完成仓库静态审阅与可公开文档核验；尚未完成目标机器上的 CLI / Desktop / Windows / WSL 运行验证。本节是待实现的接入依据，不是当前 DeskPet 已支持 OpenCode / DSH 的功能说明。
>
> **用户确认的范围**：只读监控（运行、会话、活动、完成、待用户确认/审批的提示）。**不实现**允许/拒绝、审批 API POST、模拟按键、自动控制、插件自动安装、注入、修改 Agent 配置和会话数据。现有「双击唤醒原窗口」属于 DeskPet 原有的窗口前置行为，不是审批能力。

### A. 2026-10-11 仓库实现基线（代码事实）

本次审核的基线：\`main\` / \`9a019bf5b14fd282304d9d4371105bd19f7ac86e\`（仓库名称已由 \`DeskPet\` 重定向为 \`Deskpet-For-Agents\`）。\`pet/version.py\` 仍为 **4.1.0**；\`tests/ACCEPTANCE-4.5.0.md\` 是历史测试记录，不能代替当前 app 版本，也不能证明本轮测试已经在真实目标环境通过。

- **代码事实** — [agents/models.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/models.py)：\`AgentKind\` 现有 \`CLAUDE/CODEX/KIMI/PI/ZCODE\`；\`AgentSurface\` 有 \`TERMINAL/DESKTOP\`；\`Observation\` 与 \`Snapshot\` 已分开状态、阶段、模式；实际待审枚举是 \`Status.WAITING\`（不是 \`WAITING_APPROVAL\`）。
- **代码事实** — [agents/discovery.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/discovery.py)：\`scan_windows_inventory\` 对单次 psutil inventory 分类 terminal 与 desktop；\`WslProcessProbe\` 按运行中的 distro 和 PID/uid 获取受限身份信息；当前两种新 Agent 都**未纳入分类**。新进程名只做严格正向匹配、确认父子关系，不能因 \`node.exe\`、\`bun.exe\`、\`tauri\`、\`WebView2\` 等通用进程出现就认为属于某 Agent。
- **代码事实** — [agents/monitor.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/monitor.py)：现有 \`ProcessProbeWorker\`（慢速 Windows/WSL 发现）、\`Monitor\`（周期融合）、\`WATCHERS\`（四类终端文件 watcher）、\`_register_desktop_sources\`、\`_poll_desktop_sources\`、\`_commit_exit\` 和 UI revision。网络/SSE 不能直接插入每次 \`_tick\`；断连不能以 \`authoritative empty\` 清空已知实例。
- **代码事实** — [agents/desktop.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/desktop.py)：\`DesktopSessionSource.poll(hosts, claimed_sessions, now)\`、\`DesktopSourceSnapshot\` 与 CLI \`SessionClaimKey\`。**重要兼容陷阱**：Monitor 目前按 \`source.kind\` 退出同类型 desktop targets；不应把同种 Agent 的不同桌面实现做成两条各自会清理另一条会话的 source。应每个 kind 仅注册一个桌面聚合 source，或在明确修改 Monitor 所有权匹配规则并补测后支持多 source。
- **代码事实** — [agents/state.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/state.py)：待审批只凭精确会话证据；纯文本滚动不能越权覆盖已知 Done/Idle；活动 TTL 超时转 Unknown。保持原样。
- **代码事实** — [agents/zcode_remote.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/zcode_remote.py)、[agents/codex_desktop.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/codex_desktop.py)、[agents/zcode_desktop.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/zcode_desktop.py)：复用结构化 observation、bounded sessions、host lease、last-good/stale；**不要**照搬 ZCode 私有 SQLite 表到 OpenCode/DSH。
- **代码事实** — [pet/config.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/pet/config.py)、[pet/presentation.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/pet/presentation.py)、[pet/dashboard.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/pet/dashboard.py)：\`_KIND_KEYS\`、默认 monitor agents、eligible_kinds、Dashboard 的开关和选择器均须同步；单靠新增 Enum 不会使 UI 完整工作。
- **代码事实** — [tests/test_desktop_sources.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/tests/test_desktop_sources.py)、[tests/test_discovery.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/tests/test_discovery.py)、[tests/test_state.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/tests/test_state.py)：沿用 snapshot 三态、进程代次/WSL 多用户、跨界面会话 claim、stale 与 Waiting 精确归属测试。

### B. OpenCode — 稳定公开契约、可变数据面及安全限制

1. **Official contract** — [OpenCode Server](https://opencode.ai/docs/server/)：TUI 运行时启动 HTTP Server；\`opencode serve\` 默认 \`127.0.0.1:4096\`，而 TUI 可使用**随机端口**；\`GET /global/health\` 返回健康与版本；\`GET /global/event\`、\`GET /event\` 是 SSE；\`GET /session\`、\`GET /session/status\` 是只读会话 API；\`/doc\` 提供实例级 OpenAPI。**不能把 4096 当作发现规则；不能因为发现本地 HTTP 端口就信任其为 OpenCode。**
2. **Official contract** — [OpenCode Plugins](https://opencode.ai/docs/plugins/)：列出 \`permission.asked\`、\`permission.replied\`、\`session.created/updated/deleted/idle/status/error\`、\`message.part.updated\`、\`question.asked\`（版本相关）等订阅事件。插件是**可选扩展**，零配置路径不安装它；事件是否被当前运行实例通过 SSE 广播，需要对照实例 OpenAPI/实测验证。
3. **Official contract** — [OpenCode Windows / WSL](https://opencode.ai/docs/windows-wsl/)：原生 Windows 与 WSL 均可运行；Desktop 支持连接 WSL 后端；WSL 的配置和 session 保存在该 WSL 用户环境的 \`~/.local/share/opencode/\`。官方的 \`0.0.0.0\` 示例伴随密码防护警告：DeskPet **不得为了监听而启动服务、改监听地址或打开网络暴露**。
4. **Official contract** — [OpenCode Server Authentication](https://opencode.ai/docs/server/#authentication)：\`OPENCODE_SERVER_PASSWORD\` 启用 HTTP Basic Auth，用户名可用 \`OPENCODE_SERVER_USERNAME\` 设置。DeskPet 不枚举/读取环境里的明文密码，不绕过访问控制；未能合法、安全读取的接口标为 unavailable。
5. **Upstream implementation, version-specific** — [OpenCode TUI sync](https://github.com/anomalyco/opencode/blob/dev/packages/tui/src/context/sync.tsx)：上游前端按 \`sessionID\` 缓存 status/permission，收到 \`permission.replied\` 后移除同 ID pending。此为开发分支实现佐证，而非不可变跨版本协议；使用前须用目标实例 \`/doc\` 或 fixture 验证具体字段。
6. **Experimental/variant, NOT baseline** — [OpenCode v2 API](https://dev.opencode.ai/v2/docs/api/)：\`/api/session/{sessionID}/permission/{requestID}\` 等路由属于另一接口族。当前目标**只读**，不实施或保存任何 \`reply\` 路由，不同时叠加经典/v2 写入方案。
7. **Implementation gate** — 精确等待审批必须有 \`(server incarnation, session ID, permission request ID)\` 且收到当前有效 \`asked\`，并能够以对应 \`replied\` 或权威 pending 状态撤销；如断流、重启或只读接口不提供当前 pending 快照，应显示 stale/Unknown，不永久保留 Waiting。
8. **OpenCode Desktop** — official docs support desktop app, but **host executable/process tree, spawned backend process, active tab mapping, multi-window data ownership** are NOT specified as guaranteed stable detection contracts. These must be gathered from sanitized local probe before permitting precise terminal/desktop de-duplication.

### C. DSH — 官方进程内事件与持久化，不等于公开只读远程 API

1. **Official contract** — [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)：DSH 处于 developer preview，官方明确提示 breaking changes；\`dsh web\` 通常监听 \`127.0.0.1:3080\`，但端口可配置。只能以进程所有权 + 后端身份绑定，**不能把端口 3080 当唯一身份**。
2. **Official contract** — [Cordis events](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/framework/events.md)：\`session/event\` 为 Cordis 内部回调；持久会话事件 \`turn/start\`、\`turn/end\`、\`step/start\`、\`step/end\`、\`tool/call\`、\`tool/result\` 是 log event.type，**不是 SSE 事件名或现成跨进程订阅 URL**。
3. **Official contract** — [Session](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/session.md)：每条日志带 \`type/seq/time/data\`，\`session/event\` 是完成 append 后的事件回调。尚未证明公共、只读、可安全绑定到 CLI/Web/Desktop 的订阅入口。
4. **Official contract** — [Approval](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/approval.md)：\`approval/asked\` 和 \`approval/decided\` 是审计事件，\`ApprovalRequestId\` 只与对应审批配对，不能代替 agent/session/tool-call ID。DeskPet 仅用配对事件提示/解除 Waiting，**不订阅决策瀑布流 \`approval/request\`、不注册 answerer、不回写 outcome**。
5. **Official contract** — [Session Persistence](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/persistence.md)、[JSONL backend README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-persistence-jsonl/README.md)：\`dsh-session-persistence-jsonl\` 逻辑日志按 session 持久化，**物理编码默认是校验后的 Zstandard 帧**，而非可直接 \`readline()\` 的纯 JSONL；\`compression: none\` 才使用裸文本。持久化后端还可替换，不能推断 \`~/.dsh\` 下每个文件均为可 tail 的标准 JSONL。严禁无界扫描历史 logs、解压整个仓库来推断当前进程。
6. **Community distribution, not official DeepSeek desktop** — [xingj404-lab/dsh-desktop](https://github.com/xingj404-lab/dsh-desktop) 与 [tscodeplus/dsh-desktop](https://github.com/tscodeplus/dsh-desktop)：社区 Tauri 壳可启动本地 \`dsh web\`；不同发行版各自管理 backend/端口/数据目录，DeskPet 不能把其中任一桌面包名当作官方统一标准。
7. **Community issue/evidence** — [DSH web 403 / Origin troubleshooting](https://github.com/deepseek-ai/deepseek-harness/discussions/313)、[configurable port](https://github.com/deepseek-ai/deepseek-harness/discussions/1868)：这类 issue 证明本地 host/port/trust 规则存在差异，并不授权 DeskPet 仿造浏览器令牌或重放内部 RPC。
8. **Third-party, optional and untrusted contract** — [kelemiao/dsh-api](https://github.com/kelemiao/dsh-api)：社区桥可提供独立 API，但不是 DSH 官方稳定只读监听面；第一阶段不安装、不将其读写 RPC 原样接入。

**DSH 接入决策门槛**：P0 首先寻找当期安装版本中受支持且受限的只读事件/API；若没有：默认只报告确切进程存在（Unknown 或 idle 需独立证据），不声称能精确显示 Waiting；若后续确实需要待审细节，单独讨论**用户手动安装**的 in-process 只读事件插件，其功能仅限投射脱敏状态、不可改变 Agent 生命周期与审批策略。

### D. 调研结果与未证实点矩阵

| 对象 | 可确认依据 | 尚需实机/版本验证 | 失败时行为 |
| --- | --- | --- | --- |
| OpenCode Windows CLI | CLI 启动 server；官方 GET/SSE/会话 API | exe/node/bun 进程名、随机端口归属、鉴权、pending 快照 | 保留进程 Unknown；不伪造 Waiting |
| OpenCode WSL CLI | WSL 官方支持；同源用户存储 | distro+uid+server PID 对应、跨 WSL 访问、后端调用路径 | WSL 不可读 → stale；不唤醒 distro |
| OpenCode Windows Desktop | Desktop 可连接独立/WSL 后端 | GUI 宿主与 server 子树、窗口前置、前端 session 归属 | 仅前置已验证 GUI 宿主，或只显示运行存在 |
| DSH CLI | 进程内 Cordis event 与 Session 类型 | CLI 真实进程入口及有效事件输出路径 | 只显示进程运行 Unknown |
| DSH Web | 默认本地 web UI、会话日志 | 是否提供受支持的**只读**会话与 pending 接口 | 进程运行 Unknown，不探测有权限写 RPC |
| DSH Desktop（社区多发行） | Tauri 壳常复用 dsh web | 所用分发版、主/sidecar/token/端口及数据所有权 | 单宿主状态；无会话身份不建多个 pet |

### E. 最小接入取舍与禁止项

- **推荐**：OpenCode 优先同一现有 server 上的只读 SSE/GET + 有界后台连接，DSH 先 process-only、寻找真实只读 API；如果无法得到明确会话/审批证据，则**接受功能降级**，不为“显示一个状态”追加高资源后台服务。
- **线程和性能**：复用一次 Windows process census 与现有 WSL ProcessProbeWorker；给多个 OpenCode endpoint 分配一个有界 IO 管理 worker 而非每会话起线程；Monitor 每 tick 只复制内存快照，Tk 禁止网络、数据库/WSL 读。只有检测到可证实活跃进程/后端，才建立事件连接。
- **安全只读**：API 仅 allowlisted GET/SSE；**不执行 POST/PUT/PATCH/DELETE**；即使上游某个只读 RPC 使用 POST，也不可未经审核自动纳入。禁止 Agent 插件自动安装/配置改写、HTTP 端口暴力探测、WSL root fallback、新增后台解压守护、任何审批按钮。
- **会话身份和 stale**：key 至少包含 provider + Windows/WSL plane/uid + server process incarnation + exact session ID；权威删除与读取失败严格分离；断线/未知 pending 立即撤销“确定 WAITING”展示（可改为 stale 提示），历史已完成 session 不复活成活跃 target。
- **待完成证据**：对两类 Agent 的每个目标环境采集不含对话/命令/token 的 fixture（process family、server health/version、event name、session id 的哈希、source authority）；把结果与使用的上游 commit/version 补回本文和 [plan.md](plan.md)。无实机，不得声称全部接入已通过。

