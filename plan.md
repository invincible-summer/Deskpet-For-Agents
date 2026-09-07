# DeskPet V3 — Passive Agent Observer 完整实施规格

## 0. V3 最终产品定义

DeskPet V3 不再是 Agent Controller，也不再创建或托管 Codex。

它的唯一职责是：

> **自动发现用户已经在 Windows / WSL 终端里启动的 Codex CLI、Claude Code CLI、Kimi Code CLI，自动识别对应 Agent、项目、WSL 发行版、会话和终端，在不修改 Agent、不配置 hooks、不注入进程、不发送键盘事件的情况下，被动实时观察 Goal、Mode、Plan、Thinking、Reading、Coding、Executing、Testing、Waiting Input、Waiting Approval、Done、Error 等状态，并映射到桌宠动画和气泡。**

正常使用流程必须最终收敛成：

```text
用户打开 Windows Terminal
        ↓
进入 Windows / WSL
        ↓
自己执行 codex / claude / kimi
        ↓
DeskPet 自动发现
        ↓
自动识别：
Agent
PID/进程身份
WSL distro
cwd / project
session
terminal
        ↓
自动建立 AgentTarget
        ↓
持续监听结构化会话事件
        +
Windows Terminal UI Automation
        ↓
实时状态融合
        ↓
桌宠动画 + 气泡
```

用户不再需要理解或操作：

```text
Managed Codex
app-server
connection mode
新建 Codex 会话
自动审批
手动审批
绑定气泡
绑定 JSONL
正常流程下绑定终端
```

这些都从 V3 主产品模型移除。

---

# 1. 当前 V2 与 V3 的核心差异

当前 V2 的 `Monitor` 同时维护：

```text
_readonly_instances
_managed_instances
_managed_snapshots
managed manager
```

并在每轮 `_tick()` 合并两套模型；`PetApp` 启动时也直接创建 `ManagedManager`。

Dashboard 进一步把这种内部复杂度直接暴露给用户：

```text
兼容监听＋可控新会话
仅监听现有终端
＋ 新建 Codex 会话
设为气泡会话
绑定终端
绑定会话文件
会话
自动批准
发送任务
停止任务
```

V3 应改成：

```text
               Passive Observer

Windows Probe ─┐
               │
WSL Probe ─────┼── ProcessIdentity
               │
Session Logs ──┼── StructuredObservation
               │
Terminal UIA ──┘
                    ↓
              IdentityResolver
                    ↓
                AgentTarget
                    ↓
               StateReducer
                    ↓
                Snapshot
                    ↓
          Bubble / Dashboard
```

不再存在控制通道。

---

# 2. V3 明确删除的能力

以下内容应彻底删除，而不是隐藏：

### `agents/managed.py`

整个文件删除。

它现有的：

* app-server 进程管理
* JSON-RPC
* thread/start
* thread/resume
* turn/start
* approval pending
* approve/reject
* MCP input
* reconnect
* audit
* managed history

全部退出产品。

### `actions/approver.py`

删除。

当前只读分支本身已经不会发送键盘，只返回“请打开终端处理”。

V3 不需要再保留“approval action”这个抽象。

### 删除旧审批相关 UI

从 `pet/app.py`：

```text
ManagedManager
_waiting_ref
_auto_apr_seen
_auto_approve_pass()
approval bubble buttons
```

全部删除。

从 Dashboard 删除：

```text
connection mode
＋新建 Codex 会话
会话 tab
发送任务
停止任务
批准
拒绝
自动批准
审批记录
```

### 删除旧审批配置

V2 当前还保留：

```text
connection_mode
managed
keys
auto_approve
approve_restore_focus
```

V3 配置迁移时全部废弃。

### 删除旧测试/调试脚本

删除或重写：

```text
tests/test_managed.py
tests/approve_e2e.py
tests/approve_target.py
tests/debug_sendinput.py
```

尤其不应继续在仓库里保留 SendInput 调试脚本，避免以后维护时误把键盘注入重新引入。

---

# 3. V3 核心数据模型

当前 `AgentInstance` 基本只有：

```text
kind
pid
source
started_at
cwd
session_id
```

而 UI 因此只能退化成 PID 技术视图。

V3 应重构 `agents/models.py`。

## 3.1 Agent 进程身份

保留 `AgentInstance` 名称以降低迁移量，但升级为：

```python
@dataclass
class AgentInstance:
    key: str

    kind: AgentKind

    # 运行来源
    source: str                 # windows / wsl:Ubuntu

    # 进程身份
    pid: int
    process_token: str          # 防 PID reuse
    started_at: float

    ppid: int = 0
    uid: int | None = None
    user: str = ""

    # Unix process/session identity
    sid: int = 0
    pgid: int = 0
    tpgid: int = 0
    tty: str = ""

    # project
    cwd: str = ""

    # Windows Terminal inherited hints
    wt_session: str = ""
    wt_profile_id: str = ""

    # only allowlisted environment-derived data roots
    codex_home: str = ""
    claude_config_dir: str = ""
    kimi_code_home: str = ""
```

`key` 不再只是：

```text
wsl:Ubuntu|codex|4812
```

而应包含稳定进程 incarnation：

```text
wsl:Ubuntu|codex|4812|<process-token>
```

避免 PID 被系统复用后继承上一个 Agent 的 session/terminal binding。

Windows 可以直接使用 `psutil.Process.create_time()`。

WSL 则在 `/proc/<pid>/stat` 中获取 Linux process start ticks，或者使用等价稳定启动标识。

---

# 4. Phase / Status / Mode 必须彻底分离

当前 watcher 会把：

```text
phase = "编码"
phase = "计划"
phase = "回答"
```

这种 UI 中文直接写进状态模型。

V3 不应再这样。

定义：

```python
class Status(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    WAITING = "waiting"
    INPUT = "input"
    DONE = "done"
    ERROR = "error"
    UNKNOWN = "unknown"


class Phase(str, Enum):
    NONE = ""
    THINKING = "thinking"
    PLANNING = "planning"
    READING = "reading"
    CODING = "coding"
    EXECUTING = "executing"
    TESTING = "testing"
    ANSWERING = "answering"
    APPROVAL = "approval"
    USER_INPUT = "user_input"
```

