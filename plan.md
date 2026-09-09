# DeskPet v4.1.1 Core Convergence 修复计划

> 审计与修复基线：`invincible-summer/DeskPet`  
> 当前审计提交：`1feaf4f7d203725547d1e7519d8bc2a6f95a11c0`（v4.1）  
> v3 参考提交：`af8236d15dc3bfecaa89464e1b77d7f84c2b09be`  
> 本计划目标：在保留 v4.1 已经完成的多 Agent、低资源监听、进程生命周期、安全 UIA 观察、Presentation/Fleet 等改进的前提下，彻底撤销失败的 exact Window → Tab → Pane 激活设计及其手工绑定链；终端唤起恢复为 v3 的 Window-level 语义，并用 v4.1 更强的 `WindowIdentity` 做安全校验。

---

## 1. 修复目标与最终产品语义

本轮不做功能扩展，不重构与问题无关的业务，不重新引入 hooks，不加入键盘注入，不通过 Terminal 快捷键、`SendInput`、`PostMessage`、剪贴板或模拟 Ctrl+Tab 解决精确标签页定位。

修复完成后，DeskPet 的终端相关能力必须被拆成两条互不污染的链：

1. **Terminal Window Navigation**
   - 只负责“这个 Agent 对应哪个 Windows Terminal 顶层窗口”。
   - 用户点击桌宠、气泡、Dashboard Agent 卡片、托盘 Agent 项时，只尝试恢复并前置这个窗口。
   - 不依赖 UIA 是否可用。
   - 不依赖 Tab、Pane、SelectionItemPattern、UIA focus。
   - 不能唯一确定窗口时 fail-closed，不猜。

2. **Terminal Observation**
   - 只负责被动监听 Windows Terminal 当前可观察 `TermControl` 的 Notification / TextChanged / Visible Text。
   - 内部可以使用 UIA RuntimeId 作为短生命周期的观察句柄。
   - 这个内部 UIA control identity 不属于用户可见“绑定”，不参与激活，不持久化。
   - 只有归属置信度足够高时，才允许把 WAITING/approval/activity 证据归到具体 Agent。
   - 观察无法安全归属时，宁可没有 terminal evidence，也不能错归。

最终用户语义必须是：

> “打开终端” = 打开/前置该 Agent 所在的 Windows Terminal 窗口。  
> DeskPet 不保证、不尝试切换到某个既有 Tab/Pane。

这与 Windows Terminal 目前没有稳定公开的“按 `WT_SESSION` 激活既有标签页”接口这一现实一致。Windows Terminal 项目在 2026-01-25 的相关 feature request 中也明确讨论了外部进程无法按 session identity 稳定切换既有 Tab 的缺口，因此不再把 UIA Tab selection 当成 DeskPet 的产品级承诺。

---

## 2. 必须保持的核心不变量

### 2.1 安全不变量

任何用户显式终端唤起都必须遵守：

```text
agent_key
  → Agent 仍存活
  → TerminalWindowBinding 存在
  → WindowIdentity 验证
  → restore window
  → SetForegroundWindow
  → 验证前台窗口
  → 若 OS 拒绝：FlashWindowEx
```

绝不允许：

```text
SendInput
keybd_event
PostMessage 模拟快捷键
Ctrl+Tab / Alt+数字
剪贴板注入
根据 tab title 模糊选择后强制 Select
根据 pane index 猜测
```

### 2.2 资源不变量

DeskPet 仍然以“常驻、轻量、低 CPU、低内存”为目标：

- 一个 Tk interpreter。
- 一个 Monitor core thread。
- 一个 process probe thread。
- 一个 UIA MTA thread（启用 terminal observer 时）。
- 一个 Windows process exit wait thread。
- 不允许 per-Agent polling thread。
- 不允许 per-Pet monitor thread。
- UIA event queue、visible read、ring buffer 继续有硬上限。
- Windows/WSL source 被关闭时，对应扫描必须真正停止。
- 无 Windows Agent 时 exit watcher 必须真正阻塞，不做 200 ms 周期唤醒。

### 2.3 并发 UI 不变量

所有“表示一个具体 Agent”的交互元素必须携带 **exact runtime `agent_key`**：

```text
Fleet PetView
Fleet BubbleModel
Dashboard Agent Card
Tray Agent Menu Item
Agent detail "打开终端"
未来任何 per-Agent bubble/card
```

禁止这些入口通过以下隐式状态推导目标：

```text
primary_key
attention_key
当前最新 Agent
当前 WAITING Agent
当前选中的 Terminal Tab
focused terminal element
当前唯一 Pane
```

`attention_key` 只用于视觉优先级，不是激活 identity。

---

## 3. 当前 v4.1 审计结论

### 3.1 应保留的 v4.1 能力

以下能力不是终端唤起问题的根源，应继续保留：

- `SourceProbeSnapshot` 的 authoritative / unavailable 三态。
- Windows native Agent 的 process incarnation。
- WSL process token / source-isolated discovery。
- `WindowsExitWatcher` 单线程等待模型。
- Watcher session evidence。
- `StateReducer` 对 structured session evidence 与 generic terminal activity 的优先级区分。
- Terminal observer 的 MTA COM 架构。
- Notification + TextChanged + StructureChanged 事件驱动观察。
- visible text 限制、TTL、ring / queue / read budget。
- `WindowIdentity(hwnd, pid, process_created, window_class)`。
- PresentationController 的 focused / attention 分离。
- Fleet slot → Agent 的 runtime binding。
- SharedAnimationCache / single AnimationScheduler。
- 配置原子保存与 runtime identity 不落盘。

### 3.2 必须清除的 v4.1 设计

整条 exact terminal location 产品链退出：

