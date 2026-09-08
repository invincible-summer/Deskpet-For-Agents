# DeskPet V4.1 — Exact Concurrent Agent Observer 完整实施计划

> 审计基线：`invincible-summer/DeskPet` `main`，HEAD `af8236d15dc3bfecaa89464e1b77d7f84c2b09be`（2026-09-08）。
>
> 本计划只覆盖已经敲定的 V4 第 1–4 点：精确 Window/Tab/Pane 定位与唤起、多 Agent 并发呈现、配置/自启持久化、Agent 退出生命周期。Claude/Kimi/pi 的 matcher 进一步增强属于后续 V4 工作，不在本 V4.1 主范围内，但本计划新增的生命周期与并发接口必须兼容它们。
>
> 约束修订：V4.1 **暂不迁移** `config.json`、用户 skin、animation cache 到 `%LOCALAPPDATA%`。继续使用当前项目目录；`config.json`、`assets/pets/*`、`assets/cache/` 必须保持 Git ignored，不得上传仓库。若新增配置备份文件，也必须加入 `.gitignore`。

## 0. V4.1 的产品合同

DeskPet 仍然是**被动 Observer**，不是 Agent Controller。V3 已经确定的安全边界继续作为不可回退的合同：

- 不启动、不托管用户 Agent；
- 不给 Codex/Claude/Kimi/pi 配置 hooks；
- 不注入 DLL，不 ptrace，不读 `/proc/PID/mem`；
- 不使用 `SendInput`、键盘模拟、剪贴板注入；
- 不扫描/持久化整个 terminal scrollback；
- Terminal 文本仅作为不可信字符串在内存中做有界识别；
- 用户未明确操作时，不替用户批准/拒绝任何 Agent 请求；
- 证据不足时输出 `AMBIGUOUS/UNKNOWN`，不猜。

V4.1 在这个基础上增加四项产品能力：

1. **Exact Terminal Targeting**：一个具体 `AgentInstance` 必须能够绑定到具体 Windows Terminal Window → Tab → Pane；用户点击该 Agent 时，DeskPet 必须先重新确认 Agent 仍存活，再恢复精确 Tab/P​ane，禁止只把窗口前置后落到错误 Tab。
2. **Concurrent Presentation**：并发功能由用户手动开启，支持“单宠聚合”和“多宠分离”两种展示；并发数、可参与 Agent kind、运行期具体 Agent 的加入/移除可控制；所有 Pet/Bubble 最终都只携带 exact `agent_key` 调用统一激活接口。
3. **Durable Preferences**：bubble、skin、scale、speed、位置、并发模式、Pet Slot 等长期偏好可靠落盘；exact Agent/Window/Tab/Pane identity 永远不持久化；开机启动必须能区分“注册表值存在”和“注册路径真实可用”。
4. **Exact Agent Lifecycle**：Agent CLI 进程退出即终止该 `AgentTarget` 的生命周期；terminal/shell 继续存在不能让 Agent 留存或复活。Windows 优先事件驱动退出；WSL 依赖健康、权威 census，不以扫描故障判死。

V4.1 的核心因果链必须固定为：

```text
Authoritative Process Identity
        ↓
Live AgentInstance
        ↓
Session Observation + Terminal Observation
        ↓
AgentTarget
        ↓
Exact TerminalBinding(Window/Tab/Pane)
        ↓
PresentationController
        ↓
Aggregate cards / Fleet PetViews
        ↓
activate_target(exact agent_key)
```

反方向全部禁止：

```text
config 以前记住过某 Agent      ─┐
旧 PID / HWND / RuntimeId       ├─> 不得证明 Agent 当前仍然存在
旧 Pet Slot                     ─┘

旧 Tab/P​ane binding
    └─> 不得跳过生命周期复核直接唤起
```

## 1. 当前 V3 代码审计与 V4.1 必改点

V3 的基础并不需要重写。当前结构已经具备：

- 一个 Tk UI thread；
- 一个 Monitor Core thread；
- 一个 ProcessProbeWorker；
- 一个 Windows Terminal UIA MTA；
- WSL 按 distro 合并扫描，避免每 Agent 一个 `wsl.exe`；
- Session file 增量 tail；
- Terminal UIA Notification/TextChanged/StructureChanged；
- UIA event queue ≤256、call queue ≤32；
- pane ≤16；
- per-pane terminal ring ≤8192 chars；
- visible snapshot ≤4096 chars；
- fallback visible reads 全局 ≤6/s；
- Terminal approval TTL 1.5s；
- mutual-unique resolver，证据不足不绑定。

这些应保留。

当前 V3 与 V4.1 冲突或存在缺陷的代码如下。

| 当前 V3 | 问题 | V4.1 处理 |
|---|---|---|
| `Monitor.primary_key` / `_FOLLOW_PRIORITY` | 把事实层与 UI 展示层耦合 | 从 Monitor 删除，迁到 `PresentationController` |
| `monitor.pinned` | 把 exact runtime Agent key 写配置 | 删除；运行期 focus 不持久化 |
| `is_bound(key) == key == primary_key` | 并发时错误地把“UI 当前”叫“绑定” | 删除 |
| `gone_grace_sec=15` | 健康扫描已确认进程消失仍保留幽灵 Agent | 删除 authoritative absence grace |
| `scan_windows()` 全局失败返回 `[]` | 上层会错误标为 healthy empty | 改为显式 authoritative/unhealthy 结果 |
| Monitor 仅对当前存在 kind 调 `watcher.poll(insts)` | 最后一个 Agent 退出后 `poll([])` 清理路径不会执行 | 每个 watcher 每轮都收到对应实例列表 |
| UI 直接 `winkeys.raise_terminal(binding)` | 无法重新验证 Agent 已退出；只到 Window 不到 Tab | UI 只能调用 `activate_target(agent_key)` |
| `TerminalBinding` 只有 Window + Pane | 不足以精确恢复多 Tab | 增加 WindowIdentity + Tab runtime identity |
| 手工“关联当前 Pane” | 未捕获 Tab | 升级为“关联当前 Terminal 位置” |
| `PetApp` 一个 `tk.Tk` 就是一只宠物 | Fleet 无法在不复制 App/Monitor 的情况下扩展 | 隐藏 root + N `Toplevel` PetViews |
| 每个 `Animator` 自带最多 48 MB cache | N 个 Pet 会把内存预算乘 N | 一份全局 SharedAnimationCache |
| `_special_until` 直接提前 return | Agent B WAITING 时可能仍庆祝 A DONE | WAITING/INPUT/ERROR 必须抢占 special |
| `Config.save()` 静默吞掉 OSError | 用户以为保存成功但重启丢设置 | save 返回结果并在 UI 暴露错误 |
| `PetWindow.set_topmost()` 只 set 不 save | 明确存在持久化遗漏 | 所有设置统一走 set+commit |
| `autostart.is_enabled()` 只看注册表值存在 | repo/python 路径移动后仍显示 enabled | 比较 expected/registered command，支持 repair |
| Dashboard Notebook + 详情独立 Tab | 并发设置加入后层级会失控 | 重做左侧导航 + card/master-detail |

V4.1 不删除 V3 的 `AgentTarget`、`StateReducer`、Agent watcher、Session resolver、Terminal recognizer；只调整它们的生命周期和外围编排。

## 2. 目标代码架构

建议 V4.1 形成以下结构。新文件保持少而职责明确，不引入 Qt/CustomTkinter/pywinauto 等重依赖。

```text
agents/
  models.py                 MODIFY
  discovery.py              MODIFY
  process_watch.py          NEW
  base.py                   MODIFY
  monitor.py                MODIFY
  terminal_uia.py           MODIFY
  terminal_service.py       NEW
  state.py                  KEEP
  matching.py               KEEP
  codex.py / claude.py /
  kimi.py / pi.py           KEEP（只适配生命周期接口）

actions/
  winkeys.py                MODIFY，仍只做 Win32 Window primitive

pet/
  app.py                    REFACTOR 为应用控制器
  presentation.py           NEW
  petview.py                NEW
  petwindow.py              MODIFY 为 Toplevel view
  bubble.py                 MODIFY/拆 renderer
  animator.py               REFACTOR 共享缓存
  skins.py                  MODIFY build 去重
  config.py                 MODIFY
  autostart.py              MODIFY
  dashboard.py              REFACTOR
  theme.py                  NEW
  widgets.py                NEW
  tray.py                   MODIFY

tests/
  test_process_watch.py     NEW
  test_terminal_service.py  NEW
  test_presentation.py      NEW
  test_config.py            NEW
  test_autostart.py         NEW
  现有 test_discovery.py /
  test_monitoring.py /
  test_terminal_uia.py /
  test_ui.py                EXPAND
  benchmark_monitor.py      EXPAND
  benchmark_presentation.py NEW
```

