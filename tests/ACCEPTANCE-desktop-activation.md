# Codex / ZCode 桌面窗口唤起验收

2026-09-12，本机真实 Codex Desktop 与 ZCode 进程。

| 应用 | 普通窗口 | 任务栏最小化 | 隐藏窗口 |
| --- | --- | --- | --- |
| Codex Desktop | OK，前台 | OK，恢复并前台 | OK，显示并前台 |
| ZCode | OK，前台 | OK，恢复并前台 | OK，显示并前台 |

真实窗口测试使用 Win32 ShowWindow 设置普通、最小化和隐藏状态，再调用生产 DesktopWindowService.activate_host，读取 IsWindowVisible、IsIconic 和 GetForegroundWindow 验证结果。每个应用结束时恢复测试前的显示/最小化状态。隐藏为托盘隐藏模型，未实际点击应用托盘菜单；没有将自动调用宣称为真人鼠标双击。

75 项桌面激活、Win32、终端激活和多宠 UI 回归通过；32 项交互可靠性测试通过。新增覆盖桌宠本体/气泡双击回调传递 exact Agent key、两个桌面应用隐藏主窗口恢复、辅助窗口排除及 SW_RESTORE/SW_SHOW 分支。

双击绑定桌宠本体或对应 Agent 气泡可触发激活。单宠聚合模式下本体保留互动行为，需双击对应 Agent 气泡。只唤起应用窗口，不切换应用内具体任务。Windows 拒绝前台请求时保留任务栏闪烁提示。