```text
TabInfo
TerminalLocation
BindingOrigin.MANUAL
manual_location
set_manual_location
focused_location
bind_focused_location
TerminalActivator exact transaction
select_tab
selected_tab
focus_pane
STALE_TAB
STALE_PANE
"关联当前 Terminal 位置"
Tab RuntimeId 用户诊断
Pane RuntimeId 用户诊断
exact Window → Tab → Pane 产品文案
```

注意：

`TermControl` 仍然是 UIA 读取 Terminal 文本的底层对象，因此不能简单删除所有“pane-like”内部概念。应将其从“Agent terminal binding”中拆出，作为 observation-only 私有对象。

建议重命名：

```python
PaneInfo
# →
ObservedTerminalControl
```

若为了降低改动风险，本轮也可以暂时保留 `PaneInfo` 类名，但必须在注释和接口上明确：

> 这是 UIA observer 的内部 control descriptor，不是用户可绑定 Terminal Pane，也不参与 window activation。

推荐本轮直接改名，避免下一版本再次混淆。

---

## 4. 目标数据模型

## 4.1 `WindowIdentity`

保留 v4.1：

```python
@dataclass(frozen=True)
class WindowIdentity:
    hwnd: int
    pid: int
    process_created: float
    window_class: str
```

验证合同：

```python
def validate_window(identity: WindowIdentity) -> bool:
    """
    True iff:
      IsWindow(hwnd)
      GetWindowThreadProcessId(hwnd) == pid
      current process create_time ~= process_created
      GetClassNameW(hwnd) == window_class
    """
```

`process_created` 用于防 PID incarnation 复用；`window_class` 用于防 HWND 指向其他应用窗口。

## 4.2 新的 `TerminalWindowBinding`

将当前公共 `TerminalBinding` 收敛成 window-only：

```python
@dataclass
class TerminalWindowBinding:
    provider: str = "windows-terminal"

    window: WindowIdentity | None = None
    title: str = ""

    # 只表达“窗口解析”的可靠程度
    confidence: WindowBindingConfidence = WindowBindingConfidence.NONE

    last_seen: float = 0.0
    validated_at: float = 0.0

    # 安全诊断，禁止放 terminal text
    reason: str = ""
    score: int = 0
    runner_up_score: int = 0

    @property
    def hwnd(self) -> int:
        return self.window.hwnd if self.window else 0
```

推荐新增专用 enum，避免再拿 terminal observation attribution 的 confidence 混用：

```python
class WindowBindingConfidence(str, Enum):
    CONFIRMED = "confirmed"
    HIGH = "high"
    FALLBACK = "fallback"
    AMBIGUOUS = "ambiguous"
    NONE = "none"
```

其中：

- `CONFIRMED`：Windows native PID ancestor 唯一定位。
- `HIGH`：WSL / title / cwd / distro 等证据唯一定位到一个 control，再映射到一个 window。
- `FALLBACK`：只有一个 Windows Terminal 顶层窗口，可以唤起窗口，但不代表 terminal text 可归属。
- `AMBIGUOUS`：有多个候选窗口，无法安全唯一定位。
- `NONE`：没有可用窗口。

`FALLBACK` **允许用户唤起**，但不赋予 observation attribution 权限。

## 4.3 UIA 私有观察对象

```python
@dataclass(frozen=True)
class ObservedTerminalControl:
    control_id: tuple
    hwnd: int
    window_pid: int
    title: str
    window_class: str = WT_WINDOW_CLASS
```

生命周期：

- 仅内存。
- 仅 UIA thread / observer 使用。
- UIA RuntimeId 失效后删除。
- Agent exit 不需要向它写回任何 user binding。
- 不持久化到 config。
- Dashboard 普通诊断不展示 control id。

## 4.4 观察归属结果

推荐独立：

```python
@dataclass(frozen=True)
class TerminalObservationBinding:
    agent_key: str
    control_id: tuple
    confidence: ObservationBindingConfidence
    reason: str = ""
```

只允许：

```python
CONFIRMED
HIGH
```

参与：

```text
waiting_observation
activity_observation
```

`AMBIGUOUS/NONE` 不产生 Agent-specific terminal evidence。

这样彻底消除当前一个 `TerminalBinding` 同时承担“能不能打开窗口”和“能不能安全归属审批”的语义冲突。

---

## 5. Window Resolver 详细设计

建议从当前 `TerminalResolver` 中拆出：

```python
class TerminalWindowResolver:
    def resolve(
        self,
        instances: list[AgentInstance],
        controls: Mapping[tuple, ObservedTerminalControl],
        windows: Mapping[int, WindowIdentity],
        now: float,
    ) -> dict[str, TerminalWindowBinding]:
        ...
```

不再有：

```python
set_manual_location
manual_location
_prune_manual
tab_id
pane_id
```

### 5.1 Windows native Agent

规则：

```text
Agent PID
→ ancestor PID set
→ Windows Terminal top-level windows whose owner PID ∈ ancestor set
```

结果：

```text
唯一窗口
  → CONFIRMED

多个窗口
  → AMBIGUOUS

没有窗口
  → 继续进入 observation/title fallback
```

不要求窗口下“只有一个 pane”。

旧逻辑里“窗口唯一但多个 pane = AMBIGUOUS”是 exact-pane 产品语义留下的限制；Window-only 激活后应删除。

### 5.2 WSL Agent

WSL 无法通过 Linux PID 直接证明 Windows HWND，继续使用只读、保守评分：

候选 evidence：

- Agent kind token。
- normalized cwd / `~/path`。
- user。
- distro。
- Terminal control title。
- 必要时顶层 Terminal window title。

只要某个 `ObservedTerminalControl` 能通过 mutually-unique / min-score / margin 规则高置信匹配到 Agent：