线程数量必须与 Agent/Pet 数量解耦：

```text
长期线程
1  Tk UI
1  Monitor Core
1  ProcessProbeWorker
1  Terminal UIA MTA
1  WindowsExitWatcher（仅 Windows，阻塞等待）
1  Tray thread（仅开启托盘时）

临时
≤1 Skin converter worker/subprocess
```

禁止：

```text
每 Agent 一个 watcher thread
每 Agent 一个 UIA client
每 Pet 一个 Monitor
每 Pet 一个 ProcessProbe
每 Pet 一份 48 MB frame cache
```

## 3. 数据模型：明确 Runtime Fact 与 Durable Preference 的边界

### 3.1 Agent identity 保持 V3 思路

`AgentInstance.key` 继续由：

```text
source + kind + PID + process_token
```

形成。

Windows `process_token` 继续使用 `create_time`；WSL 优先 `/proc/PID/stat starttime`。PID 被复用时，新进程必须产生新 key。

不新增 `Status.EXITED`。`Status` 描述的是**仍存活 Agent** 的状态；进程退出后直接没有 `AgentTarget`。

### 3.2 Source probe 统一成一个不可拆开的结果

替换当前：

```python
_snapshot: dict[str, list[AgentInstance]]
_ok: dict[str, bool]
```

为：

```python
@dataclass(frozen=True)
class SourceProbeSnapshot:
    source: str
    generation: int
    observed_at: float

    # True = 本轮结果有资格宣布旧实例“不存在”
    authoritative: bool

    instances: tuple[AgentInstance, ...]
    error: str = ""
```

语义只能是：

```text
authoritative=True + instances
    = 权威发现

authoritative=True + empty
    = 权威确认当前无匹配 Agent

authoritative=False
    = 无法读取；不得根据本轮结果判死
```

这样不再允许“空 list”和“health bool”被两个代码路径错误组合。

### 3.3 Window / Tab / Pane identity

在 `agents/models.py` 增加：

```python
@dataclass(frozen=True)
class WindowIdentity:
    hwnd: int
    pid: int
    process_created: float
    window_class: str


@dataclass(frozen=True)
class TabInfo:
    # 仅本次 desktop/UI 生命周期有效
    tab_id: tuple
    hwnd: int
    window_pid: int

    title: str
    index_hint: int
    selected: bool
    last_seen: float


@dataclass(frozen=True)
class TerminalLocation:
    window: WindowIdentity
    tab_id: tuple | None
    pane_id: tuple | None


class BindingOrigin(str, Enum):
    AUTO = "auto"
    OBSERVED = "observed"
    MANUAL = "manual"
```

扩展 `TerminalBinding`：

```python
@dataclass
class TerminalBinding:
    provider: str = "windows-terminal"

    hwnd: int = 0
    window_pid: int = 0
    window_created: float = 0.0
    window_class: str = ""

    title: str = ""

    # runtime-only
    tab_id: tuple | None = None
    tab_index_hint: int = -1
    pane_id: tuple | None = None

    origin: BindingOrigin = BindingOrigin.AUTO
    confidence: BindingConfidence = BindingConfidence.NONE
    observable: bool = False

    last_seen: float = 0.0
    validated_at: float = 0.0

    # 现有诊断字段继续
    score: int = 0
    runner_up_score: int = 0
    agent_margin: int = 0
    pane_margin: int = 0
    reason: str = ""
```

`tab_index_hint` 只能是 hint。用户拖动 Tab 后 index 会改变，不能作为 identity。

UIA `RuntimeId`：

- 只保证生成它的当前 desktop UI 唯一；
- 可以随时间复用；
- 格式是不透明的；
- 因此 `tab_id/pane_id` **不得写进 config**。

### 3.4 激活结果不能再是 `bool`

新增：

```python
class ActivationCode(str, Enum):
    OK = "ok"
    AGENT_GONE = "agent_gone"
    NO_BINDING = "no_binding"
    AMBIGUOUS = "ambiguous"

    STALE_WINDOW = "stale_window"
    STALE_TAB = "stale_tab"
    STALE_PANE = "stale_pane"

    UIA_UNAVAILABLE = "uia_unavailable"
    FOREGROUND_DENIED = "foreground_denied"


@dataclass(frozen=True)
class ActivationResult:
    code: ActivationCode
    repaired: bool = False
    detail: str = ""
```

`detail` 只能是安全诊断，不包含 terminal 原文。

## 4. 第 4 点：Agent 退出生命周期的完整实现

### 4.1 先修 Windows probe 健康语义

当前 `scan_windows()` 的全局 `psutil.process_iter()` 异常会返回空 list，随后 Worker 把 Windows 标记为 healthy。这必须先修，否则删除 `gone_grace` 会造成“一次扫描错误 → 全部 Windows Agent 消失”。

建议：

```python
class ProbeUnavailable(RuntimeError):
    pass


def scan_windows() -> list[AgentInstance]:
    try:
        procs = list(psutil.process_iter(...))
    except Exception as exc:
        raise ProbeUnavailable(...) from exc
```

边界：

- `process_iter()` 整体失败：`authoritative=False`；
- 单个 process 的 `AccessDenied/NoSuchProcess`：跳过该 process，不把整轮变 unhealthy；
- 扫描完成且无 Agent：`authoritative=True, instances=()`。

### 4.2 删除 authoritative absence 的 15 秒 grace

V4.1 不再保留：

```text
monitor.gone_grace_sec
Monitor._gone_since
```

规则改为：

```python
if source_snapshot.authoritative:
    old_exact_key not in snapshot.instances:
        _commit_exit(old_exact_key, "authoritative-absence")
else:
    keep old target
    snapshot.stale = True
```

`activity_grace_sec` 仍保留，因为它描述的是**活 Agent 的活动证据 TTL**，不是进程生命周期。

### 4.3 Windows 使用一个事件驱动 ExitWatcher

新增 `agents/process_watch.py`：

```python
@dataclass(frozen=True)
class ProcessExitEvent:
    key: str
    pid: int
    process_token: str
    timestamp: float


class WindowsExitWatcher:
    def start(self) -> None: ...
    def stop(self) -> None: ...

    def register(self, instance: AgentInstance) -> bool: ...
    def unregister(self, key: str) -> None: ...
    def drain(self) -> list[ProcessExitEvent]: ...
```

实现要求：

- 只对 Windows native Agent 使用；
- `OpenProcess(SYNCHRONIZE, False, pid)`，不请求 VM_READ/VM_WRITE；
- 一个 daemon thread 用 `WaitForMultipleObjects` 阻塞等待；
- 包含一个 control/event handle，用于 register/unregister/stop 后重建 wait set；
- V4.1 hard presentation cap ≤8，Monitor process target/pane cap已有16，远低于 Win32 `MAXIMUM_WAIT_OBJECTS`；
- 句柄 signal 后只产生候选 `ProcessExitEvent`，Monitor 收到后还要核对 `key/process_token` 仍是当前 exact incarnation，防止竞态；
- handle 打不开不能直接宣布退出，只依赖下一次 authoritative census 兜底；
- 所有 handle 在 unregister/stop 后关闭。

这条路径不轮询，空闲 CPU 接近零。

### 4.4 WSL 不增加常驻 helper

禁止为“更快退出”在 WSL 内启动长驻 watcher。原因：

- DeskPet 必须保持旁路；
- 长驻进程可能延长 distro 生命周期；
- 当前 V3.1.2 已经专门使用 fresh `wsl --list --running` 防止过期 Running cache 重新拉起已停止 distro。

默认 WSL census 继续约 3s，正确性来自 authoritative 三态，而不是高频扫描。

建议把 WSL scan 再分成：

```text
census
    fresh running distros
    + one ps per running distro

metadata enrichment
    仅新/变化 canonical PID 批量读取
    cwd/starttime/HOME/allowlisted env
```

给 `_PS_FORMAT` 加 `stat=` 或等价 process state：

```text
Z = zombie
X/x = dead
```

这些不作为 live Agent。

`T/t/S/R/D/I/...` 等仍代表 process object 存活，不能仅因“不运行 CPU”判退出。

### 4.5 `_commit_exit()` 必须做级联回收

