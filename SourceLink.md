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

本次审核的基线：`main` / `9a019bf5b14fd282304d9d4371105bd19f7ac86e`（仓库名称已由 `DeskPet` 重定向为 `Deskpet-For-Agents`）。`pet/version.py` 仍为 **4.1.0**；`tests/ACCEPTANCE-4.5.0.md` 是历史测试记录，不能代替当前 app 版本，也不能证明本轮测试已经在真实目标环境通过。

- **代码事实** — [agents/models.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/models.py)：`AgentKind` 现有 `CLAUDE/CODEX/KIMI/PI/ZCODE`；`AgentSurface` 有 `TERMINAL/DESKTOP`；`Observation` 与 `Snapshot` 已分开状态、阶段、模式；实际待审枚举是 `Status.WAITING`（不是 `WAITING_APPROVAL`）。
- **代码事实** — [agents/discovery.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/discovery.py)：`scan_windows_inventory` 对单次 psutil inventory 分类 terminal 与 desktop；`WslProcessProbe` 按运行中的 distro 和 PID/uid 获取受限身份信息；当前两种新 Agent 都**未纳入分类**。新进程名只做严格正向匹配、确认父子关系，不能因 `node.exe`、`bun.exe`、`tauri`、`WebView2` 等通用进程出现就认为属于某 Agent。
- **代码事实** — [agents/monitor.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/monitor.py)：现有 `ProcessProbeWorker`（慢速 Windows/WSL 发现）、`Monitor`（周期融合）、`WATCHERS`（四类终端文件 watcher）、`_register_desktop_sources`、`_poll_desktop_sources`、`_commit_exit` 和 UI revision。网络/SSE 不能直接插入每次 `_tick`；断连不能以 `authoritative empty` 清空已知实例。
- **代码事实** — [agents/desktop.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/desktop.py)：`DesktopSessionSource.poll(hosts, claimed_sessions, now)`、`DesktopSourceSnapshot` 与 CLI `SessionClaimKey`。**重要兼容陷阱**：Monitor 目前按 `source.kind` 退出同类型 desktop targets；不应把同种 Agent 的不同桌面实现做成两条各自会清理另一条会话的 source。应每个 kind 仅注册一个桌面聚合 source，或在明确修改 Monitor 所有权匹配规则并补测后支持多 source。
- **代码事实** — [agents/state.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/state.py)：待审批只凭精确会话证据；纯文本滚动不能越权覆盖已知 Done/Idle；活动 TTL 超时转 Unknown。保持原样。
- **代码事实** — [agents/zcode_remote.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/zcode_remote.py)、[agents/codex_desktop.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/codex_desktop.py)、[agents/zcode_desktop.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/agents/zcode_desktop.py)：复用结构化 observation、bounded sessions、host lease、last-good/stale；**不要**照搬 ZCode 私有 SQLite 表到 OpenCode/DSH。
- **代码事实** — [pet/config.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/pet/config.py)、[pet/presentation.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/pet/presentation.py)、[pet/dashboard.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/pet/dashboard.py)：`_KIND_KEYS`、默认 monitor agents、eligible_kinds、Dashboard 的开关和选择器均须同步；单靠新增 Enum 不会使 UI 完整工作。
- **代码事实** — [tests/test_desktop_sources.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/tests/test_desktop_sources.py)、[tests/test_discovery.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/tests/test_discovery.py)、[tests/test_state.py](https://github.com/invincible-summer/Deskpet-For-Agents/blob/9a019bf5b14fd282304d9d4371105bd19f7ac86e/tests/test_state.py)：沿用 snapshot 三态、进程代次/WSL 多用户、跨界面会话 claim、stale 与 Waiting 精确归属测试。

### B. OpenCode — 稳定公开契约、可变数据面及安全限制

1. **Official contract** — [OpenCode Server](https://opencode.ai/docs/server/)：TUI 运行时启动 HTTP Server；`opencode serve` 默认 `127.0.0.1:4096`，而 TUI 可使用**随机端口**；`GET /global/health` 返回健康与版本；`GET /global/event`、`GET /event` 是 SSE；`GET /session`、`GET /session/status` 是只读会话 API；`/doc` 提供实例级 OpenAPI。**不能把 4096 当作发现规则；不能因为发现本地 HTTP 端口就信任其为 OpenCode。**
2. **Official contract** — [OpenCode Plugins](https://opencode.ai/docs/plugins/)：列出 `permission.asked`、`permission.replied`、`session.created/updated/deleted/idle/status/error`、`message.part.updated`、`question.asked`（版本相关）等订阅事件。插件是**可选扩展**，零配置路径不安装它；事件是否被当前运行实例通过 SSE 广播，需要对照实例 OpenAPI/实测验证。
3. **Official contract** — [OpenCode Windows / WSL](https://opencode.ai/docs/windows-wsl/)：原生 Windows 与 WSL 均可运行；Desktop 支持连接 WSL 后端；WSL 的配置和 session 保存在该 WSL 用户环境的 `~/.local/share/opencode/`。官方的 `0.0.0.0` 示例伴随密码防护警告：DeskPet **不得为了监听而启动服务、改监听地址或打开网络暴露**。
4. **Official contract** — [OpenCode Server Authentication](https://opencode.ai/docs/server/#authentication)：`OPENCODE_SERVER_PASSWORD` 启用 HTTP Basic Auth，用户名可用 `OPENCODE_SERVER_USERNAME` 设置。DeskPet 不枚举/读取环境里的明文密码，不绕过访问控制；未能合法、安全读取的接口标为 unavailable。
5. **Upstream implementation, version-specific** — [OpenCode TUI sync](https://github.com/anomalyco/opencode/blob/dev/packages/tui/src/context/sync.tsx)：上游前端按 `sessionID` 缓存 status/permission，收到 `permission.replied` 后移除同 ID pending。此为开发分支实现佐证，而非不可变跨版本协议；使用前须用目标实例 `/doc` 或 fixture 验证具体字段。
6. **Experimental/variant, NOT baseline** — [OpenCode v2 API](https://dev.opencode.ai/v2/docs/api/)：`/api/session/{sessionID}/permission/{requestID}` 等路由属于另一接口族。当前目标**只读**，不实施或保存任何 `reply` 路由，不同时叠加经典/v2 写入方案。
7. **Implementation gate** — 精确等待审批必须有 `(server incarnation, session ID, permission request ID)` 且收到当前有效 `asked`，并能够以对应 `replied` 或权威 pending 状态撤销；如断流、重启或只读接口不提供当前 pending 快照，应显示 stale/Unknown，不永久保留 Waiting。
8. **OpenCode Desktop** — official docs support desktop app, but **host executable/process tree, spawned backend process, active tab mapping, multi-window data ownership** are NOT specified as guaranteed stable detection contracts. These must be gathered from sanitized local probe before permitting precise terminal/desktop de-duplication.

### C. DSH — 官方进程内事件与持久化，不等于公开只读远程 API

1. **Official contract** — [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)：DSH 处于 developer preview，官方明确提示 breaking changes；`dsh web` 通常监听 `127.0.0.1:3080`，但端口可配置。只能以进程所有权 + 后端身份绑定，**不能把端口 3080 当唯一身份**。
2. **Official contract** — [Cordis events](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/framework/events.md)：`session/event` 为 Cordis 内部回调；持久会话事件 `turn/start`、`turn/end`、`step/start`、`step/end`、`tool/call`、`tool/result` 是 log event.type，**不是 SSE 事件名或现成跨进程订阅 URL**。
3. **Official contract** — [Session](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/session.md)：每条日志带 `type/seq/time/data`，`session/event` 是完成 append 后的事件回调。尚未证明公共、只读、可安全绑定到 CLI/Web/Desktop 的订阅入口。
4. **Official contract** — [Approval](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/approval.md)：`approval/asked` 和 `approval/decided` 是审计事件，`ApprovalRequestId` 只与对应审批配对，不能代替 agent/session/tool-call ID。DeskPet 仅用配对事件提示/解除 Waiting，**不订阅决策瀑布流 `approval/request`、不注册 answerer、不回写 outcome**。
5. **Official contract** — [Session Persistence](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/persistence.md)、[JSONL backend README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-persistence-jsonl/README.md)：`dsh-session-persistence-jsonl` 逻辑日志按 session 持久化，**物理编码默认是校验后的 Zstandard 帧**，而非可直接 `readline()` 的纯 JSONL；`compression: none` 才使用裸文本。持久化后端还可替换，不能推断 `~/.dsh` 下每个文件均为可 tail 的标准 JSONL。严禁无界扫描历史 logs、解压整个仓库来推断当前进程。
6. **官方 Desktop 与社区发行版应分别适配** — [deepseek-ai/deepseek-harness/apps/desktop](https://github.com/deepseek-ai/deepseek-harness/tree/master/apps/desktop) 已提供**官方 Electron Desktop**；[官方 README](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md) 确认 Electron RunAsNode Host、私有 IPC/HTTP bridge，**当前版本默认由 OS 分配端口**。 [DeepSeek 官方下载站](https://www.deepseek.com/en/download/)亦已提供 Harness Desktop。[xingj404-lab/dsh-desktop](https://github.com/xingj404-lab/dsh-desktop) 与 [tscodeplus/dsh-desktop](https://github.com/tscodeplus/dsh-desktop) 等属于兼容范围单独确认的社区版，不代表官方产品。
7. **Community issue/evidence** — [DSH web 403 / Origin troubleshooting](https://github.com/deepseek-ai/deepseek-harness/discussions/313)、[configurable port](https://github.com/deepseek-ai/deepseek-harness/discussions/1868)：这类 issue 证明本地 host/port/trust 规则存在差异，并不授权 DeskPet 仿造浏览器令牌或重放内部 RPC。
8. **Third-party, optional and untrusted contract** — [kelemiao/dsh-api](https://github.com/kelemiao/dsh-api)：社区桥可提供独立 API，但不是 DSH 官方稳定只读监听面；第一阶段不安装、不将其读写 RPC 原样接入。

**DSH 接入决策门槛**：先审查当期官方 CLI/Web/**Electron Desktop** 的合法只读接口与会话持久化。只有 L0 时不得宣称与 Codex 功能同样齐全；若确实没有零配置只读事件数据面，应明确提出**用户主动安装的纯观察 Cordis 插件**作为可选 L1 方案，不注册审批 answerer，不修改会话与授权。

### D. 调研结果与未证实点矩阵

| 对象 | 可确认依据 | 尚需实机/版本验证 | 失败时行为 |
| --- | --- | --- | --- |
| OpenCode Windows CLI | CLI 启动 server；官方 GET/SSE/会话 API | exe/node/bun 进程名、随机端口归属、鉴权、pending 快照 | 保留进程 Unknown；不伪造 Waiting |
| OpenCode WSL CLI | WSL 官方支持；同源用户存储 | distro+uid+server PID 对应、跨 WSL 访问、后端调用路径 | WSL 不可读 → stale；不唤醒 distro |
| OpenCode Windows Desktop | Desktop 可连接独立/WSL 后端 | GUI 宿主与 server 子树、窗口前置、前端 session 归属 | 仅前置已验证 GUI 宿主，或只显示运行存在 |
| DSH CLI | 进程内 Cordis event 与 Session 类型 | CLI 真实进程入口及有效事件输出路径 | 只显示进程运行 Unknown |
| DSH Web | 默认本地 web UI、会话日志 | 是否提供受支持的**只读**会话与 pending 接口 | 进程运行 Unknown，不探测有权限写 RPC |
| DSH 官方 Desktop | 官方 Electron + RunAsNode Host + 认证 Web/IPC 桥接，默认随机端口 | GUI 与 backend PID/代次、合法只读能力、会话归属、隐藏与退出 | 无会话身份只报告宿主，禁止误报完整支持 |
| DSH 社区 Desktop | 各发行版有不同 Tauri/Electron 壳 | 具体发行版进程、端口、数据源与宿主绑定 | 逐发行版声明支持，不假定通用入口 |

### E. 最小接入取舍与禁止项

- **推荐**：OpenCode 优先同一现有 server 上的只读 SSE/GET + 有界后台连接，DSH 先 process-only、寻找真实只读 API；如果无法得到明确会话/审批证据，则**接受功能降级**，不为“显示一个状态”追加高资源后台服务。
- **线程和性能**：复用一次 Windows process census 与现有 WSL ProcessProbeWorker；给多个 OpenCode endpoint 分配一个有界 IO 管理 worker 而非每会话起线程；Monitor 每 tick 只复制内存快照，Tk 禁止网络、数据库/WSL 读。只有检测到可证实活跃进程/后端，才建立事件连接。
- **安全只读**：API 仅 allowlisted GET/SSE；**不执行 POST/PUT/PATCH/DELETE**；即使上游某个只读 RPC 使用 POST，也不可未经审核自动纳入。禁止 Agent 插件自动安装/配置改写、HTTP 端口暴力探测、WSL root fallback、新增后台解压守护、任何审批按钮。
- **会话身份和 stale**：key 至少包含 provider + Windows/WSL plane/uid + server process incarnation + exact session ID；权威删除与读取失败严格分离；断线/未知 pending 立即撤销“确定 WAITING”展示（可改为 stale 提示），历史已完成 session 不复活成活跃 target。
- **待完成证据**：对两类 Agent 的每个目标环境采集不含对话/命令/token 的 fixture（process family、server health/version、event name、session id 的哈希、source authority）；把结果与使用的上游 commit/version 补回本文和 [plan.md](plan.md)。无实机，不得声称全部接入已通过。




---

## 2026-10-11 追加：功能对等审计与官方 Desktop 更正

> 本节是针对“OpenCode、DSH 要像现有 Codex 等 Agent 一样接入桌宠全部功能”的静态审计增量；**不是实现已完成声明**。DeskPet 审计基线 `81ed56c9f6d39511dbe33fabb08b875d5d70df91`。当前仅完成代码与公开官方文档核验，Windows/WSL 上真实 GUI/Agent 尚未测。

### 新 Agent 必须经过的代码路径

| 当前源码（均为仓库 main） | 已有事实与新增遗漏 |
| --- | --- |
| [agents/models.py](agents/models.py) | AgentKind 现有 Claude/Codex/Kimi/pi/ZCode；须统一扩展 OpenCode/DSH 的 label、instance key、DesktopHost、Observation、Target，无第二套状态模型 |
| [agents/discovery.py](agents/discovery.py) | 一次 psutil census 与 WSL ProcessProbeWorker；两种新 Agent 在 Windows CLI/WSL CLI/Desktop host/helper 的严格分类还未实现；generic node/bun/Electron 不能认作 Agent |
| [agents/paths.py](agents/paths.py) | 默认会话根/子目录只列出四种 CLI；不能单加 WATCHERS 造成 KeyError 或扫描全部用户 HOME，必须为新数据面独立路径及严格 containment |
| [agents/monitor.py](agents/monitor.py) | WATCHERS 只有 Claude、Codex、Kimi、pi；desktop source 只注册 Codex/ZCode；网络/WSL 慢 IO 不可塞进 0.5s tick，须只读内存事实缓存 |
| [agents/desktop.py](agents/desktop.py) | DesktopSessionSource 与 CLI exact claims 保留。**多个相同 kind 的 source 当前会在 Monitor 依 kind 清退时互相误删**；需要单个 kind 所有者或 source ownership 修正及回归 |
| [agents/state.py](agents/state.py) | Status.WAITING 与 Status.INPUT 不同、Mode/Phase 正交，结构化证据优先；已足够好，保留 |
| [agents/terminal_uia.py](agents/terminal_uia.py) | DEFAULT_RECOGNIZERS **只有 Codex/Claude/Kimi**。新 Agent 不能自动识别终端审批弹层，结构化原生 events 优先；真实 UIA 文本录样才允许增加严格 recognizer |
| [agents/terminal_resolver.py](agents/terminal_resolver.py) | 标题的 Kind/CWD/WSL 评分与 observation/window 双绑定复用；需新 kind 的终端标题和多 pane 的准确性验收，不得因同目录把不同实例互绑 |
| [agents/desktop_window.py](agents/desktop_window.py) | 现只 allowlist chatgpt.exe、codex.exe、zcode.exe；官方 DSH Electron 与 OpenCode GUI 的真实 exe/祖先链须做严格验证和白名单，保留 APP_ONLY 不导航私有 session |
| [pet/config.py](pet/config.py) | DEFAULTS.monitor.agents、presentation.concurrent.eligible_kinds、_KIND_KEYS 必须同时新增两类，并保留旧配置迁移、source 开关、privacy policy |
| [pet/dashboard.py](pet/dashboard.py) | **至少三处写死的 Kind 循环/元组**（约 576、1128、1318 行）用于呈现筛选、监听设置和按 Kind 计数；必须同步，否则 Agent 被发现但不完整出现在 Dashboard |
| [pet/presentation.py](pet/presentation.py)、[pet/labels.py](pet/labels.py) | focused/attention、SINGLE/AGGREGATE/FLEET、slot selector、overflow、DONE/WAITING 动画、Windows/WSL/Desktop 标签须对新种类同等有效 |
| [pet/tray.py](pet/tray.py)、[pet/app.py](pet/app.py)、[pet/ui_coordinator.py](pet/ui_coordinator.py) | 经统一 AgentTarget 的托盘、双击唤醒、UI dirty revision、show/hide、shutdown 必须有实际交互回归，不建议重写稳定 UI |
| [tests/test_discovery.py](tests/test_discovery.py)、[tests/test_desktop_sources.py](tests/test_desktop_sources.py)、[tests/test_terminal_uia.py](tests/test_terminal_uia.py) | 加入上述入口的逐项身份、Waiting、stale、退出、切换、负样例，而非只测 Enum 或首次发现 |

### OpenCode 状态覆盖需要细分的两个请求族

- **Official**：[Server](https://opencode.ai/docs/server/) 提供只读 session/status、SSE；TUI 随机端口、`opencode serve` 默认 4096，只能根据确切进程端口对应识别，不能扫描全端口。
- **Official**：[Plugins](https://opencode.ai/docs/plugins/) 有 `permission.asked` / `permission.replied` 及 `question.asked` / `question.replied` / `question.rejected`。应分别映射 DeskPet **WAITING（审批）** 与 **INPUT（要求用户提供信息）**，不能混淆。同一后端其他 session 的事件不能污染现有 target。
- **Upstream code evidence**：[TUI context/sync.tsx @ dev](https://github.com/anomalyco/opencode/blob/dev/packages/tui/src/context/sync.tsx) 分开维护 permission、question 和 session_status；实际 JSON id/sessionID/property 字段版本可能变。
- **Empirical**：[OpenCode issue #11616](https://github.com/anomalyco/opencode/issues/11616) 指出特定版本有 `GET /permission/` / `GET /question/` pending 列表；**不是稳定官方承诺**，必须在目标实例 /doc/GET 安全探测后才用。否则断线不应继续声称准确 Waiting/Input。
- **产品同等要求**：除进程在场之外，必须覆盖项目/会话标题、当前 turn、阶段、完成、待确认、错误、窗口/终端唤醒、多个 session、Mode 正交、重启与 stale、聚合/多宠/托盘/诊断与配置；无可靠数据则 L0，不能标为“全功能”。

### DSH 的官方发行版、认证与只读事实获取（更正）

- **Official product**：[DeepSeek 下载页](https://www.deepseek.com/en/download/) 和 [Harness 官网](https://www.deepseek.com/en/harness/) 已包含官方 Desktop。
- **Upstream implementation**：[official apps/desktop README](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md)：Electron 主进程 + RunAsNode 私有 Host；GUI 入口 `dsh-app://app/`；后端 HTTP 请求走授权桥，WebSocket 凭证仅归官方应用。**当前 Desktop 默认 OS 分配随机端口**（历史资料中 19387 已不适合作为无版本区分的规则），另一个常见 Web 3080 亦不决定实例身份。关闭窗口可能隐藏/驻留，不能直接视为 Host 已退出。
- **Upstream implementation**：[architecture](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md)：CLI 与 Desktop 共享 `$DSH_HOME` 的产品数据但 profile/插件/运行时不同，不得把共享会话目录当作 CLI/Desktop 同时运行的会话所有权证明。
- **Official**：[client connection](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/connection/README.md)：Host RPC 和 WebSocket event-stream 由浏览器会话认证，launch token 只用于首页兑换 cookie；无“localhost 自动只读 tier”。严禁 DeskPet 读取进程环境/浏览器 cookie/`.credentials.yaml`、伪造 IPC 或拦截官方桥。
- **Official**：[session persistence JSONL](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-persistence-jsonl/README.md)：日志逻辑 JSONL，但默认 zstd 校验帧、文件版本不断变化，`sessionPersistence.open(id,'read')` 是进程内服务，不是现成的外部 API；大范围解压整个历史数据不是轻量监听。
- **Official**：[approval](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/approval.md)：`approval/asked`/`approval/decided` 是 session-scoped 审计事件；`approval/request` 是会做决议的 waterfall，监听绝不能订阅 answerer 或返回 outcome。
- **L1 取舍**：先验当前安装版是否有可合法无副作用读取的 SessionQuery/会话状态；不具备时，可评估仅由用户手动安装的、**只订阅 `session/event`/只读 Agent status 并向 DeskPet 单向输出脱敏状态的 Cordis 插件**。它不提交任何审批决议，也不得修改 session/Agent 数据。无 L1 路径时 DSH 只能声明 L0，**不能计入全功能验收**。

### 审查中发现的必要修正

1. 旧的 `19387` 固定 Desktop 端口说法、只有社区 DSH Desktop 的说法被上游新资料推翻；代码必须遵从精确版本+本机端点归属。
2. 新 Kind 只增加进程发现无法接入现有 UIA recognizer / GUI 激活白名单 / Dashboard 硬编码展示入口。
3. OpenCode 的 `question` 与 `permission` 两种等待必须分离，只有明确事件/当前未决集合才能声称待审批。
4. DSH 只做 L0 虽安全但**不满足用户最新提出的完整功能对等**；必须标记为未通过，而不是当作同等完成。
5. 目前尚未得到用户机器上的 Windows CLI、WSL、OpenCode Desktop、官方 DSH Desktop 的真实 PID/端口/会话/窗口 fixture；任何精确支持声明须经该环节验证。

本节上游主分支证据分别参考 DeepSeek Harness `master` tree `d743267388641bc76f17c45ce8b4c231aed1d32c`、OpenCode `dev` tree `0d61729b3fa005cec9aa754c075a809663c736a4`。这些是**代码树 SHA，不是发布版本号**，版本漂移仍须目标安装实测。


### 补充身份边界：子任务与短生命周期

- **OpenCode 官方 API**：[Server](https://opencode.ai/docs/server/) 的 `GET /session/:id/children` 可辨识 session parent/child。**不得把子 Agent 的 child session 与 root session 同时自动生成两个重复宠物**；如果产品选择 root 聚合，child 的活动只更新 root 的 phase/summary，审批仍保留可核对的 child request ID，且绝不可串给另一个 root。
- **DSH 官方 SessionHeader**：[Persistence](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/persistence.md) 的 `parentSession` 和 subagent child classification 可确定 lineage。旧版或不完整历史 lineage 未证实就不猜；状态归属规则必须与既有 ZCode 的 root/child 会话聚合语义一致。
- **DSH CLI 不都是长驻交互端**：[官方架构](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md) 列出 web/headless/sdk/acp profiles。headless one-shot 可能在现有约 3s census 之间结束，**进程扫描无法保证一定抓到**。要将短任务“完整监听”列为独立 P0/P5 gate；不得通过高频 process polling 人为补齐，只有合法低开销事件/持久化证据才能补足。
- **多桌面宿主 key 防碰撞**：`AgentInstance.__post_init__` 的 Desktop key 当前由 host token 与 session id 组成，host token 有时只取精度有限的创建时间；在多实例同 session id 的合成测试中必须确认不会碰撞，若真的碰撞再以最小局部修复纳入 host PID/incarnation。现有稳定实例 key 不为想象中的缺陷盲改。
