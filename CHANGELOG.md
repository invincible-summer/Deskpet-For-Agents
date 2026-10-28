# DeskPet 版本变更记录

DeskPet 使用 Semantic Versioning 2.0.0。版本 tag 与 GitHub Release 的关系见
[VERSIONING.md](VERSIONING.md)。本文件维护每个可识别版本新增了什么功能、
修复了什么问题，以及其分发状态。

## Unreleased — v4.3.0 监听、仪表盘与活动呈现升级

后续两行活动解析与原创图标修订：Windows Python 3.12 完整套件集中运行一次，
**919 项通过**；四项阻塞 benchmark、编译检查和 diff 检查通过。详见
[两行活动与图标验收](tests/ACCEPTANCE-activity-icon.md)。

状态：**2026-10-27 的监听生命周期、ZCode WSL、仪表盘与气泡修订完成源码验收。
Windows Python 3.12 完整套件 916 项通过（此前 951 项，净减少 35 项）；最终审查
和截图发现问题的相关模块复验、四项阻塞 benchmark、compileall 与 diff 检查通过。
宽/紧凑窗口和气泡三种尺寸已检查合成数据截图。当前没有 ZCode 远程活跃实例，
真实 WSL 连接交互保持 NOT_TESTED。具体证据见
[本轮验收记录](tests/ACCEPTANCE-monitoring-dashboard.md)。**
此前用户确认 Codex、DSH、ZCode 从应用自身托盘隐藏后均可正常唤起交互；反复
真实托盘、P95/RSS/CPU 专项与编译产物验收仍需完成。
按 SemVer 属于向后兼容的 MINOR 功能；源码版本已调整为 4.3.0，GitHub Release
仍待真实机器验收后单独决定。

### 新增

- 监听适配器按当前上游能力探测更新：Kimi Code 的 session-owned
  `state.json`/main wire 回退、OpenCode active-session facts、DSH 当前
  generation 事件和 pi/Claude 的空会话过滤；未知 schema 继续降级为
  UNKNOWN，不写入 Agent 数据。

### 修复（v4.3.0 监听与桌面唤起可靠性）

- 所有受支持 harness 统一提取真实命令、思考开头与搜索内容。活动固定两行、
  超长时只省略尾部；流式分片保留开头，工具输出和目的说明不会冒充命令。
  补齐 ZCode 本地/WSL、OpenCode、DSH、pi/Kimi 的正文投影和搜索状态标签。
- 新增原创猫形应用图标，仪表盘品牌标记、窗口与系统托盘共用同一资源；
  托盘保留多尺寸 ICO、缓存自愈及原有资源释放行为。
- 进程权威退出后阻止旧 census/宿主缓存重新引入同一代次；Windows/WSL 与
  Agent 类型开关在实例、宿主、会话合并处统一生效。同一会话保持唯一所有权。
- ZCode 本地/远程读取分开清退；缓存连接失败不再给旧会话续租，远程读取器
  按发行版、UID、HOME 和进程代次隔离，避免断连后持续显示旧活动。
- 仪表盘合并为单一 Agent 工作台，删除重复概况页与拥挤双栏。统一导航、表单
  和内容表面；滚动条保留槽位、viewport 单向驱动换行和布局，hover 只改颜色。
- 气泡首行统一显示 Agent、工作区和状态；目标和活动摘要保留任务主干与命令/路径等对象，避免
  完整 JSON 与仅工具名的粗略展示。收敛重复的仪表盘、菜单与摘要测试，保留
  生命周期、只读归属、布局稳定和资源清理的行为回归。

- Codex state DB rollover、归档/删除 tombstone、metadata-only admission
  和 orphan active turn 不再把旧会话永久显示；OpenCode inactive rows、
  DSH 超过发现窗口的 WAITING 日志和 ZCode orphan turn 使用有界 lease。
