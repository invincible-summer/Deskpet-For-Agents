# DeskPet V4.3.1 — 被动 Agent 观察桌宠（终端窗口唤起 + 并发呈现 + 非阻塞多皮肤 UI）

一只常驻桌面的自定义桌宠，**被动观察**你已经在 Windows / WSL 终端里启动的 AI 编码 Agent（**Codex / Claude Code / Kimi / pi**），自动识别 Agent、项目、WSL 发行版、会话与终端，实时展示 Goal、Mode（Plan/Default…）、Thinking / Reading / Coding / Testing / Waiting Approval 等状态，并映射到桌宠动画和气泡。

DeskPet 不创建、不托管、不控制任何 Agent：不配置 hooks、不注入进程、不发送键盘事件、不自动审批。

**终端唤起语义（V4.1.3，v3-compatible 被动 heuristic；V4.1.4 增强）**：DeskPet 的 Terminal window wake 使用被动启发式——Windows native 优先使用进程祖先关系（多个候选窗口时再按标题/屏幕证据细分）；WSL 使用当前可观察 TermControl 标题**与每 control 最近一次可见屏幕文本的内存摘要**（V4.1.4：标题常停在 profile 名如 "Ubuntu"，屏幕上的项目路径/Agent 标识才能把多个窗口区分开）做 kind/cwd/user/distro 评分。高置信匹配失败但仍有一个 v3 best-positive control 时，DeskPet 可以把它所属的顶层 Terminal window 作为用户显式唤起候选；这不会自动授予 Terminal text/approval attribution。屏幕摘要只在内存参与评分（产出 int 分数与证据 token），绝不进入绑定、日志或配置。DeskPet 不切换 Tab/Pane，也不发送键盘输入。Windows Terminal 目前没有稳定的公开"按 `WT_SESSION` 激活既有标签页"接口（[microsoft/terminal#19783](https://github.com/microsoft/terminal/issues/19783)，closed/not_planned），因此只承诺 window 级唤起；候选窗口经 `WindowIdentity`（HWND+PID+进程创建时间+窗口类）校验后才动作，OS 拒绝抢前台时闪烁任务栏提醒，绝不绕过。

V4.3 的能力概览：

1. **终端窗口唤起**：点击某 Agent 时恢复并前置它所在的 Windows Terminal 顶层窗口；窗口归属用 `WindowIdentity`（HWND+PID+进程创建时间+窗口类）安全校验，stale 即 fail-closed；OS 拒绝抢前台时闪烁任务栏提醒，绝不绕过系统策略（无键盘注入、无剪贴板注入）。
2. **并行监听 + 呈现模式**：并行监听默认开启；每次 DeskPet 启动固定进入"单宠聚合"（一只桌宠把各 Agent 的卡片叠成一摞气泡，V4.1.4，双击对应气泡唤起对应终端）。用户可以在本次运行内切换非并发 / 单宠聚合 / 多宠分离（多宠分离：每个 Agent 一只桌宠，**自动绑定**现有 Agent，普通空 slot 不产生额外桌宠——不是"设了 3 就唤起 3 只"）；重启再次回到并行监听 + 单宠聚合。除用户显式隐藏外不会出现 0 桌宠：整个 Fleet 0 Agent / 0 binding 时仍保留 pet-1 idle fallback。
3. **可靠设置持久化 + 自启修复**：设置写入失败会明确提示；开机自启能识别"注册路径已失效"并一键修复。
4. **精确退出生命周期**：Agent CLI 进程退出即从列表消失（terminal/shell 还开着也不会"复活"）；Windows 上事件驱动、零轮询。

```
Process tells us WHO.          /proc + psutil（含 cwd/tty/uid/启动 token）
Session data tells us WHAT.    各 Agent 自己落盘的 JSONL（增量只读 tail）
Terminal UIA tells us          Windows Terminal 官方 UI Automation 接口
  WHAT THE USER IS ASKED.      （Notification 事件 + 当前可见区域）
StateReducer combines them.    ERROR > WAITING > INPUT > WORKING > DONE > IDLE > UNKNOWN
DeskPet only observes.         桌宠动画 + 气泡 + 仪表盘
```

## 正常使用流程

```
打开 Windows Terminal → 进入 Windows/WSL → 自己执行 codex / claude / kimi
        ↓ DeskPet 自动发现、自动绑定会话、自动关联终端窗口
桌宠动画 + 气泡（Codex · Plan · 编码中 / 目标 / 当前活动）
```

等待审批时气泡提示"请在终端处理"。**交互（V4.1.3 固定三种模式，均为双击）**：

- 非并发 SINGLE：双击桌宠或气泡 → 唤起该 Agent 的 Terminal 窗口
- 并发 AGGREGATE（单宠聚合）：每张候选卡在**同一桌宠上叠成一摞气泡**（V4.1.4——最底一张带指向桌宠的倒三角尾巴，上方卡片无尾巴、卡片间只留小间隔）；**双击对应气泡 → 唤起该气泡对应 Agent 的 Terminal**；双击桌宠 → 只互动。唤起结果（如"已打开终端"）**只显示在被双击的那张气泡上**，其他气泡不受影响、叠层不收起（V4.1.4）
- 并发 FLEET（多宠分离）：双击各自的桌宠或气泡 → 唤起各自 Agent 的 Terminal

气泡/桌宠单击不激活。仪表盘卡片"打开终端"按钮与托盘 Agents 子菜单同样按 exact agent_key 唤起。托盘左键 = 显示/恢复桌宠（绝不隐藏已可见的桌宠），右键/键盘菜单键 = native context menu。

## 功能

- **五状态动画**：`walk` 工作中 ｜ `attack` 下达指令 ｜ `die` 等待审批 ｜ `special` 任务完成（×3）｜ `sleep` 空闲
- **语义化状态气泡**：`Agent · Mode · Phase` + Goal（≤120 字）+ 当前活动摘要（≤160 字，本地规则压缩，不调用 LLM）
- **等待审批检测**：Kimi 来自 wire durable `interaction.request(kind=approval)`（EXACT；legacy `ApprovalRequest`/`approval.request` 兼容兜底），`question`/`user_tool` 归类为 INPUT 而非 WAITING；Codex/Claude 来自 Windows Terminal UIA 当前可见审批 UI（高置信 + 1.5s TTL 复检）——**静默永远不被推断为等待审批**
- **多 Agent**：自动跟随（WAITING > INPUT > ERROR > WORKING …，工作中粘性），或并发模式（单宠聚合/多宠分离）
- **仪表盘 V4.3**：左侧导航七页（概览/Agents/桌宠/外观/监听与隐私/诊断/设置），页面懒构建 + retained 行（状态变化只 configure 不重建）、只有当前页刷新；**retained Toplevel——失去焦点绝不自动收起**（V4.3.1：关闭只来自 X / 显式隐藏 / 退出）；PID/HWND 等运行期细节收在"高级诊断"折叠区
- **每只桌宠独立皮肤（V4.3）**：Fleet 每个槽位可单独选皮肤（同一皮肤可被多只重复选择），"跟随全局"继承；换皮先请求构建、完成前保持当前画面，失败保持旧画面绝不空白
- **外观即时生效（V4.3）**：整体大小/速度/气泡宽高/文字缩放全部为离散值滑块，每跨一档立即应用；无 Apply 按钮，配置经 650ms debounce 原子落盘
- **非阻塞 UI（V4.3）**：单一 UiCoordinator bridge timer（125/200/500ms 三档自适应）+ 合并式 render flush；Monitor 语义 revision 不变则零 reconcile；冷动画帧每 idle slice 最多解码 1 帧、frame 级全局 LRU 保护正在显示的帧；配置保存在独立 transient 线程写盘；皮肤导入（复制/校验/manifest）在后台单 job lane 进行
- **原生托盘菜单（V4.3.1）**：托盘右键/键盘菜单键 = **标准 Windows native context menu**（`NOTIFYICON_VERSION_4` + `WM_CONTEXTMENU` + `TrackPopupMenuEx`，一次手势恰一个菜单，菜单在托盘线程内确定性销毁）；托盘左键/键盘激活 = 显示/恢复桌宠
- **首帧优先启动（V4.3.1）**：首个桌宠窗口的可见首帧（纯 Tk 启动占位）先于 Monitor 扫描 / UIA 引导 / 托盘加载 / 皮肤缓存维护；皮肤 catalog 纯内存快照，磁盘扫描全部在锁外的后台 lane
- **确定性退出（V4.3.1）**：hide-first + 单一 3s 绝对 deadline——菜单先结束、可见窗口立即隐藏，所有后台子系统只用全局剩余预算回收，无局部超时叠加、不留孤儿 converter
- **双击桌宠/气泡/卡片按钮**：唤起该 Agent 所在的 Windows Terminal 窗口（公共 Win32 API 恢复并前置；foreground 被拒时闪烁任务栏；AGGREGATE 下双击桌宠只互动）
- **系统集成**：托盘图标（程序内绘制的原创小猫，**不使用桌宠形象素材**）、开机自启、隐藏、换肤、缩放、锁定动画
- **隐私**：`/proc/<pid>/environ` 只在 WSL 内部按 allowlist（`WT_SESSION`/`CODEX_HOME` 等 9 项）过滤后才进入 Python；终端文本只在内存、绝不落盘

## 快速开始

公开源码包使用 repo-local `.venv`（`.gitignore` 已忽略，不污染系统 Python）：

```bat
:: 1) 一次性安装（只寻找已安装的 Python 3.12，不自动联网下载 Python）
Setup-Desktop.bat

:: 2) 启动（只启动，绝不联网/pip install；环境缺失时明确失败）
Start-Desktop.bat
```

安装与启动脚本都按"确定性"设计：Setup 用 `constraints-v4.3.0.txt` 锁定 CI 已验证的 Python 3.12 依赖集；Start 只使用 `.venv\Scripts\pythonw.exe`（隐藏控制台），不 fallback 到任意 Conda/System Python——"能双击"不能以"随机使用一个缺依赖环境"为代价。

全新安装默认皮肤为程序化原创 fallback `builtin-cat`（`pet/icon.py` 绘制，无版权素材依赖）；导入自己的皮肤后完全走原流程。已有用户 config 中的自定义皮肤原样保留。

旧配置自动迁移到当前 schema（`config_version=5`；v5 迁移移除已废弃的并发 enabled/mode 持久键——它们现在是运行期 session state，重启固定恢复“并行监听 + 单宠聚合”；迁移从不写入 Agent 身份）。

依赖已拆分：`requirements-core.txt`（psutil/Pillow/comtypes，常驻监控路径）与 `requirements-convert.txt`（imageio-ffmpeg/numpy/scipy，仅皮肤转换期使用，转换在独立子进程完成）；完整安装仍是 `pip install -r requirements.txt`（release 安装由 constraints 锁定版本）。

## 三路观察（安全、无 hooks）

1. **进程探测**（`agents/discovery.py`）：psutil 扫 Windows；WSL 每发行版每周期 1×`ps` + 1×匹配 PID 批量 metadata（`/proc/<pid>/cwd`、`stat` 启动 ticks=进程 token、allowlisted environ、`getent passwd` 解析 HOME，不再枚举 `/home/*`）
2. **会话文件 tail**（`agents/*.py`）：增量只读，容忍残行/轮转/超长行

| Agent | 数据根（env 覆盖） | 结构化状态 |
|---|---|---|
| Codex | `$CODEX_HOME`（默认 `~/.codex`） | `task_started.collaboration_mode_kind` → Plan/Default（EXACT）；user_message → Goal |
| Claude Code | `$CLAUDE_CONFIG_DIR`（默认 `~/.claude`） | `permission-mode` → 六种模式；`sessions/<pid>.json` 为强 hint（/clear 后自动切换新 transcript） |
| Kimi | `$KIMI_CODE_HOME`（默认 `~/.kimi-code`，legacy `~/.kimi` 兜底） | `session_index.jsonl` 按 cwd 精确定位（sessionDir 受 containment 校验）；`state.json.lastPrompt` + `prompt.accepted` → Goal；`plan_mode.enter/exit`（EXACT）；wire `interaction.request(kind=approval/question/user_tool)`（EXACT）+ legacy `ApprovalRequest` 兜底 |
| pi | `~/.pi/agent/sessions`（可用 `PI_CODING_AGENT_SESSION_DIR` 覆盖） | assistant `stopReason`（stop/length/toolUse/error/aborted）驱动 turn 生命周期；独立 `role=toolResult` message 是活动证据（工具失败 ≠ Agent ERROR） |

3. **终端 UIA**（`agents/terminal_uia.py`，观察专用）：独立 MTA 线程（comtypes `CUIAutomation8`/`IUIAutomation5`），订阅 TermControl 的 Notification（2022 起携带新增文本）+ TextChanged（0.15s debounce 的有界审批 fallback）+ 窗口级 StructureChanged（control 开合立即重发现，20s 周期仅为兜底）；弱触发词命中才读 `GetVisibleRanges()` 当前可见区域；审批识别要求**标题模式 + 选项结构同时出现**且识别器种类与绑定 Agent 一致；内存边界：delta≤2048 / ring≤8192 / control≤16 / 事件队列≤256 / UIA 命令队列≤32 / 可见读取全局≤6/s（单 control≥0.5s 间隔）。

## 终端窗口关联的置信度（诚实原则）

Windows Terminal 没有 `WT_SESSION → tab/pane` 公开接口，DeskPet 只做 window 级关联（两条独立链）。**confidence 不是唤起开关**：能否尝试唤起只由"解析器是否给出候选窗口"决定，confidence 只说明候选是怎么选出来的（仪表盘高级诊断可见）。

- **窗口唤起链**（用户双击/按钮"打开终端"，v3-compatible 候选选择）：
  - **Windows 原生 Agent**：PID 祖先链 → 唯一 WT 窗口 → `CONFIRMED`（窗口内 control 数量不影响）
  - **WSL Agent**：TermControl 标题评分（kind+3 / cwd+2 / user@+1 / distro+1），**互相唯一匹配** → `HIGH`
  - 正向证据不够唯一时：保留 best control 所属窗口作唤起候选（`AMBIGUOUS`，v3 行为）——可唤起，但不归属终端证据
  - 无 Agent 证据但桌面只有一个 WT 窗口：保留该窗口（`NONE` + 唯一窗口兜底）——可唤起，不归属终端证据
  - 多窗口且无正向证据 / 无 WT 窗口：无候选（fail-closed，不猜）
- **观察归属链**（WAITING/activity 证据归给谁）：只有 `CONFIRMED`（祖先唯一窗口 + 窗口内唯一被观察 control）或 `HIGH`（标题证据互相唯一，WT 顶层窗口标题仅在窗口内唯一 control 时作第二证据）才归属；其余宁可没有终端证据也不错归。观察用 UIA RuntimeId 是运行期内部句柄，不持久化、不参与激活、不在普通诊断展示。
- UIA 不可用时正常降级：Goal/Mode/Phase 来自会话文件，"打开终端"不受影响（窗口目录来自 Win32 枚举，不依赖 UIA；仅 WSL 标题评分与"等待审批"观察缺位）

## 稳定性设计

- **线程架构（V4.3 仍为 5 常驻线程上限）**：Tk UI ｜ Monitor Core ｜ ProcessProbe worker ｜ UIA MTA ｜ WindowsExitWatcher；临时 worker 仅用户操作产生：`deskpet-convert`（皮肤 build/import，≤1）与 `deskpet-config-save`（配置保存，≤1）—— WSL 卡顿、皮肤转换、配置写盘都不卡 Tk
- **进程身份**：key 含启动 token（`wsl:Ubuntu|codex|4812|<ticks>`），PID 复用不继承旧绑定；wrapper/runtime 折叠（npm shim → node 只保留最深 runtime，不跨 kind 折叠）；`/proc` ticks 缺失时用稳定 fallback 代次 token，绝不退化成裸 PID
- **来源隔离与三态生命周期**：探测健康按真实 source（`windows` / `wsl:Ubuntu` / `wsl:Debian`…）判定，一个 distro 扫描失败不污染其他来源的实例与"状态可能延迟"标记。WSL source 有三种内部语义（V3.1.1）：
  1. **healthy + instances** —— 发行版运行且 Agent 被发现；
  2. **healthy + empty** —— 已权威确认当前发行版没有 Agent，或发行版已停止（`wsl --list --running --quiet` 成功且输出为空即是权威空结果）；权威缺席**立即**清除该实例（V4.1 起无消失宽限），同时清掉该 distro 的进程缓存与 fallback 代次 token——重启后 Linux PID 从小整数再来也不会继承旧绑定；
  3. **unhealthy** —— WSL 枚举/ps 读取失败：DeskPet 保留上一轮缓存并显示"状态可能延迟"，绝不误判退出（无法读取 ≠ 已经不存在）。
  Running 清单**每轮全新查询，绝不缓存正结果**（V3.1.2 被动性闭环）：`wsl -d <distro> --exec` 本身会启动目标发行版（Microsoft 官方 networking 文档原文），而 probe 间隔 3s 小于 WSL 空闲关机延迟（官方 "8 second rule"），一份过期的 Running 缓存会把用户刚停止的 distro 重新拉起并形成"探测保活"循环——因此只有**本轮刚确认 Running** 的发行版才会被 `wsl -d` 探测；`--list --running` 是宿主侧查询，不会启动任何发行版。停止检测的最坏延迟约为 3s 调度 + 一轮权威缺席确认。
- **状态语义**：已知 active turn → 无限保持 WORKING；仅活动证据 → 10s 宽限后回 UNKNOWN（不伪造）；DONE 展示 8s；IDLE 只在明确见过 turn 结束后出现；**泛化终端活动（pane 有文本变化）永远不能推翻结构化 Session 的 DONE/IDLE/ERROR/INPUT**
- **interactive-terminal liveness（V4.2.3）**："进程存在"不等价于"用户还有一个打开的终端 Agent"。Claude/Codex 关闭终端后进程可能 orphan 存活（上游已确认行为）。WSL 用已有 `ps` 的 `tty/tpgid` 直接分类：有效 controlling TTY → attached 继续显示；TTY 被 revoke 且无前台进程组 → detached，下一轮健康 census 即从列表消失；证据矛盾 → UNKNOWN 保留（tmux/screen 内的 Agent 只要 tmux 仍提供 TTY 就保留；nohup/无 TTY 后台进程不再作为"终端 Agent"展示）。Windows native 采用保守判定：只有曾被 `windows-ancestor CONFIRMED` 强绑定、且外部父进程连续两个权威 generation 消失的实例才退出；证据不足一律保留。**DeskPet 只修正自己的观察事实，绝不 kill/terminate/signal 用户的残余 Agent 进程**——orphan 清理是上游 CLI 的生命周期职责。
- **Status/Phase/Mode 正交**：Mode 是独立维度（Plan/Default/UNKNOWN+原始值），终端 WAITING 成为状态胜者时无权擦除 Session 已解析的 Mode——`WAITING + APPROVAL + PLAN` 是合法且必要的最终状态；优先级为 Session 结构化 Mode → 胜者明确携带的 Mode → NONE
- **会话解析**：绑定用互相唯一匹配（source/session_id/cwd/started_at 评分，结果与实例遍历顺序无关），同分竞争保持未绑定；late-start 每 15s 无窗 fallback（最近 12 候选）；目录重扫有绑定时降为 15s
- **兼容性诊断**：会话解析器按已知记录类型集合判定 `OK / PARTIAL / UNKNOWN`，上游格式变化会在仪表盘显示"未知记录"而不是静默失败；Mode 出现未知原始值时显示 `Unknown（原始值：…）`

## 项目结构

```
main.py                 入口（DPI 感知、单实例互斥）
pet/                    UI：app/dashboard/bubble/labels/petwindow/context_menu/
                        animator/skins/tray/config（context_menu = 单一
                        Tk 右键菜单 owner + deferred 语义发布）
agents/
  models.py             Status/Phase/Mode/Observation/AgentInstance/TerminalWindowBinding/
                        TerminalObservationBinding/AgentTarget/ActivationCode
  state.py              StateReducer（状态融合；语义证据 > 泛化终端活动）
  matching.py           互相唯一匹配（session/control 绑定共用，顺序无关）
  discovery.py          Windows + WSL ProcessProbe（三层探测、canonicalization、env allowlist）
  paths.py              数据根/wsl_unc 安全转换/Kimi 索引/Claude PID registry
  base.py               watcher 基座（互相唯一绑定、late-start fallback、parser 诊断）
  codex.py claude.py kimi.py pi.py
  terminal_uia.py       UIA 观察器 + 审批识别器（observation-only；订阅生命周期有界）
  terminal_resolver.py  TerminalWindowResolver + TerminalObservationResolver（双链分离）
  terminal_service.py   观察/解析/window-only 激活统一 facade
  monitor.py            ProcessProbeWorker + Monitor Core + AgentTarget API
  tailer.py summarize.py
actions/winkeys.py      仅窗口唤起（公共 Win32 + WindowIdentity 属主 PID/创建时间/窗口类
                        一致性验证，fail-closed；无任何键盘注入）
tools/convert.py                    素材→透明GIF 管线
tools/terminal_window_probe.py      WT 窗口 list/validate/activate 实机 probe
tools/terminal_observer_probe.py    UIA 观察（observation-only）实机 probe
tests/                              单元/隐私/UIA/匹配/基准/实机回归
.github/workflows/                  CI（windows-latest：compileall + unittest + benchmark）
```

## 常用配置（config.json）

```jsonc
{
  "monitor": {
    "agents": { "claude": true, "codex": true, "kimi": true, "pi": true },
    "windows_enabled": true, "wsl_enabled": true,
    "windows_scan_sec": 3.0, "wsl_scan_sec": 3.0,
    "file_poll_sec": 0.5, "session_scan_sec": 3.0,
    "activity_grace_sec": 10.0, "terminal_observer": true
  },
  "privacy": {
    "terminal_text_to_disk": false, "session_text_to_disk": false,
    "wsl_root_metadata_fallback": false,
    "goal_max_chars": 120, "summary_max_chars": 160
  }
}
```

节奏类配置有代码级 clamp（加载与每轮读取时生效，改坏配置文件也不会制造高频 loop）：`windows_scan_sec` 1–60、`wsl_scan_sec` 1–120、`file_poll_sec` 0.2–5（运行中修改下一轮即生效）、`session_scan_sec` 1–60、`activity_grace_sec` 1–60、`active_file_window_sec` 30–3600。运行期 identity（PID/HWND/RuntimeId/WT_SESSION/exact key）绝不持久化。

`privacy.wsl_root_metadata_fallback` 默认关闭：默认绝不使用 WSL root 读取进程 metadata（Agent 仍会被发现，会话可能显示未解析）；仅在仪表盘显式开启后允许一次 root 补读（只读 cwd/启动 token/uid/HOME/allowlist env）。

## 测试

```bat
:: 使用 Setup-Desktop.bat 创建的 repo 环境（或任何 Python 3.12 + requirements）
.venv\Scripts\python.exe -m unittest discover tests -p "test_*.py"  # 全部单元测试（479+）
.venv\Scripts\python.exe tests\benchmark_monitor.py --ticks 5000 --report benchmark-report.json    # 合成基准（队列/预算/churn 上限）
.venv\Scripts\python.exe tests\benchmark_presentation.py            # Presentation/Fleet/动画缓存基准（blocking）
.venv\Scripts\python.exe testsenchmark_ui_architecture.py         # UI 架构基准：revision 驱动/dirty-view/单 worker/单 bridge（blocking）
.venv\Scripts\python.exe tools\terminal_window_probe.py --list      # WT 窗口实机 probe（list/validate/activate/resolve）
.venv\Scripts\python.exe tools\terminal_observer_probe.py           # UIA 观察实机 probe（默认不打印终端原文）
.venv\Scripts\python.exe -X utf8 tests\regression.py                # 位置/气泡/缩放/托盘/自启
.venv\Scripts\python.exe -X utf8 tests\replay_real.py               # 真实会话数据回放
```

CI（`.github/workflows/test.yml`）：windows-latest + Python 3.12，运行 compileall + 全部单元测试（含 window 激活逻辑、并发激活矩阵、source 停扫、config 运行时测试）+ monitor benchmark 5000 ticks + presentation benchmark + UI architecture benchmark（`PYTHONUTF8=1`，benchmark 报告以 artifact 上传）。真实 Windows Terminal foreground policy / UIA 事件接受度属于本机 manual acceptance：CI 只验证纯逻辑、Win32 调用契约 mock、资源边界和 UI dataflow。**Release acceptance requires GitHub Actions green**：workflow conclusion=success 是发布验收的必要条件，CI 红期间不标记版本完成。

## 已知边界（如实说明）

- Codex 的审批事件明确不持久化到 rollout（官方 transient 策略），因此 Codex/Claude 的"等待审批"只能来自终端 UIA 可见区域；若审批 control 无法唯一关联到 Agent（多 control/后台 tab），在"不 hooks、不控制 Agent"的约束下没有第三条可靠信息源——此时显示 UNKNOWN/工作中而不是猜（plan §55 物理边界）
- Claude Code 上游存在"活跃 session transcript 不实时写出"的回归 → 终端活动观察可补充 WORKING 证据，但不伪造具体 Phase
- Codex 桌面版不写 rollout → 只能检测进程存活（UNKNOWN）
- 自定义桌宠素材版权自负；`assets/pets/*`、`assets/cache/`、`config.json` 不入 git

## V3 不变量（任何实现不得违反）

```
1. 不启动 Agent        7. 不把静默解释为审批     13. 不确定 Agent↔control 时不乱绑定
2. 不修改 Agent        8. 不扫描用户整个 HOME    14. UI 只暴露 AgentTarget
3. 不配置 hooks        9. 不持久化终端原文       （PID/JSONL/HWND 只在高级诊断）
4. 不使用 SendInput   10. 不持久化完整 environ
5. 不向终端写输入      11. 终端文本只做匹配归类
6. 不自动审批          12. （见上）
```