在 Monitor 增加唯一退出入口：

```python
def _commit_exit(self, key: str, reason: str, now: float) -> bool:
    """
    exact key 不存在/已经被替换则 no-op。
    成功后任何旧 UI action 再传此 key 都只能得到 AGENT_GONE。
    """
```

职责：

1. 从 `instances` 删除 exact key；
2. 从 `snapshots` 删除；
3. 从 `bindings` 删除；
4. `TerminalResolver.drop_instance(key)`，删除 manual/observed runtime binding；
5. 找到对应 watcher，`watcher.drop_instance(key)`；
6. WindowsExitWatcher unregister；
7. 清理与该 key 相关的内部 diagnostics/counters（非全局统计）；
8. 记录不含敏感文本的 lifecycle log；
9. 不直接操作 Tk/Presentation；UI 下一轮 `reconcile()` 消费事实变化。

### 4.6 BaseWatcher 补生命周期 API，并修 zero-kind bug

`BaseWatcher` 增加：

```python
def drop_instance(self, key: str) -> None:
    self._instance_files.pop(key, None)
    self._runtime_bindings.pop(key, None)
    # 若某 file 不再被任何 live instance 使用，在下一 refresh/poll 关闭 tailer
```

Monitor 的 Session poll 改成：

```python
for kind, watcher in self._watchers.items():
    insts = by_kind.get(kind, [])
    session_obs.update(watcher.poll(insts))
```

即使某 kind 当前为 0，也必须调用 `poll([])`，让现有 BaseWatcher 的全清理语义真正发生。

### 4.7 Agent exit UI 语义

Aggregate：

- 卡片立即从 live Agent 列表移除；
- `focused_key` 如果指向它则清空；
- 不保留“EXITED AgentTarget”。

Fleet：

- runtime binding 立即解除；
- 可播放 0.8–1.2s “结束/挥手”纯 UI 动画；
- 这段时间 exact key 已经不可激活；
- 结束后：有持久 Pet Slot 就变 vacant；临时 PetView 直接关闭。

## 5. 第 1 点：Exact Windows Terminal Topology 与唤起

### 5.1 为什么不能以 `wt.exe` index/MRU 作为主路线

当前 Windows Terminal 已有：

```text
wt -w <window-id> focus-tab -t <tab-index>
```

甚至源码里已有 `focus-pane` action/CLI。

但 DeskPet 缺少的是**反向查询**：

```text
Agent PID / WT_SESSION / HWND
        ↓
Windows Terminal internal window-id
tab-index
internal pane-id
```

当前公开接口没有这条稳定映射：

- `-w 0/last` 是 MRU，不是 DeskPet exact HWND；
- 不存在的 window-id/name 可能创建新 Window；
- `focus-tab` 需要 index，而外部没有稳定 current-tab/list-tabs 查询；
- `WT_SESSION -> existing tab` 仍没有公开 focus/query API。

所以主 identity 必须是 UIA runtime topology；`wt.exe` 只保留为未来有官方 mapping 后可替换的 backend，不进入 V4.1 主激活路径。

### 5.2 Windows Terminal UI 树的物理限制

Windows Terminal 当前源码明确：Tab selection change 后才把该 Tab 对应的 terminal XAML control attach 到 XAML root。

因此 V4.1 **不得假定所有 background Tab 的 TermControl 都能同时遍历**。

正确模型：

```text
Window
├─ TabItem A
├─ TabItem B
├─ TabItem C
└─ 当前 selected Tab 的 TermControl(s)
```

实现后果：

- 可以维护所有 TabItem runtime identity；
- Pane observation 只针对当前 XAML root 暴露的 live TermControl；
- 绝不为了识别而后台自动逐 Tab 切换；
- 当用户自然切换 Tab 时，由 UIA selection event 学习 `Tab ↔ Pane ↔ Agent`；
- cold start 多个完全相同 Tab 且从未被观察过时，证据不足就 `AMBIGUOUS`；
- 用户可用一次“关联当前 Terminal 位置”确认。

### 5.3 扩展现有唯一 UIA MTA，不新增 UIA thread

当前 `UiaBackend._submit()` 的 bounded MTA queue 应继续作为所有 UIA 操作唯一入口。

扩展 `TerminalBackend`：

```python
class TerminalBackend:
    def discover_layout(self) -> "TerminalLayout": ...
    def selected_tab(self, hwnd: int) -> TabInfo | None: ...
    def focused_location(self) -> TerminalLocation | None: ...

    def select_tab(self, tab_id: tuple) -> bool: ...
    def focus_pane(self, pane_id: tuple) -> bool: ...
```

建议：

```python
@dataclass
class TerminalLayout:
    windows: dict[int, WindowIdentity]
    tabs: dict[tuple, TabInfo]
    panes: dict[tuple, PaneInfo]

    # 每个 HWND 当前 selected tab
    selected_tabs: dict[int, tuple]
```

`discover_panes()` 可暂时保留 adapter，待测试稳定后删除，避免同时维护两套发现实现。

### 5.4 TabItem discovery

UIA 侧新增：

- TabItem control type/property condition；
- `SelectionItemPattern.CurrentIsSelected`；
- `GetRuntimeId()`；
- 当前显示 index 只作为 `index_hint`；
- title 只用于 UI/诊断，不能作为 exact identity。

订阅：

```text
UIA_SelectionItem_ElementSelectedEventId
```

事件只携带 identity/topology dirty 信号，不读取 terminal text。

用户自然切 Tab：

```text
Selection event
    ↓
更新 selected Tab
    ↓
一次 bounded current TermControl discover
    ↓
更新 Tab ↔ Pane observation
    ↓
resolver 有唯一证据时升级 binding
```

这比轮询全树更省 CPU，也不会打扰用户。

### 5.5 Tab runtime identity 的使用规则

`tab_id` 建议：

```python
tab_id = (hwnd, tuple(tab_element.GetRuntimeId()))
```

规则：

- reorder 后 RuntimeId 仍存在则 binding 不受 index 变化影响；
- index 只更新 `tab_index_hint`；
- RuntimeId 消失时必须 re-resolve；
- RuntimeId 绝不落盘；
- Tab tear-out 到另一 Window 后视为 topology changed，新 Window/Tab identity 重新学习。

### 5.6 Pane runtime identity 的兼容策略

已确认 `pane_id=(hwnd, TermControl RuntimeId)` 是当前 V3 的 runtime identity。

仍有一个必须用实机 probe 验证的 Windows Terminal 实现行为：

> inactive Tab detach / selected Tab reattach 后，内部 TermControl RuntimeId 是否保持稳定。

V4.1 不依赖它一定稳定，而定义 fail-closed revalidation：

```text
选中 exact Tab
    ↓
重新 discover current Tab TermControl(s)

stored pane RuntimeId 存在
    → exact focus

stored pane RuntimeId 不存在
且只有一个 TermControl
    → sole-pane safe rebind

stored pane RuntimeId 不存在
且多个 TermControl
    → STALE_PANE / AMBIGUOUS
    → 不猜
```

### 5.7 WindowIdentity 真正启用 `window_created`

当前 `TerminalBinding.window_created` 基本填 0，应改成真正的 Windows Terminal process `create_time`。

Window validation：

```text
IsWindow(hwnd)
AND current PID == expected window_pid
AND current process create_time == expected window_created
AND class == expected window_class
```

这样 HWND/PID reuse 不会继承旧绑定。

`actions/winkeys.py` 只保留：

```python
enum_windows()
window_identity(hwnd)
validate_window(identity)
restore_window(hwnd)
try_set_foreground(hwnd)
flash_window(hwnd)
```

不要把 Tab/P​ane UIA 逻辑塞进 winkeys。

### 5.8 `TerminalService`：观察、解析、激活的统一 facade

新增 `agents/terminal_service.py`，持有同一个 `UiaBackend`：

```python
class WindowsTerminalService:
    def start(self) -> bool: ...
    def stop(self) -> None: ...

    # Monitor 每轮
    def poll(self, now: float) -> None: ...
    def resolve(
        self,
        instances: list[AgentInstance],
        now: float,
    ) -> dict[str, TerminalBinding]: ...

    def waiting_observation(self, binding: TerminalBinding) -> Observation | None: ...
    def activity_observation(...) -> Observation | None: ...

    # 用户显式 action
    def activate(
        self,
        target: AgentTarget,
        is_agent_live: Callable[[str], bool],
    ) -> ActivationResult: ...

    def bind_focused_location(self, agent_key: str) -> bool: ...
    def drop_instance(self, agent_key: str) -> None: ...

    def stats(self) -> dict: ...
```