- 修正 Electron 托盘隐藏的恢复方式：Codex、DSH、ZCode Desktop 通过已核验的
  应用单实例启动入口恢复内部内容；任务栏最小化仍使用原生恢复。此前仅显示
  HWND 会留下停在关闭/最小化按钮的旧画面，延迟前置不能修复。窗口筛选排除
  helper、IME、tool/owned/child，重复双击合并；启动和有界等待由 worker 处理，
  Tk 不等待外部进程，Dashboard 保持自身显隐状态。
- Codex/DSH/Claude/Kimi/pi 的显式 aborted、cancelled、interrupted、failed
  事件现在进入有界 ERROR 提醒窗口；正常 completed 仍进入 DONE，不再把异常
  中断静默降级成 IDLE。
- 绑定会话目录重扫默认从 15 秒降到 5 秒，仍保持每轮 revision 增量读取和
  有界 I/O。

- 新增 `OpenCode` / `DSH` 两个 Agent 类型，默认开启；旧 `config.json`
  自动补齐 `monitor.agents` 与 `presentation.concurrent.eligible_kinds`
  的新键。Dashboard 的类型开关、参与展示、健康诊断同步支持两类。
- OpenCode 数据面：`~/.local/share/opencode/opencode.db`（含渠道后缀
  候选）只读 SQLite 会话索引；`agents/opencode.py` 复用
  `agents/sqlite_ro.py` 合同（mode=ro + query_only + 40ms busy + 固定
  SELECT + 行数上限），并在读连接前后对比 `-shm` sidecar 实现 WAL
  零写入门禁（无法证明纯读即降级停用）。`session_pending` 是输入队列，
  绝不当作审批。
- OpenCode 待审批补充：仅当 `~/.local/state/opencode/service.json` 的
  loopback url+pid 与 census 中存活 OpenCode 进程吻合且 `GET /api/info`
  自报 pid 一致时，读取 `GET /api/permission/request`；绝不读取注册文件
  中的服务密码。401/403 → "状态暂不可读"（长退避，不重试风暴）。
- OpenCode WSL：由既有 ProcessProbeWorker 在同一 fresh census 代次内经
  发行版内 `python3` 只读脚本查询；发行版无读取器即降级，绝不向 WSL
  安装任何组件。
- DSH 数据面：`agents/dsh.py` 精确移植上游 `projectKey`/`encodeSegment`
  路径编码、`session[.vN].jsonl[.zstd]` 世代选择（最高版本、双编码并存
  拒绝、未来版本拒绝）与 `scanZstdFrames` 结构扫描；压缩日志逐完整帧
  解码（checksum 校验、解压输出硬上限、半帧等待、损坏拒绝）。新增
  `zstandard` 依赖（懒加载，缺失时压缩日志明示降级）。
- DSH 状态机：turn/start / turn/end（completed/error/interrupted 等
  reason.kind）→ Working/短暂 Done/Error/Idle；打开的 turn 静默超限
  判 UNKNOWN（crash orphan 不是 Working）；approval/asked 与
  approval/decided 同会话按 id 配对给出 WAITING/解除，回放预算外的
  旧 asked 不伪装等待；未知必需事件按上游语义拒绝重构。
- 进程识别：`opencode.exe` CLI（需终端 ancestry/conhost 佐证）与
  OpenCode Desktop（Electron --type= 后代或 opencode-cli.exe 后代
  佐证）严格区分；`dsh.exe` CLI；`DeepSeek Harness.exe` 宿主与同 exe
  RunAsNode Host helper 收敛；WSL 侧 opencode/dsh 严格 argv0 匹配。
- CLI 与桌面会话跨 surface 去重：watcher 暴露 exact session claims，
  被终端实例绑定的会话不再重复生成桌面 target；每 Kind 只注册一个
  Desktop source。
- `tools/desktop_source_probe.py` 新增 OpenCode（DB schema/零写入门禁/
  注册文件结构）与 DSH（sessions 布局/世代文件/帧头）脱敏实机探测。

