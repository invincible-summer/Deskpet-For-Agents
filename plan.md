# DeskPet v4.1.3 完整修复实施计划

> **用途**：v4.1.3 代码修改、审查、测试、实机验收的详细执行依据。  
> **当前实现基线**：`266f5ae5e4695a1ec2cff6936f74a9723bb11b6f`（v4.1.2）  
> **v3 行为参考**：`af8236d15dc3bfecaa89464e1b77d7f84c2b09be`  
> **仓库**：`invincible-summer/DeskPet`

本计划的核心不是继续增强 v4.1/v4.1.2 的 Terminal 绑定算法，而是**恢复 v3 已经验证可用的 Terminal 顶层窗口候选选择语义**，同时保留 v4.1.2 的多 Agent、并发呈现、严格 UIA Observation、强 `WindowIdentity`、低资源生命周期等正确改进。

---

## 1. v4.1.3 最终目标

本版本只收敛四件事情：

1. **Terminal Window Wake 恢复 v3 语义**：Windows native 用 PID ancestor；WSL 使用被动观察到的 `TermControl` 标题按 v3 权重评分；HIGH 匹配失败但存在正向 best control 时仍保留其 HWND；桌面只有一个 Windows Terminal 顶层窗口时仍保留该 HWND。`confidence` 不再决定能不能打开终端，`TerminalWindowBinding.window != None` 才决定 wakeability。
2. **Terminal Observation 保持 v4.1.2 严格链**：`ObservedTerminalControl`、审批识别、WAITING/activity attribution 不因 Window wake 放宽；低置信 Window 可供用户显式唤起，但不能自动把 Terminal 文本归给 Agent。
3. **固定三种交互模式**：非并发 SINGLE 双击气泡或桌宠都唤起；并发 AGGREGATE 只有双击气泡唤起，双击桌宠只互动；并发 FLEET 每只气泡或桌宠双击都唤起各自 exact Agent。
4. **修复 Tray / Dashboard 桌宠消失**：Tray 左键不再 toggle，而是幂等 show/recover；Dashboard 打开不改变 logical hidden state，只对原本 visible Pet 做一次 no-activate Z-order reassert；Dashboard 隐藏时停止 refresh timer。

明确不做：

```text
BEST_EFFORT 新等级
Agent → Tab
Agent → Pane
TerminalLocation
TabInfo
manual Terminal binding
focused_location / focused_pane 作为产品绑定
SelectionItemPattern 激活
WT_SESSION → existing tab 激活
Ctrl+Tab / SendInput / keybd_event / PostMessage / 剪贴板注入
```

---

## 2. v3 终端唤起真正需要恢复的部分

v3 的用户激活链是：

```text
Agent
→ TerminalBinding
→ binding.hwnd != 0
→ validate_terminal_window(binding)
→ IsWindow + owner PID + window class
→ SW_RESTORE（若最小化）
→ SetForegroundWindow(hwnd)
→ GetForegroundWindow()==hwnd ?
   ├─ yes: success
   └─ no : FlashWindowEx
```

v3 的关键点是：**用户显式唤起没有要求 `BindingConfidence` 必须为 CONFIRMED/HIGH。** 只要 resolver 给出了候选 HWND，并且 action 层验证通过，就会尝试唤起。

v3 WSL resolver 的决策顺序：

```text
A. manual Pane binding                 ← v4.1.3 不恢复
B. Windows native PID ancestor
C. Agent ↔ TermControl mutual-unique scoring
D. mutual-unique 失败但存在 best positive control
E. 唯一 Windows Terminal window fallback
```

最重要的是 D。v3 在正向证据存在但不够唯一时：

```python
TerminalBinding(
    hwnd=best_pane.hwnd,
    confidence=AMBIGUOUS,
    ...
)
```

即：**AMBIGUOUS 仍然携带 HWND。**

v4.1.2 当前却把同类情况改成：

```python
TerminalWindowBinding(
    confidence=AMBIGUOUS,
    window=None,
)
```

于是 `WindowsTerminalService.activate()` 在进入 Win32 前就返回 `NO_BINDING`。这是当前“不能像 v3 一样唤起终端”的首要修复点。

### 2.1 不是 bug-for-bug 复制 v3

v3 sole-window fallback 虽然保留 `hwnd`，但没有完整写入 `window_pid`，而 v3 的 `validate_terminal_window()` 又要求 `expected_pid > 0`，这条 fallback 本身存在旧缺陷。

v4.1.3 必须采用：

```text
v3 决定“选哪个 HWND”
+
v4.1.2 winkeys.window_identity(hwnd) 建立完整身份
```

最终所有可唤起候选必须具有：

```text
HWND
PID
process create_time
window class
```

因此恢复的是 **v3 的候选选择行为**，不是原样复制旧数据结构。

### 2.2 v3 不恢复项

v3 的 `manual_bind_focused()`、`set_manual_binding()`、`pane_id`、用户“高级关联当前 Pane”全部继续删除。当前决策是 Window-only wake，绝不重新引入 manual Tab/Pane 修复链。

---

## 3. 最终数据模型

### 3.1 `WindowBindingConfidence`

当前 v4.1.2：

```python
CONFIRMED
HIGH
FALLBACK
AMBIGUOUS
NONE
```