内部可组合：

```text
UiaBackend
TerminalObserver
TerminalTopologyRegistry
TerminalResolver
TerminalActivator
```

但外部 Monitor 不再分别持 `_terminal` 和 `_resolver` 两套生命周期。

### 5.9 exact activation transaction

所有 UI action 只允许调用：

```python
Monitor.activate_target(agent_key)
```

Monitor：

```python
def activate_target(self, key: str) -> ActivationResult:
    target = self.get_target(key)
    if target is None:
        return ActivationResult(ActivationCode.AGENT_GONE)

    # get_target 后仍可能退出，TerminalService 内还会二次 is_agent_live
    return self._terminal_service.activate(
        target,
        is_agent_live=self.is_live_key,
    )
```

Terminal activation 顺序固定：

1. 再确认 exact `agent_key` 仍 live；否则 `AGENT_GONE`。
2. binding 必须存在；否则 `NO_BINDING`。
3. `CONFIRMED/HIGH` 才允许 exact activate；`AMBIGUOUS` 返回 `AMBIGUOUS`。
4. 校验 WindowIdentity。
5. 失败时只允许一次 topology refresh/re-resolve；仍失败 `STALE_WINDOW`。
6. 在 exact HWND 下枚举 TabItem。
7. 用 `tab_id` 定位；不得 title/index 猜测。
8. `SelectionItemPattern.Select()`。
9. 验证目标 Tab selected。
10. 重新发现该 Tab 当前 TermControl(s)。
11. 按 pane revalidation 规则定位 exact/sole pane。
12. `ShowWindow(SW_RESTORE)`（如最小化）。
13. `SetForegroundWindow(hwnd)`。
14. 只有确认 `GetForegroundWindow()==hwnd` 后才对 Pane `SetFocus()`。
15. Windows 拒绝 foreground：Tab 已经可以选对，但不强行绕过 OS policy；调用 `FlashWindowEx`，返回 `FOREGROUND_DENIED`。
16. 任意 identity 失效都 fail-closed，不 fallback 到“第一个 Tab/第一个 Pane/MRU Window”。

### 5.10 手工绑定升级

删除公共语义：

```text
bind_focused_pane()
高级：关联当前 Pane
```

替换：

```python
def bind_focused_location(self, key: str) -> BindResult:
    ...
```

一次 MTA transaction 捕获：

```text
exact Agent key/process token
exact HWND + WindowIdentity
selected Tab RuntimeId + index_hint
focused TermControl RuntimeId
```

结果：

```text
BindingConfidence.CONFIRMED
origin=MANUAL
```

只保存在本次应用运行期。

Agent exit / Tab close / Pane close / Window identity 变化立即使其失效。

## 6. 第 2 点：并发模型必须从 Monitor 中分离

### 6.1 并发是用户手动开启的 Presentation feature

为兼容 V3：

```text
V3 -> V4.1 migration:
presentation.concurrent.enabled = false
```

因此升级后默认仍是单目标桌宠。

用户手动开启：

```text
并发监听显示 [ON]
```

开启后默认 submode：

```text
aggregate
```

并发设置：

```python
@dataclass
class ConcurrentSettings:
    enabled: bool = False
    mode: Literal["aggregate", "fleet"] = "aggregate"

    max_targets: int = 3       # user configurable 1..8
    eligible_kinds: set[AgentKind]
```

`max_targets` 是展示/分配上限，不是 Monitor discovery 上限。超出的 live Agent 仍继续被 Monitor 正确监听，并在 Dashboard 可见。

### 6.2 区分“发现开关”和“展示开关”

保留：

```text
monitor.agents.codex/claude/kimi/pi
```

含义：

> 是否发现/解析这个 Agent kind。

新增：

```text
presentation.concurrent.eligible_kinds
```

含义：

> 并发模式里是否自动候选显示这个 kind。

Dashboard 还允许对**当前 exact live instance**：

```text
加入并发
移出并发
```

这是 runtime state：

```python
included_keys: set[str]
excluded_keys: set[str]
```

不写 exact key 到配置。

### 6.3 PresentationController

新增 `pet/presentation.py`：

```python
class PresentationMode(str, Enum):
    SINGLE = "single"
    AGGREGATE = "aggregate"
    FLEET = "fleet"


@dataclass
class RuntimeViewBinding:
    view_id: str
    agent_key: str
    assigned_at: float


class PresentationController:
    def __init__(self, config): ...

    @property
    def mode(self) -> PresentationMode: ...

    def reconcile(self, targets: dict[str, AgentTarget], now: float) -> "PresentationState":
        ...

    def set_focus(self, agent_key: str | None) -> None: ...
    def set_instance_included(self, agent_key: str, included: bool) -> None: ...

    def bind_slot(self, slot_id: str, agent_key: str) -> bool: ...
    def unbind_slot(self, slot_id: str) -> None: ...

    def attention_key(self, targets) -> str | None: ...
```

Monitor 不 import `pet.*`。PresentationController 只消费 `get_targets()`，避免循环依赖。

### 6.4 `focused_key` 与 `attention_key` 分开

`focused_key`：

- 用户最近明确选择/点击的 Agent；
- runtime only；
- 不因另一个普通 WORKING 自动改变；
- Agent exit 后清空。

`attention_key`：

- 当前最需要引起注意的 Agent；
- 继续使用 V3 的优先级思想，但从 Monitor 移到 Presentation：

```text
WAITING
> INPUT
> ERROR
> WORKING
> DONE
> IDLE
> UNKNOWN
```

同优先级稳定排序：

```text
当前 focused（若仍在候选）
> 最近状态变更需要注意
> started_at
> exact key
```

WAITING 可在视觉上移动到顶部/高亮，但不偷偷改用户 focused_key。

### 6.5 special 动画优先级修正

Presentation 的 Pet animation target 规则：

```text
WAITING / INPUT
    → die（最高，可中断任何 special）

ERROR
    → error 对应现有可用动画语义（若仍只有五状态，可暂时 die）

WORKING
    → walk

DONE 且没有更高 attention
    → special ×3

无 active work
    → sleep
```

`now < _special_until` 不得提前 return 掩盖新的 WAITING/INPUT/ERROR。

## 7. Aggregate：一个桌宠，多张精确 Agent 卡片

### 7.1 交互合同

并发 Aggregate 时：

- 桌宠 body 单击/双击只做互动；
- **桌宠 body 不激活任何 Terminal**；
- 每张 Agent Card 都绑定一个 exact `agent_key`；
- 点击该 card / card footer 才调用 `activate_target(card.agent_key)`；
- 超过 `max_targets` 的 Agent 不丢监控，只显示“还有 N 个 Agent”入口。

### 7.2 Bubble renderer 重构

不要让现有 `BubbleModel` 变成一个含几十个 optional 字段的大对象。

建议：

```python
@dataclass(frozen=True)
class AgentCardModel:
    agent_key: str
    title: str
    status: str
    text: str
    footer: str
    accent: str
    attention: bool = False


@dataclass(frozen=True)
class AggregateBubbleModel:
    cards: tuple[AgentCardModel, ...]
    overflow_count: int = 0
    visible: bool = False
```

Renderer：

```text
BubbleRendererBase
├─ SingleAgentBubbleRenderer
└─ AgentStackBubbleRenderer
```

共享：

- DPI metrics；
- text fit/wrap；
- rounded rectangle；
- font cache；
- hit testing。

Hit result 改成强类型：

```python
@dataclass(frozen=True)
class HitTarget:
    action: Literal["activate_agent", "open_dashboard", "none"]
    agent_key: str = ""
```

彻底移除 `tag=="details" -> current primary` 这种隐式关联。

### 7.3 推荐视觉尺寸

逻辑像素（最终仍乘 scale/DPI）：

```text
宽 330~350
顶部 summary 28~34
每 Agent card 56~64
最多默认 3 张
overflow 28~30
card gap 6~8
```

单 Agent 时可以继续呈现接近 V3 的 300×132 卡，减少视觉回归。

### 7.4 Aggregate target selection

默认从“eligible + included - excluded”中按：

```text
focused
attention priority
stable runtime order
```

选前 `max_targets`。

如果用户在 Dashboard 显式“加入并发”一个 Agent，而达到 cap：

- 不自动踢掉另一个显式 included；
- UI 提示先移出一个，或临时把 cap 调高；
- 不修改 Monitor。

