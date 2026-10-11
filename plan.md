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