v4.1.3 改为：

```python
class WindowBindingConfidence(str, Enum):
    CONFIRMED = "confirmed"
    HIGH = "high"
    AMBIGUOUS = "ambiguous"
    NONE = "none"
```

删除：

```text
FALLBACK
BEST_EFFORT（不得新增）
```

语义：

- `CONFIRMED`：Windows native PID ancestor 唯一定位 WT window。
- `HIGH`：v3-compatible TermControl 标题评分达到 mutual-unique 高置信。
- `AMBIGUOUS`：证据不够安全用于自动 observation attribution，但仍可能有一个 v3 best-positive control 所属 HWND 可供用户显式 wake。
- `NONE`：没有 Agent-specific 可靠证据；如果桌面只有一个 WT window，仍可以带这个 window 作为 single-window fallback。

必须在注释和测试中写明：

> `confidence` 不是 activation gate。是否可以尝试唤起只由 `TerminalWindowBinding.window` 是否存在决定。

### 3.2 `TerminalWindowBinding`

保持当前 window-only 结构：

```python
@dataclass
class TerminalWindowBinding:
    provider: str = "windows-terminal"
    window: WindowIdentity | None = None
    title: str = ""
    confidence: WindowBindingConfidence = WindowBindingConfidence.NONE
    last_seen: float = 0.0
    validated_at: float = 0.0
    reason: str = ""
    score: int = 0
    runner_up_score: int = 0

    @property
    def hwnd(self) -> int:
        return self.window.hwnd if self.window else 0
```

建议增加：

```python
@property
def wakeable(self) -> bool:
    return self.window is not None and self.window.hwnd > 0
```

禁止重新加入：

```text
pane_id
tab_id
RuntimeId
manual origin
selected/index
```

### 3.3 `TerminalObservationBinding`

保持当前：

```python
@dataclass(frozen=True)
class TerminalObservationBinding:
    agent_key: str
    control_id: tuple
    confidence: ObservationBindingConfidence  # 仅 CONFIRMED/HIGH
    reason: str = ""
```

Window wake 与 Observation attribution 完全分离。

---

## 4. `agents/terminal_resolver.py`：恢复 v3 candidate selection

这是 v4.1.3 的核心文件。

### 4.1 Window navigation 不再继续增强

从 `TerminalWindowResolver` 中移除/退出以下 v4.1.2 新语义：

```text
WT 顶层 Window title 作为新的 Window wake 直接评分模型
control 数量决定 Window title 是否可用于 navigation
FALLBACK confidence
AMBIGUOUS 强制 window=None
“必须唯一确定 Window 才允许 wake”
```

不新增 Agent↔HWND 新评分矩阵，也不使用 one-to-one HWND 分配。

恢复：

```text
Agent ↔ ObservedTerminalControl score
→ control.hwnd
```

`ObservedTerminalControl` 在 Window resolver 中只是被动 title hint，`control_id` 不进入公开 binding。

### 4.2 v3-compatible scoring

Window wake 使用 v3 权重：

```text
kind    +3
cwd     +2
user@   +1
distro  +1

HIGH_MIN_SCORE  = 3
HIGH_MIN_MARGIN = 1
```

但不复制 v3 的字符串误匹配 bug。允许保留 v4.1.2 已完成的纯安全修正：

```text
pi 使用词边界，不匹配 pip
~/path 做归一化
/home/dev 不因 dev@host 被当成 cwd 命中
distro 使用词边界
```

这些修正不改变 v3 的决策拓扑。

### 4.3 Navigation scorer 与 Observation scorer 分开

当前共享 `_EvidenceScorer.titles_for()` 会把 WT 顶层窗口标题作为第二证据。Window navigation 为了严格回到 v3，不应继续使用这条增强；ObservationResolver 则保持现状。

建议拆为：

```python
class _BaseTerminalTitleScorer:
    def score_title(self, inst, title) -> tuple[int, str]: ...

class _V3WindowControlScorer(_BaseTerminalTitleScorer):
    def score_control(self, inst, control) -> int:
        return self.score_title(inst, control.title)[0]

class _ObservationScorer(_BaseTerminalTitleScorer):
    # 保持 v4.1.2 当前 titles_for / window-title secondary evidence
    ...
```

不要用一个 `navigation=True/False` 参数隐藏两套合同。

---

## 5. Window catalog 与 `WindowIdentity`

### 5.1 Window catalog 独立于 UIA layout

Window resolver 的 Windows Terminal 顶层窗口列表来自：

```python
winkeys.enum_windows()
```

身份来自：

```python
winkeys.window_identity(hwnd)
```

不要求 UIA `TerminalLayout.windows` 存在。

建议接口：

```python
class TerminalWindowResolver:
    def __init__(
        self,
        enum_windows=None,
        ancestor_pids=None,
        identity_for_hwnd=None,
    ):
        ...
```

测试通过 `identity_for_hwnd` 注入真实非零 `process_created`，生产默认使用 `winkeys.window_identity()`。

### 5.2 内部窗口缓存

沿用 v3 的轻量 3 秒 catalog cache：

```python
WINDOW_CACHE_SEC = 3.0

self._last_windows = []
self._windows_ts = 0.0
```