```text
Agent
→ control
→ control.hwnd
→ WindowIdentity
→ HIGH TerminalWindowBinding
```

这里 control identity 只是“帮助找到 hwnd”的一次性证据，不写进公开 window binding。

### 5.3 唯一 Terminal window fallback

如果：

```text
无法对 Agent 做高置信匹配
AND
当前桌面只有一个 Windows Terminal 顶层窗口
```

则：

```python
TerminalWindowBinding(
    window=only_window,
    confidence=FALLBACK,
    reason="single-window-fallback",
)
```

允许：

```text
打开终端
```

不允许：

```text
把这个窗口里的 WAITING 自动归给该 Agent
```

### 5.4 多窗口无法区分

```text
多个 Windows Terminal window
+
没有可靠 evidence
```

返回：

```python
confidence = AMBIGUOUS
window = None
```

`activate_target()` 返回 `NO_BINDING` 或专用 `AMBIGUOUS_WINDOW`。

为了简化用户语义，推荐不再保留 `AMBIGUOUS` activation result，统一：

```text
NO_BINDING
detail="multiple terminal windows"
```

普通 UI 只提示：

> 无法唯一确定该 Agent 所在的终端窗口。

高级诊断保留 reason。

---

## 6. Window-only 激活接口

## 6.1 `actions/winkeys.py`

保留 v4.1 原语：

```python
window_identity(hwnd)
validate_window(identity)
restore_window(hwnd)
try_set_foreground(hwnd)
flash_window(hwnd)
enum_windows()
```

增加一个薄的 window-level helper 也可以：

```python
def activate_window(identity: WindowIdentity) -> WindowActivationResult:
    if not validate_window(identity):
        return STALE
    if not restore_window(identity.hwnd):
        return STALE
    if try_set_foreground(identity.hwnd):
        return OK
    flash_window(identity.hwnd)
    return FOREGROUND_DENIED
```

不建议把 resolver 逻辑塞回 `winkeys.py`。

## 6.2 ActivationCode

收敛为：

```python
class ActivationCode(str, Enum):
    OK = "ok"
    AGENT_GONE = "agent_gone"
    NO_BINDING = "no_binding"
    STALE_WINDOW = "stale_window"
    FOREGROUND_DENIED = "foreground_denied"
```

删除：

```text
STALE_TAB
STALE_PANE
UIA_UNAVAILABLE
```

原因：

Terminal window activation 不依赖 UIA。

## 6.3 `WindowsTerminalService.activate`

目标签名：

```python
def activate(
    self,
    agent_key: str,
    *,
    is_agent_live: Callable[[str], bool],
) -> ActivationResult:
    ...
```

推荐直接传 `agent_key`，不要传一个可能已经 stale 的 `AgentTarget`。

事务：

```python
def activate(agent_key, is_agent_live):
    if not is_agent_live(agent_key):
        return AGENT_GONE

    binding = current_window_binding(agent_key)
    if not binding or not binding.window:
        return NO_BINDING

    if not validate_window(binding.window):
        refresh_windows_and_resolve_once()

        if not is_agent_live(agent_key):
            return AGENT_GONE

        binding = current_window_binding(agent_key)
        if not binding or not binding.window:
            return NO_BINDING(repaired=True)

        if not validate_window(binding.window):
            return STALE_WINDOW(repaired=True)

    restore_window(binding.hwnd)

    if try_set_foreground(binding.hwnd):
        return OK

    flash_window(binding.hwnd)
    return FOREGROUND_DENIED
```

限制：

- refresh 最多一次。
- 不调用 `observer.backend.select_tab()`。
- 不调用 `selected_tab()`。
- 不调用 `focus_pane()`。
- UIA observer 为 `None` 时仍可正常激活已有有效 window binding。
- 若 window binding 的生成依赖 UIA title evidence，而当前 UIA 暂不可用，允许使用仍通过 `WindowIdentity` 校验的最近一次 runtime binding；一旦 identity stale，不能继续猜。

---

## 7. Terminal Observation 详细设计

### 7.1 保留 UIA MTA

真实 UIA backend 继续：

```text
one MTA thread
no owned windows
all UIA calls marshalled to that thread
event handler add/remove on same MTA thread
bounded call queue
```

不要为了删除 Tab selection 而退回 Tk UI thread 调 UIA。

### 7.2 删除 Tab topology

从 `terminal_uia.py` 删除：

```text
TabInfo
TerminalLocation
TerminalLayout.tabs
TerminalLayout.selected_tabs
_find_tab_items
select_tab
selected_tab
focused_location
focus_pane
SelectionItemPattern
tab-selected event
_tab_dirty
tab_events stats
```

目标 observer 只关心：

```text
Windows Terminal top-level windows
TermControl descendants
Notification events
TextChanged events
StructureChanged events
visible text
```

`StructureChanged` 仍用于 control 开/关、split 改变、active control attach/detach 后的重新发现。

### 7.3 保留有界读取

硬限制继续保留或收紧：

```text
DELTA_MAX                    2048 chars
RING_MAX                     8192 chars / control
VISIBLE_MAX                  4096 chars
MAX_CONTROLS                 16
EVENT_QUEUE_MAX              256
UIA_CALL_QUEUE_MAX           32
GLOBAL_VISIBLE_READ_LIMIT    <= 6 / sec
PER_CONTROL_VISIBLE_READ     >= 0.5 sec interval
approval TTL                 ~1.5 sec
waiting recheck              ~0.75 sec
```

只读 `TextPattern.GetVisibleRanges()`，不读取完整 scrollback。

### 7.4 WAITING 归属

`Monitor._terminal_observation()` 改为：