## 8. Fleet：一套 Monitor，多只 Toplevel Pet

### 8.1 Tk 架构必须重构

当前 `PetApp.root` 自己就是透明桌宠。

V4.1：

```python
class PetApp:
    def __init__(...):
        self.root = tk.Tk()
        self.root.withdraw()    # controller root，不是宠物

        self.monitor = Monitor(...)
        self.presentation = PresentationController(...)
        self.pet_manager = PetViewManager(...)
```

每只宠物：

```python
class PetView:
    view_id: str
    agent_key: str | None

    window: PetWindow      # Toplevel
    animator: AnimationCursor
    bubble: SingleAgentBubbleRenderer
```

`PetWindow`：

```python
class PetWindow:
    def __init__(self, master: tk.Misc, view_config, ...):
        self.root = tk.Toplevel(master)
```

所有 PetView 同一个 Tcl/Tk interpreter。

### 8.2 Fleet 绑定语义

满足“每只桌宠双击绑定各自 Agent”的要求：

空 slot：

```text
双击 Pet body
    → 打开轻量 live Agent picker
    → 选择一个当前 live exact Agent
    → bind_slot(slot_id, exact agent_key)
```

已绑定 slot：

```text
双击 Pet body
    → activate_target(exact agent_key)
```

重新绑定：

```text
右键 → 更换 Agent
Dashboard slot card → 绑定/解除
```

Bubble 点击也激活该 slot 的 exact Agent。

默认不让两个 Pet 同时绑定同一个 exact key；若用户尝试，提示“该 Agent 已由 Pet X 展示”。

### 8.3 Fleet 持久 Pet Slot，不持久 exact Agent

长期保存的是：

```text
pet-1
pet-2
pet-3
```

不是 Agent。

Agent exit：

```text
pet-1 runtime agent_key → None
```

但 `pet-1` 的 skin/size/position/selector 保留。

### 8.4 语义 Selector 只用于重启后“尝试重新认领”

```python
@dataclass
class AgentSelector:
    kind: str | None
    source: str | None
    workspace_fp: str | None
    label: str
```

`workspace_fp` 可由 normalized cwd 做 SHA-256 后截断，例如 16 bytes/32 hex；`label` 只作 UI。

恢复规则：

```text
0 candidate
    → slot vacant

1 unique candidate
    → 自动 runtime bind

>1 candidate
    → 不猜，slot vacant / 显示“需要重新确认”
```

绝不能因为 selector 模糊而把某个 Pet 唤起到另一 Agent Terminal。

## 9. 第 3 点：配置持久化，保留当前项目内路径

### 9.1 文件位置保持不变

V4.1 继续：

```text
<repo>/config.json
<repo>/assets/pets/
<repo>/assets/cache/
```

现有 `.gitignore` 已经覆盖：

```gitignore
assets/pets/*
!assets/pets/README.md
assets/cache/
config.json
```

如果加入备份：

```gitignore
config.json.bak
.deskpet-config-*
```

不要迁移 `%LOCALAPPDATA%`。

同时在计划中承认边界：

> 如果将项目放到无写权限目录，V4.1 会明确报告“配置无法写入”，而不是静默假装成功；路径迁移留给后续版本。

### 9.2 V4 config 结构

保持现有 root appearance 字段作为**全局默认**，降低 V3 迁移量：

```json
{
  "config_version": 4,

  "skin": "amiya",
  "scale": 1.0,
  "speed": 1.0,
  "animated": true,
  "topmost": true,
  "tray_enabled": true,
  "pet_pos": [1200, 900],

  "bubble": {
    "enabled": true,
    "font_family": "Microsoft YaHei UI",
    "font_size": 11,
    "width": 300,
    "height": 132,
    "relative_width": 1.0,
    "relative_height": 1.0,
    "relative_font": 1.0,
    "always_visible": true
  },

  "monitor": {
    "agents": {
      "codex": true,
      "claude": true,
      "kimi": true,
      "pi": true
    },
    "windows_enabled": true,
    "wsl_enabled": true,
    "windows_scan_sec": 3.0,
    "wsl_scan_sec": 3.0,
    "file_poll_sec": 0.5,
    "session_scan_sec": 3.0,
    "activity_grace_sec": 10.0,
    "terminal_observer": true
  },

  "presentation": {
    "concurrent": {
      "enabled": false,
      "mode": "aggregate",
      "max_targets": 3,

      "eligible_kinds": {
        "codex": true,
        "claude": true,
        "kimi": true,
        "pi": true
      },

      "slots": [
        {
          "id": "pet-1",
          "selector": null,
          "appearance": {
            "skin": null,
            "scale": null,
            "speed": null,
            "animated": null,
            "bubble": {
              "enabled": null
            }
          },
          "placement": {
            "monitor": "",
            "u": null,
            "v": null,
            "anchor": null,
            "manual": false
          }
        }
      ]
    }
  },

  "privacy": { "...": "保留 V3" },
  "animation_cache_mb": 48,
  "force_state": "",
  "convert": { "height": 240, "fps": 12 }
}
```

删除：

```text
monitor.pinned
monitor.gone_grace_sec
```

### 9.3 Slot appearance 用 inherit override

`null` = 继承全局。

实现 `ResolvedViewConfig`：

```python
class ResolvedViewConfig:
    def __init__(self, global_config: Config, slot: dict | None):
        ...

    def get(self, path: str, default=None):
        # 先 slot override，null/不存在则 global
        ...
```

这样现有 Renderer/Animator/PetWindow 的 `config.get("scale")` 风格可以继续使用，不必为 Fleet 把所有组件 API 全推翻。

### 9.4 Config API 统一，禁止 silent save

建议：

```python
@dataclass(frozen=True)
class ConfigSaveResult:
    ok: bool
    path: str
    error: str = ""


class Config:
    def __init__(self, path: str = CONFIG_PATH): ...

    def get(self, path, default=None): ...
    def set(self, path, value) -> None: ...

    def update_many(self, values: dict[str, object]) -> None: ...

    def commit(self) -> ConfigSaveResult:
        ...

    def set_and_commit(self, path, value) -> ConfigSaveResult:
        ...

    @property
    def dirty(self) -> bool: ...
    @property
    def last_save_result(self) -> ConfigSaveResult: ...
```

`commit()`：

1. 在当前 ROOT 建 `.deskpet-config-*` temp；
2. JSON dump + `flush()`；
3. 可选 `os.fsync()`（配置很小，只有用户显式保存时调用，不是 hot path）；
4. 若已有 valid `config.json`，复制/replace 成 `config.json.bak`；
5. `os.replace(temp, config.json)`；
6. 成功才 `dirty=False`；
7. 失败返回错误，不吞掉。

加载：

```text
config.json valid
    → normalize + use

invalid
    → 尝试 config.json.bak

仍 invalid
    → DEFAULTS
    → migration/diagnostics 提示
```

### 9.5 Schema normalize

加载后对用户可修改字段 clamp：

```text
scale             0.5..2.0
speed             0.1..3.0
font_size         8..24
max_targets       1..8
bubble width      安全范围
bubble height     安全范围
slot ids          唯一、非空
mode              single/aggregate/fleet whitelist
agent kind keys   whitelist
```

未知键可以保留以兼容未来版本，但运行时只消费 schema 中已知字段。

### 9.6 设置写入统一入口

删除这种分散代码：

```python
config.set(...)
# 某些路径 save，某些不 save
```

UI 层通过：

```python
app.settings.set(path, value)
app.settings.update_many(...)
```

或者直接 `Config.set_and_commit`。

几何拖动做 debounce：

```text
拖动过程中不写磁盘
ButtonRelease
    → 一次 placement commit
```

Slider：

```text
拖动中只 preview
ButtonRelease
    → 一次 commit
```

避免增加磁盘写入和 CPU。

## 10. 开机启动完整修复

继续使用当前轻量的：

```text
HKCU\Software\Microsoft\Windows\CurrentVersion\Run
DeskPet
```

不引入 Task Scheduler、服务或管理员权限。

### 10.1 `is_enabled()` 不再只检查“值存在”

新增：

```python
@dataclass(frozen=True)
class AutostartStatus:
    registered: bool
    healthy: bool

    expected_command: str
    registered_command: str

    reason: str = ""


@dataclass(frozen=True)
class AutostartResult:
    ok: bool
    enabled: bool
    reason: str = ""
```

API：

```python
def expected_command() -> str: ...
def status() -> AutostartStatus: ...
def set_enabled(enable: bool) -> AutostartResult: ...
def repair() -> AutostartResult:
    return set_enabled(True)
```