```python
def _windows(self, force=False):
    if not force and now - self._windows_ts < WINDOW_CACHE_SEC:
        return self._last_windows
    rows = winkeys.enum_windows()
    rows = [r for r in rows if r.class == WT_WINDOW_CLASS]
    ...
```

这样不增加 Monitor 0.5s 主循环成本；stale activation 时只 force 一次。

### 5.3 候选 HWND 身份失败

若 v3 heuristic 选中了 HWND，但：

```python
winkeys.window_identity(hwnd) is None
```

不得构造 `process_created=0` 的假身份。

返回：

```python
TerminalWindowBinding(
    window=None,
    confidence=<证据等级>,
    reason="...·window-identity-failed",
)
```

用户 action 不得触碰该 HWND。

---

## 6. `TerminalWindowResolver.resolve()` 确定实现

目标签名：

```python
def resolve(
    self,
    instances: list[AgentInstance],
    controls: dict[tuple, ObservedTerminalControl],
    now: float,
) -> dict[str, TerminalWindowBinding]:
    ...
```

Window resolver 删除 `layout=` 参数；ObservationResolver 仍可使用 layout。

### 6.1 Windows native

```text
Agent PID
→ _ancestor_pids(pid)
→ 枚举 WT window owner PID
```

规则：

```text
命中 1 个 WT HWND
→ CONFIRMED + 完整 WindowIdentity

命中 >1
→ AMBIGUOUS + window=None

命中 0
→ 进入 v3 control scoring fallback
```

特别注意：**唯一 native WT window 下有多个 TermControl 也仍然 CONFIRMED。** Window-only wake 已不再关心 pane 数量。

### 6.2 WSL / native fallback scoring

```python
agent_keys = [inst.key for inst in scored_instances]
control_ids = list(controls)

def score_fn(agent_key, control_id):
    return scorer.score_control(
        inst_by_key[agent_key],
        controls[control_id],
    )

decisions = mutual_unique_matches(
    agent_keys,
    control_ids,
    score_fn,
    min_score=3,
    min_margin=1,
)

diagnostics = best_effort_scores(
    agent_keys,
    control_ids,
    score_fn,
)
```

### 6.3 v3 决策顺序

```text
1. mutual unique 成功
   → HIGH
   → window = selected control.hwnd

2. mutual unique 失败，但 best_control != None 且 best_score > 0
   → AMBIGUOUS
   → window = best control.hwnd             ★ v3 关键行为

3. 完全无正向 control evidence，且只有一个 WT HWND
   → NONE
   → window = sole WT hwnd                  ★ v3 fallback
   → reason=single-window-fallback

4. 多个 WT window 且无正向 evidence
   → NONE
   → window=None
   → reason=multiple-terminal-windows-no-evidence

5. 无 WT window
   → NONE
   → window=None
   → reason=no-terminal-window
```

### 6.4 `_binding_from_hwnd()`

所有 confidence 共用同一个构造函数：

```python
def _binding_from_hwnd(
    self,
    hwnd,
    row,
    now,
    confidence,
    reason,
    score=0,
    runner_up=0,
):
    identity = self._identity(hwnd)

    if identity is None:
        return TerminalWindowBinding(
            window=None,
            title=(row.title if row else "")[:80],
            confidence=confidence,
            last_seen=now,
            reason=reason + "·window-identity-failed",
            score=score,
            runner_up_score=runner_up,
        )

    return TerminalWindowBinding(
        window=identity,
        title=(row.title if row else "")[:80],
        confidence=confidence,
        last_seen=now,
        validated_at=now,
        reason=reason,
        score=score,
        runner_up_score=runner_up,
    )
```

---

## 7. `TerminalObservationResolver` 保持 v4.1.2

本轮不放宽 Observation。

保留：

```text
Windows native：CONFIRMED window + 该 window 当前只有一个 observed control
→ Observation CONFIRMED

其他情况：Agent ↔ exact ObservedTerminalControl 严格 mutual unique
→ Observation HIGH

否则不生成 binding
```

重要不变量：

```text
Window AMBIGUOUS + valid HWND
≠ 自动获得 terminal text attribution

Window NONE + sole-window HWND
≠ 自动获得 terminal text attribution
```

但 ObservationResolver 可以依据自己独立的 strict control evidence 得出 HIGH；这是允许的，因为依据不是低置信 Window binding。

---

## 8. `agents/terminal_service.py`

### 8.1 `resolve()`

改为：

```python
controls = self.observed_controls()

window_bindings = self.window_resolver.resolve(
    list(instances),
    controls,
    now,
)

observation_bindings = self.observation_resolver.resolve(
    list(instances),
    controls,
    window_bindings,
    now,
    layout=self.layout(),
)
```

结果：Window catalog 不依赖 UIA；UIA controls 只是 v3 WSL title hint。

UIA unavailable：

```text
Windows native ancestor → 仍能绑定
sole WT window → 仍能 NONE + window
multiple WT + WSL + controls={} → 不猜，NONE + no window
```

### 8.2 `activate()` 不看 confidence

必须只判断：

```python
binding = self._window_bindings.get(agent_key)
if binding is None or binding.window is None:
    return NO_BINDING
```

以下全部合法进入相同 activation：

```text
CONFIRMED + window
HIGH + window
AMBIGUOUS + window
NONE + window
```