```python
obs_binding = self._terminal_service.observation_binding(inst.key)

if obs_binding is None:
    return None

if obs_binding.confidence not in (CONFIRMED, HIGH):
    return None

waiting = service.waiting_observation(obs_binding.control_id)

if waiting and waiting.agent_kind in (None, inst.kind):
    return waiting

return service.activity_observation(
    obs_binding.control_id,
    now,
    grace,
)
```

不能再通过公共 `TerminalWindowBinding` 中的 `pane_id` 取得 observation。

---

## 8. 并发模式与 per-Agent 终端唤起

这是本轮必须单独验收的功能。

当前 v4.1 已经具有正确的基础链：

```text
PresentationState.slot_keys[slot_id] = exact_agent_key
        ↓
PetViewManager.sync()
        ↓
view.set_agent(exact_agent_key)
        ↓
view.set_single_model(target)
        ↓
BubbleModel.agent_key = target.key
```

必须保留并强化这条链。

### 8.1 Fleet 模式

每个 slot：

```text
pet-1 → Agent A
pet-2 → Agent B
pet-3 → Agent C
```

必须得到：

```text
PetView("pet-1").agent_key == A.key
PetView("pet-2").agent_key == B.key
PetView("pet-3").agent_key == C.key

pet-1.bubble.model.agent_key == A.key
pet-2.bubble.model.agent_key == B.key
pet-3.bubble.model.agent_key == C.key
```

用户动作：

```text
双击 pet-1 body
→ PetView._on_double()
→ on_activate(A.key)
→ PetApp.activate_agent(A.key)
→ Monitor.activate_target(A.key)
→ TerminalWindowService.activate(A.key)
→ A 对应 WindowIdentity
```

`pet-2`、`pet-3` 同理。

任何一个 Pet 都不能通过 `PresentationController.focused_key` 替换自己的 key。

### 8.2 Fleet 气泡

气泡底行 hit target：

```python
HitTarget(
    action="activate_agent",
    agent_key=self.model.agent_key,
)
```

因此：

```text
点击 pet-2 气泡
→ exact B.key
→ B 的 Terminal window
```

必须测试气泡的 `model.agent_key` 与 `view.agent_key` 一致。

建议在 `PetView.set_single_model()` 增加 defensive invariant：

```python
if target is None:
    ...
else:
    assert not self.agent_key or self.agent_key == target.key
    m.agent_key = target.key
```

生产代码可以不用 Python `assert`，也可以检测不一致后清空 hit target，并记录安全诊断：

```python
if self.agent_key and self.agent_key != target.key:
    m.agent_key = ""
    ...
```

推荐测试保证永不发生即可，不在生产路径增加额外分支。

### 8.3 Aggregate 模式

当前 Aggregate 采用一个 Pet + 一个与单 Agent 一致的单卡气泡：

```text
focused Agent
如果无 focused → attention Agent
```

这里不重新引入旧的多卡叠加 bubble。

气泡显示谁：

```text
BubbleModel.agent_key 必须就是被显示 Agent 的 exact key
```

点击这个气泡：

```text
只激活 bubble.model.agent_key
```

不能在点击时重新计算 attention key。

原因：

从绘制到点击之间 Agent 状态可能变化；如果 click handler 临时重新取“当前最需要关注 Agent”，用户看到的是 Agent A，点击却可能跳到 Agent B。

所以：

```text
visual identity == click identity
```

必须由 `BubbleModel.agent_key` 固化。

### 8.4 Dashboard / Tray

Dashboard Agent card：

```text
button closure captures key
→ app.activate_agent(key)
```

Tray submenu：

```text
lambda k=key: app.activate_agent(k)
```

这些也不能走 `focused_key` 推导。

### 8.5 是否修改 focused_key

终端激活动作与 UI focus 是两个概念。

推荐接口：

```python
def activate_agent(self, key: str) -> None:
    # 只激活对应 Terminal window
    ...

def focus_and_activate_agent(self, key: str) -> None:
    self.presentation.set_focus(key)
    self.activate_agent(key)
```

使用规则：

- Fleet pet body/bubble：`activate_agent(key)`，不必改变 global focused key。
- Dashboard “查看并设为当前”类操作：可用 `focus_and_activate_agent(key)`。
- Dashboard “打开终端”：仅 `activate_agent(key)`。
- Tray Agent：仅 `activate_agent(key)`。

这样一个 Fleet Pet 被点击时不会偷偷改变 aggregate/focused presentation 状态。

---

## 9. `Monitor` 修复

### 9.1 删除 manual binding API

删除：

```python
Monitor.bind_focused_location()
```

删除对应日志：

```text
终端位置手动关联
```

### 9.2 新 activation 入口

```python
def activate_target(self, key: str) -> ActivationResult:
    if not key:
        return ActivationResult(NO_BINDING)

    if not self.is_live_key(key):
        return ActivationResult(AGENT_GONE)

    return self._terminal_service.activate(
        key,
        is_agent_live=self.is_live_key,
    )
```

不需要先构造 `AgentTarget` 再交给 activator。

### 9.3 binding 表拆分

当前：

```python
self.bindings
```

建议改为两个表：

```python
self.window_bindings: dict[str, TerminalWindowBinding]
self.terminal_observation_bindings: dict[str, TerminalObservationBinding]
```

`AgentTarget.terminal` 若 UI 仍需显示 window 诊断，可继续指 `TerminalWindowBinding`，但应重命名字段：

```python
AgentTarget.terminal_window
```

如果改动影响面过大，本轮可暂时保留 `terminal` 字段名，但其类型必须变成 `TerminalWindowBinding`，并删除所有 tab/pane 含义。

推荐本轮直接改名，避免长期技术债。

### 9.4 exit cleanup

`_commit_exit()`：

```python
self.window_bindings.pop(key, None)
self.terminal_observation_bindings.pop(key, None)
self._terminal_service.drop_instance(key)
```