### 修复（v4.4.1 监听可靠性）

以下问题均在本机复现取证后修复；**没有放宽任何 fail-closed 语义**，也
没有新增轮询频率或常驻线程。

- **Codex 完全监听不到**：`codex app-server --remote-control` 常驻形态
  刻意脱离 controlling TTY（`tty=?` / `tpgid=-1`），被 terminal attachment
  判定为 DETACHED 而从 authoritative snapshot 整组过滤。现在同一进程组
  内存在官方 daemon 自证据（`app-server` / `pid-update-loop` /
  `--remote-control` / `--listen` / `--managed-daemon`）时提升为
  UNKNOWN 保留观察；无 daemon 证据的 DETACHED 仍按孤儿过滤。Windows 侧
  对称修复：daemon 形态的 `codex.exe` 不再要求 terminal ancestry。
- **大文件显示“状态暂不可读 / 未解析”**：≥512KB 的会话文件从
  `size-256KB` 起步会跳过文件头，Codex 因此读不到 `session_meta`
  （`session_id`/`cwd` 全空，绑定评分 1000→0），DSH 则因起点落在
  zstd 帧中段而 magic 校验失败（`CORRUPT`）。现在：Codex 系打开时做一次
  有界 64KB 头部身份预读；DSH 改为**帧对齐**起步（明文按行对齐），并用
  一次性 consumer 有界引导 header。已绑定但当前无状态证据时会话仍保留
  session 身份（不再退化成没有上下文的空壳）。
- **重复监听 / 状态滞后**：① 被 terminal claim 覆盖的会话，desktop
  source 现在**完全不创建** tailer/解码器（此前仍会先读再判 claimed，
  同一份日志被两份解码器重复解压）；② bound 文件永久占用一个 tracked
  名额，不再被更新的历史 session 挤出后反复 pop/重建；③ DSH 单轮事件
  消化上限 512→2048，实机上 1215 帧的日志不再只前进到 2438 秒前的事件。
- **路径解析缺陷**：`paths.default_root()` 曾把 Linux HOME
  （`/home/<user>`）在 Windows 上替换成反斜杠形式，导致 `wsl_unc()`
  拒绝、该 source 永远 0 候选。现在按路径形态（而非 `os.name`）判断布局。
- **审批可见性诚实化**：上游 rollout policy 把审批请求列为 transient
  不落盘，rollout 读者无法判断是否有未决审批。现在 Codex active turn
  显式携带 `approval_visibility="unavailable"`（仅展示，绝不参与状态
  合成），不再给用户一个没有任何说明的 WORKING 空壳。

### 修复（v4.4.2 重复监听与解析正确性）

以下问题经代码取证 + 上游仓库复核（openai/codex main、deepseek-harness
master，2026-10）定位；**没有放宽任何 fail-closed 语义**，没有新增轮询
频率或常驻线程。

- **Codex / DSH 已 claim 会话的重复增量解析**：v4.4.1 只阻止了 claim 后
  **新开** tailer/解码器；claim 建立前已打开的那份仍每轮 `poll_lines` /
  tail——terminal watcher 与 desktop source 永久双份解析同一会话文件。
  现在被 claim 覆盖的会话一律冻结增量读（保留读位置与状态，CLI 消失后
  下一轮恢复/reclaim），Codex 侧 idle lease 仍以 DB `updated_at` 为锚，
  活跃会话不会被误释放。
- **Codex 解析健康误判 PARTIAL**：`_EVENT_TYPES` 白名单缺上游 rollout
  policy.rs 的 durable 事件（`thread_goal_updated` / `thread_rolled_back` /
  `thread_settings_applied` / `context_compacted` / `entered_review_mode` /
  `exited_review_mode` / `patch_apply_end` / `image_generation_end` /
  `sub_agent_activity`；serde snake_case 命名已对照 main 的
  `protocol.rs` 复核）。含这些记录的 rollout 不再被计入 unknown 类型。
