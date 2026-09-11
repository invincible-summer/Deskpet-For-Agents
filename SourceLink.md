# DeskPet v4.3.1 — SourceLink

## DeskPet implementation baseline

- [DeskPet v4.3.0 release commit](https://github.com/invincible-summer/Deskpet-For-Agents/commit/39941c2815581eadf948ab8e4e90d82eb1c52207) — 当前审计/发布基线（v4.3.1 DP43 修复计划 plan.md §1 所固定的代码基线 `39941c2`）。
- [README.md @ v4.3.0](https://github.com/invincible-summer/Deskpet-For-Agents/blob/39941c2815581eadf948ab8e4e90d82eb1c52207/README.md) — 当前产品合同、安装说明与 Agent 支持范围。
- [agents/discovery.py @ v4.3.0](https://github.com/invincible-summer/Deskpet-For-Agents/blob/39941c2815581eadf948ab8e4e90d82eb1c52207/agents/discovery.py) — Windows/WSL 进程发现、canonicalization 与现有 ps 字段。
- [agents/monitor.py @ v4.3.0](https://github.com/invincible-summer/Deskpet-For-Agents/blob/39941c2815581eadf948ab8e4e90d82eb1c52207/agents/monitor.py) — SourceProbeSnapshot 合并、退出级联、状态融合和线程架构。
- [agents/process_watch.py @ v4.3.0](https://github.com/invincible-summer/Deskpet-For-Agents/blob/39941c2815581eadf948ab8e4e90d82eb1c52207/agents/process_watch.py) — Windows process handle 阻塞退出观察器。
- [agents/terminal_uia.py @ v4.3.0](https://github.com/invincible-summer/Deskpet-For-Agents/blob/39941c2815581eadf948ab8e4e90d82eb1c52207/agents/terminal_uia.py) — 单 MTA UIA observer、事件订阅、可见文本读取预算。
- [agents/terminal_resolver.py @ v4.3.0](https://github.com/invincible-summer/Deskpet-For-Agents/blob/39941c2815581eadf948ab8e4e90d82eb1c52207/agents/terminal_resolver.py) — window-only 与 observation-only 双绑定解析。
- [pet/app.py @ v4.3.0](https://github.com/invincible-summer/Deskpet-For-Agents/blob/39941c2815581eadf948ab8e4e90d82eb1c52207/pet/app.py) — tray menu、Agents 唤醒菜单和应用生命周期。
- [pet/petwindow.py @ v4.3.0](https://github.com/invincible-summer/Deskpet-For-Agents/blob/39941c2815581eadf948ab8e4e90d82eb1c52207/pet/petwindow.py) — 桌宠 Tk popup menu 的当前实现。
- [pet/animator.py @ v4.3.0](https://github.com/invincible-summer/Deskpet-For-Agents/blob/39941c2815581eadf948ab8e4e90d82eb1c52207/pet/animator.py) — SharedAnimationCache、单 scheduler 与缓存预算。
- [pet/icon.py @ v4.3.0](https://github.com/invincible-summer/Deskpet-For-Agents/blob/39941c2815581eadf948ab8e4e90d82eb1c52207/pet/icon.py) — 程序化原创小猫 renderer，可作为公开发行 fallback skin 源。
- [pet/autostart.py @ v4.3.0](https://github.com/invincible-summer/Deskpet-For-Agents/blob/39941c2815581eadf948ab8e4e90d82eb1c52207/pet/autostart.py) — HKCU Run 健康校验与当前解释器路径生成。
- [tests/benchmark_monitor.py @ v4.3.0](https://github.com/invincible-summer/Deskpet-For-Agents/blob/39941c2815581eadf948ab8e4e90d82eb1c52207/tests/benchmark_monitor.py) — Monitor/UIA 有界队列和资源预算基准。

## v4.3.1 reliability closure（DP43-R14..R22）上游依据

- [Notifications and the Notification Area](https://learn.microsoft.com/en-us/windows/win32/shell/notification-area) — 任务栏通知区 context menu 的官方交互模型（TrackPopupMenu 前的前台准备、菜单结束后 NIM_SETFOCUS）。
- [Shell_NotifyIconW](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/nf-shellapi-shell_notifyiconw) — NIM_ADD/NIM_MODIFY/NIM_DELETE/NIM_SETFOCUS/NIM_SETVERSION 消息语义与返回值合同；NIM_SETVERSION 必须在每次 NIM_ADD 后调用。
- [NOTIFYICONDATAW](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ns-shellapi-notifyicondataw) — NOTIFYICON_VERSION_4 回调组成：LOWORD(lParam)=通知事件（WM_CONTEXTMENU/NIN_SELECT/NIN_KEYSELECT/鼠标消息）、HIWORD(lParam)=icon id、wParam=锚点坐标；NIF_SHOWTIP 保留标准 tooltip。
- [TrackPopupMenuEx](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-trackpopupmenuex) — TPM_RETURNCMD 返回所选 command id（0=取消）；native HMENU 生命周期。
- [EndMenu](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-endmenu) — 结束“调用线程”的活动菜单（WM_CANCELMODE 仅为文档回退路径）；托盘 worker 在 wndproc 收到 WM_CANCELMODE/WM_APP_QUIT 时自行调用，保证菜单真实跟踪被授予前台时 request_stop 仍在 shutdown 预算内到达 STOPPED。
- [DestroyMenu](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-destroymenu) — root menu 销毁递归释放 submenus；菜单资源不依赖 GC。
- [DestroyIcon](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-destroyicon) — 文件加载（非 shared）icon 由调用方释放；shared icon（LoadIconW 所得）不得 Destroy。
- [WM_CONTEXTMENU](https://learn.microsoft.com/en-us/windows/win32/menurc/wm-contextmenu) — v4 Shell 对鼠标右键与键盘 context selection 统一发送 WM_CONTEXTMENU（替代 legacy WM_RBUTTONDOWN/UP 组合）的依据。
- [CreateWindowExW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-createwindowexw) / [GetModuleHandleW](https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-getmodulehandlew) / [LoadImageW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-loadimagew) — tray worker 隐藏窗口与 HICON 的 64 位 ABI 原型声明依据（handle 返回值不得依赖 ctypes 默认 C int restype）。
- [Python 3.12 ctypes](https://docs.python.org/3.12/library/ctypes.html) — 未声明 restype 的 foreign function 默认按 C int 处理（指针截断风险）。
- [Tk `tk_popup`](https://www.tcl-lang.org/man/tcl9.1/TkCmd/popup.html) — Tk popup menu 的 traversal/lifecycle 入口；DP43-R14 的 deferred-after-teardown 菜单语义基础。

## Historical baselines

- [DeskPet v4.2.2 audit baseline commit](https://github.com/invincible-summer/Deskpet-For-Agents/commit/3d0ab8e515cd79fa703ee62138789b00f3c8f474) — v4.2.3 计划所依据的固定代码基线（历史追溯）。

## Windows Terminal / Win32 / UI Automation

- [Windows Terminal command-line arguments](https://learn.microsoft.com/en-us/windows/terminal/command-line-arguments) — `wt` 的 window/tab 命令能力与不能作为 exact runtime identity 的边界。
- [microsoft/terminal#19783](https://github.com/microsoft/terminal/issues/19783) — 外部进程缺少稳定 `WT_SESSION → existing tab` 激活接口的能力缺口。
- [microsoft/terminal#19818](https://github.com/microsoft/terminal/issues/19818) — Windows Terminal 缺少公开 tab/state query 接口的能力缺口。
- [microsoft/terminal#18692](https://github.com/microsoft/terminal/issues/18692) — `focus-tab` 有 index 执行接口但缺乏可靠 selected-tab query。
- [microsoft/terminal#18429](https://github.com/microsoft/terminal/issues/18429) — 仅 foreground 顶层窗口不等价于恢复正确 Tab。
- [Windows Terminal TabManagement.cpp](https://github.com/microsoft/terminal/blob/main/src/cascadia/TerminalApp/TabManagement.cpp) — 当前 Tab selection/XAML content attach 行为的上游实现参考。
- [Windows Terminal ConptyConnection.cpp](https://github.com/microsoft/terminal/blob/main/src/cascadia/TerminalConnection/ConptyConnection.cpp) — `WT_SESSION` 等 session environment 的上游实现来源。
- [Windows Terminal AppCommandlineArgs.cpp](https://github.com/microsoft/terminal/blob/main/src/cascadia/TerminalApp/AppCommandlineArgs.cpp) — 当前 focus-tab/focus-pane/move-focus 命令解析实现。
- [Windows Terminal README / OpenConsole](https://github.com/microsoft/terminal) — OpenConsole/ConPTY 与 Windows Terminal 的总体进程架构说明。
- [Default Terminal spec #492](https://github.com/microsoft/terminal/blob/main/doc/specs/%23492%20-%20Default%20Terminal/spec.md) — 说明 terminal/console server 解耦以及依赖 process-tree spelunking 的可靠性风险。
- [ClosePseudoConsole](https://learn.microsoft.com/en-us/windows/console/closepseudoconsole) — 关闭 ConPTY 会向连接客户端发送 CTRL_CLOSE_EVENT，但客户端可继续存活一段时间。
- [UI Automation threading](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading) — UIA client 使用独立非 UI MTA、同线程管理 event handler 的官方指导。
- [IUIAutomationElement::GetRuntimeId](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-getruntimeid) — RuntimeId 只适合作 runtime-only opaque identity。
- [IUIAutomationSelectionItemPattern::Select](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationselectionitempattern-select) — v4.1 历史 Tab 选择方案的公开 UIA 语义，v4.1.2 后已弃用为产品导航合同。
- [IUIAutomationSelectionItemPattern](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nn-uiautomationclient-iuiautomationselectionitempattern) — v4.1 历史 SelectionItemPattern 能力参考。
- [UI Automation Event IDs](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-event-ids) — UIA 标准事件 ID 参考。
- [IUIAutomationElement::SetFocus](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-setfocus) — v4.1 历史 Pane focus 方案语义，当前 window-only 激活不依赖它。
- [SetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow) — Windows 前台抢占策略与可能被 OS 拒绝的合同。
- [FlashWindowEx](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-flashwindowex) — foreground 被拒时的非侵入任务栏提醒能力。
- [GetWindowThreadProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowthreadprocessid) — HWND 属主 PID 校验依据。
- [GetClassNameW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getclassnamew) — WindowIdentity 的窗口类校验依据。
- [SetWindowPos](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowpos) — DeskPet 自身 Z-order 调整且配合 SWP_NOACTIVATE 的依据。
- [Terminating a Process](https://learn.microsoft.com/en-us/windows/win32/procthread/terminating-a-process) — process 终止后 process object 变 signaled 的官方语义。
- [OpenProcess](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocess) — WindowsExitWatcher 获取 SYNCHRONIZE process handle 的接口。
- [WaitForMultipleObjects](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitformultipleobjects) — 多 process handle 阻塞等待以及 pending wait 时关闭 handle 属 undefined behavior 的合同。
- [Run and RunOnce Registry Keys](https://learn.microsoft.com/en-us/windows/win32/setupapi/run-and-runonce-registry-keys) — HKCU Run 自启动和 260 字符 command-line 限制。
- [MonitorFromWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-monitorfromwindow) — 多显示器窗口归属参考。
- [MonitorFromPoint](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-monitorfrompoint) — 多显示器位置恢复参考。
- [GetMonitorInfoW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getmonitorinfow) — monitor work area 读取依据。
- [GetDpiForWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getdpiforwindow) — 每 Pet/窗口 DPI 读取依据。

## WSL / Linux process semantics

- [/proc/PID/stat](https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html) — PPID、process group、session、controlling terminal、TPGID、starttime 和 dead/zombie state 的定义。
- [/proc/PID/cwd](https://man7.org/linux/man-pages/man5/proc_pid_cwd.5.html) — WSL Agent cwd/project evidence 的内核接口。
- [ps(1)](https://man7.org/linux/man-pages/man1/ps.1.html) — `tty/tpgid/stat` 等 process table 字段的用户态定义。
- [credentials(7)](https://man7.org/linux/man-pages/man7/credentials.7.html) — session、process group、controlling terminal 与 foreground/background group 语义。
- [tty(4)](https://man7.org/linux/man-pages/man4/tty.4.html) — controlling terminal、detach 与 hangup 相关终端语义。
- [WSL basic commands](https://learn.microsoft.com/en-us/windows/wsl/basic-commands) — `wsl --list --running` 等发行版生命周期命令。
- [WSL configuration](https://learn.microsoft.com/en-us/windows/wsl/wsl-config) — WSL VM/distribution 生命周期与停止行为参考。

## Agent upstream references

- [Codex protocol.rs](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs) — Codex `task_started/turn_started`、collaboration mode 等当前 rollout/protocol 类型来源。
- [openai/codex#14962](https://github.com/openai/codex/issues/14962) — terminal close 后 Codex wrapper/runtime orphan、PTY revoked/EIO spin 的上游缺陷证据。
- [anthropics/claude-code#21287](https://github.com/anthropics/claude-code/issues/21287) — terminal close 后 Claude orphan 且 `PPID=1/TTY=??` 的直接复现证据。
- [anthropics/claude-code#53037](https://github.com/anthropics/claude-code/issues/53037) — `/clear` 后 PID registry sessionId stale、旧 JSONL 冻结而新 JSONL 增长的直接证据。
- [Kimi data locations](https://github.com/MoonshotAI/kimi-code/blob/main/docs/en/configuration/data-locations.md) — `$KIMI_CODE_HOME`、session_index 与 `sessions/<workDirKey>/<sessionId>` 布局。
- [Kimi interactionOps.ts](https://github.com/MoonshotAI/kimi-code/blob/main/packages/agent-core-v2/src/agent/interaction/interactionOps.ts) — durable `interaction.request/resolved` 及 approval/question/user_tool schema。
- [Kimi promptOps.ts](https://github.com/MoonshotAI/kimi-code/blob/main/packages/agent-core-v2/src/agent/prompt/promptOps.ts) — durable `prompt.accepted` 当前 schema。
- [Kimi session-store.ts](https://github.com/MoonshotAI/kimi-code/blob/main/apps/vis/server/src/lib/session-store.ts) — canonical `<sessionDir>/state.json` 与 session directory 读取逻辑。
- [pi session format](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/session.md) — pi v3 JSONL 路径、session version、assistant/toolResult message 结构。
- [pi AI types](https://github.com/badlogic/pi-mono/blob/main/packages/ai/src/types.ts) — `stopReason=stop|length|toolUse|error|aborted` 与独立 ToolResultMessage 类型。
- [pi settings](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/settings.md) — pi sessionDir 与 `PI_CODING_AGENT_SESSION_DIR` 配置来源。

## Tk / Python / public distribution

- [Tk `tk_popup`](https://www.tcl-lang.org/man/tcl9.1/TkCmd/popup.html) — popup menu 及其 cascaded children 的标准 traversal/lifecycle 入口。
- [Python venv 3.12](https://docs.python.org/3.12/library/venv.html) — Windows repo-local `.venv/Scripts` 与无需 activate 即可调用环境解释器的官方说明。
- [Python on Windows](https://docs.python.org/3/using/windows.html) — Windows Python/launcher 发现与版本选择参考。
- [Windows `start`](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/start) — `Start-Desktop.bat` 的 `start "" /D ...` 命令语义与 quoted title 规则。
- [GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases) — tag-based public release 和 release asset 的官方工作流。
- [GitHub source archives](https://docs.github.com/en/repositories/working-with-files/using-files/downloading-source-code-archives) — clean source archive/reproducibility 与 release 的区别。

## UI guidance retained by the project

- [NavigationView guidance](https://learn.microsoft.com/en-us/windows/apps/design/controls/navigationview) — Dashboard 左侧顶级导航的信息架构参考，不要求迁移 WinUI。
- [App settings guidelines](https://learn.microsoft.com/en-us/windows/apps/design/app-settings/guidelines-for-app-settings) — Dashboard settings 页面布局与可滚动宽度参考。
- [Accessible text requirements](https://learn.microsoft.com/en-us/windows/apps/design/accessibility/accessible-text-requirements) — 状态文字可读性、contrast 与不只依赖颜色表达状态的依据。