### 10.2 expected command

源码运行模式：

```text
"<absolute pythonw.exe>" "<absolute repo\main.py>"
```

如果没有 pythonw：

```text
"<absolute sys.executable>" "<absolute repo\main.py>"
```

未来 frozen exe 可在同一函数 feature-detect `sys.frozen`。

校验：

- executable exists；
- main.py exists（source mode）；
- 命令完整 quote；
- command length ≤ Windows Run key 官方 260-char 限制。

### 10.3 status/repair

`status()`：

```text
registry value missing
    → registered=False, healthy=False

value exists + canonical command == expected + paths exist
    → healthy=True

value exists但路径/解释器不匹配
    → registered=True, healthy=False, reason="stale-command"
```

Dashboard：

```text
开机启动    已开启
```

或：

```text
开机启动    需要修复
旧路径：...
[修复]
```

用户手动运行 DeskPet 后可一键 repair。

写入后必须重新读取验证，不再仅返回请求值。

## 11. Fleet 的内存/CPU：共享 Animator，而不是复制 48 MB

### 11.1 当前风险

现有 `Animator` 每实例包含：

```text
Animation pool
decoded PhotoImage frames
cache_bytes = 48 MB
after timer
```

如果直接创建 6 个 Animator：

```text
理论预算 ~ 6 × 48 MB
```

不符合 DeskPet 轻量目标。

### 11.2 拆成 SharedAnimationCache + Cursor

```python
class SharedAnimationCache:
    max_bytes: int          # 全进程 48 MB
    animations: ...
    decoded_frames: LRU

    def get_frame(self, asset_key, index) -> tk.PhotoImage: ...
    def prune(self) -> None: ...
    def stats(self) -> dict: ...


class AnimationCursor:
    view_id: str
    state: str
    frame_index: int
    speed: float
    repeat_left: int
    paused: bool
    next_due: float
```

所有 `Toplevel` 同属一个 Tk interpreter，可复用同一个 `PhotoImage` object。

### 11.3 一个 AnimationScheduler

```python
class AnimationScheduler:
    def register(cursor, on_frame): ...
    def unregister(view_id): ...
    def schedule_next(): ...
```

只保留一个最早 due `root.after()`，到期批量推进所有 due cursor。

效果：

```text
Pet 数增多
≠ Tk after timer 线性堆积
≠ GIF decode 线性重复
```

隐藏 Pet：

```text
cursor.paused=True
```

不推进帧。

### 11.4 Skin build 去重

新增 `SkinBuildManager`，可放 `skins.py`：

```python
BuildKey = tuple[str, int, int]  # skin,height,fps

class SkinBuildManager:
    # 同一 key 只有一个 build
    # 全局最多一个 converter 同时运行
    def request(self, key, callback_id) -> None: ...
    def poll_results(self) -> ...: ...
```

不同 Pet 请求相同 `amiya@300@12` 只转一次。

继续沿用 V3 已有“转换放独立 subprocess，结束后释放 numpy/scipy 内存”的方向。

## 12. Pet 位置、DPI 与多显示器

这不是额外功能膨胀，而是 Fleet 持久化必须处理的几何基础。

保留 V3 很好的：

```text
anchor = pet bottom center
```

气泡高度变化不会移动宠物 body。

每个 Pet Slot 保存自己的 placement：

```python
@dataclass
class Placement:
    monitor: str = ""
    u: float | None = None
    v: float | None = None
    anchor: tuple[int, int] | None = None
    manual: bool = False
```

拖动结束才调用 Win32：

```text
MonitorFromPoint
GetMonitorInfo(rcWork)
```

算出相对 work-area `(u,v)`；同时保留 anchor fallback。

恢复：

```text
同 monitor 存在
    → 用 u/v 恢复

monitor 不存在
    → fallback monitor
    → clamp 到可见 work area
```

DPI：

- 进程继续使用现有 per-monitor DPI awareness；
- 每个 PetView 缓存自己的 `GetDpiForWindow()`；
- Pet 建立、跨屏拖动完成、debounced Configure 时才重新检查；
- DPI 改变才请求新 skin pixel height；
- 不每帧查 DPI。

## 13. Dashboard V4.1：信息架构和视觉提前定稿

继续使用 Tk/ttk，不引入大型 GUI runtime。

默认窗口：

```text
1120 × 760
min 880 × 620
```

左侧导航约 184px：

```text
概览
Agents
桌宠与外观
监听与隐私
诊断
────────
设置
```

内容区：

- page padding 24；
- card gap 8；
- section gap 24；
- card padding 16；
- control radius 4；
- card radius 8。

`ttk.Notebook` 旧结构删除。

### 13.1 Theme token

新增 `pet/theme.py`，禁止到处硬编码颜色。

Light 初始 token：

```text
page             #F7F8F6
surface          #FFFFFF
surface_subtle   #F0F3F1
border           #DCE3DF

text             #202522
text_secondary   #66716C
accent           #4D7C6B

working          #4D7C6B
done             #487F73
waiting          #995F24
error            #A55353
unknown          #6D7772
```

状态永远同时有文字/icon，不只靠颜色。

Dashboard 字体独立于 bubble font：

```text
Segoe UI Variable Text
fallback Segoe UI
fallback Microsoft YaHei UI
```

诊断 monospace 使用 Consolas fallback。

### 13.2 Overview

Header：

```text
DeskPet V4.1                         3 Agents · 1 Waiting · UIA ✓
```

小型 summary，不做重图表：

```text
Live 3     Waiting 1     Working 2     Ambiguous 0
```

Agent Cards：

```text
┌────────────────────────────────────────────────────┐
│ Claude Code · backend                    等待审批  │
│ Bash 命令需要确认                                  │
│ WSL Ubuntu · Terminal 已确认                       │
│                                [打开终端] [详情]    │
└────────────────────────────────────────────────────┘
```

`打开终端` 永远使用该 card exact key。

### 13.3 Agents master-detail

左侧 live list：

```text
Claude · backend       Waiting
Codex · DeskPet        Coding
Kimi · tools           Testing
```

右侧普通 detail：

```text
Agent
状态 / Mode / Phase
Goal
Current activity
Environment

Terminal
Window        confirmed
Tab           confirmed/high/ambiguous
Pane          confirmed/sole/ambiguous
[打开终端]
```

高级诊断折叠：

```text
PID + process token/source
TTY/SID/PGID
WT_SESSION/WT_PROFILE_ID
session id/file
WindowIdentity
Tab RuntimeId
Pane RuntimeId
resolver score/margins
parser health
```

技术 ID 不进入主 list。

### 13.4 Terminal ambiguous repair card

```text
⚠ 无法唯一确定 Windows Terminal 位置

DeskPet 找到了多个候选。为避免打开错误 Tab 或把审批状态
归给错误 Agent，本次不会猜测。

1. 在 Windows Terminal 中切到目标 Tab/Pane
2. 回到此处点击：

[关联当前 Terminal 位置]
```

成功显示：

```text
已确认 · 本次 DeskPet 运行期有效
```

### 13.5 桌宠与外观

顶部：

```text
并发监听显示      [OFF / ON]
```

OFF：

- 展示 V3-compatible 单目标选择；
- 单宠；
- 一个 bubble；
- double-click 可 exact 激活当前 focused Agent。

ON：

```text
展示方式
[ 单宠聚合 ] [ 多宠分离 ]

最大并发数        [ 3 ]    (1..8)

参与并发
☑ Codex  ☑ Claude  ☑ Kimi  ☑ pi
```

强调说明：

> 这里控制“并发展示”；Agent 是否被发现由“监听与隐私”页控制。

Aggregate preview 显示真实 stack card 预览。

Fleet：

```text
Pet 1   Amiya · 100%   Codex · DeskPet       已绑定
[编辑] [更换 Agent] [解除]

Pet 2   skin-b · 90%   Claude · backend       已绑定
[编辑] [更换 Agent] [解除]

Pet 3   Amiya · 100%   等待绑定
[绑定]
```

每 slot 编辑：

```text
使用全局外观    on/off

skin
scale
speed
animation
bubble enabled

记住此项目      on/off
位置            DISPLAY / anchor
[恢复全局默认]
```

### 13.6 监听与隐私

Discovery：

```text
Agent discovery
Codex      on
Claude     on
Kimi       on
pi         on

Environment
Windows    on
WSL        on

Terminal UIA observation on
```

Privacy：