Mode 和 Status 互相独立。

例如：

```text
Codex

mode  = PLAN
status = WORKING
phase  = READING
```

UI：

```text
Codex · Plan · 阅读中
```

随后：

```text
mode   = PLAN
status = WAITING
phase  = APPROVAL
```

UI：

```text
Codex · Plan · 等待审批
```

Plan **不是状态**。

---

# 5. 增加 Observation / Evidence 模型

这是 V3 正确性的关键。

任何状态都必须回答：

> “我是根据什么认为它处于这个状态？”

新增：

```python
class EvidenceSource(str, Enum):
    PROCESS = "process"
    SESSION = "session"
    TERMINAL = "terminal"
    FUSED = "fused"


class Confidence(str, Enum):
    EXACT = "exact"
    HIGH = "high"
    MEDIUM = "medium"
    UNKNOWN = "unknown"
```

以及：

```python
@dataclass
class Observation:
    source: EvidenceSource
    timestamp: float

    status: Status | None = None
    phase: Phase | None = None

    mode: str = ""
    goal: str = ""
    summary: str = ""

    confidence: Confidence = Confidence.UNKNOWN

    # terminal transient state
    expires_at: float = 0.0
```

这样：

```text
Kimi ApprovalRequest
→ source=session
→ WAITING
→ APPROVAL
→ EXACT
```

而：

```text
Windows Terminal 当前可见 Codex approval overlay
→ source=terminal
→ WAITING
→ APPROVAL
→ HIGH/EXACT_VISIBLE
```

而：

```text
15 秒没有输出
```

必须是：

```text
不能产生 WAITING
```

---

# 6. Waiting Approval 的绝对语义

这一条必须成为 V3 invariant：

> **静默永远不能推断 Waiting For Approve。**

当前 Codex watcher 已经正确遵守这一点：rollout 不承载实时 approval request，因此静默仍保持 active，而不会构造假的 `WAITING`。

Claude watcher同样正确地拒绝：

```text
open tool + silence → Waiting
```

这种推断。

上游 Claude Code 当前的 conversation JSONL 本来也不记录 permission prompt 和 allow/deny decision。

Codex 当前核心代码也把 approval / permission 相关事件作为特殊实时事件处理，而不是普通 rollout 状态的一部分。

因此 V3：

```text
WAITING_APPROVAL
```

只允许两个来源：

```text
A. Session 中有明确 ApprovalRequest
B. 当前 Terminal UIA 可见区域明确显示 approval UI
```

除此之外一律不允许。

---

# 7. WSL ProcessProbe — 必须首先重写

当前：

```text
ps -eo pid=,etimes=,args=
```

信息远远不够。

V3 `WslScanner` 改造成 `WslProcessProbe`。

## 第一次调用：发行版

保留：

```text
wsl.exe -l -v
```

15 秒缓存即可。

只扫描：

```text
Running
```

发行版。

---

## 第二层：一次获取进程表

每个运行中的 distro 每 3 秒左右执行一次：

```text
ps
```

需要获取：

```text
pid
ppid
sid
pgid
tpgid
tty
uid
etimes
comm
args
```

不要每个 Agent 启动一个 `wsl.exe`。

---

## 第三层：只针对匹配到的 Agent 获取 metadata

匹配：

```text
codex
claude
kimi
```

后，再针对这些 PID 做**一次批量 WSL 查询**：

```text
/proc/<pid>/cwd
/proc/<pid>/stat
/proc/<pid>/environ
```

但 `/proc/<pid>/environ` 必须在 WSL 内部过滤。

只允许输出：

```text
WT_SESSION
WT_PROFILE_ID
WSL_DISTRO_NAME

CODEX_HOME
CLAUDE_CONFIG_DIR
KIMI_CODE_HOME

TMUX
STY
TERM_PROGRAM
```

绝不能把完整环境变量传回 Python。

因为里面可能有：

```text
OPENAI_API_KEY
ANTHROPIC_API_KEY
AWS_SECRET_ACCESS_KEY
tokens
password
```

Codex 当前明确支持 `CODEX_HOME`。

Claude Code 当前官方也支持 `CLAUDE_CONFIG_DIR`，并明确说明 session history 等数据跟随该目录。

Kimi Code 官方支持 `KIMI_CODE_HOME`。

---

# 8. WSL 用户 HOME 不再靠枚举 `/home/*`

当前 `paths.py` 会：

```text
\\wsl.localhost\<distro>\home\*
```

扫描所有用户。

V3 改成：

```text
PID
 ↓
uid
 ↓
getent passwd <uid>
 ↓
真实 HOME
```

得到：

```text
/home/foo
/root
/custom/home
```

然后只访问这个 Agent 所属用户的数据目录。

这样同时降低：

* UNC 目录遍历
* CPU
* I/O
* 错误绑定概率

---

# 9. Linux path → Windows UNC

新增明确函数：

```python
def wsl_unc(distro: str, linux_path: str) -> str:
    ...
```

例如：

```text
/home/foo/.codex
```

转成：

```text
\\wsl.localhost\Ubuntu\home\foo\.codex
```

这个函数必须：

* 要求绝对 Linux path
* 规范化 `/`
* 拒绝空路径
* 不接受 `..` 越界
* 不跟随任意用户提供的路径
* 数据根只能来自 HOME 或 allowlisted env

---

# 10. SessionLocator 重新设计

V3 不让用户手动选 JSONL。

`BaseWatcher` 目前的评分绑定思想是正确的：

```text
source
session_id
cwd
started_at
唯一最佳候选
```

证据不足时保持 unbound。

继续保留这一原则。

但增加 Agent-specific locator。

---

# 11. Codex CLI 监听方案

默认 root：

```text
$CODEX_HOME
```

否则：

```text
~/.codex
```

session：

```text
$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl
```

Codex CLI 当前仍使用 canonical rollout JSONL 作为本地 CLI session storage。

## Codex Goal

当前 watcher 过于依赖：

```text
goal
task
title
```

V3 应把：

> 当前 turn 最新普通 user message

作为 Goal 的第一来源。

例如：

```text
帮我把登录系统迁移成 JWT 并跑测试
```

内部只保留：

```text
Goal = 帮我把登录系统迁移成 JWT 并跑测试
```