`drop_instance` 只清 runtime resolver cache，不再有 manual binding。

---

## 10. Process Probe 与低功耗修复

## 10.1 `windows_enabled=False` 真正停扫描

当前问题：

UI 关闭 Windows source 后 Monitor 虽然不展示 Windows instance，但 `ProcessProbeWorker` 仍周期调用 `scan_windows()`。

修复：

```python
windows_enabled = bool(cfg_m.get("windows_enabled", True))

if windows_enabled:
    if due:
        ...
else:
    with self._lock:
        self._snapshot.pop("windows", None)
    self._windows_cache = ()
    self.windows_scan_ms = 0.0
    self.windows_probe_error = ""
```

验收：

```text
windows_enabled=False
运行 N 个 probe tick
scan_windows mock call_count == 0
```

若运行中从 True → False：

- source snapshot 清掉。
- Monitor 下一轮按 `source-disabled` commit exit。
- ExitWatcher unregister 对应 Windows Agent。
- 不再继续扫描。

从 False → True：

- `_last_windows=0` 或检测 enable transition 后立即 scan。
- 不需要重启 DeskPet。

## 10.2 `WindowsExitWatcher` 空闲 INFINITE wait

当前只有 control handle 时使用 200ms timeout。

改为：

```python
WaitForMultipleObjects(1, arr, False, INFINITE)
```

因为：

```text
register → SetEvent(control)
unregister → SetEvent(control)
stop → SetEvent(control)
```

均会安全唤醒。

验收：

- 空集合时不调用 `time.sleep()`。
- 不存在固定 200ms wake。
- register 后能立即重建 wait set。
- stop 能在 join timeout 内退出。

## 10.3 exit event 顺序

当前风险：

```text
copy instances
→ drain exit event
→ 后续仍使用旧 copy
```

改为：

```text
_merge_instances
→ snapshot current instances for exit watcher registration
→ register
→ drain exit events
→ 重新 snapshot instances
→ session poll
→ terminal observation
→ state reduce
```

保证 `_commit_exit()` 后同一 tick 不再为已退出 Agent 重建 snapshot/binding。

---

## 11. 动态配置与硬边界

`Monitor._loop()` 不应只在进入循环前读取一次 `file_poll_sec`。

改为：

```python
while not stop:
    start = ...
    _tick()
    poll_sec = clamp(
        config.get("monitor.file_poll_sec", 0.5),
        0.2,
        5.0,
    )
    wait(max(0.15, poll_sec - elapsed))
```

`Config.normalize()` 增加：

```text
windows_scan_sec        1.0 .. 60.0
wsl_scan_sec            1.0 .. 120.0
file_poll_sec           0.2 .. 5.0
session_scan_sec        1.0 .. 60.0
activity_grace_sec      1.0 .. 60.0
active_file_window_sec  30  .. 3600
```

配置文件手改异常值也不能制造高频 loop。

---

## 12. Presentation 修复

### 12.1 selector reclaim origin bug

当前 `_reclaim_slots()` 唯一匹配时：

```python
self._slot_bindings[slot_id] = candidates[0].key
```

但没有：

```python
self._slot_auto.add(slot_id)
```

导致 UI 可能把自动 reclaim 显示成“手动绑定”。

修复：

```python
if len(candidates) == 1:
    self._slot_bindings[slot_id] = candidates[0].key
    self._slot_auto.add(slot_id)
    self._slot_vacant_reason.pop(slot_id, None)
```

### 12.2 不要误删 Fleet 的“手动 slot 绑定”

本计划删除的是：

> 手动绑定 Agent 到 Terminal Tab/Pane。

不是：

> 用户选择某个 Agent 由哪一只桌宠展示。

`PresentationController.bind_slot(slot_id, agent_key)` 是 UI presentation binding，不是 terminal binding，应保留。

命名和文档应显式区分：

```text
Fleet slot assignment
≠
Terminal manual binding
```

---

## 13. UI 修复

## 13.1 Dashboard

删除：

```text
关联当前 Terminal 位置
手工修复说明
CONFIRMED/HIGH 后禁用修复按钮
Tab ID
Pane ID
Manual origin
exact Tab/Pane 帮助文案
```

“打开终端”帮助文字改为：

> 打开该 Agent 所在的 Windows Terminal 窗口。DeskPet 不切换 Terminal 标签页、不发送键盘输入。若 Windows 阻止后台程序抢前台，会闪烁任务栏提醒。

### 13.2 Activation toast

`PetApp.activate_agent()`：

```text
OK
→ 已打开该 Agent 所在的终端窗口

FOREGROUND_DENIED
→ Windows 未允许将终端置于前台，已闪烁任务栏提醒

AGENT_GONE
→ 该 Agent 已退出

NO_BINDING
→ 无法唯一确定该 Agent 所在的终端窗口

STALE_WINDOW
→ 原终端窗口已失效，重新识别后仍无法安全打开
```

删除：

```text
请关联当前 Terminal 位置
已选中 exact tab
UIA unavailable
STALE_TAB
STALE_PANE
```

### 13.3 普通诊断

保留：

```text
Agent key
process PID/token
source/distro
session binding health
Terminal HWND
Terminal PID
Terminal process create_time
Terminal window class
window resolve confidence/reason
UIA observer available
UIA events / dropped / visible reads
parser health
```

不展示：

```text
Tab RuntimeId
Pane RuntimeId
manual terminal location
selected tab
```

---

## 14. `terminal_service.py` 目标结构

建议改成：