禁止任何：

```python
if binding.confidence not in (...):
    return NO_BINDING
```

### 8.3 stale refresh

保留 v4.1.2 的“一次刷新、一次 re-resolve”：

```text
validate false
→ window_resolver.invalidate_window_cache()
→ refresh_observed_controls(force=True)（可选增强，不作为 Window catalog 前提）
→ resolve once
→ Agent live check
→ validate once
→ success / STALE_WINDOW
```

不循环。

### 8.4 UIA availability latch

当前 `service.failed` 与 `backend.available` 有语义冲突。改为：

```python
def available(self):
    backend = self.backend
    return bool(
        self.observer is not None
        and backend is not None
        and backend.available
    )
```

startup failed 只能作为历史诊断，不能永久 gate 后续 ready 的 backend。

---

## 9. `actions/winkeys.py`

Terminal 继续保留 v4.1.2 原语：

```text
enum_windows
window_identity
validate_window
restore_window
try_set_foreground
flash_window
```

不恢复 v3 的 `raise_terminal(binding)` 业务封装。

### 9.1 `process_created` 必须 fail-closed

当前只有 `expected_created > 0` 才比较，和注释“期望缺失即失败”不一致。

改为：

```python
if expected_created <= 0:
    return False

created = psutil.Process(pid).create_time()
if abs(created - expected_created) > 0.5:
    return False
```

完整验证条件：

```text
IsWindow(hwnd)
expected pid > 0
actual pid == expected pid
expected process_created > 0
actual create_time ~= expected
expected class != ""
actual class == expected class
```

---

## 10. 新增 DeskPet Pet Z-order helper

Terminal foreground 与 DeskPet 自己的 Toplevel Z-order 必须分开。

新增：

```python
def reassert_window_z_order(hwnd: int, *, topmost: bool) -> bool:
    ...
```

Win32：

```python
HWND_TOP = 0
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

insert_after = HWND_TOPMOST if topmost else HWND_TOP
flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW
user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, flags)
```

只用于 DeskPet 自身 Pet Toplevel；Terminal 用户显式 action 仍使用 `SetForegroundWindow()`。

不使用：

```text
focus_force
grab_set
SetForegroundWindow(Pet)
```

---

## 11. `pet/petwindow.py`：气泡改成真正双击

当前 `_on_press()` 命中气泡后立即 `on_click_button(tag)`，所以气泡现在实际是单击激活。

### 11.1 callback

建议改名：

```python
self.on_bubble_double = None
self.on_body_double = None
```

### 11.2 `_on_press()`

```python
def _on_press(self, ev):
    tag = self.hit_button(ev.x, ev.y)
    if tag:
        self._drag_off = None
        return "break"

    self._drag_off = (ev.x, ev.y)
```

即：单击气泡不 activation，也不启动拖动。

### 11.3 `_on_double()`

```python
def _on_double(self, ev):
    self._drag_off = None

    tag = self.hit_button(ev.x, ev.y)
    if tag:
        if self.on_bubble_double:
            self.on_bubble_double(tag)
        return "break"

    if self.on_body_double:
        self.on_body_double()
    return "break"
```

一次 double-click 只能产生一次 activation callback。

---

## 12. `pet/bubble.py`

`BubbleModel.agent_key` 和 `HitTarget` 保持 exact identity。

推荐把整个 visible bubble rectangle 作为 double-click target，而不是只有 footer 小区域：

```python
if self.model.agent_key:
    self._hit_boxes.append((
        (ox, oy, x1, y1),
        HitTarget(
            action="activate_agent",
            agent_key=self.model.agent_key,
        ),
    ))
```

实际 action 仍只由 PetWindow 的 `<Double-Button-1>` handler 执行。

禁止 double handler 现场重新读取 `attention_key/focused_key`。必须使用绘制时固化的 `BubbleModel.agent_key`，保证：

```text
visual identity == click identity
```

---

## 13. `pet/petview.py` 三模式规则

当前 `PetViewManager.sync()` 的 body activation 规则已经符合需求，保留。

### SINGLE（非并发）

```text
bubble double → exact Agent Terminal
body double   → exact Agent Terminal
```

### AGGREGATE（并发单宠）

```text
bubble double → 当前绘制 Agent Terminal
body double   → interact only
```

### FLEET（并发多宠）

```text
pet A bubble/body → Agent A
pet B bubble/body → Agent B
```

建议方法重命名：

```text
PetView._on_double
→ PetView._on_body_double
```

实现：

```python
def _on_body_double(self):
    if not self.body_activates:
        self._on_interact_cb()
        return

    if self.agent_key:
        self._on_activate(self.agent_key)
        return

    if self._on_double_vacant:
        self._on_double_vacant(self)
```

Window hook：

```python
self.window.on_bubble_double = self._on_hit_tag
self.window.on_body_double = self._on_body_double
```

---

## 14. 并发 exact routing 不改变

保留现有：

```text
PresentationState.slot_keys[slot_id]
→ PetViewManager.sync()
→ view.set_agent(exact key)
→ BubbleModel.agent_key = target.key
```

所有 Terminal wake：

```text
exact agent_key
→ PetApp.activate_agent(key)
→ Monitor.activate_target(key)
→ WindowsTerminalService.activate(key)
→ _window_bindings[key]
→ exact candidate HWND
```

