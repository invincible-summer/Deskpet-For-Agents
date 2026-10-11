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

## 2026-10-11: OpenCode / DSH 无插件只读监听调研（覆盖此前方案）

**已确认用户约束**：不向 OpenCode 或 DeepSeek Harness (DSH) 安装任何插件、Hook、MCP 或 Agent-side bridge；不改 Agent 配置、不启动额外服务、不批准/拒绝、不注入输入。DeskPet 自己打包必要的只读文件解码库不属于 Agent 插件，但其 CPU、RSS、构建体积必须实测。**目前只有静态源码审计，并未实现或实机验收。**

源码审查基线：DeskPet [2271814](https://github.com/invincible-summer/Deskpet-For-Agents/commit/2271814db25edd8b0bff503e50743d7c086e92b5)；OpenCode dev tree `0d61729b3fa005cec9aa754c075a809663c736a4`；DSH master tree `d743267388641bc76f17c45ce8b4c231aed1d32c`。上游源码分支/私有数据库 schema 属版本性实现，不等同稳定 API。详细实施与验收见 [plan.md](plan.md)。

### 1. DeskPet 不需要重写的已有架构

- [agents/models.py](agents/models.py)：AgentKind、TERMINAL/DESKTOP、AgentInstance/DesktopHost、Observation/Status/Mode/Phase/Snapshot/AgentTarget；新增 Kind 而不另造状态系统。
- [agents/discovery.py](agents/discovery.py)：Windows 单次 psutil 进程清单，WSL fresh running-distro gate、uid/进程代次、authoritative/stale/tombstone。不能再开一个高频全进程扫描器。
- [agents/sqlite_ro.py](agents/sqlite_ro.py)：已经具备 SQLite URI 只读读取与 schema capability；[agents/zcode_remote.py](agents/zcode_remote.py) 证明慢速 WSL 数据读取应由 ProcessProbeWorker 拥有；[agents/desktop.py](agents/desktop.py) 的 DesktopSourceSnapshot 支持逻辑会话。
- [agents/monitor.py](agents/monitor.py)：旧 WATCHERS 只含 Claude/Codex/Kimi/pi，桌面 source 仅 Codex/ZCode。`_poll_desktop_sources` 当前按 source.kind 清理 desktop target，两个同 Kind source 不可直接并列注册。
- [agents/state.py](agents/state.py) 已有 WAITING、INPUT、WORKING、DONE、IDLE、UNKNOWN 严格证据分离；[agents/terminal_uia.py](agents/terminal_uia.py) 默认待审识别器只有 Codex/Claude/Kimi；[agents/desktop_window.py](agents/desktop_window.py) 当前 GUI 白名单只有 Codex/ZCode。
- [pet/config.py](pet/config.py)、[pet/dashboard.py](pet/dashboard.py) 多处显式 Kind 集合必须同步；[pet/presentation.py](pet/presentation.py)、[pet/tray.py](pet/tray.py)、[pet/app.py](pet/app.py) 按 AgentTarget 复用关注度、单宠/聚合/多宠、唤醒、动画与托盘。

### 2. OpenCode：SQLite 为低成本底座，已运行服务的只读 GET/SSE 补齐瞬态状态

- **上游源码事实**：[core/src/session/sql.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/sql.ts) 定义 `session_v2`（id/project_id/parent_id/directory/title/time_idle/time_suspended/idle_outcome 等）及 `session_message`、`session_pending`、`session_inbox`；注意 **session_pending 是会话输入队列，绝非审批 pending**。
- **上游源码事实**：[core/src/session/store.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/store.ts)、[core/src/session/execution.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/execution.ts) 表明 `time_suspended` 是 durable execution claim，崩溃后可能遗留。不可只凭非空 claim 宣称 Working；需要确认运行中进程、正确 session owner 和最近 turn。
- **上游源码事实**：[cli/src/database-path.ts](https://github.com/anomalyco/opencode/blob/dev/packages/cli/src/database-path.ts) 允许 `opencode.db` 或发布渠道后缀，或配置覆盖。基于正确用户的数据根/正向版本探测找目标 DB，不能扫描整个 HOME 或读取进程环境中的认证 secrets。
- **上游源码事实**：[core/src/permission.ts](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/permission.ts) 显示 pending permission 在运行时 Map 中；SQLite **不足以无条件捕获 WAITING**。[server/src/handlers/permission.ts](https://github.com/anomalyco/opencode/blob/dev/packages/server/src/handlers/permission.ts) 提供只读 permission list 处理器；[server/src/handlers/event.ts](https://github.com/anomalyco/opencode/blob/dev/packages/server/src/handlers/event.ts) 实现 SSE。
- **官方文档**：[Server](https://docs.opencode.ai/docs/server/) 与 [V2 API](https://dev.opencode.ai/v2/docs/api) 覆盖实例 health、session/status、SSE、部分版本的 permission/session pending GET。只有识别为具体 **live PID 所有的 loopback 服务**、实例 `/doc` 可用且授权合法时才可 GET/SSE；无所有权映射时不探测，也不启动 serve 或强迫 Agent 开端口。
- **SQLite 安全规则**：[SQLite URI](https://www.sqlite.org/uri.html) 用 `mode=ro`、`query_only=ON`，[SQLite WAL](https://sqlite.org/wal.html) 规定正确读锁与共享环境。**正在变化的 DB 禁止 immutable=1**：它会忽略并发变更。Windows DB 直接读；WSL SQLite/WAL 禁止 Windows 端跨 UNC 直开，必须由现有 WSL Worker 在已运行的相同 distro/uid 内做有界 read-only SQL；如运行环境缺读取器，应降级而非自动安装软件。
- **明确缺口**：DB 不保证实时 permission、question、当前 TUI 绑定或 Plan Mode。仅通过高置信实时 API/事件或实际当前 UIA 才显示 WAITING/INPUT；其余 UNKNOWN，不得报“全功能通过”。

### 3. DSH：直接读取已经存在的会话持久化，增量解码 Zstandard frame

- **官方默认路径**：[bundle/base/cordis.patch.yml](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/base/cordis.patch.yml) 的 session-persistence-jsonl `root: !!js dshHomePath('sessions')`，通常为 `$DSH_HOME/sessions`。但用户 profile 可以改写；必须由实际用户/profile/root 的可信正向证据验证，不能盲扫。
- **官方物理格式**：[JSONL persistence README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-persistence-jsonl/README.md) 描述 `session.vN.jsonl.zstd`（默认）与 compression:none 的文本 `session.vN.jsonl`，按 cwd projectKey / encoded session id 分目录，且支持历史代际，不可无界解析所有日志。
- **上游字节实现**：[src/zstd.ts](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-persistence-jsonl/src/zstd.ts) 的 `scanZstdFrames` 区分完整 frame 与不完整尾帧；每次 append 一个独立可校验、可解码的 zstd frame。实现 DeskPet 有界 `file identity + format generation + complete-frame offset` tail，**仅解码已完成帧并进行 checksum 校验**，读取失败不修改 Agent 文件、不可 replay 半帧。
- **上游落盘机制**：[src/storage.ts](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-persistence-jsonl/src/storage.ts) 的 `LIVE_WRITE_BATCH_MAX_DELAY_MS=200` 是批处理延迟上限常量，不代表外部监听 200ms SLA；Event Loop / fsync 等可能导致更长延迟。尚未 materialize 的 session 根本没有文件。
- **官方事件**：[Cordis event system](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/framework/events.md) 的 `session/event` 为**进程内部**广播，不是跨进程 SSE。Durable `turn/start`、`turn/end`、`step/*`、`tool/call`、`tool/result` 属于日志记录；[User approval](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/approval.md) 将 `approval/asked` 与 `approval/decided` 在同 session 内按 request id 配对。只做事实观察，不参与会决议的 `approval/request` waterfall。
- **官方桌面**：[apps/desktop/README.md](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md)：已存在官方 Electron + RunAsNode Host，**当前默认 OS 随机端口**，关闭窗口可隐藏而非终止；[client/connection](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/connection/README.md) 指明 Host API/WS 均有会话认证。DeskPet **不使用**其私有接口、IPC、launch token、cookie、`.credentials.yaml`。
- **WSL 读取边界**：DSH 是顺序追加的普通压缩文件，和 SQLite/WAL 不同。若本轮 WSL census 确认 distro 正在运行且 uid/HOME/路径 containment 严格匹配，可通过 UNC **只读已知单文件尾部**，但不能用 UNC 探索停止的发行版；UNC 延迟必须实测并可按成本切换为现有 worker 有界远端读取/降级。
- **DeskPet 的可选库（不是 Agent 插件）**：[python-zstandard](https://pypi.org/project/zstandard/) 可以考虑随程序打包并 lazy-import，测试 Python 3.12 Windows wheel、安装体积和 RSS；若不愿加入该库，默认 zstd 就不能按纯标准库正确增量解码，功能需要明示降级。
- **可靠性限制**：很久前未配对 asked 可能落在 bounded tail 之前；Agent crash 的不完整 turn 不是仍在工作；可替换的 session persistence 不一定是 JSONL。无法确认未决状态时应 stale/UNKNOWN，绝不将旧记录误报实时审批。

### 4. 明确方案和验收边界

**最终选择**：现有单进程进程 census + 新增 OpenCode SQLite adapter / DSH append-log adapter + 可确认实例的 OpenCode GET/SSE 辅助 + 统一 Observation、DesktopSessionSource、窗口绑定与 Presentation。没有任何 Agent-side plugin 或附带 Node 服务；不安装东西进 DSH/WSL，不改变认证边界。不能为显示状态牺牲低 CPU/RSS 或误报 WAITING。

**仍需实机证据**：Windows、WSL、官方 OpenCode GUI、DSH 官方 Electron GUI，进程/文件根、session ownership、zstd 多代格式、权限对消时效、窗口恢复、混合多实例、启动/退出，及对现有 Codex/Claude/Kimi/pi/ZCode 零回归。每项必须 PASS/FAIL/DEGRADED/NOT_TESTED 四态；只看到进程（L0）不能宣称与现有 Codex 完全对等。详细完整矩阵与文件接口见 [plan.md](plan.md)。


**追加的 SQLite 只读验证细节**：[官方 WAL 只读行为](https://www.sqlite.org/wal.html#readonly) 允许 WAL 的 read-only 打开依赖现存 `-wal`/`-shm` 或目录写权限；故 `mode=ro` 不代表进程绝对不可能创建 `-shm`。DeskPet 的“零 Agent-owned 写入”需要在打开前后检查 sidecar、数据库目录与文件元数据，若不能证明纯读则放弃该数据源。不可把 `immutable=1` 用在仍更新的 OpenCode 数据库，参见 [SQLite URI](https://www.sqlite.org/uri.html)。

---

## 2026-10-11 实现状态：OpenCode / DSH adapter 已落地（实机 NOT_TESTED）

上一节的方案已按 [plan.md](plan.md) 实现为源码（实现文件与自动化验收如下）。**真实机器上的 OpenCode / DSH 实机探测仍未执行；按 plan §四 P0/§七 的判定规则，所有实机相关能力保持 NOT_TESTED，实机证据以 `tools/desktop_source_probe.py` 的脱敏输出为准。**

实现（本工作树，尚未打 tag；合并时在本节回填 commit 链接）：

- [agents/opencode.py](agents/opencode.py) — `session_v2` capability 探测 + 固定白名单 SELECT；WAL `-shm` sidecar 零写入门禁（读连接前后对比）；`service.json` 只取 url/pid/version（password 字段显式丢弃）；`GET /api/info` + `GET /api/permission/request` 只读补充（401/403 → 合法不可用降级）；WSL 侧由 ProcessProbeWorker 经发行版内 `python3` 只读脚本查询。
- [agents/dsh.py](agents/dsh.py) — 上游 `projectKey`/`encodeSegment`（UTF-16 code unit 语义精确移植）、`session[.vN].jsonl[.zstd]` 世代选择（最高版本 / 双编码拒绝 / >v4 拒绝）、`scanZstdFrames` 结构扫描与逐完整帧解码（checksum + 解压硬上限 + 半帧等待）；`turn/*`、`step/*`、`tool/*`、`approval/asked|decided` 状态机（id 配对、回放预算、crash orphan 不判 Working、未知必需事件拒绝）。
- [agents/monitor.py](agents/monitor.py) — `OpenCodeWatcher`（注入共享 runtime，消费 Worker 缓存的 WSL/HTTP 事实）与 `DshWatcher` 进入既有 WATCHERS；每 Kind 注册唯一 Desktop source（`OpenCodeDesktopSource` / `DshDesktopSource`）。
- [agents/discovery.py](agents/discovery.py) — `opencode.exe` CLI 与 OpenCode Desktop（Electron 佐证）二义消解；`dsh.exe` CLI；`DeepSeek Harness.exe` 宿主 + 同 exe RunAsNode helper 收敛；WSL 严格 argv0 匹配；`OPENCODE_DB`/`DSH_HOME` 进入 env allowlist。
- [agents/paths.py](agents/paths.py) — OpenCode data/state 根与 DB/service 候选（有界正向）；DSH `~/.dsh/sessions`；WSL containment helper；旧四类文件 watcher 映射对新 Kind 显式拒绝。
- [agents/desktop.py](agents/desktop.py) — `build_terminal_claims` 增加 `terminal_claims()` 钩子（DB 索引型 watcher 暴露 exact session claim）。
- [agents/desktop_window.py](agents/desktop_window.py) — GUI APP_ONLY 激活白名单加入 OpenCode（OpenCode/OpenCode Dev/OpenCode Beta.exe）与 DeepSeek Harness.exe。
- [tests/test_opencode_source.py](tests/test_opencode_source.py) / [tests/test_dsh_source.py](tests/test_dsh_source.py) — schema/零写入门禁/session_pending 负样例/审批配对/帧扫描与解码上限/路径遍历中和/同 cwd 多实例不绑定/世代与双编码拒绝/claims 去重/子会话折叠等 59 项回归。
- [tools/desktop_source_probe.py](tools/desktop_source_probe.py) — 新增 OpenCode 与 DSH 脱敏实机探测项。

自动化验收（本机 Windows Python 3.12）：`python -m compileall -q agents pet tools tests` PASS；`python -m unittest discover -s tests` 886 tests PASS；monitor / desktop-sources / presentation / ui-architecture 四项阻塞 benchmark PASS；`git diff --check` PASS。

已知的诚实降级（非缺陷）：OpenCode 服务端在当前上游版本恒设随机密码，硬约束禁止读取密码，因此待审批/用户问题实时状态实际不可用（显示“状态暂不可读”，DEGRADED）；DSH 压缩日志解码依赖 `zstandard` 库（懒加载，缺失时明示降级）；同一 cwd 多个同类 CLI 无法外部区分会话归属，全部不绑定。

### 实机结构证据（2026-10-11，本机 Windows，脱敏）

- **DSH 文件格式 PASS（结构级）**：本机真实安装的 DSH 在 `~/.dsh/sessions` 下存在 `--C-Users-...-deepseek-harness-default-workspace--` project 目录（与 `dsh_project_key` 移植输出逐字符一致）、`session.v4.jsonl.zstd`（v4+zstd）、5 个完整 Zstandard 帧（无 torn）、全部帧 checksum 解码通过；首行 header 通过 `parse_header_line` 结构校验（version=4、cwd 与真实项目路径一致、root 会话）；事件类型（permission/preset、sandbox/mode、approval/policy、session/end-seed、agent-preset/selected）全部属于上游 KNOWN_SESSION_EVENT_TYPES。该会话为旧空会话（无 turn 事件），状态机正确输出"无状态"（不伪造）。
- **DSH LIVE 语义 / 官方 Desktop 宿主：NOT_TESTED**（探测时无运行中的 dsh 进程与 DeepSeek Harness.exe）。
- **OpenCode：NOT_TESTED**（本机未安装；`~/.local/share/opencode` 与 state 根均不存在）。
- 探针同时确认 `$DSH_HOME/.credentials.yaml` 存在且从未被读取（仅存在性检查）。