```python
class WindowsTerminalService:
    def __init__(
        self,
        observer: TerminalObserver | None,
        window_resolver: TerminalWindowResolver | None = None,
        observation_resolver: TerminalObservationResolver | None = None,
        cfg: dict | None = None,
    ):
        ...

    # lifecycle
    def start(self) -> bool: ...
    def stop(self) -> None: ...
    def available(self) -> bool: ...
    def startup_error(self) -> str: ...

    # observation
    def poll(self, now: float) -> None: ...
    def refresh_observed_controls(self, force=False) -> bool: ...
    def waiting_observation(self, control_id): ...
    def activity_observation(self, control_id, now, grace): ...

    # resolver
    def resolve(
        self,
        instances: list[AgentInstance],
        now: float,
    ) -> tuple[
        dict[str, TerminalWindowBinding],
        dict[str, TerminalObservationBinding],
    ]:
        ...

    def current_window_binding(
        self,
        agent_key: str,
    ) -> TerminalWindowBinding | None:
        ...

    def observation_binding(
        self,
        agent_key: str,
    ) -> TerminalObservationBinding | None:
        ...

    # user action
    def activate(
        self,
        agent_key: str,
        *,
        is_agent_live: Callable[[str], bool],
    ) -> ActivationResult:
        ...

    def drop_instance(self, agent_key: str) -> None:
        ...
```

不再暴露 UIA backend 的 tab-control methods 给 Monitor/UI。

---

## 15. `terminal_uia.py` 推荐拆分

当前文件过大，且观察与 exact navigation 混合。

推荐本轮最小安全拆法：

```text
terminal_uia.py
  UiaBackend
  ObservedTerminalControl
  TerminalEvent
  TerminalObserver
  Recognizers
  SubscriptionTracker

terminal_resolver.py
  TerminalWindowResolver
  TerminalObservationResolver
```

如果为了控制 diff 暂不拆文件，也至少按 class 边界完全分离。

`matching.py` 继续复用 `mutual_unique_matches()`，因为 session binding 也在用，不能因删除 TerminalResolver 直接删掉。

---

## 16. `actions/winkeys.py` 计划

不回退文件内容到 v3。

保留 v4.1：

```text
window_identity
validate_window
restore_window
try_set_foreground
flash_window
monitor_work_area
dpi_for_window
```

删除任何只为 Tab/Pane activation 服务的注释。

可补：

```python
@dataclass(frozen=True)
class WindowActivation:
    ok: bool
    foreground_denied: bool = False
```

但不是必要。

原则：

> 行为回到 v3，安全校验保留 v4.1。

---

## 17. Tray / 配置启动写盘优化

当前 app 初始化若 `tray_enabled=True` 调 `start_tray()`，而 `start_tray()` 会再次保存 `tray_enabled=True`。

改成：

```python
def _start_tray_runtime(self):
    # 不写 config
    ...

def set_tray_enabled(self, enabled: bool):
    if enabled:
        self._start_tray_runtime()
    else:
        self._stop_tray_runtime()

    config.set_and_commit("tray_enabled", enabled)
```

启动：

```python
if config.get("tray_enabled"):
    self._start_tray_runtime()
```

减少无意义常驻启动写盘。

---

## 18. Shutdown 生命周期

给 `ProcessProbeWorker`：

```python
def stop(self):
    self._stop.set()

def join(self, timeout=2.0):
    ...
```

Monitor：

```python
def stop(self):
    self._stop.set()
    self._probe.stop()

    exit_watcher.stop()
    terminal_service.stop()

    if self._thread:
        self._thread.join(timeout=...)

    self._probe.join(timeout=...)
```

注意避免：

```text
Monitor thread join 自己
UIA MTA thread 被 Tk thread 无限等待
```

所有 join 都必须有界。

---

## 19. 测试重构

## 19.1 删除错误产品语义测试

从 `test_terminal_service.py / test_terminal_uia.py / test_ui.py` 删除：

```text
select exact tab
selected tab verification
selection pattern unavailable
manual focused location
manual binding lifecycle
sole-pane rebind
STALE_TAB
STALE_PANE
focus_pane
```

这些测试当前是在保护已经决定移除的行为。

## 19.2 Window activation 单测

新增 `tests/test_terminal_activation.py`：

### A. Valid window

```text
live Agent
+ valid WindowIdentity
→ restore_window called once
→ try_set_foreground called with exact hwnd
→ OK
```

### B. UIA unavailable

```text
observer=None
+ valid TerminalWindowBinding
→ activation still OK
```

这是重要回归测试。

### C. stale HWND

```text
validate_window=False
→ resolve refresh once
→ new valid binding
→ OK(repaired=True)
```

### D. stale after retry

```text
first invalid
refresh
second invalid
→ STALE_WINDOW
```

### E. foreground denied

```text
try_set_foreground=False
→ flash_window exact hwnd
→ FOREGROUND_DENIED
```

### F. no binding

```text
→ NO_BINDING
→ no restore
→ no foreground
```

### G. Agent exited

```text
is_agent_live=False
→ AGENT_GONE
→ no window action
```

### H. forbidden API regression

静态搜索测试或 source inspection：

```text
terminal_service.py 不含 select_tab
terminal_service.py 不含 focus_pane
terminal_service.py 不含 SendInput
```

---

## 20. 并发激活测试矩阵

新增 `tests/test_concurrent_activation.py`。

### 20.1 Fleet body

构造：

```text
slot pet-1 -> A
slot pet-2 -> B
```

触发：

```text
view1._on_double()
```

断言：

```text
on_activate == [A.key]
```

触发：

```text
view2._on_double()
```

断言：

```text
on_activate == [B.key]
```

绝不调用 `focused_key`。

### 20.2 Fleet bubble

构造：

```text
view1.bubble.model.agent_key = A.key
view2.bubble.model.agent_key = B.key
```

命中底行：

```text
view1 bubble → A.key
view2 bubble → B.key
```