不能通过 `focused_key` 替代 Fleet Pet 自己的 key。

---

## 15. Tray 左键消失修复

当前：

```python
if ev == "left":
    self.toggle_visible()
```

这会让已经 visible 的 Pet 被主动 hide。

新增：

```python
def restore_pet_from_tray(self):
    if self.pet_manager.any_visible():
        self.pet_manager.reassert_visible_windows()
    else:
        self.pet_manager.show_all()
        self.root.after_idle(self.pet_manager.reassert_visible_windows)
```

`_poll_tray_events()`：

```python
if ev == "left":
    self.restore_pet_from_tray()
```

右键菜单的显式“显示/隐藏桌宠”仍可以使用 `toggle_visible()`。

Tooltip 改成：

```text
DeskPet - 左键显示桌宠，右键菜单
```

---

## 16. `PetViewManager.reassert_visible_windows()`

新增：

```python
def reassert_visible_windows(self):
    for view in self.views.values():
        if view.hidden:
            continue
        view.window.reassert_z_order()
```

必须使用 `view.hidden` 表示用户逻辑 hidden intent，不使用 `winfo_viewable()` 决定是否应该重新显示。

---

## 17. `PetWindow.reassert_z_order()`

新增：

```python
def reassert_z_order(self):
    if not self.root.winfo_exists():
        return False

    self.root.update_idletasks()
    hwnd = int(self.root.winfo_id())

    return winkeys.reassert_window_z_order(
        hwnd,
        topmost=bool(self.config.get("topmost", True)),
    )
```

非 Windows 测试环境可 fallback `root.lift()`。

同时修当前：

```python
def set_topmost(self, flag):
    self._apply_topmost()
```

参数未直接生效的问题。改为：

```python
def set_topmost(self, flag):
    self.root.attributes("-topmost", bool(flag))
```

---

## 18. Dashboard 打开不改变 Pet hidden state

当前 `open_dashboard()` 没有显式调用 `hide_pet()`；修复策略不是强制 `show_all()`，而是恢复原本 visible Pet 的 Z-order。

改为：

```python
def open_dashboard(self):
    if self.dashboard is None or not self.dashboard.winfo_exists():
        self.dashboard = Dashboard(self)

    self.dashboard.open()
    self.root.after_idle(self.pet_manager.reassert_visible_windows)
```

效果：

```text
原本 visible Pet → Dashboard 打开后仍 visible，并重新声明 Z-order
原本 view.hidden=True → 不 show、不 reassert
```

---

## 19. Dashboard refresh 生命周期

当前 `_refresh_once()` 永久 `after(500)`，withdrawn 后仍 `after(1000)`，且没有保存/cancel ID；这会造成隐藏 Dashboard 周期唤醒和 destroy 后 Tcl callback。

新增：

```python
self._refresh_after = None
self._closing = False
```

### `open()`

```python
def open(self):
    self.deiconify()
    self.lift()
    self.start_refresh()
```

### `start_refresh()` / `_refresh_tick()`

```python
def start_refresh(self):
    if self._refresh_after is None:
        self._refresh_tick()


def _refresh_tick(self):
    self._refresh_after = None
    if self._closing or not self.winfo_exists():
        return
    if self.state() == "withdrawn":
        return  # hidden 时完全停止

    # 当前各页 refresh
    ...

    self._refresh_after = self.after(500, self._refresh_tick)
```

### `hide_dashboard()`

```python
def hide_dashboard(self):
    self.stop_refresh()
    self.withdraw()
```

### `stop_refresh()`

```python
def stop_refresh(self):
    callback = self._refresh_after
    self._refresh_after = None
    if callback is not None:
        try:
            self.after_cancel(callback)
        except tk.TclError:
            pass
```

### `shutdown()`

```python
def shutdown(self):
    self._closing = True
    self.stop_refresh()
    self.destroy()
```

`WM_DELETE_WINDOW` 改绑定 `hide_dashboard`。

`PetApp.quit()` 在 root destroy 前调用 `dashboard.shutdown()`。

---

## 20. Dashboard Terminal action 统一

当前 Dashboard `_open_terminal()` 直接调用 `monitor.activate_target()` 并复制 toast。

改成：

```python
def _open_terminal(self, key):
    if key:
        self.app.activate_agent(key)
```

所有 UI 最终统一：

```text
Pet body / Bubble / Tray Agent / Dashboard Agent / Fleet menu
→ PetApp.activate_agent(exact key)
```

---

## 21. Activation toast

普通用户不需要知道 binding confidence。

建议：

```text
OK:
  已打开该 Agent 的终端窗口

FOREGROUND_DENIED:
  Windows 未允许将终端置于前台，已闪烁任务栏提醒

AGENT_GONE:
  该 Agent 已退出

NO_BINDING:
  未能定位该 Agent 的终端窗口

STALE_WINDOW:
  原终端窗口已失效，重新识别后仍无法安全打开
```

不再把 AMBIGUOUS 自动翻译为“无法打开”。

---

## 22. Dashboard Terminal 诊断

删除 FALLBACK label。

展示规则：

