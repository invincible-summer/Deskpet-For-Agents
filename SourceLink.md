# DeskPet V4.1 — SourceLink / Evidence Map

> 审计日期：2026-09-08  
> DeskPet 审计基线：`invincible-summer/DeskPet@af8236d15dc3bfecaa89464e1b77d7f84c2b09be`
>
> 本文件是 V4.1 第 1–4 点的证据链与接口依据。链接分为：
>
> - **实现合同**：Microsoft/Linux 官方 API 文档，可作为代码语义依据；
> - **上游当前行为**：Windows Terminal 当前源码，说明现行实现，但必须 feature-detect，不能把内部源码细节当永久公开 ABI；
> - **能力缺口证据**：Windows Terminal 官方仓库 issue，证明截至当前公开接口仍缺某项能力；issue 本身不是 API 合同；
> - **DeskPet 内部审计证据**：固定到本次审计 commit，便于之后核对 V3→V4.1 修改。

## 1. DeskPet 实现基线

### 1.0 V4.1 当前实现（2026-09-08 完成，工作树）

V4.1 新增/重构（`v4plan.md` 的实施产物；上游依据见后续章节）：

- `agents/process_watch.py`：WindowsExitWatcher（一个阻塞等待线程 +
  OpenProcess(SYNCHRONIZE) + WaitForMultipleObjects，§11 证据）；
- `agents/terminal_service.py`：WindowsTerminalService 统一 facade +
  TerminalActivator exact 激活事务（§8/§9/§10 证据）；
- `agents/models.py`：SourceProbeSnapshot 三态 / WindowIdentity /
  TabInfo / TerminalLocation / BindingOrigin / ActivationCode /
  ActivationResult；
- `agents/monitor.py`：`_commit_exit()` 级联回收；primary/pinned/
  gone_grace 全部移除（V4.1 审计确认 grep 为零）；
- `pet/presentation.py`：PresentationController（focused/attention 分离）；
- `pet/petview.py`：PetView/PetViewManager（N Toplevel 一 interpreter）；
- `pet/animator.py`：SharedAnimationCache/AnimationCursor/
  AnimationScheduler（进程级一份预算）；
- `pet/config.py`：config_version=4 + ConfigSaveResult + backup；
- `pet/autostart.py`：AutostartStatus 三态 + repair（§14 证据）；
- `pet/theme.py` + `pet/widgets.py` + `pet/dashboard.py`：左侧导航
  六页仪表盘（§15 信息架构依据）；
- `tools/terminal_layout_probe.py`：topology 实机 probe。

### 1.1 V3 历史审计基线（superseded，保留作证据）

- V3 Monitor：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/agents/monitor.py
- V3 models：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/agents/models.py
- V3 process discovery：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/agents/discovery.py
- V3 BaseWatcher：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/agents/base.py

历史审计结论（V4.1 已全部修复）：

1. `Monitor.instances/snapshots/bindings` 已是 multi-target，V4.1 不需要重写事实层。
2. `primary_key/primary_target/set_primary/is_bound/_FOLLOW_PRIORITY` 是单 UI presentation 逻辑，应从 Monitor 清除。（V4.1：已删除，迁至 PresentationController）
3. `gone_grace_sec=15` 会在 authoritative absence 后继续保留旧实例。（V4.1：已删除，SourceProbeSnapshot 三态 + `_commit_exit()`）
4. `scan_windows()` 全局 `process_iter` 失败时返回 `[]`，上层可误标 healthy。（V4.1：抛 ProbeUnavailable，authoritative=False）
5. Monitor 只给存在实例的 kind 调 watcher，最后一个实例退出后 BaseWatcher 的 `poll([])` 清理路径不会被调用。（V4.1：每 watcher 每轮 poll，含空列表）

### 1.2 UIA observer

- V3 Terminal UIA：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/agents/terminal_uia.py
- V3 matching：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/agents/matching.py
- V3 Win32 window primitive：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/actions/winkeys.py

V3 已建立、V4.1 必须保留的资源上限：

```text
DELTA_MAX                   2048
RING_MAX                    8192 chars / pane
VISIBLE_MAX                 4096
MAX_PANES                   16
EVENT_QUEUE_MAX             256
UIA_CALL_QUEUE_MAX          32
APPROVAL_TTL                1.5 s
RECHECK_SEC                 0.75 s
PANE_VISIBLE_READ_INTERVAL  0.5 s
GLOBAL_VISIBLE_READ_LIMIT   6 / s
```