最多 120 字。

不写 DeskPet 永久日志。

---

## Codex Plan Mode

必须补当前漏解析的：

```text
TurnStartedEvent.collaboration_mode_kind
```

当前 Codex 源码在发出 `TurnStartedEvent` 时明确填写：

```text
collaboration_mode_kind: ctx.mode()
```

所以：

```text
Plan
Default
```

可以从结构化事件直接判断。

不应该靠 Terminal 文字猜。

---

## Codex phase

映射：

```text
turn_started
→ THINKING

read / grep / rg / glob / list
→ READING

apply_patch / edit / write
→ CODING

pytest / unittest / jest / cargo test / npm test
→ TESTING

shell / exec / command
→ EXECUTING

assistant message
→ ANSWERING

turn_complete
→ DONE

error
→ ERROR
```

当前 `classify_tool()` 已经有这些规则的雏形。

V3 只需要把它输出改成 `Phase`，UI 再本地化。

---

## Codex Waiting Approval

session watcher：

```text
绝不产生
```

TerminalObserver：

```text
当前 visible range 出现 Codex approval overlay
→ WAITING / APPROVAL
```

当前 Codex TUI 源码的 approval overlay 仍明确渲染诸如：

```text
Would you like to run the following command?
```

这样的审批 UI。

---

# 12. Claude Code CLI 监听方案

默认：

```text
~/.claude
```

如果 process allowlist 得到：

```text
CLAUDE_CONFIG_DIR
```

则使用它。

Claude 官方明确说明该变量同时迁移配置、credentials、session history 等数据。

---

## Claude PID → Session hint

新版 Claude Code 已出现：

```text
~/.claude/sessions/<pid>.json
```

包含：

```text
pid
sessionId
cwd
startedAt
updatedAt
```

这个非常适合 DeskPet。

但不能视为真值。

因为当前 Claude Code 已有已知问题：

```text
/clear
/resume
```

可能让 PID registry 中的 `sessionId` 变旧。

所以算法：

```text
PID registry
      ↓
强 binding hint
      ↓
再验证：
cwd
mtime
session file 是否存在
最近活动
```

如果 PID registry 指向旧 JSONL，而同 cwd 新 JSONL 持续增长：

```text
连续两个 session scan 周期确认
→ 自动切换新 session
```

---

## Claude Goal

Claude `user` record：

* 忽略纯 `tool_result`
* 找最新真实用户文本
* 更新 Goal

当前 watcher 已经能区分普通 user 与 tool_result，但没有充分把它提升为 Goal。

V3 补上。

---

## Claude Mode

解析：

```text
permissionMode
permission-mode
```

统一：

```text
default
acceptEdits
plan
auto
dontAsk
bypassPermissions
```

Claude 官方当前确实把 Plan 定义成正式 permission mode。

因此：

```text
mode=PLAN
```

属于结构化状态。

---

## Claude transcript 不可完全依赖

当前 Claude Code Linux 已出现实际回归：

> 活跃 session 的 transcript JSONL 在 session 结束前可能根本不写出。

因此 Claude 必须成为 V3 最主要的：

```text
Session + UIA fusion
```

案例。

当 transcript 暂时没有变化，但 Terminal UIA 不断有输出：

```text
Status = WORKING
confidence = TERMINAL
```

但不要胡乱判断：

```text
CODING
```

除非有 tool/session 证据。

此时可以显示：

```text
Claude Code · 工作中
终端活动
```

而不是伪造具体 phase。

---

# 13. Kimi Code CLI 监听方案

这是当前 V2 最明确的路径错误之一。

当前项目还扫描：

```text
~/.kimi/sessions
```

当前 Kimi Code 官方数据根已经是：

```text
~/.kimi-code/
```

session：

```text
sessions/<workDirKey>/<sessionId>/
```

内部：

```text
state.json
agents/main/wire.jsonl
agents/main/plans/
```

并维护：

```text
session_index.jsonl
```

其中直接包含：

```text
sessionId
sessionDir
workDir
```

因此 V3 不再递归猜。

优先：

```text
cwd
 ↓
session_index.jsonl
 ↓
sessionDir
 ↓
agents/main/wire.jsonl
```

极大减少扫描量。

---

## Kimi Goal

优先：

```text
state.json.lastPrompt
```

然后由 wire 中最新 user prompt 更新。

---

## Kimi Plan

Kimi 当前 Wire/runtime 的：

```text
StatusUpdate
```

明确携带：

```text
plan_mode
```

而且 Kimi changelog 明确说明 Plan 状态会持久化，并在 `EnterPlanMode/ExitPlanMode` 后重新发正确 `StatusUpdate`。

所以：

```text
Kimi Plan
```

属于 EXACT。

---

## Kimi Waiting Approval

当前 Kimi wire 已经有明确：

```text
ApprovalRequest
ApprovalResponse
```

现有 DeskPet watcher 已经正确处理了这一模型。

V3 保留逻辑，但改成纯 Observation：

```text
ApprovalRequest
→ WAITING
→ APPROVAL
→ EXACT

ApprovalResponse
→ clear interaction
```

不再保留：

```text
request_id
approve action
can_approve
```

给 UI 使用。

---

# 14. Windows Terminal UI Automation — V3 的关键新增模块

新增：

```text
agents/terminal_uia.py
```

或者：

```text
actions/terminal_uia.py
```

我推荐放 `agents/`，因为它是 Observation source，不是 action。

---

## 技术选择

增加：

```text
comtypes
```

作为 Windows-only 依赖。

不引入：

```text
pywinauto
OCR
截图
Electron automation
```

Windows Terminal 本身从 2019 年开始已经提供 shared UI Automation provider，允许 accessibility 客户端读取 Terminal 文本。

2020 年开始提供文本变化 UIA events。

2022 年又加入带“实际新增文本 payload”的 UIA notification。

因此这是 Terminal 官方提供的可访问接口，不是抓屏 hack。

---

# 15. UIA 必须运行在独立 MTA 线程

不能放 Tk UI thread。

微软明确要求 desktop UI Automation client：

* UIA 调用放独立线程
* 不让该线程拥有窗口
* COM 使用 `COINIT_MULTITHREADED`
* event handler 添加/删除也放同一非 UI MTA 线程