```text
CONFIRMED + window
→ 已确认

HIGH + window
→ 高置信

AMBIGUOUS + window
→ 候选窗口（可唤起）
→ 证据不足以直接授予 Terminal observation attribution

NONE + window + reason=single-window-fallback
→ 唯一 Terminal 窗口兜底（可唤起）
→ 不作为审批归属依据

window=None
→ 未定位
```

普通 Agents 列表只显示“可打开/未定位”；confidence 放高级诊断。

---

## 23. Monitor 保持简单

`Monitor.activate_target(key)` 不检查 confidence：

```python
if not key:
    return NO_BINDING
if not self.is_live_key(key):
    return AGENT_GONE
return self._terminal_service.activate(key, is_agent_live=self.is_live_key)
```

Terminal Observation 继续只使用 `observation_bindings[key]`，不得重新拿 Window confidence 判断 WAITING。

---

## 24. v4.1.2 已经做对、禁止回退的部分

保持：

```text
SourceProbeSnapshot authoritative 三态
Windows process incarnation
WSL process token
WindowsExitWatcher
windows_enabled=False 真停扫
ExitWatcher idle INFINITE wait
exit drain 后同 tick 重新 snapshot
dynamic file_poll_sec
monitor config hard clamp
StateReducer structured session priority
UIA MTA thread
Notification/TextChanged/StructureChanged
bounded queues/rings/visible read budget
approval TTL
SharedAnimationCache
single AnimationScheduler
Fleet exact slot→Agent routing
Presentation focused/attention 分离
Tray runtime start 不重复写配置
```

---

## 25. 测试：Terminal resolver

建议新增 `tests/test_terminal_window_resolver.py`，把 Window candidate 语义从 UIA recognizer tests 中分开。

必须覆盖：

1. Windows native unique ancestor → `CONFIRMED + hwnd`。
2. native unique window + 多 controls → 仍 `CONFIRMED + hwnd`。
3. native multi-window ancestor → `AMBIGUOUS + window=None`。
4. WSL mutual unique → `HIGH + hwnd`。
5. WSL positive but non-unique → `AMBIGUOUS + hwnd`。**这是 v4.1.3 发布阻断测试。**
6. 只命中低分 `user@`、存在 best positive control → `AMBIGUOUS + hwnd`，保持 v3 行为。
7. no evidence + sole WT window → `NONE + hwnd`，`reason=single-window-fallback`。
8. no evidence + multiple WT windows → `NONE + window=None`。
9. no WT window → `NONE + window=None`。
10. resolver 输出顺序与 Agent 输入顺序无关。

测试 identity 必须注入非零 `process_created`，不再构造 `process_created=0` 的假生产 binding。

---

## 26. 测试：Activation 不看 confidence

修改 `tests/test_terminal_activation.py`：

```text
CONFIRMED + valid window → OK
HIGH      + valid window → OK
AMBIGUOUS + valid window → OK
NONE      + valid window → OK
```

删除 FALLBACK test。

继续覆盖：

```text
window=None → NO_BINDING
Agent dead → AGENT_GONE
stale → refresh exactly once
foreground denied → flash exact HWND
```

增加源检查，防止未来重新写 confidence gate。

---

## 27. 测试：WindowIdentity

新增：

```text
process_created == 0 → validate False
wrong pid             → False
wrong class           → False
wrong create_time     → False
invalid hwnd          → False
all match             → True
```

---

## 28. 测试：Observation 不被 Window wake 放宽

必须覆盖：

```text
Window AMBIGUOUS + hwnd
+ 多 control 无 strict evidence
→ 无 TerminalObservationBinding
```

```text
Window NONE + sole-window hwnd
+ generic profile title
→ 无 TerminalObservationBinding
```

同时保留 Observation 自己 strict HIGH 的正向测试。

---

## 29. 测试：三种桌宠模式

完整矩阵：

| Mode | Bubble single | Bubble double | Body double |
|---|---|---|---|
| SINGLE | no action | exact Agent activate | exact Agent activate |
| AGGREGATE | no action | drawn exact Agent activate | interact only |
| FLEET | no action | own exact Agent activate | own exact Agent activate |

额外：

```text
一次 bubble double-click → activation_count == 1
Aggregate 绘制 A 后 attention 变 B → 点击仍 activate A
Fleet pet-1 / pet-2 分别保持 own key
```

---

## 30. 测试：Tray / Dashboard visibility

必须新增：

```text
Pet visible + tray left
→ 仍 visible
→ reassert called

all Pet hidden + tray left
→ show_all

explicit tray menu hide
→ 仍可以隐藏

Pet visible + open_dashboard
→ logical hidden 仍 False
→ reassert visible window

Fleet pet1 visible / pet2 hidden + open_dashboard
→ pet1 reassert
→ pet2 不 show

Dashboard withdraw
→ _refresh_after is None

Dashboard reopen
→ refresh timer restart

Dashboard shutdown/root destroy
→ 无 invalid command Tcl callback
```

---

## 31. CI ResourceWarning 清理

测试中 `Popen` cleanup 统一：

```python
def terminate_process(proc):
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
```

目标：

```text
unit tests PASS
benchmark PASS
stderr 无 subprocess ResourceWarning
stderr 无 Tcl invalid command name
```

---

## 32. 资源预算

v4.1.3 不新增常驻线程，不新增轮询。