- **DSH 单轮事件预算超限静默丢事件**：超 `MAX_EVENTS_PER_POLL` 的行此前
  被直接丢弃（注释却承诺留在缓冲）。上游一个 zstd 帧=一个追加批次、
  单帧可含数百事件，丢 `turn/end` 漏 DONE、丢 `approval/decided` 幻影
  WAITING。现在预算耗尽时未消化数据全部留缓冲（明文残行 / zstd 整帧 /
  帧内剩余解码文本），下一轮继续；持续超速使缓冲超上限才整体拒绝
  （fail closed）。帧扫描上限改用独立的 `MAX_FRAMES_PER_POLL`。
- **DSH 子会话审批污染 root tracker**：`_merged_tracker` 浅拷贝在 root
  审批为空时与 root 共享同一 dict，子会话审批经 `setdefault` 永久写入
  root——decided 只到达子会话，root 幻影 WAITING 要等 600s 回放预算
  剪除。现在无条件复制 dict，折叠视图立即跟随子会话配对解除。
- **DSH CLI 误绑子会话日志**：同 cwd 下繁忙 subagent 的日志 mtime 更新，
  会按"最新候选"抢占本应属于 root 会话的终端绑定（错误归属 + 与桌面
  source 重复显示）。现在 CLI 候选用有界 header 探测排除
  origin=subagent / parentSession 的子会话（探测不可读时保持包含，
  不引入新的猜测）。
- **DSH terminal claim 缺 session_id 时漏发**：header 尚未解析的绑定现在
  退回 canonical 路径 claim（对齐 Codex watcher 的 file_id 回退），缩短
  claim 建立前 desktop 侧的重复解码窗口。
- **WSL `/.dsh/` 子串误报**：任何参数里含 `/.dsh/` 数据路径的进程
  （如 `tail -f ~/.dsh/sessions/...`）都被判成 DSH agent。现在收紧为
  规范安装路径 `/.dsh/bin/dsh`（上游 `apps/desktop/cli/dsh`），argv0
  正则与 `dsh ` 前缀不变。
- **DSH 事件词表落后上游**：上游 master `known-event-types.ts` 新增
  `job/registered` / `job/settled`；缺词表时含这些事件的整个会话按
  fail-closed 契约被判 UNKNOWN（harness 升级后"全部无法解析"）。词表
  已同步；对更新的未知必需事件仍拒绝重构。
- **DSH 标准 npm/pnpm 安装漏报**：当前 `@deepseek-ai/dsh` 的 bin 指向
  `lib/bin.js`，WSL/Windows 进程表常见形态是 `node .../@deepseek-ai/dsh/`
  或 `.bin/dsh`，不含裸 argv0 `dsh`。现在按官方入口精确匹配，并保留
  数据路径误报防护；同时不再用 project 目录 mtime 截断老项目中的新会话。
- **Codex 压缩 rollout 与新字段**：支持上游 `.jsonl.zst` 压缩归档的只读
  解码、plain/zst 表示切换和有界输出；补齐当前 `EventMsg`/`TurnItem`/
  `ResponseItem` 字段解析，消息、工具、计划、reasoning 与 goal 字段在
  paginated rollout 中不再丢失。
- **Codex Desktop 状态库兼容与重复会话收敛**：兼容仅提供
  `updated_at_ms` 的新 `threads` schema，并优先读取 `CODEX_SQLITE_HOME`；
  同一 rollout 的 DB 别名、`.jsonl`/`.jsonl.zst` 表示切换和 CLI claim
  现在共享 canonical 身份，不再生成重复 target 或重复增量 reader。Desktop
  的 `preview`/`first_user_message` 也可作为空标题回退。
- **Dashboard 布局稳定性**：导航栏固定宽度并增加在线摘要，Agents 页在常见
  窗口宽度下保持列表与详情并列；刷新只更新变化文本，减少侧栏闪烁并明确
  标示 Codex Desktop / WSL 来源。