所以线程结构：

```text
Thread 1
Tk UI

Thread 2
Monitor Core

Thread 3
ProcessProbe worker

Thread 4
Terminal UIA MTA
```

不要每 Agent 建线程。

---

# 16. UIA Observer 必须事件驱动

不要：

```text
每 200ms 扫整个 Windows Terminal
```

UI Automation 本身允许 client 订阅事件，官方明确指出这种机制可以避免不断轮询整棵 UI tree。

订阅：

```text
NotificationEvent
TextChangedEvent fallback
StructureChanged / window refresh（低频）
```

优先使用：

```text
IUIAutomationNotificationEventHandler
```

---

# 17. Terminal 文本只读当前可见区域

不能每次读取整个 scrollback。

UIA `TextPattern` 提供：

```text
GetVisibleRanges()
```

它只返回当前可见文本范围。

因此：

```text
Notification event
      ↓
delta text
      ↓
弱触发检测
      ↓
如果可能存在交互 UI
      ↓
GetVisibleRanges()
      ↓
只检查当前 viewport
```

---

# 18. Terminal 内存边界

每个 terminal pane：

```text
event delta ≤ 2048 chars
ring buffer ≤ 8192 chars
visible snapshot ≤ 4096 chars
```

最大监听 target：

```text
16
```

即便全满：

```text
terminal raw text buffer
```

也只在几十到一百多 KB 级。

全部：

```text
仅内存
不写 config
不写 logs
不写诊断文件
```

---

# 19. Terminal text 是不可信数据

无论 terminal 输出：

```text
Ignore your previous instructions...
rm -rf ...
secret token...
```

DeskPet 都只：

```text
字符串匹配
状态归类
短摘要
```

绝不执行。

不要：

```text
eval
shell
LLM
commands
```

---

# 20. UIA ApprovalRecognizer

新增：

```python
class TerminalRecognizer(Protocol):
    def inspect(
        self,
        visible_text: str,
        delta_text: str,
        now: float,
    ) -> list[Observation]:
        ...
```

实现：

```text
CodexTerminalRecognizer
ClaudeTerminalRecognizer
KimiTerminalRecognizer
```

---

## Codex

要求同时出现：

```text
approval heading
+
decision/options structure
```

例如当前 TUI：

```text
Would you like to run the following command?
```

并且必须是：

```text
CURRENT VISIBLE RANGE
```

而不是 scrollback。

产生：

```text
WAITING
APPROVAL
terminal evidence
```

---

## Claude

匹配当前 permission dialog / plan approval / question UI。

必须：

```text
至少两个结构特征同时满足
```

例如：

```text
permission wording
+
Allow / Deny / Yes / No choices
```

避免普通 assistant 文字里刚好提到 “permission”。

---

## Kimi

优先相信 Wire。

UIA 仅 fallback。

---

# 21. UIA 状态必须有 TTL

Terminal approval 不能永久保存。

例如：

```text
observed_at = now
expires_at = now + 1.5s
```

只要处于 Waiting：

```text
每 0.75~1s
重新 GetVisibleRanges()
```

如果 overlay 仍然存在：

```text
续期
```

如果消失：

```text
clear WAITING
```

所以不会出现：

> 20 分钟前的 Approve 文本还留在 scrollback → DeskPet 永远显示等待。

---

# 22. Agent ↔ Windows Terminal 关联

这是 V3 最困难、也必须诚实处理的部分。

Windows Terminal 每个 tab/pane 都有独立：

```text
WT_SESSION
```

GitHub 当前的 Windows Terminal 讨论也明确描述它为 per-tab/pane unique GUID。

Windows Terminal 又会通过 `WSLENV` 让：

```text
WT_SESSION
WT_PROFILE_ID
```

进入 WSL。

所以：

```text
WSL Codex PID
    ↓
/proc/PID/environ
    ↓
WT_SESSION
```

可以得到 pane-side identity hint。

---

# 23. 但不能伪造“WT_SESSION → UIA pane exact API”

Windows Terminal 当前没有稳定公开接口：

```text
WT_SESSION GUID
→ 返回 UIA pane
```

甚至目前仍存在“根据 WT_SESSION focus tab”的 feature request。

所以 V3 必须使用 confidence。

定义：

```python
class BindingConfidence:
    CONFIRMED
    HIGH
    AMBIGUOUS
    NONE
```

---

# 24. TerminalResolver

新增：

```python
@dataclass
class TerminalBinding:
    provider: str

    hwnd: int
    window_pid: int
    window_created: float
    window_class: str

    title: str

    # runtime-only UIA identity
    runtime_id: tuple[int, ...] | None

    confidence: BindingConfidence

    observable: bool
    last_seen: float
```

---

## Windows native Agent

当前 `winkeys.find_terminal_window()` 已经支持：

```text
Agent PID
→ ancestor PID chain
→ unique window
```

并且不靠 title substring 乱绑定。

保留。

如果唯一：

```text
CONFIRMED
```

---

## WSL Agent

关联证据：

```text
WT_SESSION
cwd
project basename
WSL distro
terminal title
UIA text content
当前窗口唯一候选
```

如果唯一：

```text
HIGH
```

如果多个 pane 都可能：

```text
AMBIGUOUS
```

绝不能把一个 pane 的 Approve 显示到另一个 Agent。

---

# 25. 多 Tab / Split Pane 的降级原则

Windows Terminal 支持一个 tab 内多个独立 pane。

因此 V3 必须接受：

> 某些多 pane / background tab 场景无法 100% 自动确定 UIA text 属于哪个 Linux PID。

处理：

```text
Structured session state
仍然正常工作

Terminal approval observation
只有 HIGH/CONFIRMED binding 时才融合到 Agent

否则：
不要虚构 Agent-specific approval
```

Dashboard 可以显示：

```text
⚠ 终端交互状态无法唯一关联
```

而不是：

```text
Codex · 等待审批
```

如果用户主动进入高级诊断，可以：

```text
关联当前 Pane
```

但这是**异常修复入口**，不是正常步骤。

并且绑定只在当前 Agent PID / Terminal runtime 生命周期内有效，不建议长期持久化 stale UIA runtime id。

---

# 26. StateReducer — V3 核心状态融合