```text
WSL root metadata fallback off
```

保留现有说明，只读 cwd/token/uid/HOME/allowlisted env。

扫描间隔放 Advanced expander，不占普通 UI。

### 13.7 Diagnostics

健康摘要：

```text
Process
Windows        OK
WSL Ubuntu     OK

Session
Codex          OK
Claude         PARTIAL

Terminal
UIA             OK
Windows         2
Tabs            6
Panes           3 / 16

Bindings
Confirmed       2
High            1
Ambiguous       0
```

性能：

```text
monitor tick ms
windows census ms
wsl census count/ms
metadata enrich count
uia calls/errors/timeouts
uia event queue high water
visible reads/s
animation cache MB / budget
PetView count
skin build pending
```

日志仍不包含 terminal 原文。

### 13.8 Settings

```text
开机启动
托盘
窗口置顶
配置保存状态
配置文件位置（只读展示）
```

Autostart unhealthy 时：

```text
开机启动：需要修复
注册路径与当前 DeskPet 路径不一致
[修复]
```

Config commit 失败：

```text
⚠ 最近一次设置没有写入磁盘
<简短 OSError>
[重试保存]
```

## 14. Tray 和右键菜单

全应用只有一个 tray icon。

Tooltip：

```text
DeskPet · 3 Agents · 1 waiting
```

Tray menu：

```text
显示 / 隐藏桌宠

Agents >
  Claude · backend · 等待审批
  Codex · DeskPet · 工作中
  Kimi · tools · 测试中

仪表盘
重新扫描
退出
```

每个 Agent command 捕获 exact key：

```python
lambda k=key: app.activate_agent(k)
```

Fleet Pet menu：

```text
Codex · DeskPet
打开此 Agent 终端
查看详情
更换 Agent
解除绑定
────────
隐藏此桌宠
桌宠设置
仪表盘
```

Aggregate Pet menu：

```text
并发聚合 · 3 Agents
关注 Agent >
  Claude
  Codex
  Kimi
展示方式 >
  单宠聚合
  多宠分离
仪表盘
```

Aggregate body 本身仍不激活 Terminal。

## 15. `pet/app.py` 的重构边界

`PetApp` 从“一个宠物窗口本身”变成 controller：

```python
class PetApp:
    config
    root

    monitor
    terminal activation facade
    presentation

    pet_manager
    shared_animation_cache
    animation_scheduler
    skin_build_manager

    dashboard
    tray
```

删除/迁移：

```text
self.win                → PetViewManager
self.animator           → Shared cache/scheduler + cursor
self.bubble             → PetView
self._display_key       → 删除
self._raise_current_terminal → activate_agent(key)
self._aggregate         → PresentationController + PetView state
self._special_until     → per view/presentation state
```

保留：

- app lifecycle；
- tray event polling；
- dashboard singleton；
- janitor，但改为对共享 cache/build manager 运行；
- UI tick，但只 reconcile/signature diff，不做外部扫描。

## 16. 需要彻底清理的 V3 架构代码

完成 V4.1 后不要留下“新旧两套都能跑”的兼容垃圾。

从 `agents/monitor.py` 删除：

```text
_FOLLOW_PRIORITY
primary_key
primary_target()
primary_snapshot()
set_primary()
reset_primary()
is_bound()
_select_primary()
_gone_since
gone_grace logic
直接持有独立 resolver/observer（由 TerminalService 收口后）
```

从 config 删除：

```text
monitor.pinned
monitor.gone_grace_sec
```

从 `pet/app.py` 删除：

```text
_display_key
primary_target-based bubble
primary_target-based double click
“监听目标”旧 radio menu
UI direct winkeys activation
```

从 Dashboard 删除：

```text
自动跟随（Monitor API）
设为当前 Agent
当前 ★ 列
详情独立 Notebook tab
直接 import winkeys 的打开终端路径
旧“高级：关联当前 Pane”
```

测试删除/重写：

```text
Monitor pinned/auto-follow tests
primary_key UI tests
gone_grace tombstone 延迟测试
```

替换为：

```text
Presentation focus/attention tests
authoritative exit tests
exact activation tests
```

保留并强化：

```text
StateReducer
matching mutual uniqueness
WSL passive lifecycle
Session resolver
Terminal recognizers
bounded queues/buffers
```

## 17. 实施顺序：必须按依赖关系落地

### Phase A — lifecycle correctness

先完成：

1. `SourceProbeSnapshot`；
2. Windows global scan failure health 修复；
3. WSL state + authoritative cleanup；
4. 删除 gone grace；
5. BaseWatcher zero-kind cleanup；
6. `_commit_exit()`；
7. WindowsExitWatcher。

此阶段 UI 仍可暂时 V3 single target，但 Agent exit 必须已经正确。

### Phase B — Terminal topology

1. WindowIdentity；
2. TabInfo/TerminalLayout；
3. TabItem discovery；
4. selection event；
5. focused_location；
6. manual bind Window+Tab+Pane；
7. TerminalService；
8. `ActivationResult`；
9. exact activation；
10. terminal compatibility probe。

此阶段把 Dashboard/Pet 激活切到 `Monitor.activate_target(key)`，完全删除 UI direct winkeys。

### Phase C — Presentation split

1. PresentationController；
2. 删除 Monitor primary/pinned；
3. Single compatibility mode；
4. focused/attention；
5. Aggregate stack cards；
6. aggregate body no activation；
7. max/kind/runtime include semantics。

### Phase D — Fleet + shared graphics

1. hidden Tk controller root；
2. PetWindow → Toplevel；
3. PetView/PetViewManager；
4. SharedAnimationCache；
5. AnimationScheduler；
6. SkinBuildManager；
7. Fleet slot bind/unbind；
8. per-slot appearance/placement。

### Phase E — Config/autostart

可以与 C/D 并行开发，但合并时要求：

1. config_version 4 migration；
2. commit result + backup；
3. centralized setting writer；
4. V3 persistence omissions修复；
5. slots；
6. autostart status/repair；
7. `.gitignore` backup patterns。

### Phase F — Dashboard

等 API 稳定后再重做 UI，避免 UI 绑定旧接口：

1. theme/widgets；
2. nav shell；
3. Overview；
4. Agents master-detail；
5. Deskpet & Appearance；
6. Monitoring & Privacy；
7. Diagnostics；
8. Settings；
9. tray/menu consistency。

### Phase G — 清理

全局 grep 以下符号必须为零（文档迁移说明除外）：

```text
primary_key
primary_target
monitor.pinned
gone_grace_sec
_raise_current_terminal
bind_focused_pane
winkeys.raise_terminal(   # UI 层
```

## 18. 测试矩阵

### 18.1 Process lifecycle

必须有：

1. Windows healthy scan exact key消失 → 本轮 commit exit；
2. Windows global `process_iter` fail → authoritative false，旧 Agent retained + stale；
3. WindowsExitWatcher signal → exact key exit；
4. exit event arrived after key replaced → 不删除新 incarnation；
5. PID reuse + create_time change → new key，无旧 session/terminal binding；
6. WSL healthy+empty → exit；
7. WSL unhealthy → retain；
8. WSL Z/X process → 不作为 live Agent；
9. 最后一个 Codex exit → `CodexWatcher.poll([])` 清理；
10. terminal shell继续产生 TextChanged → 不会复活已退出 Agent。

### 18.2 Terminal topology

FakeBackend 扩展成 Window/Tab/Pane：

1. 两 Window，各多个 Tab；
2. 同 HWND 两个相同 title Tab；
3. tab reorder，RuntimeId不变 → exact；
4. target Tab close → STALE_TAB；
5. Tab tear-out → old binding失效；
6. pane runtime稳定 → exact pane；
7. pane RuntimeId变化但 sole pane → safe rebind；
8. pane RuntimeId变化且 multi-pane → AMBIGUOUS；
9. HWND reuse/PID mismatch → STALE_WINDOW；
10. HWND+PID reuse但 process create_time不同 → STALE_WINDOW；
11. SelectionItemPattern unavailable → UIA_UNAVAILABLE/AMBIGUOUS，不 keyboard fallback；
12. foreground denied → tab已选，flash，`FOREGROUND_DENIED`；
13. manual focused location → CONFIRMED；
14. Agent exit和 activate竞态 → AGENT_GONE。

### 18.3 Presentation