- **GLM/ZCode 与旧 schema**：兼容最新 GLM bundle 的 `zcode-agent.mjs`
  入口；ZCode `model_usage`/`tool_usage` 缺少可选列时生成 `NULL`，不再
  因旧 schema 的 `no such column` 让整个 Desktop source 失效。

本次验证使用 Linux Python 3.14：监听核心定向回归 183 项、OpenCode/Kimi/
路径补充回归 66 项通过；全量收集到
669 项时，剩余 65 项均因环境缺少 `tkinter`、Windows `ctypes.windll` 或
`LocalAppData` 而无法导入，未发现监听核心断言失败。Windows Python 3.12
全量 UI/编译验收仍需在目标环境执行。

### 边界

- OpenCode 当前版本的服务端总是启用随机密码；在不读取密码的硬约束下，
  待审批/用户问题实时状态不可用，显示"状态暂不可读"（DEGRADED），
  绝不伪造状态。
- 同一 cwd 多个同类 CLI 进程无法从外部区分会话归属，全部显示"未解析"。
- 真实 OpenCode/DSH 安装（Windows/WSL/官方桌面）的实机验收 NOT_TESTED；
  完成后按表面公布 PASS/FAIL/DEGRADED。

DeskPet 使用 Semantic Versioning 2.0.0。版本 tag 与 GitHub Release 的关系见
[VERSIONING.md](VERSIONING.md)。本文件维护每个可识别版本新增了什么功能、
修复了什么问题，以及其分发状态。

## 4.1.0 — ZCode WSL Remote Development 被动监听

状态：**已验收源码里程碑；创建标准 annotated `v4.1.0` tag，但不创建 GitHub
Release，也不生成新的 Portable/EXE 发布文件。** 最新 Portable Release 仍为
`v4.0.0`。

版本从原先拟定的 `4.0.1` 调整为 `4.1.0`：本次工作不是单纯修正既有行为，而是
新增了“Windows ZCode Desktop 连接 WSL Remote Development 时仍能被 DeskPet
识别和读取状态”这一新的用户可见执行拓扑，按项目 SemVer 规则属于向后兼容的
MINOR 功能。

### 新增与修复

- 新增 ZCode Desktop -> WSL Remote Development 数据面监听。Windows
  `ZCode.exe` 继续作为桌面宿主和激活目标，会话状态则从实际运行 Agent 的 WSL
  distro + Linux uid/user/HOME 数据面读取。
- `WslProcessProbe` 精确识别当前已观察到的
  `~/.zcode/server/zcode-server.cjs`、
  `~/.zcode/server/agents/glm/zcode.cjs` 和
  `~/.zcode/server/agents/glm/zcode-agent`；同一 uid 的 server + agent child
  合并为一个 remote data plane，不把这些 runtime 错误显示成 Terminal Agent。
- WSL 用户身份使用 `getent passwd <uid>` 解析真实 user/HOME，不再从 HOME
  basename 猜用户名，支持自定义 Linux 用户与非 `/home/<name>` HOME。
- 远端 SQLite 由 WSL 内 ZCode 自带的 `~/.zcode/server/node` + `node:sqlite`
  读取，显式 `readOnly: true`、bounded timeout、禁止 extension；Windows 不通过
  `\\wsl.localhost` 打开 live SQLite/WAL。
- 远端读取复用已有 `ProcessProbeWorker`，不新增常驻线程或 per-session worker；
  remote plane 上限 4，ZCode Desktop 全局接纳 session 上限仍为 8。
- 远端读取只能由同一轮 fresh `wsl --list --running` / process census 授权。
  WSL 枚举失败保留 non-authoritative last-good；权威 stopped-distro tombstone 会
  删除 remote plane，不会用旧缓存重新唤醒已停止的 distro。