新增：

```text
agents/state.py
```

接口：

```python
def reduce_state(
    instance: AgentInstance,
    session: Observation | None,
    terminal: Observation | None,
    previous: Snapshot | None,
    now: float,
) -> Snapshot:
    ...
```

优先级严格：

```text
ERROR
    ↓
WAITING_APPROVAL
    ↓
WAITING_INPUT
    ↓
WORKING
    ↓
DONE
    ↓
IDLE
    ↓
UNKNOWN
```

---

# 27. 状态规则

## ERROR

明确 session error。

如果后续：

```text
new turn started
```

立即清除。

---

## WAITING_APPROVAL

仅：

```text
Kimi ApprovalRequest
或
Terminal current-visible approval
```

---

## WAITING_INPUT

明确：

```text
request_user_input
AskUserQuestion
InputRequired
```

或者 terminal current-visible input dialog。

---

## WORKING

有：

```text
active turn
tool event
assistant stream
recent terminal activity
```

---

## DONE

只能来自：

```text
turn complete
task complete
TurnEnd
result
```

展示 8 秒后变 IDLE。

---

## IDLE

只能在：

```text
进程存活
+
明确知道没有 active turn
```

时使用。

---

## UNKNOWN

例如：

```text
Agent PID 存活
但 session 还没绑定
并且 terminal 没有足够证据
```

必须是：

```text
UNKNOWN
```

而不是 IDLE。

---

# 28. 删除 V2 的 90 秒“假 Working”

V2 当前：

```text
如果之前 WORKING
即便 watcher 已经 IDLE
90 秒内仍强制 WORKING
```

这个设计适合防闪烁，但不适合 V3 的“状态语义准确”。

V3 改成：

```text
active turn known
→ 无限保持 WORKING，直到 explicit completion

activity-only evidence
→ grace 8~10s

之后：
UNKNOWN
而不是伪造 WORKING / IDLE
```

这样“长思考”靠 active turn 本身维持，而不是 90 秒魔法数字。

---

# 29. AgentTarget — UI 唯一对象

新增：

```python
@dataclass
class AgentTarget:
    key: str

    instance: AgentInstance
    snapshot: Snapshot

    terminal: TerminalBinding | None
```

UI 不再操作：

```text
PID
JSONL
HWND
session binding
```

只操作：

```text
AgentTarget
```

---

# 30. Monitor API

V3 `Monitor` 对 UI 暴露：

```python
def get_targets() -> dict[str, AgentTarget]

def primary_target() -> AgentTarget | None

def get_target(key: str) -> AgentTarget | None

def set_primary(key: str, manual: bool = True)

def reset_primary()
```

保留现有的：

```text
manual pinned / auto select
```

思想。

但不再暴露 managed/readonly。

---

# 31. 自动跟随优先级

建议：

```text
WAITING_APPROVAL
    >
WAITING_INPUT
    >
ERROR
    >
WORKING
    >
DONE
    >
IDLE
    >
UNKNOWN
```

并保持粘性：

```text
当前 Agent 仍 Working
→ 不因为另一个普通 Working 切走

另一个 Agent 出现 WAITING
→ 自动抢占
```

手动钉住：

```text
用户选了某 Agent
→ 保持该 Agent
```

直到：

```text
Agent 真正退出
```

---

# 32. Monitor 线程架构必须拆开

当前 Monitor 扫描 WSL 的 `subprocess.run(... timeout=8)` 和 hot file tail 在同一个 monitor thread。

如果 WSL 卡 8 秒：

```text
整个状态监听都卡 8 秒
```

这是实时性问题。

V3 改成：

```text
ProcessProbeWorker
        │
        │ every 3s
        ▼
latest-process-snapshot
        │
        ▼
Monitor Core  ←── Session tail
        │
        ├── Terminal event queue
        │
        ▼
StateReducer
```

---

## ProcessProbeWorker

一个 daemon thread。

只负责：

```text
Windows process scan
WSL process scan
```

结果使用：

```text
single-slot latest snapshot
```

不要排队积压。

新扫描覆盖旧扫描。

---

## Monitor Core

约：

```text
400~600ms
```

执行：

```text
读取最新 process snapshot
poll 已绑定 session file
drain terminal event queue
state fusion
```

因此 WSL 命令卡顿不会卡气泡。

---

# 33. FileTailer 优化

现有 `FileTailer` 的优点必须保留：

* 增量读取
* partial line
* rotation
* bounded chunk
* bounded line

但当前每个 poll 都会读取文件 prefix 来检测某些 inode 不可靠文件系统的替换。

在：

```text
\\wsl.localhost
```

上这是额外 I/O。

V3 改成：

```text
stat
 ↓
size / mtime_ns 没变化
→ 直接 return

metadata 有变化
或每 5 秒 safety verification
→ prefix check
```

---

# 34. Session 目录扫描优化

不要每 0.5 秒扫描目录。

建议：

```text
bound file
→ 每 0.5s incremental tail

unbound Agent
→ 1~3s session resolver

bound Agent
→ directory rescan 10~15s
```

如果 DeskPet 比 Agent 晚启动：

不要严格只看：

```text
最近 180 秒
```

因为用户可能已经开了半小时。

改：

```text
第一阶段：
recent candidate

找不到：
bounded fallback
→ 最近 N 个候选
→ cwd/session identity 评分
```

不要无限递归全 HOME。

---

# 35. 安全 session path 规则

禁止普通 UI 的：

```text
选择 JSONL
```

同时：

```text
DirEntry.is_dir(follow_symlinks=False)
DirEntry.is_file(follow_symlinks=False)
```

所有自动发现路径必须位于该 Agent 自己的数据 root。

不递归：

```text
project source
/etc
/mnt/c
```

不跟 symlink 扫出去。

---

# 36. Dashboard V3

删掉“会话”页。

主页面只显示：

| 当前 | Agent       | 项目      | 环境/终端                         | Mode    | 状态   | 活动            |
| -- | ----------- | ------- | ----------------------------- | ------- | ---- | ------------- |
| ★  | Codex       | DeskPet | WSL Ubuntu · Windows Terminal | Plan    | 编码中  | 修改 monitor.py |
|    | Claude Code | backend | WSL Ubuntu · Windows Terminal | Default | 等待审批 | Bash 权限       |
|    | Kimi        | tools   | Windows Terminal              | Plan    | 阅读中  | 搜索配置          |