1. concurrency disabled → single compatible view；
2. enable aggregate → one PetView + N cards；
3. aggregate body double-click → 不调用 activate；
4. card A click → exact key A；
5. WAITING B出现 → attention B，focused A不被偷偷改；
6. WAITING抢占 special；
7. max_targets=3，5 Agent → 3 cards + overflow 2；
8. exact runtime exclude → card消失但 Monitor target还在；
9. Agent exit → card立即移除；
10. concurrent mode switch不重建 Monitor/UIA；
11. eligible kind变化只影响 presentation；
12. deterministic ordering 不依赖 dict iteration。

### 18.4 Fleet

1. N slots → N Toplevel，仍 1 Monitor；
2. vacant pet doubleclick → picker；
3. bind exact A → slot exact key A；
4. duplicate bind A → 拒绝；
5. bound Pet A doubleclick → exact activate A；
6. A exit → slot vacant，slot skin/position保留；
7. restart + unique selector → runtime rebind；
8. selector ambiguous → 不猜；
9. 每 slot不同 skin/scale；
10. 拖动只写自己的 placement；
11. second monitor缺失 → clamp可见；
12. hide all pets → cursor pause，Monitor继续。

### 18.5 Config/autostart

1. V3 config migration保存外观；
2. V4 migration concurrent=false；
3. pinned/gone_grace 被清；
4. invalid scale/max targets clamp；
5. atomic commit；
6. write failure → `ok=False`、dirty保留；
7. primary config损坏 → backup；
8. `PetWindow/topmost` 修改真正 commit；
9. autostart missing/stale/healthy；
10. stale command repair；
11. >260 char command → 显式失败；
12. config/backup/cache/skin 均 Git ignored。

## 19. 性能基准与硬预算

现有 `tests/benchmark_monitor.py` 的这些 V3 约束继续为 hard regression：

```text
EVENT_QUEUE_MAX        256
UIA_CALL_QUEUE_MAX      32
MAX_PANES                16
RING_MAX               8192 chars / pane
VISIBLE_MAX            4096 chars
GLOBAL_VISIBLE_READ     ≤6/s fallback
```

新增 V4.1 性能不变量：

```text
Pet count 1 → 8
    不增加 ProcessProbeWorker 数
    不增加 Monitor thread 数
    不增加 UIA MTA 数
    不增加 WSL census 频率
    不增加 UIA visible-read budget
    不让 animation decoded cache 超过全局 animation_cache_mb

Agent count 1 → 8
    WSL 每 distro 每 census 仍约 1×ps
    metadata 只批量/按变化 enrich
    不出现 per-Agent wsl.exe

Idle
    Terminal 仍 event-driven
    WindowsExitWatcher 阻塞等待
    Dashboard hidden 时不刷新 widgets
    hidden Pet 不推进动画
```

建议 `benchmark_presentation.py`：

```text
8 Agent
8 Fleet Pet
持续 30min synthetic ticks

assert:
  PetView=8
  Monitor=1
  UiaBackend=1
  ProcessProbe=1
  decoded_cache<=configured bytes
  scheduler after count近似固定
  no growth in view bindings after churn
```

UI 更新：

- Monitor/Presentation reconcile 400–500ms 可以保持；
- 只有 model signature 改变才重建 Bubble/Card Canvas item；
- 动画帧按素材 FPS 单独推进；
- Dashboard hidden 时维持 1000ms 或完全停 render refresh，重新 deiconify 再即时 refresh；
- 不把外部 `ps/UIA tree` 扫描绑在 UI tick。

## 20. Terminal 实机 Compatibility Probe（产品合并前必须跑）

新增：

```text
tools/terminal_layout_probe.py
```

只能输出 topology，不输出 terminal text：

```text
HWND
window PID/create time/class
Tab index
Tab RuntimeId
Tab selected
Tab name
TermControl RuntimeId
focused
```

测试环境：

```text
2 Windows Terminal Windows
每个 3 Tabs
重复 title
重复 cwd
至少 1 Tab 有 split panes
```

操作：

1. 切 Tab；
2. Tab reorder；
3. rename；
4. split/close pane；
5. minimize/restore；
6. tear-out Tab；
7. inactive → active → inactive → active。

核对：

- Tab RuntimeId reorder 稳定性；
- TermControl RuntimeId detach/reattach 行为；
- selected Tab UIA structure；
- SelectionItemPattern availability；
- SetFocus 对 split pane 行为。

若某版本行为不同，只调整 runtime feature-detection/fallback，不允许引入 keyboard simulation。

## 21. V3 → V4.1 migration

`config_version < 4`：

保留：

```text
skin
scale
speed
animated
topmost
tray_enabled
pet_pos
bubble.*
monitor.agents
windows/wsl enabled
scan intervals
activity_grace
terminal_observer
privacy
animation_cache_mb
force_state
convert
```

删除/不载入：

```text
monitor.pinned
monitor.gone_grace_sec
任何旧 runtime terminal/session binding
```

增加：

```text
presentation.concurrent.enabled=false
mode=aggregate
max_targets=3
eligible kinds = 当前 monitor agents enable 值
slots 默认 pet-1
```

第一次运行不自动打开并发，不改变用户 V3 的视觉习惯。

## 22. 错误与降级合同

| 故障 | 正确行为 |
|---|---|
| Windows process enumeration fail | 保留 target，stale；不判死 |
| WSL inventory/ps fail | 保留该 source target，stale |
| Session不可读 | live process保留，UNKNOWN/已有安全证据 |
| UIA不可用 | Session状态继续；Terminal waiting功能降级 |
| Tab mapping ambiguous | 不归属 terminal approval，不 exact activate |
| Pane mapping stale+multi-pane | STALE_PANE，不选第一个 |
| SetForegroundWindow拒绝 | Flash；不绕 OS policy |
| Config写失败 | UI明确显示未保存 |
| Autostart stale | 显示需要修复 |
| Fleet selector ambiguous | slot保持vacant |
| Agent exit | exact target立即失效，不因 terminal shell存在保留 |

## 23. 安全/隐私回归清单

代码审查必须确认 V4.1 没有重新引入：

```text
SendInput
keybd_event
PostMessage keyboard
clipboard write/paste
OCR/screenshot
ReadProcessMemory
process injection
Agent hooks
Agent settings mutation
terminal raw text disk logging
runtime IDs in config
PID/HWND in durable Pet slots
```

唯一新增 Windows process handle只申请同步等待所需权限；不读 Agent 内存。

## 24. Definition of Done

V4.1 只有同时满足以下条件才算完成：

- 一个 Windows Terminal 内多个相同 cwd/same-title Codex，经学习/一次确认后，点击对应 Bubble/Pet 能恢复 exact Tab；
- 不同 Windows、不同 Tab、split Pane 都遵守 Window→Tab→Pane exact/fail-closed；
- cold-start 完全同质且未学习的 Tab 显示 ambiguous，不猜；
- Agent CLI 退出后 Terminal 仍开着，AgentTarget 正确消失；
- Windows scan失败不会误判全部退出；
- Windows exit通常在下一 Monitor tick 内被发现，WSL在下一 authoritative census 内；
- 并发必须由用户手动开启；
- Aggregate body不激活 Terminal，卡片精确激活；
- Fleet 每 Pet 可以绑定不同 Agent、不同 skin，且共享一套 Monitor/UIA/cache；
- max concurrency 与 eligible kinds 可配置；
- settings 重启后可靠恢复；
- autostart 能识别 stale registration 并修复；
- config/user skins/cache继续留项目目录且不会被 Git 提交；
- V3 passive observer 的安全边界没有回退；
- 1→8 Pet 不让 Monitor/UIA/WSL polling/animation cache线性复制；
- 旧 `primary/pinned/gone_grace/direct UI->winkeys` 架构代码清理干净；
- 单元测试、UI smoke、terminal probe、benchmark 全通过。

## 25. 本计划的最终架构判断

V4.1 最重要的不是“增加多宠物”，而是建立四个不能互相越权的层：

```text
Lifecycle
    决定 Agent 是否真实存在

Terminal topology
    决定这个 live Agent 的精确 Window/Tab/Pane

Presentation
    决定哪些 live Agent 现在由哪些 Bubble/Pet 展示

Durable config
    只保存用户长期偏好，不保存 runtime truth
```

这使 V4.1 在并发扩大后仍保持 V3 的核心特性：**旁路、轻量、证据驱动、失败时不猜**。未来无论继续支持更多 CLI、Zcode、桌面 Agent，还是以后有官方 Windows Terminal session query API，都可以替换某一层 backend，而不需要再次推翻 DeskPet 的 UI 和生命周期模型。