- Windows 本地 ZCode 与多个 WSL remote plane 可以同时存在；相同 session id
  按 plane 隔离，不交叉继承状态。
- WAITING 仍只来自明确、session-scoped 的
  `tool_usage approval_status=requested + running`；不从静默推测审批，也不自动
  批准。
- UI 来源标签增加 `ZCode · Desktop · WSL <distro>`；激活仍只恢复 Windows
  ZCode 宿主，不注入输入、不调用私有会话跳转/控制面。
- `tools/desktop_source_probe.py` 增加脱敏的 WSL runtime 结构诊断；不输出
  prompt、transcript、tool arguments、token/secret。

### 安全与兼容性

- 不要求 Hook、插件、MCP 或 Agent 配置改动；不自动审批。
- 不写 Agent 数据库，不执行 checkpoint、repair、migration 或 extension load。
- 私有 ZCode 路径/schema/runtime 名称只做 capability detection；上游变化时
  fail closed / UNKNOWN / last-good，不猜测状态。
- 保持轻量边界：无新常驻线程、无 resident WSL helper、无 `/home/*` 全盘扫描。

### 自动验收

- Windows/Python 3.12 全量单元回归：**812 项通过**。
- `benchmark_monitor.py --ticks 5000`：通过。
- `benchmark_desktop_sources.py --ticks 5000`：通过。
- Presentation 与 UI architecture blocking benchmark：通过。
- changed-file 白名单与 `git diff --check`：通过；未引入无关文件或 CRLF/LF
  全文件重写。
- 自动验收覆盖合成/Mock 的 WSL runtime、只读 transport、状态投影、plane/session
  隔离和资源边界；GitHub runner 不冒充真实用户机器上的 ZCode + WSL Remote
  Development 实机测试，最终环境 smoke check 仍使用只读 probe。

### 版本与发布

- `v4.1.0` 使用 annotated Git tag，tag message 写明本版本核心功能、安全边界、
  验收结果和“source milestone only / no GitHub Release”状态。
- tag push 不再自动触发 GitHub Release。正式 Portable Release 改为对已存在的
  annotated tag 显式运行 release workflow。

## 4.0.0 — Portable 正式发布基线

状态：**GitHub Release / latest Portable**。

- 首次提供 Windows x64 Portable 独立发行包和 `DeskPet.exe`；普通用户不需要
  Python、pip 或虚拟环境。
- 可变数据统一迁移到 `%LOCALAPPDATA%\DeskPet`，程序目录与配置/皮肤/cache
  分离，便于覆盖升级。
- 建立正式构建/验收/Release pipeline、SHA-256 manifest 和编译产物验收。
- 包含 v3 开发线之后完成的被动终端/桌面 Agent 观察、三种呈现模式、Dashboard、
  自定义皮肤和可靠性改进。
- 正式安全合同保持：不注入、不自动审批、不写 Agent 数据库。

## 3.0.0 — Passive-only observation

Tag/commit: `v3.0.0` -> `da20a689654093e247abf1c996011b9351d36604`

状态：历史源码 Release。该版本早于当前 Portable 分发合同，因此 Release 只固定
真实 tag/源码历史，不补造后来才存在的 EXE/Portable 资产。

### 相对 v2 的核心变化

- **移除受控审批/控制 plane**：删除 `agents/managed.py`、旧 approver 流程和
  managed-session 审批 UI，把产品合同统一为 passive-only：不创建 Agent、不托管
  Agent、不发送键盘事件、不自动审批。
- **重新建立发现层**：Windows 使用 psutil，WSL 使用运行中 distro 的 process
  census + `/proc` metadata；引入 cwd/tty/uid/启动 token 等身份事实，降低 PID
  复用和 wrapper/runtime 重复实例问题。
- **来源隔离**：`windows`、`wsl:<distro>` 分开维护健康状态，一个 WSL distro
  探测失败不会让其他来源的实例错误消失。