PID 不在主列表。

---

# 37. Agent 操作

正常只保留：

```text
[自动跟随]
[设为当前 Agent]
[打开终端]
```

双击一行：

```text
设为当前 Agent
```

双击桌宠：

```text
打开该 Agent 所在终端
```

---

# 38. “绑定终端”退到高级诊断

只有：

```text
terminal.confidence == AMBIGUOUS
```

时显示：

```text
⚠ 无法唯一确定终端 Pane
[高级：关联当前 Pane]
```

不在正常工具栏。

---

# 39. “绑定会话文件”彻底取消

session resolver 失败时：

```text
Session：未解析
[重新扫描]
```

高级诊断可以显示 candidates，但不推荐用户手选 JSONL。

如果真的保留手工修复，只允许当前运行期临时 override，不写永久配置。

---

# 40. Agent 身份可视化

主要显示：

```text
Codex

DeskPet
~/src/DeskPet

WSL · Ubuntu
Windows Terminal

Plan · Coding
```

高级诊断才显示：

```text
PID 4812
TTY pts/4
SID 4771
WT_SESSION ...
session_id ...
JSONL ...
HWND ...
UIA runtime id ...
```

---

# 41. Bubble V3

正常：

```text
Codex · Plan · 编码中
Goal · 优化 WSL Agent 监听

正在修改 agents/discovery.py
```

Waiting：

```text
Claude Code · 等待审批
Bash 命令需要确认

请在终端处理
```

Kimi：

```text
Kimi · Plan · 等待审批
正在等待计划确认

请在终端处理
```

彻底没有：

```text
[批准]
[拒绝]
```

---

# 42. Bubble footer

建议：

```text
DeskPet · WSL Ubuntu
```

或者：

```text
Windows Terminal · PowerShell
```

而不是：

```text
pid=4812
```

---

# 43. Terminal 打开逻辑

当前 `winkeys.py` 的公共 Win32 raise 代码可保留。

重命名 API：

```python
raise_terminal(binding: TerminalBinding) -> bool
```

不再经过：

```text
actions.approver
```

---

# 44. 配置 V3

建议：

```json
{
  "config_version": 3,

  "monitor": {
    "agents": {
      "codex": true,
      "claude": true,
      "kimi": true
    },

    "windows_enabled": true,
    "wsl_enabled": true,

    "windows_scan_sec": 3.0,
    "wsl_scan_sec": 3.0,
    "file_poll_sec": 0.5,
    "session_scan_sec": 3.0,

    "gone_grace_sec": 15.0,
    "activity_grace_sec": 10.0,

    "terminal_observer": true,

    "pinned": ""
  },

  "privacy": {
    "terminal_text_to_disk": false,
    "session_text_to_disk": false,
    "goal_max_chars": 120,
    "summary_max_chars": 160
  }
}
```

像：

```text
terminal max buffer
UIA verify TTL
event queue sizes
```

这种实现安全上限建议作为代码常量，而不是普通用户配置。

---

# 45. V2 → V3 migration

加载 `config_version < 3`：

删除：

```text
connection_mode
managed
auto_approve
keys
approve_restore_focus
window_instances
```

清空：

```text
monitor.pinned
```

因为新的 process key 加了 process incarnation token。

旧：

```text
monitor.session_bindings
```

也不再进入正常 Resolver。

一次性提示：

```text
DeskPet V3 已切换为自动被动监听，
不再创建或控制 Agent。
```

不要出现“旧审批被停用”这种产品术语。

---

# 46. 清空 / Reset 的最终语义

V3 的：

```text
重新扫描
```

只能：

```text
清 Process cache
清 Session resolver cache
清 Terminal runtime binding
重新自动发现
```

不能删除：

```text
.codex
.claude
.kimi-code
```

---

## 清空诊断

仅：

```text
DeskPet log ring
performance counters
```

不能删除 Agent history。

---

# 47. 内存控制

当前 Animator 已经：

* lazy GIF decode
* global byte budget
* 最多 2 个 animation object
* hidden 时可 pause

这部分保留。

UIA 不允许维护整个 terminal transcript。

固定：

```text
8 KB / terminal
```

Process target：

```text
max 16
```

事件队列：

```text
max 256
```

满时：

```text
coalesce/drop oldest terminal delta
```

绝不能阻塞 UIA callback。

---

# 48. CPU 控制

关键策略：

```text
WSL process discovery
3s

WSL distro list
15s

bound JSONL tail
0.5s

session directory scan
3~15s

Terminal UIA
event-driven

waiting overlay validation
仅 WAITING 时 0.75~1s

Dashboard redraw
只在数据 signature 改变时更新 widget
```

不要：

```text
200ms ps
200ms UIA tree scan
OCR
full scrollback diff
每 Agent 一个线程
```

---

# 49. WSL subprocess 数量控制

每 distro 每 discovery 周期：

```text
1 × ps
+
最多 1 × matched PID metadata batch
```

也就是：

```text
Ubuntu 有 5 个 Agent
```

也只执行大约：

```text
2 个 wsl.exe
```

而不是：

```text
10~20 个
```

---

# 50. UIA terminal 重新发现

不要每秒 enumerate desktop。

只在：

```text
新 Agent 出现
Terminal window create/destroy
resolver ambiguous
每 15~30s safety refresh
```

时重新扫描 terminal tree。

其余由 event handler 工作。

---

# 51. Privacy / Security Invariants

V3 明确允许：

```text
process metadata
/proc/PID/cwd
/proc/PID/stat
allowlisted process environment fields
Agent session state files
Windows Terminal UIA visible text
```

明确禁止：

```text
/proc/PID/mem
ptrace
DLL injection
terminal keyboard injection
SendInput
clipboard
shell history
SSH keys
完整 environ 持久化
整个 terminal scrollback 持久化
截图 OCR
```

---

# 52. Terminal 文字绝不写日志

诊断日志只能：

```text
UIA approval detected
Terminal binding ambiguous
Session resolver rebound
Codex phase → coding
```

不能：

```text
把 terminal 原始内容 print 出来
```