### 20.3 binding/window independence

构造：

```text
A.window.hwnd = 101
B.window.hwnd = 202
```

点击 pet-1：

```text
try_set_foreground(101)
not 202
```

点击 pet-2：

```text
try_set_foreground(202)
not 101
```

这是本轮最关键的 end-to-end logical test。

### 20.4 same Windows Terminal window

允许：

```text
A → hwnd 101
B → hwnd 101
```

因为用户要求回退到 window-level。

此时点击两个不同 Pet 都会前置同一个 Terminal window，这是正确行为；DeskPet 不承诺自动切到 A/B 各自 Tab。

测试必须明确记录这一语义，防止后续开发又把它当 bug 重引入 exact tab selection。

### 20.5 Aggregate bubble race

绘制时：

```text
bubble.model.agent_key = A.key
```

随后 attention 变成 B。

用户点击原气泡：

```text
仍 activate(A.key)
```

不能重新按新 attention 选 B。

---

## 21. Observation 测试

继续保留：

- Notification event 入队。
- queue overflow 丢最旧。
- delta truncation。
- ring size bound。
- visible read debounce。
- global read budget。
- approval TTL。
- old visible approval 不无限 WAITING。
- recognizer kind mismatch 不归属。
- generic activity 不覆盖 structured session DONE/IDLE。
- StructureChanged 后 controls refresh。
- COM handler churn 不增长。

删除所有 selected-tab topology assertions。

---

## 22. Process / 生命周期测试

新增：

```text
windows_enabled=False → scan_windows call_count == 0
False→True → 下一 probe 周期立即 scan
True→False → windows cache/source 清理

ExitWatcher no entries → INFINITE wait contract
register → control event wakes thread
stop → control event wakes thread

exit event in tick
→ instance removed
→ same tick watcher/terminal/state 不再处理旧 key
```

---

## 23. 配置测试

新增 normalize：

```text
file_poll_sec=-1 → 0.2
file_poll_sec=999 → 5.0
windows_scan_sec=0 → 1.0
wsl_scan_sec=999 → 120
```

运行中改 `file_poll_sec`：

```text
下一 loop 使用新值
```

不要求重启。

---

## 24. UI 测试

确保普通界面不再存在字符串：

```text
关联当前 Terminal 位置
STALE_TAB
STALE_PANE
精确 Tab
精确 Pane
```

Help 文案必须出现：

```text
打开 Windows Terminal 窗口
不切换标签页
不发送键盘输入
```

Fleet slot binding UI 仍保留，因为这是 Presentation 绑定。

---

## 25. 手工 Windows 验收工具

废弃/重构 `tools/terminal_layout_probe.py`。

新增：

```text
tools/terminal_window_probe.py
```

功能：

```bash
python tools/terminal_window_probe.py --list
python tools/terminal_window_probe.py --validate <hwnd>
python tools/terminal_window_probe.py --activate <hwnd>
```

输出只含：

```text
hwnd
pid
process_created
class
title
valid
foreground result
```

不输出 Terminal 可见文本。

另保留一个 observation-only probe：

```bash
python tools/terminal_observer_probe.py
```

只显示：

```text
control count
event count
visible read count
recognizer type/status
```

默认不打印 terminal raw text。

CI 不操作用户真实 Windows Terminal。

---

## 26. README / SourceLink / plan 文档更新

README 删除：

```text
exact Window → Tab → Pane
精确返回 Agent Tab/Pane
手动关联 Terminal 位置
gone_grace_sec 当前配置
pinned 当前配置
tests/uia_probe.py
```

README 新增明确说明：

> DeskPet 通过公共 Win32 API 恢复并尝试前置 Agent 所在的 Windows Terminal 顶层窗口，不切换既有标签页。Terminal UIA 只用于被动状态观察，不用于用户显式导航。

`SourceLink.md` 更新官方依据。

`v4plan.md` 标记 exact Tab/Pane 方案为 abandoned，不再作为当前实现要求。

推荐本文件 `plan.md` 成为 v4.1.1 的唯一实施验收依据；修复完成后再同步 README/SourceLink。

---

## 27. CI

现有 Windows CI 保留：

```text
compileall
unittest discover
monitor benchmark
artifact upload
```

增加：

```text
terminal window activation logical tests
concurrent activation tests
source-disabled scan tests
config runtime tests
```

删除/改正：

```text
tests/uia_probe.py
```

因为仓库当前没有这个文件。

CI 注释改为：

> 真实 Windows Terminal foreground policy / UIA event acceptance 为本机 manual acceptance；CI 只验证纯逻辑、Win32 调用契约 mock、资源边界和 UI dataflow。

---

## 28. Benchmark 验收

现有 Monitor benchmark 继续要求：

```text
event queue bounded
UIA call queue bounded
controls <= MAX_CONTROLS
ring <= RING_MAX
visible reads <= configured global budget
dropped 在压力范围内
resolver deterministic
```

新增 lightweight checks：

```text
windows disabled:
  windows scan delta == 0

idle ExitWatcher:
  no polling timeout path

N Fleet Pets:
  still one Monitor
  one UIA thread
  one AnimationScheduler
  one SharedAnimationCache
```

不设“每 Pet 一个线程”。

---

## 29. 分阶段实施顺序

### Phase A — Data contract cleanup

修改：

```text
agents/models.py
```

完成：

```text
Window-only binding model
observation-only binding model
ActivationCode cleanup
TabInfo/TerminalLocation removal
```

完成后全仓库修复类型错误，先不改 UI。

### Phase B — UIA observer cleanup

修改：

```text
agents/terminal_uia.py
```

完成：

```text
删除 Tab topology/control
保留 TermControl observation
保留 MTA/event/bounds
```

若拆文件：

```text
agents/terminal_resolver.py
```