- **结构化状态融合**：新增 `StateReducer`，以明确语义证据合并 Session、Terminal
  和 lifecycle 状态，定义 ERROR/WAITING/INPUT/WORKING/DONE/IDLE/UNKNOWN 优先级，
  不再用“静默=审批”等猜测覆盖结构化事实。
- **互相唯一绑定**：新增 matching 层，把进程、会话和 Terminal pane 的匹配改为
  双向唯一、顺序无关的评分绑定；证据不足时保持 AMBIGUOUS/NONE。
- **Windows Terminal UIA 观察器**：新增 bounded MTA UI Automation 线程，订阅
  Notification/TextChanged/StructureChanged，用当前可见审批 UI 补充 Codex/Claude
  等不会把实时审批持久化到 session 文件的状态；只有 CONFIRMED/HIGH 绑定才归属
  给具体 Agent。
- **隐私边界收紧**：WSL environ 只保留 allowlist；终端文本只在内存中有界处理，
  不落盘；root metadata fallback 默认关闭。
- **轻量与可靠性工程**：监控线程拆为 Tk UI / Monitor Core / single-slot
  ProcessProbe / UIA MTA，WSL 卡顿不阻塞 UI；增加 queue/ring/read-rate 上限和
  5000-tick monitor benchmark。
- **工程化验收**：加入 Windows GitHub Actions、完整 unittest、matching/state/UIA/
  discovery 测试和 SourceLink 调研证据；依赖拆分为常驻 core 与仅转换期组件。

## 2.0.0 — Managed Codex approval channel

Tag/commit: `v2.0.0` -> `e032640fb78fa043214214b292e76b81405e277f`

状态：历史源码 Release；不补造 Portable 二进制。

### 相对 v1 的核心变化

- **审批通道从通用按键注入改为受控 Codex app-server JSON-RPC**：新增
  `agents/managed.py`，只有 DeskPet 主动创建并托管的 Codex managed session 才能
  在气泡/会话页处理官方审批请求。
- **只读监听与控制能力分离**：Windows/WSL 中用户自己启动的 Codex、Claude、
  Kimi、pi 继续只读观察；只读会话只提供终端唤起，不再合成或注入批准按键。
- **请求归属强化**：app-server command/file-change/permission 请求使用原始
  JSON-RPC request id 建立一次性 pending record，UI 暴露短期 opaque token 和
  connection generation，避免旧按钮误操作后续请求。
- **自动批准收窄**：只允许指定 managed Codex session 按会话开启；重连后关闭；
  用户输入和 MCP elicitation 始终要求人工处理。
- **状态语义更保守**：只读 Codex 不再因 rollout 静默猜测 WAITING；无法安全读取
  审批时保持观察状态而不是制造可操作审批。
- **监控/解析增强**：Claude/Codex/Kimi/pi watcher、discovery、tailer、summary 和
  monitor 都增加了会话身份、状态保持与边界处理；UI/Dashboard 同步适配 managed
  与 readonly 两类会话。
- **测试扩充**：新增 managed channel、monitoring、UI 单元测试与 bubble benchmark，
  为随后 v3 的被动架构重构建立回归基线。

这是一次行为合同的 breaking change：v1 的“对任意已打开终端做按键审批”不再是
默认能力，审批只存在于明确受控的 Codex app-server session。该控制路径又在 v3
被完全移除。

## 1.0.0 — Initial public behavior

Tag/commit: `v1.0.0` -> `c0ca087b74c2545dbbccbff5a58a380638becfd1`

状态：历史源码 Release；这是仓库初始提交，不补造后期 Portable 二进制。

### 初始能力

- **五状态桌宠动画**：`walk` 工作、`attack` 指令/批复发出、`die` 等待批复、
  `special` 完成庆祝、`sleep` 空闲；支持自定义五素材皮肤、缩放和转换缓存。