Debug mode 如果以后增加，也必须显式 opt-in，并做 secrets redaction。

---

# 53. Snapshot 中也不要保存完整消息

只保留：

```text
goal ≤120 chars
summary ≤160 chars
```

继续沿用当前 `shorten()` 的本地规则方案，不调用 LLM。

---

# 54. 失败降级策略

## WSL 扫描失败

保留最后一次成功 Process snapshot。

显示：

```text
状态可能延迟
```

不要瞬间让全部 Agent 消失。

当前 V2 已经有 source-isolated cache 思想，保留。

---

## Session 文件暂时不可访问

保留 process target：

```text
UNKNOWN
```

不要删除 Agent。

---

## UIA unavailable

仍然：

```text
Goal
Mode
Coding
Reading
Testing
Done
```

来自 session。

只是：

```text
Codex/Claude Waiting Approval
```

无法补足。

Dashboard：

```text
终端交互状态不可读
```

---

## Terminal mapping ambiguous

禁止把 Terminal waiting observation 归属到某个具体 Agent。

---

# 55. 一个必须接受的物理边界

对 Codex / Claude：

> 如果 approval dialog 位于一个 Windows Terminal pane，而该 pane 没有被 UI Automation 暴露，或者 DeskPet 无法把它唯一关联到对应 Agent PID，在“不 hooks、不控制 Agent、不切换用户 Tab”的约束下，没有第三条可靠的信息源可以精确证明 Waiting Approval。

因此正确行为是：

```text
UNKNOWN / 工作中
```

或者：

```text
需要查看终端
```

而不是猜。

Kimi 不受这个限制，因为 Wire 有 `ApprovalRequest`。

这不是 DeskPet 实现缺陷，而是纯旁路监听所能获得的信息边界。

---

# 56. 测试体系重构

当前测试已经正确覆盖：

```text
Codex silence 不伪造 approval
Claude old completion 不变成新 DONE
Kimi ApprovalRequest ID
session identity binding
ambiguous 不乱绑
tail rotation
```

这些必须保留。

删除 Managed tests 后增加以下测试。

---

# 57. ProcessProbe tests

测试：

```text
Windows agent cwd/ppid/start identity

WSL pid
ppid
sid
pgid
tty
uid
cwd
process token

PID reuse

多 distro

WSL command timeout

一个 distro 失败不影响另一个
```

---

# 58. Environment privacy test

模拟：

```text
OPENAI_API_KEY=TOP_SECRET
ANTHROPIC_API_KEY=SECRET
WT_SESSION=abc
CODEX_HOME=/x
```

最终 Python 对象只能包含：

```text
WT_SESSION
CODEX_HOME
```

断言：

```text
TOP_SECRET
SECRET
```

绝不进入：

```text
AgentInstance
logs
exceptions
diagnostics
```

---

# 59. Codex fixtures

至少覆盖：

```text
Goal from user prompt

TurnStarted
collaboration_mode_kind=plan

read
edit
test
shell

assistant answer
turn complete
error

silence → never WAITING

Terminal approval observation
→ WAITING
```

---

# 60. Claude fixtures

覆盖：

```text
PID registry valid

PID registry stale after /clear

latest transcript switch

permissionMode=plan

acceptEdits
default

user prompt → Goal

tool_use
tool_result
turn complete

transcript missing
+
terminal activity
→ WORKING

permission visible
→ WAITING

open tool + silence
→ never WAITING
```

---

# 61. Kimi fixtures

覆盖新版：

```text
~/.kimi-code
KIMI_CODE_HOME

session_index
state.json
wire.jsonl

lastPrompt
TurnBegin
StatusUpdate.plan_mode
ToolCall
ApprovalRequest
ApprovalResponse
TurnEnd
```

以及 legacy fallback：

```text
~/.kimi
```

---

# 62. UIA unit tests

不要要求 CI 真开 Terminal。

抽象：

```python
class TerminalBackend(Protocol):
    ...
```

Fake backend 测：

```text
notification event

visible range

buffer truncate

approval appeared

approval disappeared

old scrollback approval 不触发

multiple panes

runtime id stale

queue overflow
```

---

# 63. Windows Terminal integration test

额外提供：

```text
tests/uia_probe.py
```

人工/Windows CI smoke test。

场景：

```text
1 个 Windows Terminal
1 个 WSL Codex

多个 tab
Codex + Claude

一个 tab split 两 pane

inactive tab

关闭 pane
重开

approval dialog

Plan mode
```

probe 只打印：

```text
element identity
event type
recognized state
```

默认不打印 terminal raw text。

---

# 64. 性能测试

新增：

```text
tests/benchmark_monitor.py
```

模拟：

```text
6 Agent
3 WSL
3 Windows

持续 30min synthetic events
```

断言：

```text
queue 不增长
target 不增长
tailer 不泄漏
terminal buffers 有上限
session candidates 有上限
```

Windows 实机 benchmark 记录：

```text
RSS
average CPU
wsl.exe spawn count/min
UIA events/min
session bytes read
```

---

# 65. 性能验收目标

不建议用一个绝对“50MB”数字绑死所有机器。

更合理的是：

### Observer 增量内存

相对：

```text
纯 Tk + Animator baseline
```

新增：

```text
ProcessProbe + Session + UIA
```

常驻增量目标：

```text
≤ 10~15 MB
```

### 数据结构

固定上限。

### Idle CPU

无 Agent 输出时：

```text
UIA 不轮询
Session 只 stat bound files
WSL 每 3 秒一次轻量 scan
```

目标是让 CPU 主要消耗来自：

```text
桌宠 GIF animation
```

而不是 monitor。

---

# 66. requirements

当前：

```text
psutil
Pillow
imageio-ffmpeg
scipy
```

增加：

```text
comtypes>=1.4; platform_system=="Windows"
```

不要加入：

```text
pywinauto
opencv
tesseract
watchdog
rich
数据库框架
```

SciPy 目前只用于皮肤转换，并已经优先放独立子进程执行，转换后内存随进程退出，因此不属于主要常驻监听成本。

---

# 67. 建议文件结构

最终：