保持：

```text
Tk UI
Monitor core
Process probe
UIA MTA
Windows ExitWatcher
Tray Win32 thread（启用时）
```

Z-order reassert 只发生在：

```text
Tray left recovery
Dashboard open
explicit show
```

Dashboard withdrawn 时 0 refresh timer。

---

## 33. 文件级修改清单

### 必改

```text
agents/models.py
agents/terminal_resolver.py
agents/terminal_service.py
actions/winkeys.py

pet/bubble.py
pet/petwindow.py
pet/petview.py
pet/app.py
pet/dashboard.py

 tests/test_terminal_activation.py
 tests/test_terminal_uia.py 或新增 test_terminal_window_resolver.py
 tests/test_concurrent_activation.py
 tests/test_ui.py
 tests/test_ui_regressions.py
 tests/test_process_watch.py

README.md
SourceLink.md
plan.md
```

### 原则上不改

```text
agents/state.py
agents/discovery.py
Agent session parsers
pet/presentation.py（除测试发现真实 bug）
pet/animator.py
```

---

## 34. 全仓 dead-code gate

产品代码必须无：

```text
TerminalLocation
TabInfo
bind_focused_pane
bind_focused_location
manual_location
set_manual_location
set_manual_binding
focused_location
select_tab
selected_tab
focus_pane
SelectionItemPattern
STALE_TAB
STALE_PANE
BEST_EFFORT
WindowBindingConfidence.FALLBACK
```

输入注入必须无：

```text
SendInput
keybd_event
SendKeys
PostMessage 模拟按键
clipboard injection
Ctrl+Tab 自动切 tab
```

---

## 35. 文档同步

README / SourceLink 必须明确：

> DeskPet 的 Terminal window wake 使用 v3-compatible 被动 heuristic：Windows native 优先使用进程祖先关系；WSL 使用当前可观察 TermControl 标题做 kind/cwd/user/distro 评分。高置信匹配失败但仍有一个 v3 best-positive control 时，DeskPet 可以把它所属的顶层 Terminal window 作为用户显式唤起候选；这不会自动授予 Terminal text/approval attribution。DeskPet 不切换 Tab/Pane，也不发送键盘输入。

交互写明：

```text
非并发 SINGLE：双击桌宠或气泡 → Terminal
并发 AGGREGATE：双击气泡 → Terminal；双击桌宠 → 互动
并发 FLEET：双击各自桌宠或气泡 → 各自 Agent Terminal
```

Tray：

```text
左键显示/恢复桌宠
右键菜单
```

同步清理 README 中旧 `config_version:3`、已删除 `gone_grace_sec` 等陈旧说明。

---

## 36. 推荐实施顺序

1. `models.py` 删除 FALLBACK，明确 confidence != wakeability。
2. `terminal_resolver.py` 恢复 v3 candidate selection。
3. 所有候选 HWND 统一 `window_identity()`。
4. 修 `validate_window(process_created<=0)`。
5. 更新 Terminal resolver/activation tests，先确认 v3 wake 语义。
6. 确认 Observation tests 不退步。
7. 改 `PetWindow` single/double event routing。
8. 固化 SINGLE / AGGREGATE / FLEET matrix。
9. Tray left 改为 restore/show。
10. 增加 Pet no-activate Z-order reassert。
11. Dashboard refresh lifecycle 重构。
12. Dashboard Terminal action 统一走 `PetApp.activate_agent()`。
13. 修 UIA availability latch。
14. 清 subprocess/Tcl CI warnings。
15. README / SourceLink / plan 同步。
16. 全仓 forbidden search。
17. Windows CI + benchmark。
18. Windows 实机 probe + 三模式交互验收。

---

## 37. 推荐提交拆分

```text
Commit 1: v4.1.3 terminal v3 wake semantics
  models / resolver / service / winkeys / terminal tests

Commit 2: v4.1.3 double-click interaction
  bubble / petwindow / petview / interaction tests

Commit 3: v4.1.3 tray dashboard visibility
  SetWindowPos helper / app / dashboard / visibility tests

Commit 4: v4.1.3 lifecycle cleanup
  UIA availability / subprocess cleanup / Tcl lifecycle

Commit 5: v4.1.3 docs and acceptance
  README / SourceLink / plan / CI comments
```

---

## 38. 实机验收

保留 `tools/terminal_window_probe.py`：

```bat
python tools\terminal_window_probe.py --list
python tools\terminal_window_probe.py --validate <HWND>
python tools\terminal_window_probe.py --activate <HWND>
```

建议增加：

```bat
python tools\terminal_window_probe.py --resolve
```

只输出非敏感诊断：

```text
agent kind/source/project basename
binding confidence/reason
hwnd
window pid/create_time/class
valid
```

禁止输出 Terminal raw visible text。

实机判断：

```text
resolver 有 hwnd + probe activate 成功 + DeskPet 不成功
→ activation 链问题

resolver 无 hwnd
→ candidate selection 问题

validate false
→ WindowIdentity 问题

validate true + FOREGROUND_DENIED
→ Windows foreground policy，Flash 即正确 fallback
```

---

## 39. Definition of Done

### Terminal