### 1.3 V3 UI / config / animation

- PetApp：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/pet/app.py
- PetWindow：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/pet/petwindow.py
- Bubble：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/pet/bubble.py
- Animator：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/pet/animator.py
- Skins：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/pet/skins.py
- Config：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/pet/config.py
- Autostart：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/pet/autostart.py
- Dashboard：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/pet/dashboard.py
- Tray：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/pet/tray.py
- Main：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/main.py

关键审计证据：

- `PetApp` 当前一个 `tk.Tk` = 一个 Pet，Bubble/Animator 都直接挂其上。
- Bubble `details` hitbox 最终调用当前 primary target，而不是携带 target key。
- 每个 Animator 自己有 `cache_bytes=48MB` 与独立 frame pool；Fleet 必须改成共享 cache。
- `Config.save()` 使用同目录 temp + `os.replace`，这个原子替换方向正确，但 OSError 被静默吞掉。
- `PetWindow.set_topmost()` 当前只 `config.set()` 不 `config.save()`。
- `autostart.is_enabled()` 当前只检查 `HKCU Run` value 是否存在，不验证 command 是否等于当前路径。
- Dashboard 当前是 Notebook + Treeview + 独立详情 tab，需要为并发重做信息架构。

### 1.4 V3 benchmark

- Monitor benchmark：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/tests/benchmark_monitor.py

该基准已经验证：

- queue bounded；
- pane bounded；
- terminal ring bounded；
- visible-read budget；
- pane churn subscription 不累积；
- resolver 不依赖输入顺序。

V4.1 不能降低这些约束。

### 1.5 V3 plan

- V3 plan：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/plan.md

V4.1 明确 supersede：

```text
V3 §29–31  primary/auto-follow/pinned
V3 §36–43  single Dashboard/Bubble/window raise
V3 §44–45  config pinned/gone_grace
```

继续保留：

```text
V3 §14–25  UIA passive observer / confidence
V3 §26–28  StateReducer evidence semantics
V3 §32–35  process/session worker与I/O优化
V3 §47–55  resource/privacy/fail-closed
```

## 2. Git ignore / 本地数据路径

- 当前 `.gitignore`：
  https://github.com/invincible-summer/DeskPet/blob/af8236d15dc3bfecaa89464e1b77d7f84c2b09be/.gitignore

当前已经忽略：

```gitignore
assets/pets/*
!assets/pets/README.md
assets/cache/
config.json
```

V4.1 按当前决定不迁 `%LOCALAPPDATA%`。

若新增 last-known-good backup，再加入：

```gitignore
config.json.bak
.deskpet-config-*
```

证据链：

```text
用户要求保留项目内路径
        ↓
现有 .gitignore 已防 config/user skins/cache 上传
        ↓
V4.1 只补 backup/temp pattern
        ↓
不需要引入路径迁移与额外 migration 风险
```

## 3. Windows Terminal 命令行：为什么不能把 `wt.exe` 当 exact identity backend

### 3.1 Microsoft Learn — Windows Terminal command-line arguments

https://learn.microsoft.com/en-us/windows/terminal/command-line-arguments

实现合同确认：

- `--window/-w window-id` 可向指定 Windows Terminal window 发命令；
- `last/0` 是 most recently used window；
- 若指定 window-id/name 不存在，Windows Terminal 可以创建新 window；
- `focus-tab/ft --target/-t` 按**整数 tab index**切换；
- `move-focus` 可以按方向移动 Pane focus。

V4.1 推论：

```text
DeskPet exact HWND ≠ Windows Terminal CLI window-id
-w 0 = MRU，不是 exact target
不存在 id 可能创建 Window
tab-index 会随 reorder 变化
```

所以 `wt.exe` 不作为 V4.1 主 exact activation backend。

## 4. Windows Terminal 当前公开能力缺口

### 4.1 Focus/Activate by WT_SESSION — issue #19783

https://github.com/microsoft/terminal/issues/19783

当前状态：`closed / not_planned`。

issue 明确描述的问题就是：

> external process 无法按 `WT_SESSION` 切换到具体 existing tab。

该 issue 还记录现有 workaround：

```text
UI Automation TabItem
+ SelectionItemPattern.Select()
```

同时指出 title matching 易碎。

用途：

