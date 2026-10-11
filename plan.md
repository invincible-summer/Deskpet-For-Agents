# DeskPet：OpenCode 与 DeepSeek Harness（DSH）纯监听接入计划

> 制定日期：2026-10-11。目标仓库：[Deskpet-For-Agents](https://github.com/invincible-summer/Deskpet-For-Agents)，源代码版本 `pet/version.py = 4.1.0`。  
> 静态审计基线：`main@9a019bf5b14fd282304d9d4371105bd19f7ac86e`；外部研究已整理在 [SourceLink.md](SourceLink.md)。  
> **状态：设计方案，尚未实现、未运行 Windows/WSL/实际 Agent 验收。** 本文件不能视为已支持产品的声明。  
> 2026-10-11 的需求决定：**仅监听，不审批**。本文完全替换此前带审批 API/按钮/控制平面的草案。

## 1. 目标、范围及完成定义

DeskPet 被动识别 **OpenCode Windows CLI、OpenCode WSL CLI、OpenCode Windows Desktop**，以及 **DSH CLI、DSH Web 与经确认的 Windows 社区桌面发行版**；尽可能呈现真实任务的 Working / Thinking / Reading / Coding / Testing / Waiting / Done / Idle / Error / Unknown，模式与状态独立；允许原有双击唤醒对应宿主窗口，但不负责具体 tab 或会话导航。支持同时多实例、同用户/跨 WSL 发行版、同一 backend 被 CLI 和 GUI 同时使用。

功能有两层验收：**L0 进程在场**（可识别真实运行的终端 Agent / desktop host，未知会话状态显示 Unknown）；**L1 精确会话状态**（确凿、当前、可重放的 session/turn/permission 事实）。不得用进程存在推断为 Working/Idle，不得用“日志停止更新”推断 Waiting。若某一 DSH 发行版没有可合法读取的结构化数据源，L0 合格、L1 标记“待证据/暂不支持”，不得伪装 L1 全覆盖。

非目标及永久禁止：审批、批准/拒绝、自动权限控制、POST 决议接口、模拟 Enter/Y、粘贴/键盘注入、OCR/桌面截图轮询、启动/停止或重配 Agent 后端、添加 Agent Hook/MCP、修改 Agent 配置、写 Agent 数据库、跨网络暴露服务、读取模型 API key、保存原始提示和命令内容。**不创建 `approval_control.py`、Approve UI、任何决议命令。**

保持轻量：沿用 Python/Tk 单应用、现有 ProcessProbeWorker、Monitor、WindowsExitWatcher、TerminalService、Presentation；不创建常驻 Web 服务、浏览器/Node sidecar、第二套全局进程扫描、每会话线程或不受控数据文件扫描。

## 2. 已审计的当前实现和真正需要改的位置

- **身份与状态**：`agents/models.py` 的 `AgentKind` 仅有 CLAUDE、CODEX、KIMI、PI、ZCODE；`AgentSurface` 是 TERMINAL / DESKTOP；`AgentInstance.key` 使用进程代次，桌面 key 使用 host 代次与 logical session id；`Observation`、`SourceProbeSnapshot` 有准确来源、三态 authority、TTL；`Status.WAITING` 是现有审批提醒状态。应增加 `OPENCODE`、`DSH`，**不新增第二套 Status/Mode 体系**。
- **进程与 WSL**：`agents/discovery.py` 的 `scan_windows_inventory` 对同次 psutil census 分类 terminal/desktop；`WslProcessProbe._match_agent`、`_ps_scan` 和现有 allowlist 读取 distro/uid/PID。当前没有这两种 Agent 的匹配。新增只能在原扫描中完成，重用 authoritative / stale / tombstone、process_token、terminal attachment、launcher canonicalization。不可通过 `node.exe`/`bun.exe`/`WebView2` 的宽泛关键字匹配推断。
- **主循环与数据源**：`agents/monitor.py`：`ProcessProbeWorker` 负责慢 IO；`Monitor._tick` 约 0.5s 执行缓存读取、watcher、terminal、desktop source、reduce；`WATCHERS` 仅配置 Claude/Codex/Kimi/pi；`_register_desktop_sources` 仅 Codex/ZCode。任何网络连接、长期 SSE、WSL 命令不能跑在 Monitor 的逐轮 tick 或 Tk。
- **桌面会话**：`agents/desktop.py` 的 `DesktopSessionSource.poll(hosts, claimed_sessions, now)` 和 `DesktopSourceSnapshot` 适合继续采用。**陷阱**：`Monitor._poll_desktop_sources` 当前根据 `inst.kind == source.kind` 清理不存在于本 source 的 desktop target，两个同 kind source 会互相误删。一个 kind 优先只注册一个 desktop source；若未来扩展为多 source，必须增加明确所有权命名空间及跨 source 测试，而不是静默叠加。
- **状态**：`agents/state.py` 的 ERROR > WAITING > INPUT > WORKING > DONE > IDLE > UNKNOWN、泛化 UIA 活动低于结构化 session 事实、activity TTL 超时为 UNKNOWN 均合理，**保留现状**。新增 adapter 产出同结构 Observation，不把网络异常当作审批或任务完成。
- **WSL remote 实例**：`agents/zcode_remote.py` 证明远程慢 IO 已置于现有 Worker、按新鲜 WSL census 授权、失败保留 last-good。原则可复用；不得照搬 ZCode 私有 DB schema；不得通过 Windows UNC 实时读 WSL SQLite/WAL；不要通过检查 UNC 路径唤醒停止的 distro。
- **UI/配置**：`pet/config.py` 的 `_KIND_KEYS`、`DEFAULTS.monitor.agents`、`presentation.concurrent.eligible_kinds`、`normalize/migrate`，以及 `pet/presentation.py`、`pet/dashboard.py`、`pet/labels.py` 需要同步新增 provider 标签与开关。`pet/app.py` 的窗口前置、`pet/ui_coordinator.py` dirty/revision 和托盘已有机制不做架构修改。
- **回归**：优先扩充 `tests/test_discovery.py`、`tests/test_desktop_sources.py`、`tests/test_monitoring.py`、`tests/test_state.py`、`tests/test_config.py`，使用已有 `tests/benchmark_monitor.py`、`tests/benchmark_desktop_sources.py`、`tests/benchmark_presentation.py`、`tests/benchmark_ui_architecture.py` 作性能前后对照；不要独立再造基准框架。

代码基线另有版本事实：`pet/version.py` 为 4.1.0；`tests/ACCEPTANCE-4.5.0.md` 是既有历史记录，**不是**本次新增功能的已通过证明。未在此环境执行该 Windows 专用测试。

## 3. 上游能力与不可假设的部分

### 3.1 OpenCode

[官方 Server 文档](https://opencode.ai/docs/server/)确认：`opencode` TUI 会运行 server，TUI 地址通常是**随机端口**；`opencode serve` 默认 `127.0.0.1:4096`；`GET /global/health` 提供版本，`GET /global/event` 或 `GET /event` 是 SSE，`GET /session`、`GET /session/status` 是只读接口，`/doc` 是实例自己的 OpenAPI。 [官方 Plugins 文档](https://opencode.ai/docs/plugins/)列出 `permission.asked`、`permission.replied`、`session.status`、`session.idle`、`session.updated`、`session.error` 等事件。**API/事件必须以目标运行实例版本的实际 schema 为准；不默认安装插件**。

[官方 Windows/WSL 文档](https://opencode.ai/docs/windows-wsl/)支持原生 Windows、WSL CLI，以及 Desktop 连接 WSL server；WSL session 存储位于对应 Linux 用户的 `~/.local/share/opencode/`。不能为 DeskPet 监听而改 OpenCode 的绑定地址、开启 `0.0.0.0`、启动额外 `serve`。 [官方认证说明](https://opencode.ai/docs/server/#authentication)指出 `OPENCODE_SERVER_PASSWORD` 可保护 server；凭证不能从目标进程全环境抓取。当前设计**不实现凭证管理器**，对需要口令且无法安全获授权的 endpoint 降级为 L0。

**关键缺口**：CLI 随机端口绑定、Windows 桌面版 exe / server 父子关系、session 是否确实属于同一前端，官方文档没有提供跨进程发现的稳定保证。可从经验证的 `pid → listening loopback socket`、明确命令行端口和宿主进程族映射发现；Windows 尽量复用同次 psutil inventory 得到的 PID 关系；不做任意端口范围扫描；无法精确绑定则不连接。

### 3.2 DSH

[官方仓库](https://github.com/deepseek-ai/deepseek-harness)将 DSH 定义为开发预览期、可能 breaking changes 的 Cordis 应用；`dsh web` 通常有本地默认端口 3080，但可改变、与身份不是等价关系。 [Cordis Event 文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/framework/events.md)确认 `session/event` 是进程内插件回调；`turn/start` / `turn/end` / `step/start` / `step/end` / `tool/call` / `tool/result` 是 durable session log 的 `event.type`，不是可直接请求的 HTTP SSE 路径。 [审批文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/approval.md)确认 `approval/asked` / `approval/decided` 按申请 ID 成对审计；**只读消费者绝对不注册 `approval/request` answerer，也不返回 outcome**。

[官方 Persistence 文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/persistence.md)及 [JSONL backend](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-persistence-jsonl/README.md)确认：日志的逻辑格式为 JSONL，**默认物理编码是 Zstandard frames**；还支持其他持久化 provider。不能凭 `~/.dsh` 猜测所有日志都可普通 `readline()`；不能每次扫完整历史压缩数据；不应强加 zstd 常驻解析依赖。

DSH Desktop 并非当前已证实的统一官方 Windows 可执行产品；[xingj404-lab/dsh-desktop](https://github.com/xingj404-lab/dsh-desktop)、[tscodeplus/dsh-desktop](https://github.com/tscodeplus/dsh-desktop) 等均是社区 Tauri 封装，通常启动原生 `dsh web`。DeskPet 应把实际 `dsh` 引擎进程作为事实源，将经过验证的 Tauri 宿主只作为 surface/窗口前置，不能复制单一社区分支的名称表去推断其他发行版。

**硬性功能门槛**：首次只保证 DSH L0；若 P0 找到官方/当前版本具备稳定只读 session API，才开发 L1。若不存在，用户继续使用零配置只读 L0；可单独评估**用户主动安装的只读 Cordis 插件**（同一 DSH 进程内采集脱敏事件，单向向 DeskPet 发布，不返回任何操作），但不把该插件列入本次默认实现或自动安装路径。

## 4. 建议实施架构（少改、少线程、不拆双架构）

1. **共享身份，不共享协议**。在 `agents/models.py` 增加 `AgentKind.OPENCODE` / `AgentKind.DSH` 及 label。旧 `SourceProbeSnapshot` / `Observation` / `DesktopSessionSource` 语义保持。网络事件必须先归属 `(provider, OS plane, distro+uid, process incarnation, backend identity, session id)`；不能只用 PID、端口、cwd、session id。一个 backend 被 CLI/GUI 多前端共享时，按照**单一权威 session owner**去重：可证实绑定到终端才产出终端 observation；GUI 逻辑会话由归属已证实的 desktop source 产出；两边都不能证实则只显示宿主 Unknown，不复制多个宠物。
2. **进程发现**。在 `agents/discovery.py` 补全严格的 `_match_kind`、Windows runtime roles、`WslProcessProbe._match_agent`；仅识别进程可执行文件、包入口、父子进程链/启动参数的**正向证据组合**。Node/Bun、renderer、gpu、utility、generic webserver 只作为 helper，绝不能生成 Agent terminal target。对 Windows CLI 无直接 ancestry 的场景，列出准确缺失证据，不提升置信度。WSL 复用 fresh running-distro gates、uid/HOME 映射和现有不提权默认。
3. **OpenCode 只读引擎**。新增 `agents/opencode.py`，实现有界端点发现 + GET health/status/session、SSE 流解析、单源内存 snapshot、重连与 session 归属，尽可能通过**一个 provider 级 IO worker**管理有限端点，不为每个会话起线程。Monitor 仅调用 `snapshot()`；绝不在 `_tick` GET/SSE/WSL。`GET /doc`/版本能力在连接建成后检测一次，失败使用最小 GET/SSE 合同且标注版本未知；只保存有限元数据，不保存消息和 tool 原文。
4. **CLI/desktop 映射**。OpenCode 终端实例来自 process snapshot；`OpenCodeRuntimeReader` 按精确后台归属提供终端 `Observation`。桌面用同文件中的单个 `OpenCodeDesktopSource(DesktopSessionSource)` 将实际 host/session 转成现有 `DesktopSourceSnapshot`；必要时 `agents/monitor.py` 扩出极小只读 provider 缓存消费入口。不要给 CLI 安装一个带 BaseWatcher 文件扫描逻辑的空壳，也不要把所有 session 映射到一个不相关 TUI PID。
5. **DSH 最小分支**。先以 `agents/discovery.py` 的严格 DSH runtime 分类构建 L0；P0 未确认 L1 读取合同前，**不新建 `agents/dsh_source.py` 或插件**。有合规原生只读读取面时，新增与 OpenCode 一致的有限 snapshot source；若只有进程内 Cordis event，先更新 SourceLink 再由用户明确批准可选只读插件范围。插件仅为订阅端，绝不能监听 `approval/request` waterfall 或提供双向 RPC。若插件不可行，维持 L0 即可。
6. **不额外抽象共享事件框架**。只有真实两种 provider 均需要相同连接生命周期、queue cap 和 SSE framing 时，才抽出 `agents/event_stream.py` 或 `agents/live_sources.py`；否则放在 `agents/opencode.py` 内，避免为抽象而抽象。复用 `agents/desktop.py` 与 `agents/state.py`，绝不新增独立 session reducer、重复 DesktopSource。
7. **归约语义**。映射 `session.status=busy` / 已知 active turn → Working；`session.idle` / 明确 turn end →短暂 Done 再按实际权威状态转 Idle/Unknown；`permission.asked` 与未决 exact ID → Waiting；`permission.replied` 或权威清除 →解除 Waiting；`session.error`/失败 turn → Error；仅见输出变化 → Working 有限 TTL；仅存活 → Unknown。Unknown/unsupported mode 保留 `mode_raw`，不臆造 Plan 状态；来自其他 session/endpoint 的权限事件一律不可投射。
8. **失败语义**。进程仍在但 endpoint 失败/密码拒绝/SSE 断线/403/版本不兼容 → data-plane non-authoritative/stale，不得当成权威 session exit；`WAITING` 一旦失去可确认的 current pending，不应无限保留确定性 Waiting（变为 Unknown + stale）；进程/host 权威缺席或用户关闭 provider →按现有 `_commit_exit` 完整清理。重连时先订阅后 reconcile 权威 GET 快照；若无可靠 pending 快照，就只能显示重连之后新的等待事件，不“恢复”历史审批。事件去重通过 exact (backend, session, request, event class) 且有界代次；旧代次 event 一律忽略。
9. **配置和呈现**。新增两个 provider 开关，Windows/WSL 总开关继续生效；`presentation.concurrent.eligible_kinds` 对新增两项默认与其他类型一致。Dashboard 仅显示“OpenCode / DSH、Windows/WSL、CLI/Desktop、观察健康、只读事件支持情况”，不加控制面板。气泡维持有界短摘要和现有状态色；超过最大展示数保持 Monitor 全量发现。沿用窗口 identity 检查唤醒 GUI/终端，无法精确对应 session 时只唤醒**确认的宿主**，绝不猜测具体 tab。

## 5. 资源、隐私和网络约束（实施阻塞项）

- 连接必须来自**运行实例和 PID/祖先的精确正向证据**。只允许确认归属的本机 loopback GET/SSE；禁止针对 127.0.0.1 的端口全范围扫描，禁止采集所有监听端口中的内容，禁止连接任意传入的远程/WSL IP。CLI 随机端口不能定位时接受 L0 降级，不为监听替用户改变安全边界。
- 端点探测、GET 和 SSE 必须有读取超时、字节上限、缓冲上限、事件 JSON 深度/字段数量限制和 bounded queue；断流指数退避 + jitter，避免所有 Agent 同时重连；不在 Tk/Monitor tick 发网络或阻塞 8 秒 WSL 调用。首次显示桌宠帧的时机不依赖探测。
- **初始资源预算（待同机验证，非测得成绩）**：额外常驻线程优先 ≤1；同时 SSE 连接 cap 4（复杂场景可配置且有上限），每连接每条消息 ≤64 KiB、队列 ≤256 条、活跃 session 缓存 ≤8/后端、全局可展示 session 仍遵守原有上限；新 source **不得**按会话扩张线程/worker。超预算要丢弃/合并中间活动事件并触发权威 reconcile，而非丢失审批解除事实而持续误报。
- 只保留 `kind/host/session opaque id/状态/时间/最小安全摘要` 于内存，应用日志输出 counts、hash 和错误码，不输出用户 prompt、完整 assistant answer、命令参数、环境变量、access token、HTTP Authorization 或 session full path。敏感字段的 `repr(exc)` 也须净化。源端不作会话数据写入或 WAL checkpoint。
- Windows CLI/WSL CLI/桌面窗口及后端必须分别证明关联；Windows Explorer 中的通用端口、陈旧 PID、重用端口、停止的 WSL distro 都不得成为数据访问授权。保留现有 WindowsExitWatcher exact token 和 WSL cache 三态。
- **DSH 不做压缩全量 tail**：默认 zstd 无法直接 readline，且 session persistence 可替换。若后续找到受支持的增量 decoder，也必须是按物理版本/偏移读取、限定文件数/字节、最小新依赖，并在有基准证据的情况下才采纳；本次不作为默认实现。
- 禁止所有可造成副作用的 Agent API，包括审批 POST、prompt submit、shell exec、session edit、agent install、config patch；**“HTTP 200 就可以读取”的泛化 RPC 探测也不做**。用户只要求观察，不预留审批接口或弃用按钮。

## 6. 阶段化实施、文件变更与阻塞条件

| 阶段 | 交付内容（精确文件） | Gate / 结束条件 |
| --- | --- | --- |
| **P0 合同与实机探针** | 扩展 `tools/desktop_source_probe.py` 或新增唯一 `tools/agent_capability_probe.py`（禁止两个重叠工具），只读探查 Windows/WSL/GUI 进程树、监听口归属、上游版本、SSE 事件类型、可用 GET、会话身份、是否有可验证 pending 集合。输出脱敏能力矩阵并将日期/版本记录到 `SourceLink.md` | 6 种形态逐个标记 `verified / degraded / unsupported / not tested`；没有实机就标 `not tested`，不能跳过后声称全支持。对 DSH 必须先证明 L1 来源再允许做 DSH session adapter |
| **P1 身份发现** | `agents/models.py` 增 Enum；`agents/discovery.py` 补精确 Windows terminal/GUI + WSL kind 和 process ownership；需要时 `agents/paths.py` 添加纯路径函数；`pet/config.py` 同步设置字段 | 精确区分 CLI/desktop/helper、PID 复用、两个 distro、两个 UID、两个相同 session id、缺失 cmdline、假冒 node 与缓存 tombstone |
| **P2 OpenCode L1 监听** | `agents/opencode.py` + `agents/monitor.py` 的最小只读 snapshot 消费/单个 `OpenCodeDesktopSource` 注册；必要时修改 `agents/desktop.py` 的所有权辅助函数，不重写 state | GET/SSE 状态 replay、当前 pending 精确归属、权限解除、重连与 authority、GUI/CLI 共享 backend 不重复、断线无 phantom Waiting |
| **P3 DSH 分支** | 先保留 P1 L0；仅当 P0 验证原生 L1 只读读取面后，新增 `agents/dsh.py` 并连接现有 Monitor/source contract。没有则只写清限制、**不提交占位插件或伪 adapter** | 只有获得强 session/revision/approval ID 证据才宣称 DSH L1；各社区 Desktop 个别核对；零配置 L0 永远可用且不强制插件 |
| **P4 UI、回归、清理** | `pet/config.py`、`pet/presentation.py`、`pet/dashboard.py`、`pet/labels.py`、相关测试、`README.md`、`CHANGELOG.md`、`SourceLink.md`；移除 P0 试验代码、未引用路由/重复分支 | 全量测试与基准 + Windows 实机通过、静态检查不含写接口/输入注入、UI 无重复目标、README 只描述已验收形态，未通过能力有明确限制 |

修改前先冻结 P0 实测 fixture；避免 P2 从“桌面看到一个进程”直接跨越到“精确监听所有会话”。若某一步的证据无法建立，**终止该子能力，保留 L0**，不引入 hack。

## 7. 细化测试与验收标准

**A. 分类与 identity**：`tests/test_discovery.py` 增 Windows 原生 OpenCode CLI、Node/Bun 包入口、`opencode serve`、desktop 主/renderer/helper、DSH CLI/web/Tauri 侧进程、伪装同名程序、无 cmdline、嵌套工具、端口复用、PID create_time 变化；WSL 两发行版、两个 UID、root 禁用、tombstone。必须无假 desktop hosts/无重复终端目标。

**B. 运行态事件**：新增 `tests/test_opencode_source.py`，在 fake local GET/SSE 驱动以下确定性样例：启动/订阅、项目多 session、active turn、事件顺序反转、`asked→replied`、`idle`、`session.error`、SSE 断线、半包/超大记录/坏 UTF-8、401/403/404、服务重启和端口复用、已取消请求晚到、快照读取与事件同时发生、session 列表历史冷数据不加入运行 targets。Expected: Waiting 必须 exact、无 pending 证据便降级 Unknown、不把关闭误报为 Done。

**C. 多表面与生命周期**：`tests/test_desktop_sources.py` 和 `tests/test_monitoring.py` 覆盖一个 OpenCode backend 被 CLI+desktop 使用、两个 GUI host、同 kind source 清理风险、host 退出一次清全部、单源 non-authoritative 不删除其他数据、禁用 kind/source 立即退出、`_commit_exit` 与 WindowsExitWatcher exact token、stale source 恢复后不复活过去实例。

**D. DSH**：`tests/test_dsh_discovery.py` 必测 L0，确保 `node`/`npx`/`webview` 假阳性被拒；若 P3 获准实现 L1，再用固定版本的 session/approval fixture 测 `approval/asked`/`approval/decided` ID 配对、turn lifecycle、压缩持久化不被当作普通 JSONL、未知 schema 只输出 Unknown、无任何写操作和双向 RPC。

**E. 展示与隐私**：`tests/test_config.py` 旧 config 迁移/默认新增 key/未知键、`tests/test_presentation.py` 关注优先级/展示 cap、Dashboard 开关、托盘和 slot selector；敏感字符串注入 HTTP payload/异常信息/argv/WSL 命令行，断言磁盘日志、probe 输出、UI 诊断中不可见明文 token、完整 prompt、命令参数。窗口前置仅已验证宿主，不调用任何审批或输入控制。

**F. 性能预算**（在同一 Windows 主机、相同宠物数/实例数/动画设置进行 A/B，记录 P50/P95/P99）：无人运行 Agent、1 OpenCode、4 OpenCode SSE、1 DSH、Windows+双 WSL、网络中断/恢复；10 分钟稳态及 30 次启停循环。对照历史 `tests/benchmark_monitor.py` 等；目标闲置 CPU 增幅 ≤0.5 个百分点、额外 RSS ≤20 MiB，新增网络/WSL 调用不随 UI tick 增长；新增后台线程优先 ≤1，子进程常驻 0，单连接消息上限 64 KiB、队列上限 256、无泄漏的 socket/handle/线程。以上都是目标，**不是当前测量结果**。

**G. 自动化和实机**：仓库根运行 `python -m compileall -q agents pet tools tests`、`python -m unittest discover -s tests -v`、现有 monitor/desktop/presentation/UI 架构基准、`git diff --check`；Windows CI 对 Python 3.12 验证。实机至少 Windows CLI、WSL CLI、Desktop OpenCode，DSH CLI/Web 和**所选定的一个社区 Desktop 发行版**；结果写测试矩阵并录截图但禁止公开会话原文。CI mock 通过不等于真实机器通过。

**验收宣告约束**：最终在 README 为每个支持对象标明 L0 / L1 / stale / not tested；不以“有启动进程”宣告“会话/审批完整监听”；无稳定只读数据面的 DSH 不伪装支持 L1。未达到基准、未过回归或无法证明 session 身份时，拒绝合并相关功能，并保持先前已验收的五种 Agent 路径不变。

## 8. 文件清理、建议取舍与官方来源

推荐实现顺序：**先 P0 → P1 → P2；P3 的 DSH L1 只有确定证据才进入；最后 P4**。保持 `agents/state.py`、`agents/process_watch.py`、UIA、现有五类 Agent adapter 的稳定部分；如果只用 L0 就满足实际需求，不为 DSH 再增加插件。重连和动态网络逻辑不能散落在多个 Desktop Source；只存在一个 provider 端点拥有者和有限缓存，`Monitor` 只负责读快照、生命周期合并。新代码替代试验路径后必须清除失效 probe、重复 host identity、遗留审批设计和未使用配置。不得仅为了“多抽象”而创建 `core/`、`backend/` 新框架。

**后续唯一需要产品抉择的问题**：若你的 DSH 版本确实没有可供外部读取的会话/待审接口，你是否愿意为更准确的 L1 状态**手动**安装一个**仅订阅事件、没有任何控制能力**的官方插件机制适配器？当前默认：**不装，零配置 L0，安全降级**；不因本计划而自动执行安装。

来源索引（细化证据、信任级别与现状在 [SourceLink.md](SourceLink.md)）：
- DeskPet 实现：[models](agents/models.py)、[discovery](agents/discovery.py)、[monitor](agents/monitor.py)、[desktop](agents/desktop.py)、[state](agents/state.py)、[config](pet/config.py)、[tests](tests/test_desktop_sources.py)
- OpenCode：[Server](https://opencode.ai/docs/server/)、[Plugins](https://opencode.ai/docs/plugins/)、[Windows/WSL](https://opencode.ai/docs/windows-wsl/)
- DSH：[Official repository](https://github.com/deepseek-ai/deepseek-harness)、[Events](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/framework/events.md)、[Session](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/session.md)、[Approval audit](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/approval.md)、[Persistence](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/persistence.md)