- [ ] native unique ancestor → CONFIRMED + WindowIdentity
- [ ] native unique ancestor 不受 control 数量影响
- [ ] WSL mutual unique → HIGH + WindowIdentity
- [ ] WSL best-positive non-unique → AMBIGUOUS + WindowIdentity
- [ ] sole WT / no evidence → NONE + WindowIdentity
- [ ] multiple WT / no evidence → NONE + window=None
- [ ] activation 不以 confidence 作为 gate
- [ ] AMBIGUOUS + valid window 能打开
- [ ] NONE + valid sole window 能打开
- [ ] process_created<=0 fail-closed
- [ ] stale 只 retry 一次
- [ ] foreground denied 只 Flash

### Observation

- [ ] AMBIGUOUS window 不直接授予 observation
- [ ] NONE fallback 不直接授予 observation
- [ ] strict CONFIRMED/HIGH observation 正常
- [ ] UIA MTA / TTL / read budget 不回归

### UI interaction

- [ ] SINGLE：bubble double ✓ / body double ✓
- [ ] AGGREGATE：bubble double ✓ / body double=互动
- [ ] FLEET：每只 bubble/body double → own exact Agent
- [ ] bubble single 不 activation
- [ ] 一次 double 只触发一次 activation
- [ ] Aggregate 使用绘制时 exact key

### Tray / Dashboard

- [ ] Tray left 不隐藏 visible Pet
- [ ] Tray left 能恢复 all-hidden Pet
- [ ] 右键菜单仍可显式 hide
- [ ] Dashboard open 不改变 logical hidden
- [ ] visible Pet reassert Z-order
- [ ] hidden Fleet Pet 不被恢复
- [ ] Dashboard withdrawn 无 refresh timer
- [ ] 无 callback-after-destroy

### Resources / CI

- [ ] 不新增线程
- [ ] 不新增轮询
- [ ] windows source off 真停扫
- [ ] ExitWatcher idle INFINITE
- [ ] benchmark 全绿
- [ ] tests 全绿
- [ ] 无 subprocess ResourceWarning
- [ ] 无 Tcl invalid command

### Clean-up

- [ ] 无 FALLBACK
- [ ] 无 BEST_EFFORT
- [ ] 无 manual Terminal binding
- [ ] 无 Tab/Pane activation
- [ ] 无输入注入
- [ ] README / SourceLink / plan 与 v4.1.3 一致

---

## 40. 官方实现依据

### Windows foreground

Microsoft `SetForegroundWindow`  
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow

- 用户显式 Terminal activation 使用 HWND。
- Windows 可以拒绝后台进程抢 foreground。
- DeskPet 不通过键盘/输入注入绕过；拒绝时 `FlashWindowEx`。

### DeskPet Pet Z-order

Microsoft `SetWindowPos`  
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowpos

- `SWP_NOACTIVATE` 可改变 Z-order 而不激活窗口。
- 用于 Dashboard 打开后恢复 Pet Toplevel 层级。
- 不用于 Terminal foreground activation。

### UI Automation threading

Microsoft `Understanding Threading Issues`  
https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading

- desktop-wide UIA 在独立线程。
- UIA thread 不拥有窗口。
- COM MTA。
- event handler add/remove 同一非 UI/MTA thread。

v4.1.2 当前架构符合，v4.1.3 不改变。

### Windows Terminal exact tab 限制

Windows Terminal issue #19783  
https://github.com/microsoft/terminal/issues/19783

上游明确没有稳定公开的外部 `WT_SESSION → existing tab focus` API；UIA Tab title + `SelectionItemPattern.Select()` 和 keyboard simulation 都属于 fragile workaround。因此 v4.1.3 继续只承诺顶层 Window wake。

---

## 41. 最终架构合同

```text
Process Discovery
    ↓
exact AgentInstance / agent_key
    ↓
Session Observation ───────────────┐
                                   │
Terminal UIA Observation ──────────┼→ StateReducer → Snapshot
    │                              │
    └→ strict control attribution ─┘

exact agent_key
    ↓
v3-compatible Window candidate selection
    ↓
TerminalWindowBinding
    ↓
WindowIdentity
    ↓
用户双击
    ↓
validate
    ↓
restore + SetForegroundWindow
    ↓
denied → Flash
```

UI：

```text
SINGLE:
    bubble double → exact Agent → Terminal
    body double   → exact Agent → Terminal

AGGREGATE:
    bubble double → drawn exact Agent → Terminal
    body double   → interact

FLEET:
    pet A bubble/body → Agent A → Terminal candidate A
    pet B bubble/body → Agent B → Terminal candidate B
```

Visibility：

```text
Tray left
→ show/recover
→ never hide an already visible Pet

Dashboard open
→ dashboard lift
→ visible Pets SetWindowPos(... SWP_NOACTIVATE)
→ no logical hidden mutation
```

---

## 42. 一句话验收标准

> **v4.1.3 必须恢复 v3 的“只要被动 resolver 能给出一个候选 Terminal HWND，用户就可以显式尝试把它唤起”的体验，同时继续使用 v4.1.2 的强 `WindowIdentity` 和严格 Terminal observation attribution；并把交互固定为 SINGLE/FLEET 气泡与 body 均双击唤起、AGGREGATE 仅气泡双击唤起，Tray/Dashboard 不再意外隐藏桌宠。**