- 证明 V4.1 不能假设 `WT_SESSION -> Tab` 是公开 API；
- 支持“UIA runtime identity + 不按 title 猜”的设计。

注意：issue 是能力缺口证据，不是 API 合同。

### 4.2 Query tabs/metadata — issue #19818

https://github.com/microsoft/terminal/issues/19818

截至本次审计仍 open。

需求本身要求增加：

```text
--list-tabs
--query-state
--active-tab
--query-tabs --detailed
```

当前 limitation 中明确写：

- 无法 programmatically query open terminal tabs；
- 无法获得每 Tab cwd/profile。

用途：

> V4.1 不能设计成“先用 wt 查询所有 tab，再按 WT_SESSION 选”。

### 4.3 Current selected tab index — issue #18692

https://github.com/microsoft/terminal/issues/18692

截至本次审计仍 open。

它明确指出：

```text
focus-tab --target 接受 index
但不知道当前 selected index / tab count
```

用途：

> `focus-tab -t index` 是执行接口，不是 identity/query 接口。

### 4.4 Foreground Window ≠ correct Tab — issue #18429

https://github.com/microsoft/terminal/issues/18429

截至本次审计仍 open。

问题：

> BringWindowToTop/foreground window 本身不会恢复正确 active tab。

用途：

> V3 的 `raise_terminal(hwnd)` 对 V4 多 Tab 不够，必须 Window + Tab + Pane。

## 5. Windows Terminal 当前源码：selected Tab content 的 UI 树行为

### 5.1 `TerminalPage::_InitializeTab`

https://github.com/microsoft/terminal/blob/main/src/cascadia/TerminalApp/TabManagement.cpp

当前源码在设置：

```cpp
_tabView.SelectedItem(tabViewItem);
```

前的注释明确说明，这会触发 `TabView::SelectionChanged`，并在响应过程中把该 Tab 的 terminal XAML control attach 到 XAML root。

这是**当前上游行为证据**，不是公开 ABI。

V4.1 推论：

```text
所有 TabItem 可作为 topology 元素跟踪
但不能假设 inactive Tab 的 TermControl 都同时在 root
```

因此：

- 不后台逐 Tab 自动切换；
- 只观察 current selected Tab 的 TermControl；
- 用户自然切换时逐步学习；
- cold start 同质 background tabs 保持 ambiguous。

### 5.2 Windows Terminal WT_SESSION 来源

当前源码：

https://github.com/microsoft/terminal/blob/main/src/cascadia/TerminalConnection/ConptyConnection.cpp

其中 Terminal 为 session environment 设置 `WT_SESSION` GUID。

用途：

- `AgentInstance.wt_session` 继续是有价值的强 hint；
- 但没有公开 reverse-query，所以不是直接 Tab locator。

### 5.3 Current `focus-tab` / `focus-pane` parser

https://github.com/microsoft/terminal/blob/main/src/cascadia/TerminalApp/AppCommandlineArgs.cpp

当前源码包含：

```text
focus-tab
focus-pane
move-focus
```

用途：

- 证明 Windows Terminal 内部有这些 focus action；
- 同时说明 DeskPet 的障碍不是“Terminal 不能切”，而是外部没有可靠 identity mapping。

## 6. UI Automation Threading — 单 MTA 是正确架构

Microsoft Learn：

https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading

实现合同：

- 与 desktop 上所有 UI element 交互的 UIA client 应把 UIA calls 放独立 thread；
- 该 thread 不应拥有 window；
- 应为 COM MTA；
- event handler add/remove 应在非 UI/MTA thread，并在同一 thread 管理；
- 不建议多个 thread 同时 add/remove UIA event handlers。

V4.1 证据链：

```text
V3 已有一个 UiaBackend MTA
        ↓
Tab discovery/select/pane focus 继续封送到该线程
        ↓
不能为 Tab/Fleet/Pet 再建 UIA thread
```

## 7. UIA RuntimeId — 只能是运行期 identity

Microsoft Learn：

https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-getruntimeid

官方语义：

- only guaranteed unique within the desktop UI where generated；
- identifiers may be reused over time；
- format may change；
- treat as opaque and use only for comparison。

V4.1 直接合同：

```text
Tab RuntimeId / Pane RuntimeId
    = runtime-only

禁止：
    写入 config
    跨 DeskPet restart 恢复
    当作永久 UUID
```

## 8. UIA Tab selection