### Phase C — Terminal service

重写：

```text
agents/terminal_service.py
```

实现：

```text
resolve window
resolve observation control
window-only activation
```

完全删除 manual path。

### Phase D — Monitor

修改：

```text
agents/monitor.py
```

完成：

```text
双 binding 表
manual API 删除
activation API
exit tick 顺序
windows_enabled gate
dynamic file_poll
```

### Phase E — UI / concurrency

修改：

```text
pet/app.py
pet/dashboard.py
pet/petview.py
pet/bubble.py
pet/presentation.py
```

确认：

```text
Fleet Pet body exact key
Fleet bubble exact key
Aggregate bubble visual key
Dashboard exact key
Tray exact key
```

同时修 selector reclaim origin。

### Phase F — resource lifecycle

修改：

```text
agents/process_watch.py
pet/config.py
pet/app.py
```

完成：

```text
INFINITE idle wait
config clamp
tray startup no redundant save
thread join
```

### Phase G — tests/tools/docs

删除旧 exact tests，新增 window-only + concurrency tests。

更新：

```text
README.md
SourceLink.md
v4plan.md
.github/workflows/test.yml
tools/*
```

### Phase H — final dead-code audit

全仓库搜索必须无产品路径残留：

```text
TerminalLocation
bind_focused_location
set_manual_location
manual_location
select_tab
selected_tab
focus_pane
STALE_TAB
STALE_PANE
关联当前 Terminal 位置
```

允许出现这些词的唯一位置：

```text
历史迁移文档 / changelog / abandoned design 说明
```

当前产品代码和测试不得依赖。

---

## 30. 最终 Definition of Done

本轮只有同时满足以下条件才算完成：

1. 用户无法手工绑定 Terminal Tab/Pane。
2. 产品代码没有 manual terminal location runtime chain。
3. “打开终端”不依赖 UIA。
4. “打开终端”不依赖 Tab/Pane。
5. 有效 WindowIdentity 可通过 v3 风格 restore + foreground 唤起。
6. OS 拒绝 foreground 时只 Flash，不绕过系统 policy。
7. stale HWND/PID incarnation fail-closed。
8. WSL 多窗口无法唯一定位时不猜。
9. 唯一 Terminal window fallback 可以唤起，但不赋予 terminal evidence attribution。
10. UIA observer 继续识别 WAITING/approval。
11. Terminal observation 不安全时不归属具体 Agent。
12. Fleet 模式每只 Pet 双击都使用自己的 exact `agent_key`。
13. Fleet 模式每只气泡点击都使用自己的 exact `agent_key`。
14. 不同 Agent 若映射到不同 HWND，分别打开各自 HWND。
15. 不同 Agent 若恰好同属一个 Terminal window，则都打开同一 window，且不尝试自动切 Tab。
16. Aggregate 气泡使用绘制时固化的 exact key，不按点击瞬间 attention 重新选 Agent。
17. Dashboard / Tray 所有 Agent action 捕获 exact key。
18. `windows_enabled=False` 真正停止 Windows process scan。
19. ExitWatcher 无 Agent 时为真正 blocking wait。
20. `file_poll_sec` 运行时修改实际生效。
21. monitor 配置有代码级 clamp。
22. exit event 后同 tick 不再处理 stale instance。
23. Fleet selector reclaim 正确标记 auto。
24. 启动 Tray 不无条件重写 config。
25. shutdown thread 生命周期有界。
26. README/SourceLink/CI 与新合同一致。
27. Windows unit tests 全绿。
28. Monitor benchmark 继续满足原资源预算。
29. 不新增 hook/config requirement。
30. 不新增输入注入或审批自动执行能力。

---

## 31. 外部实现依据

### Microsoft Win32

SetForegroundWindow  
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow

核心约束：

- API 接收顶层 `HWND`。
- Windows 限制哪些进程能够抢到 foreground。
- DeskPet 必须接受调用被拒，不能通过输入注入绕过。

WaitForMultipleObjects  
https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitformultipleobjects

核心约束：

- 可等待 Event / Process 等 waitable handle。
- `INFINITE` 可用于直到对象 signal。
- 当前 ExitWatcher control event 足以安全唤醒 register/unregister/stop。

### Microsoft UI Automation

Understanding Threading Issues  
https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading

核心约束：

- desktop-wide UIA client 应把 UIA 调用放到独立线程。
- 线程不应拥有窗口。
- 建议 COM MTA。
- add/remove event handler 应在同一个非 UI/MTA thread。

IUIAutomationTextPattern::GetVisibleRanges  
https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationtextpattern-getvisibleranges

核心约束：

- 读取当前可见 text ranges。
- 继续用于 DeskPet approval fallback，避免扫描完整 scrollback。

### Windows Terminal

Feature Request: Focus/Activate Tab by WT_SESSION (#19783)  
https://github.com/microsoft/terminal/issues/19783

该 issue 明确描述：

- 外部进程目前没有稳定的按 `WT_SESSION` 激活既有 Terminal Tab 的接口。
- UI Automation Tab title / SelectionItemPattern 是 fragile workaround。
- DeskPet 因此不再把 exact existing Tab activation 作为可靠产品合同。

---

## 32. 最终架构一句话

修复后的 DeskPet 应当是：

> **Process/Session/UIA 被动观察负责“Agent 现在在做什么”，Window Resolver 只负责“Agent 大概在哪个 Windows Terminal 顶层窗口”，Presentation 负责“哪个桌宠/气泡代表哪个 exact Agent”，而用户点击始终沿 exact `agent_key` 唤起那个 Agent 的 Terminal window；三条职责不再通过 Tab/Pane 或手工绑定互相耦合。**

这就是 v4.1.1 的最终收敛目标。