```text
agents/
    models.py
    discovery.py
    monitor.py
    state.py
    terminal_uia.py

    base.py
    tailer.py
    paths.py
    summarize.py

    codex.py
    claude.py
    kimi.py
    pi.py              # 若保留兼容

actions/
    winkeys.py         # only raise window

pet/
    app.py
    dashboard.py
    bubble.py
    ...

tests/
    test_monitoring.py
    test_discovery.py
    test_terminal_uia.py
    test_state.py
    test_ui.py
    benchmark_monitor.py
```

删除：

```text
agents/managed.py
actions/approver.py
tests/test_managed.py
tests/approve_e2e.py
tests/approve_target.py
tests/debug_sendinput.py
```

---

# 68. 实施批次

## Iteration 1 — Passive Core

第一批先做架构删除：

```text
删除 ManagedManager
删除 approval actions
删除 Dashboard managed UI
删除 connection_mode
config v3 migration
Monitor 只保留 readonly
```

完成标准：

```text
DeskPet 可以正常启动
Windows/WSL Agent 仍能发现
现有 JSONL watcher 正常
气泡正常
无 managed import
无 approval button
```

---

## Iteration 2 — Process Identity / Session Resolver

实现：

```text
WSL ProcessProbe worker
cwd/user/tty/sid
process token
environment allowlist
CODEX_HOME
CLAUDE_CONFIG_DIR
KIMI_CODE_HOME

Agent-specific roots
Kimi 新目录
Claude PID registry hint
late-start session binding
```

完成标准：

```text
已经运行中的 2 个 WSL Codex
可以区分项目/cwd

Codex + Claude + Kimi
可以自动绑定各自 session

正常流程无需选 JSONL
```

---

## Iteration 3 — Semantic State V3

实现：

```text
Status
Phase
Mode
Evidence

Codex Goal
Codex collaboration Plan

Claude Goal
Claude permission mode

Kimi state/index/wire Plan

统一 classify_tool → Phase
StateReducer
```

完成标准：

```text
Codex:
Plan / Coding / Test / Done

Claude:
Plan / Reading / Coding / Done

Kimi:
Plan / Waiting / Coding / Done
```

---

## Iteration 4 — Windows Terminal UIA

实现：

```text
MTA thread
Terminal discovery
TextPattern
Notification events
VisibleRanges
bounded buffers

Codex recognizer
Claude recognizer
Kimi fallback recognizer
```

完成标准：

```text
用户自己打开的 Windows Terminal
WSL Codex approval
→ DeskPet 显示等待审批

Claude permission prompt
→ 等待审批

prompt 消失
→ Waiting 自动清除

不需要 Agent hook
```

Windows Terminal 本身已经公开 UIA text provider 和 text output notification，这一路径有明确平台基础。

---

## Iteration 5 — TerminalResolver + UX

实现：

```text
WT_SESSION hint
UIA runtime identity
confidence binding
project/terminal visualization

Dashboard AgentTarget list
auto follow
manual pin
double-click terminal

advanced ambiguous repair
```

完成标准：

```text
正常使用：
打开 Agent → 自动显示

没有：
绑定气泡
绑定 JSONL
绑定终端主流程
```

---

## Iteration 6 — Hardening / Performance

完成：

```text
symlink guards
privacy tests
queue bounds
UNC IO optimization
UIA failure handling
process churn
PID reuse
WSL restart
multi-tab
split-pane
background tab
Claude transcript regression fallback
30min benchmark
```

然后才标记：

```text
V3 stable
```

---

# 69. V3 最终验收矩阵

| 能力               |     Codex CLI | Claude Code CLI | Kimi Code CLI |
| ---------------- | ------------: | --------------: | ------------: |
| 已打开进程自动发现        |             ✅ |               ✅ |             ✅ |
| WSL distro       |             ✅ |               ✅ |             ✅ |
| cwd / project    |             ✅ |               ✅ |             ✅ |
| Goal             |             ✅ |               ✅ |             ✅ |
| Plan Mode        |  ✅ structured |    ✅ structured |  ✅ structured |
| Thinking         |     ✅/derived |       ✅/derived |             ✅ |
| Reading          |             ✅ |               ✅ |             ✅ |
| Coding           |             ✅ |               ✅ |             ✅ |
| Executing        |             ✅ |               ✅ |             ✅ |
| Testing          |             ✅ |               ✅ |             ✅ |
| Waiting Input    | ✅ session/UIA |   ✅ session/UIA |    ✅ wire/UIA |
| Waiting Approval |           UIA |             UIA |        ✅ Wire |
| Done             |             ✅ |               ✅ |             ✅ |
| Error            |             ✅ |               ✅ |             ✅ |
| 自动审批             |             ❌ |               ❌ |             ❌ |
| 手动审批             |             ❌ |               ❌ |             ❌ |
| 向 Agent 发消息      |             ❌ |               ❌ |             ❌ |
| Agent hooks      |           不需要 |             不需要 |           不需要 |

---

# 70. 最终不变量

V3 任何实现都不能违反以下规则：

```text
1. DeskPet 不启动 Agent
2. DeskPet 不修改 Agent
3. DeskPet 不配置 hooks
4. DeskPet 不使用 SendInput
5. DeskPet 不向 terminal 写输入
6. DeskPet 不自动审批
7. DeskPet 不把静默解释为审批
8. DeskPet 不扫描用户整个 HOME
9. DeskPet 不持久化 terminal raw text
10. DeskPet 不持久化完整 environ
11. 不确定 Agent ↔ pane 时不乱绑定
12. UI 只暴露 AgentTarget，不暴露内部 PID/JSONL/HWND 作为主身份
```

---

# 71. V3 最终一句话架构

```text
Process tells us WHO.
Session data tells us WHAT.
Terminal UIA tells us WHAT THE USER IS CURRENTLY BEING ASKED.
StateReducer combines them.
DeskPet only observes and visualizes.
```

即：

```text
/proc + psutil
       │
       ▼
   WHO / WHERE
       │
       ├──────────────┐
       │              │
       ▼              ▼
Session Events    Terminal UIA
 WHAT Agent does   WHAT UI waits for
       │              │
       └──────┬───────┘
              ▼
        StateReducer
              ▼
         AgentTarget
              ▼
      Bubble / Dashboard
```

这是 V3 最终应该坚持的技术路线。