### 8.1 SelectionItemPattern.Select

Microsoft Learn：

https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationselectionitempattern-select

官方语义：

> Clears any selected items and then selects the current element.

V4.1 用法：

```text
exact TabItem RuntimeId
    ↓
SelectionItemPattern.Select()
```

不使用：

```text
Ctrl+Tab
Ctrl+1
SendInput
keybd_event
```

### 8.2 SelectionItemPattern interface

https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nn-uiautomationclient-iuiautomationselectionitempattern

确认可读取：

```text
CurrentIsSelected
CurrentSelectionContainer
Select()
```

### 8.3 Selection event ID

Microsoft Learn：

https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-event-ids

`UIA_SelectionItem_ElementSelectedEventId = 20012`。

V4.1 用法：

```text
用户自然切 Tab
    ↓
selection event
    ↓
topology dirty
    ↓
bounded current-tab refresh
```

而不是 200ms 枚举所有 Tab。

## 9. UIA Pane focus

Microsoft Learn：

https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-setfocus

官方语义：

> Sets keyboard focus to this UI Automation element.

V4.1 使用顺序：

```text
Select exact Tab
        ↓
restore / foreground exact HWND
        ↓
确认 foreground 成功
        ↓
SetFocus(exact pane)
```

不把 `SetFocus` 当绕过 Windows foreground policy 的手段。

## 10. Windows foreground policy

Microsoft Learn：

https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow

官方说明：

- Windows restricts which processes can set foreground；
- 即使满足条件也可能被拒绝；
- 当用户正在使用另一个 window，应用不能任意强制抢 foreground；
- 可用 taskbar flashing 通知用户。

V4.1 合同：

```text
DeskPet 保证“选的是正确 Window/Tab/Pane”
≠ 保证 OS 一定允许抢前台

foreground denied:
    FlashWindowEx
    return FOREGROUND_DENIED
```

不使用输入模拟绕过。

## 11. Windows process exit event

### 11.1 Process object becomes signaled on termination

Microsoft Learn：

https://learn.microsoft.com/en-us/windows/win32/procthread/terminating-a-process

官方语义：

> When a process terminates, the process object becomes signaled, releasing threads waiting for termination.

这是 `WindowsExitWatcher` 的核心依据。

### 11.2 OpenProcess

Microsoft Learn：

https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocess

用途：

- 获取 local process object handle；
- V4.1 只申请 wait 所需最小权限；
- 不申请 VM_READ/VM_WRITE。

### 11.3 WaitForMultipleObjects

Microsoft Learn：

https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitformultipleobjects

官方语义：

- 等待多个 kernel object 进入 signaled state；
- process handle 是支持的 object 类型；
- 最大数量 `MAXIMUM_WAIT_OBJECTS`。

V4.1：

```text
一个 WindowsExitWatcher thread
+ ≤16 Agent process handles
+ one control event
```

远小于系统上限，不需要 per-Agent thread。

## 12. Linux / WSL process identity

### 12.1 `/proc/PID/stat`

Linux man-pages：

https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html

关键字段：

- process state；
- PPID；
- process group/session；
- TTY/foreground process group；
- `starttime`。

Process state 包括：

```text
Z zombie
X/x dead
```

V4.1：

- `starttime` 继续作为 WSL process incarnation token；
- `Z/X/x` 不作为 live Agent；
- 不因为 `S/T/D` 等“当前没执行 CPU”而判退出。

### 12.2 `/proc/PID/cwd`

https://man7.org/linux/man-pages/man5/proc_pid_cwd.5.html

继续作为 Agent cwd/project evidence。

## 13. WSL lifecycle：fresh running inventory

Microsoft Learn：

https://learn.microsoft.com/en-us/windows/wsl/wsl-config

官方文档说明：

- WSL subsystem 在最后实例关闭后仍可能约 8 秒才完全停止；
- `wsl --list --running` 可检查当前仍 running 的 distribution；
- `wsl --terminate <distro>` 可立即终止指定 distribution。

Microsoft Learn Basic Commands：

https://learn.microsoft.com/en-us/windows/wsl/basic-commands

V4.1 继续 V3.1.2 规则：

```text
只有本轮 fresh inventory 确认 Running 的 distro
才允许进入 distro 做 ps/metadata
```

枚举失败：

```text
无法读取 ≠ distro stopped
```

不宣布旧 Agent 退出。

