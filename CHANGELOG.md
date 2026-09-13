# DeskPet 版本变更记录

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
