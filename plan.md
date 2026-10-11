# DeskPet 无插件监听 OpenCode / DSH：实施计划

> 日期：2026-10-11。仓库 [Deskpet-For-Agents](https://github.com/invincible-summer/Deskpet-For-Agents)，应用版本 4.1.0。技术证据与代码源见 [SourceLink.md](SourceLink.md)。**当前是设计，尚未实施或通过 Windows/WSL 实机验收。**
>
> **用户硬约束：不在 OpenCode 或 DSH 安装任何插件、Hook、MCP、监听服务，不修改 Agent 配置、不注入输入、不批准/拒绝。** 仅 DeskPet 自己读取原有数据；一个 DeskPet 自带 Zstandard 文件解码库不是 Agent 插件，但其性能和体积必须验收。此版取代全部既有插件集成草案。

## 一、确定的最小方案

复用当前 `ProcessProbeWorker → Monitor → Observation / DesktopSessionSource → StateReducer → AgentTarget → Presentation / Dashboard / Tray`。**不建第二套监听架构、不建常驻服务、不为每个 session 起线程**。新增两个数据源，分别由单个 owner 同时服务 Windows/WSL CLI 与官方 Desktop。只读取现有 Agent 进程、经过身份验证的数据根、数据库和日志；不触碰 Agent 控制接口。

**OpenCode**：以只读 SQLite 为基础。[上游 `session/sql.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/session/sql.ts) 证明目前的 `session_v2` / `session_message` 包含会话标题、parent/project、完成与时间字段；但 `session_pending` 是 inbox 队列，**不是审批请求**。[`permission.ts`](https://github.com/anomalyco/opencode/blob/dev/packages/core/src/permission.ts) 的未决审批保存在运行时内存 Map。只有确切归属到该活动进程的 OpenCode 本地服务可使用官方**只读 GET/SSE** 提供 pending、用户问题、会话状态，否则应显示 UNKNOWN 而不是伪造审批。不能探测全部 localhost 端口、启动新 serve、绕过认证或抓取 server password。

**DSH**：以现有会话文件为基础。[官方基础配置](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/base/cordis.patch.yml) 默认将 session 持久化到 `dshHomePath('sessions')`。默认物理格式是 `session.vN.jsonl.zstd`，[上游 `zstd.ts`](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-persistence-jsonl/src/zstd.ts) 证实每次追加独立可校验的 Zstandard frame；**仅增量读取追加的新完整帧**，按缓存 offset 和格式版本消费 `turn/start/end`、`step/*`、`tool/call/result`、`approval/asked/decided`，无需 Agent 插件或 Host RPC。未落盘/压缩格式不受支持/超出安全回放窗口时 UNKNOWN/stale，不因历史不完整的 asked 假装仍在 WAITING。

**WSL**：必须受现有 fresh running-distro + 同 UID/HOME + 进程代次门控。OpenCode 活 SQLite/WAL 只能在**同一发行版内**有界查询，不可经 Windows UNC 直接打开；若 WSL 无读取器则降级，不安装 Python/SQLite 到 WSL。DSH 是普通 append-only 文件，可在通过运行中 distro 和 containment 检查后从 Windows 只读已知单文件尾部，慢则退避/降级，绝不扫描或唤醒停止的 WSL。

**额外依赖抉择已经定为推荐默认**：随 DeskPet 打包维护中的 `zstandard` Python Windows wheel，懒加载、每次只解码有界完整帧。这不是安装进 DSH/OpenCode 的插件，不额外常驻 Node 或服务。若连 DeskPet 自身依赖都禁止，则不能声称默认 zstd DSH 日志可完整解析。

## 二、准确性与无侵入不变量

1. 进程和数据绑定至少使用 `provider + source (Windows/WSL distro) + UID + PID incarnation + verified backend/profile + session ID`，否则不把历史文件当活跃 Agent。任意同 cwd 的 CLI/GUI、同名进程、server 端口复用、两个 WSL 用户不可合并。
2. `Status.WAITING` 仅来自当前可信的 permission request / DSH `approval/asked` 且可确认尚未 `decided`；`Status.INPUT` 必须使用独立 question/input 事实。Done/Idle/Error 依真实 turn 事件；静默、文件 mtime、运行进程、旧的 SQLite `time_suspended` 都不能单独证明 Working/Done/Waiting。Mode 与 Phase 继续独立。
3. 任意 unreadable/401/403/version-mismatch/帧撕裂/超预算为 non-authoritative / stale，绝不把本轮失败当 session authoritative absence。精确进程权威退出才触发旧 `_commit_exit`。断流不无限保留历史 WAITING/INPUT。
4. OpenCode 的 SQLite 只复用旧 `agents/sqlite_ro.py`：`mode=ro`、query_only、schema feature detection、短 busy timeout、固定 SELECT、行数 cap。**live SQLite 禁止 immutable=1**、WAL checkpoint、repair。DSH 文件只读，不写 cursor 到 Agent 目录；JSON 解码前先验证完整 Zstd 帧、checksum、解压大小和事件条数。
5. 不读取模型 API key、浏览器 cookie、launch token 或 DSH `.credentials.yaml`；不调用审批/会话写入 POST/DELETE/PATCH，不注册 hooks、注入输入、开启端口或重配置 Agent。日志只记录哈希与错误码，Agent 的 prompt、命令全文与 token 不落盘。
6. 复用 Windows 单次 psutil 扫描与 WSL 现有 Worker；默认无新增后台常驻线程，只有已确认 OpenCode SSE 时可按需启动**最多一个** bounded IO loop；无 SSE 就退出。CPU/内存先设回归预算，最终必须实机量化。

## 三、代码改动映射

| 文件 | 修改 |
| --- | --- |
| `agents/models.py` | 新增 `AgentKind.OPENCODE` / `DSH` 与 label；复用已有 Status/Mode/Phase/Observation/Target |
| `agents/discovery.py` | 在既有扫描中严格识别 Windows/WSL CLI、OpenCode Desktop、DSH 官方 Electron + RunAsNode Host；通用 Node/Bun/Electron/helper 不能冒充 Agent |
| `agents/paths.py` | 有界数据根和 WSL containment；不要让新 Kind 误入只支持四类旧文件 watcher 的映射 |
| `agents/opencode.py`（新增） | 一个 `OpenCodeReader`、一个 `OpenCodeDesktopSource`，SQLite 事实及可信 GET/SSE pending 补充，统一 CLI/GUI ownership |
| `agents/dsh.py`（新增） | 一个 `DshLogReader`、一个 `DshDesktopSource`，明文 JSONL/默认 zstd 完整帧增量读取、版本检查和 WAITING 配对 |
| `agents/monitor.py` | 只消费 cached observation 和 DesktopSourceSnapshot；放慢速文件/WSL IO 到现有 Worker，不在 Tk/Monitor tick 等待；一个 Kind 一个 Desktop source，禁止按 kind 清退互相覆盖 |
| `agents/terminal_uia.py`、`agents/terminal_resolver.py`、`agents/desktop_window.py` | 只有取得真实 UIA fixture 才添加新 Agent 审批识别；继承双表终端绑定，验证 GUI APP_ONLY 宿主白名单 |
| `pet/config.py`、`pet/dashboard.py`、`pet/labels.py` | 同步两 Kind 默认开关、eligible kinds、旧配置迁移、Dashboard 三处硬编码筛选/统计及安全诊断 |
| `pet/presentation.py`、`pet/tray.py`、`pet/app.py`、`pet/petview.py`、`pet/ui_coordinator.py` | 已是 AgentTarget 通用消费者，**尽量保留代码**；需覆盖单宠/聚合/Fleet/托盘/双击/动画/气泡的真正 UI 测试 |
| `tests/*`、`tools/desktop_source_probe.py` | P0 脱敏实机探测、事件/帧/身份/性能回归；不得新增重复探针框架 |

**重要现有代码陷阱**：`Monitor._poll_desktop_sources` 按 `source.kind` 清除 desktop 目标，所以每个新 kind 只注册一个权威 DesktopSessionSource（即使 GUI 有多个 host），不得分别挂多条 source 互相误删。桌面 target key 需用 host 的真实代次与会话 id，确认多进程/同 session ID 不碰撞。旧的 StateReducer、WindowsExitWatcher、动画、UI revision 已足够好，不为接入重构。

## 四、实施 Gate

- **P0（先证实）**：只读探针记录各目标安装版进程 exe/签名/父子链、WSL distro/uid、OpenCode DB 文件名与表列、可信 server GET/SSE、DSH profile/root/session.vN 代际和 zstd checksum、官方 Electron 隐藏/退出语义。输出版本+匿名元数据，**没有真实机器就是 NOT_TESTED**。
- **P1（进程）**：改 enum、严格 Windows/WSL 与桌面宿主归属、配置；验证 PID 复用、同 cwd、多实例、stopped distro。
- **P2（OpenCode）**：读 SQLite 的准确信息，若实例只读 API 可信则加强 WAITING/INPUT；未经证实的 pending 一律 UNKNOWN；接入 CLI 与单个 DesktopSource。
- **P3（DSH）**：Windows+WSL 已知 session 的增量 raw/zstd 解析；上游事件按 ID 对消；加安全 CPU/解压/内存上限；桌面官方 Host liveness 与 session 去重。
- **P4（原有全部功能）**：kind filter、旧 config、单宠/多宠、Dashboard、托盘、皮肤动画、WSL 标签、Windows Terminal 和 APP_ONLY 激活，全部通过旧 UI 的统一 AgentTarget 路径。
- **P5（真实验收）**：自动化全量回归与 Windows Python 3.12 实机、性能和隐私测试；只将逐 surface 实测通过的能力加入 README，清理临时代码和任何残留插件建议。


## 五、功能对等矩阵：OpenCode / DSH 和原有 Codex 一样接入桌宠

| DeskPet 用户可见能力 | OpenCode Windows CLI | OpenCode WSL CLI | OpenCode Desktop | DSH CLI/Web | DSH 官方 Desktop |
| --- | --- | --- | --- | --- | --- |
| 启动发现与权威退出 | 必须 | 必须 | 必须 | 必须 | 必须 |
| 进程代次与不同用户/发行版隔离 | 必须 | 必须 | 必须 | 必须 | 必须 |
| 真实活会话，不把历史冷 session 生成宠物 | 必须 | 必须 | 必须 | 必须 | 必须 |
| Working / Done / Idle / Error | 以精确事实验收 | 同左 | 同左 | 同左 | 同左 |
| WAITING 及解除 | **需要实时请求证据** | **需要实时请求证据** | **需要实时请求证据** | **需要完整可归属的 asked/decided** | 同左 |
| 用户问题 INPUT 区别于审批 | 需要 question 证据 | 同左 | 同左 | 需要独立人机问答事件 | 同左 |
| Thinking/Reading/Coding/Executing/Testing 等 Phase | 有上游结构化依据才展示，否则 NONE | 同左 | 同左 | 同左 | 同左 |
| Plan/Default 等 Mode | 只有明确 mode 才映射 | 同左 | 同左 | 同左 | 同左 |
| 当前 Title / Goal / Summary / CWD | 有界且确切 session 事实 | 同左 | 同左 | 同左 | 同左 |
| Windows Terminal/WSL 终端唤醒 | 必须 | 必须 | 不适用 | CLI 必须 | 不适用 |
| 桌面窗口 APP_ONLY 唤醒 | 不适用 | 不适用 | 必须 | Web 不保证桌面宿主 | 必须 |
| 单宠、Aggregate、Fleet、多实例和 slot | 必须 | 必须 | 必须 | 必须 | 必须 |
| 关注抢占、皮肤动画、气泡详情 | 必须 | 必须 | 必须 | 必须 | 必须 |
| Dashboard、托盘 Agent 项、筛选和计数 | 必须 | 必须 | 必须 | 必须 | 必须 |
| Kind/Windows/WSL 开关、旧配置迁移 | 必须 | 必须 | 必须 | 必须 | 必须 |
| stale/restart/shutdown/隐私/低资源 | 必须 | 必须 | 必须 | 必须 | 必须 |

**判定**：Kind 进入原有 UI、窗口、进程、并发展示体系是必须全通过的“DeskPet parity”；但某一上游当前版本确实无法无侵入提供未决请求或用户问题时，只能标记该类“实时状态 parity”为 DEGRADED，不能称全部状态无遗漏。禁止为了达到表面 100% 而读凭据、安装插件、乱猜状态。DSH Desktop 以 [官方 Electron 包](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/desktop/README.md) 为首选，社区发行版另行版本逐项验收。

## 六、按具体事实的状态映射与恢复条件

| 事实来源 | 允许的 Observation | 错误映射（禁止） | 清理/降级 |
| --- | --- | --- | --- |
| OpenCode ^session_v2^ title/project/revision | 仅 title/cwd/部分 mode 元数据；准确完成时间线需 session message 或 runtime | DB row 存在 => WORKING | 当前进程和会话无法唯一绑定 => UNKNOWN |
| OpenCode ^time_suspended^ | 已开始过执行的 durable claim（仅辅助） | time_suspended 非空 => 永久 WORKING | crash orphan + 没有运行时确认 => UNKNOWN |
| OpenCode 已确认 server ^session.status/idle^ | 当前 WORKING/IDLE、有效 turn completion DONE | SSE 安静 => DONE/IDLE | SSE 断链 => stale，等待重新对账 |
| OpenCode permission asked/replied / GET pending | WAITING/解除 | SQLite 的 ^session_pending^ = permission | pending 无法证实 => UNKNOWN |
| OpenCode question asked/replied/rejected | INPUT/解除 | 用户问题 = 待审批 | 断线无法对账 => UNKNOWN |
| DSH ^turn/start/end^ 与 ^step/start/end^ | Working、短暂 Done、阶段或 Error（以事件真实字段判断） | 仅文件 mtime / 打开 turn => 永久 Working | 日志断裂/进程退出/非当前 session => UNKNOWN/exit |
| DSH ^approval/asked/decided^ 同 session/id | WAITING/解除 | 未配对的历史 asked => 永久 WAITING | 回放窗口不足/缺当前实例所有权 => UNKNOWN |
| DSH 明确的用户问答事件（需 P0 实测 schema） | INPUT/解除 | 纯文本疑问句 => INPUT | 缺 event 合同 => UNKNOWN |
| 当前可见终端 UIA 严格弹窗 | 有界 TTL 的 WAITING fallback | 旧 scrollback 或其他 kind 文案 => WAITING | UI 消失或 TTL 到期 => UNKNOWN |

**OpenCode 的架构约束**：SQLite 是基础会话索引；实际 permission pending 保存在活进程内。每个 server 的版本和路由以本机 ^/doc^ 或 [官方 V2 API](https://dev.opencode.ai/v2/docs/api) 的实例级能力探测为准。OpenCode TUI 的随机端口不能假设为 4096。对读不到的权限状态，应展示“会话可见、待审批状态不可用”，不把 status 伪造成 IDLE。

**DSH 的架构约束**：默认 ^session.vN.jsonl.zstd^ 必须按完整校验帧处理；上游未 materialize 的 session、超过回放预算的旧 asked、日志修复/版本迁移和可替换 persistence backend 不能保证无需额外接口时实时追踪所有请求。能够完整读取当前活跃回合并与确切 host/session 绑定时才允许准确 WAITING；否则显示不确定。不能用更高频率、无限历史扫描或私有 Host 认证旁路掩盖限制。

## 七、验收测试与性能门禁

**进程/身份测试**（扩 ^tests/test_discovery.py^、^tests/test_desktop_sources.py^、^tests/test_monitoring.py^）：同 cwd 多个 OpenCode/DSH、两个 WSL distro/UID、Windows EXE/bun/node wrapper canonicalization、官方 Electron helper/RunAsNode 子进程、两个 GUI host 同 session id、CLI+GUI 共享数据、端口复用、PID create_time/starttime 改变、切换 profile、host 隐藏到托盘再退出、WSL non-authoritative/stopped tombstone。所有状态只归属自己的 instance，来源断连不使其他 kind 退出；现有五种旧 Agent 均无回退。

**OpenCode reader 测试**（新增 ^tests/test_opencode_source.py^）：schema current/unknown/旧数据库、read-only WAL、busy/locked、channel 后缀与错误 DB 根、^time_suspended^ crash orphan、session_pending 负样例；仅在当前进程精确 owner + 可信 port 下执行 GET/SSE；请求 asked→replied、question→answered、busy→done→idle、error→next-turn、SSE 坏包/乱序/断连/重连、401/403、历史 session 冷数据与两个 frontend 去重。

**DSH frame 测试**（新增 ^tests/test_dsh_source.py^）：官方正常 zstd 帧样例+checksum、相连多个 frame、批写时末尾半帧、跨轮完成、写入后 inode/代际变化、compressed & compression:none、未来版 header、坏 checksum、巨型解压输出、超长 JSON/深度、max-bytes、路径 traversal/symlink、首尾回放预算、审批 asked→decided、child/root、日志延迟、无 materialized 文件时 UNKNOWN。Zstd 解码库仅在源确实存在时导入，不在 UI 初始化加载。

**UI/配置测试**（扩 ^tests/test_config.py^、^tests/test_presentation.py^、^tests/test_dashboard_v43.py^、^tests/test_ui_coordinator.py^、^tests/test_terminal_uia.py^、^tests/test_terminal_window_resolver.py^、^tests/test_desktop_activation.py^）：Dashboard 硬编码 Kind 筛选修复、eligible kinds/旧 config、attention/focus、WAITING/INPUT/ERROR 打断 DONE 动画、皮肤、单宠/聚合/Fleet slot/overflow、气泡、托盘 Agents、准确 APP_ONLY GUI/WindowsTerminal 唤醒、WSL 标签、source stale 诊断。不能以只有 AgentKind 枚举/进程识别测试来替代全产品验收。

**性能与隐私**：使用现有 ^tests/benchmark_monitor.py^、^tests/benchmark_desktop_sources.py^、^tests/benchmark_presentation.py^、^tests/benchmark_ui_architecture.py^，在同一 Windows 机器 A/B 比对：无 Agent、单 OpenCode、单 DSH、四个混合 Agent、双 WSL、连续审批事件、重复打开/关闭 30 次、稳态 10 分钟。记录 idle CPU、P50/P95/P99 Monitor tick、RSS/Private Bytes、句柄、线程、WSL 命令数/秒、实际压缩读取字节与解压输出。**初始目标（非实测）：额外 RSS ≤20 MiB、idle CPU 增幅 ≤0.5 百分点、无新常驻进程、无 SSE 时无新增常驻线程**；性能失败则降频/减少候选而非放弃上限。

**验收命令和签字**：^python -m compileall -q agents pet tools tests^；^python -m unittest discover -s tests -v^；上述阻塞 benchmarks；^git diff --check^；Windows Python 3.12 + OpenCode Windows/WSL/Desktop + DSH CLI/Web/官方 Desktop 的无敏感信息实机 probe。README 逐 surface 公布 PASS、FAIL、DEGRADED、NOT_TESTED，写清无法读取的等待态。CI 的 mock 不等于实机；此前 ^tests/ACCEPTANCE-4.5.0.md^ 不属于此次完成证据。

**清理**：当两种 adapter 定型后，清除早期计划里曾考虑过的 Cordis 观察插件、审批控制 API、sidecar 的未实现接口/配置、实验性多套 reader；代码只保留 ^agents/opencode.py^、^agents/dsh.py^ 两个专用 adapter 及现有 Monitor/Presentation 的最小接线。保持本项目“轻量、被动、无侵入”的原始开发契约。