- **Windows + WSL 四类 CLI Agent 监听**：初始支持 Claude Code、Codex、Kimi CLI
  和 pi，通过进程发现 + Agent 自己落盘的 JSONL 增量 tail 获取活动和任务事实，
  不要求 hooks。
- **状态气泡与本地摘要**：显示 Agent 类型、模式、任务标题和活动摘要；摘要使用
  本地规则压缩截断，不调用额外 LLM。
- **多 Agent 体验**：可同时监听多个实例，支持主绑定/手动绑定、气泡轮播和
  Dashboard 管理。
- **早期审批能力**：气泡提供批准/拒绝以及自动批准，通过定位 Windows Terminal
  并使用 Win32 `SendInput` 发送按键；这是 v1 的真实历史行为，后来因安全和归属
  可靠性问题先在 v2 收窄、再在 v3 完全移除。
- **桌面集成**：双击唤起终端、托盘显隐、开机自启动、位置锚点、隐藏、换肤、
  播放速度和锁定动画。
- **早期稳定性策略**：包含进程消失宽限、working hold、固定尺寸气泡、缓存清理
  等机制，目标是在 tkinter/psutil 架构下保持较低常驻资源占用。

## 历史 Release 回填说明

`v1.0.0`、`v2.0.0`、`v3.0.0` 的 annotated tags 原本已经存在，但 GitHub Release
页面未按项目开发顺序建立。2026-09-13 按 **v1 -> v2 -> v3** 顺序补齐历史源码
Release：

- Release target 严格使用原有 immutable tag；不移动 tag。
- Release notes 根据对应 tag 的 README、代码和 `v1->v2` / `v2->v3` 实际 diff
  撰写，不把 v4 才出现的 Portable/Desktop 能力回填到旧版本。
- 不附加后来才存在的 EXE、Portable ZIP、SHA256SUMS 等资产。
- 三个历史 Release 显式标记为非 latest，因此 latest Portable 仍保持 `v4.0.0`。

## 2026-09-12 一次性 tag 归一化

在首个正式 GitHub 二进制 Release 前，旧的开发阶段 tag 被清理，使今后的版本号
表达兼容性而不是内部实现阶段。Git commit 历史没有删除或改写。

| 已退役的开发 tag | 历史 commit |
| --- | --- |
| `v3.1.0` | `dfc32b7169087318c88e7c8fdb9a538849d46ae4` |
| `v3.1.2` | `7840568e15f024f78b885a92d7cc839b59121556` |
| old `v4.0.0` | `2330c26d86d38d6e8dc82ba511ff73a63d6d2d99` |
| old `v4.1.0` (pre-normalization development tag) | `ef3eea404524e46dff6a76c84bdafe1ccf179eb1` |
| `v4.1.1` | `0572da27819698bb8f263d553044916e3db2db3b` |
| `v4.2.0` | `ace08379761d8c1e63cc025f1e1093624e535444` |
| `v4.2.1` | `939c98e3cc056ed73b036e274a478b5c12c09125` |
| `v4.2.2` | `3d0ab8e515cd79fa703ee62138789b00f3c8f474` |
| `v4.2.3` | `57eebece9d77ba58a55c3045e39789976ad2764b` |
| `v4.3.0` | `39941c2815581eadf948ab8e4e90d82eb1c52207` |
| `v4.3.1` | `9635e1bac225378fd120e32f0a6f89512da46069` |
| `4.4.0` | `1cdd18bdaf2d21c2e4011d7c012f02cf97844753` |
| `4.5.0` | `67a5d3320c413773ae373c76efe84b065dc4e6a4` |

这些 SHA 仍是有效历史引用。旧的 `v4.1.0` 是未发布 Release 的开发阶段 tag，已在
归一化时删除；2026-09-13 因当前 ZCode WSL 功能按 SemVer 应为 MINOR，项目进行
一次明确记录的例外，将 `v4.1.0` 重新建立为**当前永久规范线**的 annotated source
tag。任何已经发布过 GitHub Release 的 tag 都不允许这样重用。