## 14. Autostart — HKCU Run

Microsoft Learn：

https://learn.microsoft.com/en-us/windows/win32/setupapi/run-and-runonce-registry-keys

官方语义：

- `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 每次该用户登录时运行；
- value 是 command line；
- command line 最大 260 chars。

V4.1 因此继续 HKCU Run，不引入 admin/service。

需要修的不是机制，而是当前 DeskPet 的 verification：

```text
旧：
value 存在 → enabled

V4.1：
value 存在
AND command == 当前 expected
AND exe/main.py path exists
AND length合法
→ healthy
```

## 15. Dashboard 信息架构依据

DeskPet 仍用 ttk，以下 Microsoft 文档只作为**信息架构/可访问性设计指导**，不是要求换 WinUI。

### 15.1 NavigationView

https://learn.microsoft.com/en-us/windows/apps/design/controls/navigationview

当前 Microsoft 指南说明 NavigationView 适合：

- 顶级导航；
- 多个 navigation categories；
- left/top adaptive navigation。

V4.1 ttk Dashboard 模仿其信息架构：

```text
Overview
Agents
Deskpet & Appearance
Monitoring & Privacy
Diagnostics
Settings
```

不引入 WinUI runtime。

### 15.2 App settings layout

https://learn.microsoft.com/en-us/windows/apps/design/app-settings/guidelines-for-app-settings

当前指南：

- Navigation pane layout 时 Settings 适合放底部；
- settings content 使用可滚动、受限最大宽度；
- 约 1000–1100 px 可读宽度。

V4.1 Dashboard 默认 1120×760，与此阅读密度接近。

### 15.3 Accessible text

Microsoft Learn：

https://learn.microsoft.com/en-us/windows/apps/design/accessibility/accessible-text-requirements

V4.1 UI 规则：

- 普通文字按 WCAG/Windows guidance 保持足够 contrast；
- 不只靠颜色表达 Waiting/Error/Working；
- 状态同时有 text/icon/accent。

## 16. 多显示器 / DPI（Fleet 持久位置）

### 16.1 MonitorFromWindow / MonitorFromPoint

Microsoft Learn：

https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-monitorfromwindow

### 16.2 GetMonitorInfo

https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getmonitorinfow

用途：

- 获取 Pet 所在 monitor；
- 用 work area 保存/恢复相对 placement；
- monitor 消失时 clamp 到可见 work area。

### 16.3 GetDpiForWindow

https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getdpiforwindow

用途：

- Fleet 每个 Toplevel 可以位于不同 DPI monitor；
- 每 Pet 缓存自己的 DPI；
- 只在 create/move/debounced geometry change 时刷新；
- DPI 变化才请求新 skin pixel height。

## 17. V4.1 证据链总表

| 设计判断 | 证据强度 | 依据 | 实现后果 |
|---|---|---|---|
| UIA 必须一个独立 MTA | 官方 API 指南 | UIA threading | 继续 V3 单 UiaBackend |
| Tab 可用 SelectionItem.Select | 官方 API | SelectionItemPattern | 不发送键盘 |
| RuntimeId 不能持久化 | 官方 API | GetRuntimeId | Tab/Pane runtime-only |
| Pane 可用 UIA SetFocus | 官方 API | SetFocus | foreground成功后 exact pane focus |
| OS 可能拒绝抢前台 | 官方 API | SetForegroundWindow | Flash + typed failure |
| Windows process可阻塞等待退出 | 官方 API | process signaled + WaitForMultipleObjects | 一线程 exit watcher |
| Run key command有260字符限制 | 官方 API | Run/RunOnce | autostart预校验 |
| `focus-tab` 只有 index | 官方 WT docs | command-line args | index只能 hint |
| WT_SESSION→Tab query缺失 | 官方仓库 issue | #19783/#19818/#18692 | UIA topology + ambiguous |
| Bring Window不等于正确Tab | 官方仓库 issue | #18429 | Window→Tab两层不可省 |
| selected Tab content attach XAML root | WT当前源码 | TabManagement.cpp | 不扰动 background tabs |
| V3 UIA 已有有界 observer | DeskPet固定commit | terminal_uia.py | 扩展而不重写 |
| V3 primary在 Monitor | DeskPet固定commit | monitor.py | presentation拆层 |
| V3 config save可静默失败 | DeskPet固定commit | config.py | commit result/backup |
| V3 Animator cache是per-instance | DeskPet固定commit | animator.py | Fleet共享cache |
| config/skins/cache已gitignore | DeskPet固定commit | .gitignore | 暂不迁LocalAppData |

## 18. 需要实机验证、不能伪装成已知事实的点

V4.1 剩余真正需要 compatibility probe 的主要问题只有这些：

1. Windows Terminal 当前版本中，Tab reorder 前后 `TabItem RuntimeId` 的实际稳定性；
2. inactive Tab detach、再 selected/attach 后 `TermControl RuntimeId` 是否保持；
3. split pane 中 `TermControl.SetFocus()` 的实际焦点行为；
4. Terminal 在不同版本/设置（tabs in titlebar、focus mode、fullscreen）下 TabItem UIA tree 的结构差异。

这些都不能写成永久假设。

### 18.1 2026-09-08 实机 probe 结果（tools/terminal_layout_probe.py）

在真实 Windows Terminal（两同 title "Ubuntu" Tab）上验证：

* `discover_layout()`：Window（HWND/PID/create_time/class）+ TabItem
  （index/RuntimeId/selected/name）+ TermControl（仅 selected tab）全部正常；
* 两个同 title Tab 拥有不同 RuntimeId → 同 title 不影响 exact 定位；
* `SelectionItemPattern.Select()` 对两个 Tab 均成功且 selected 验证通过；
* `TermControl.SetFocus()` 在窗口非前台时调用成功但焦点不转移 →
  证实 v4plan §5.9 "foreground 成功后才 SetFocus" 顺序是必要的；
* 切换 Tab 后 TermControl 是新 RuntimeId 实例（不同 Tab 的 pane rid
  不同）→ 证实 sole-pane revalidation/rebind 设计是必要的。

### 18.2 2026-09-08 绑定评分实机验证（V4.1.1 修复）

在真实 WT（私有窗口 + OSC 固定标题）上验证的补充事实：

* **TermControl 的 UIA Name 可能停留在 profile 名**（实测为
  "Ubuntu"），而 TabItem 标题已带 shell 设置的
  "user@host: ~/path" → 标题评分必须把 TabItem 标题作为第二条证据
  （`_pane_titles`，两者取高分，互斥唯一门槛不变）；
* 裸 basename 匹配在真实环境必然假命中：用户名==家目录名时
  （invincible + /home/invincible），每个 "user@host: ~/x" 标题都
  含该 basename → 必须按 `~/a/b` 路径标记归一化比对；
* `wt -w _new` 不总是新开窗口（复用已有窗口），关闭多 Tab 窗口会弹
  确认框 → 探针/自动化需自记录基线窗口集；
* 端到端（真实 UIA + 私有 WT 窗口 + 合成 WSL Agent）：Tab 标题证据 →
  `cwd+user@` 评分 3 → HIGH → `TerminalActivator` 返回 OK 且前台
  正确（2026-09-08 实测通过）。

实现按上述事实 feature-detect + fail-closed，无键盘模拟。

实现必须：

```text
feature detect
→ use exact capability
→ refresh once
→ safe fallback
→ ambiguous/fail
```

而不是：

```text
版本不一样
→ title guess
→ keyboard guess
```

## 19. V4.1 不允许使用的“替代方案”

即使看似能工作，也不进入主实现：

```text
wt -w 0                   # MRU，不 exact
focus-tab by remembered index
Ctrl+Tab / Ctrl+number
SendInput / keybd_event
title substring as exact identity
后台自动轮询并切遍所有 tabs
ReadProcessMemory 取 Terminal internals
持久化 UIA RuntimeId
每 Agent 开一个 WSL helper
每 Pet 开一份 Monitor/UIA
OCR/screenshot terminal
```

原因分别由本文件前述 API/上游限制/DeskPet 轻量与安全合同支持。

## 20. 文档更新规则

完成 V4.1 后：

- `v4plan.md` 是 V4.1 的实施合同；
- `SourceLink.md` 用本文件替换/更新，保留每项依据的“权威等级”；
- `README.md` 只写用户可见行为，不把内部 RuntimeId/PID 当产品概念；
- 如果未来 Windows Terminal 发布正式 session/tab query API：
  1. 先在 SourceLink 记录正式 API；
  2. 新增 backend；
  3. 保留 UIA fallback；
  4. 不改变 `TerminalService.activate(target)` 上层接口。
