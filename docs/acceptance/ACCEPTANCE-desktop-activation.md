# Codex / DSH / ZCode 桌面窗口唤起验收

## 2026-10-26：真实托盘故障取证与恢复

用户使用重启后的 Python 源码运行 DeskPet。ZCode 从应用托盘隐藏状态被唤起后，画面停在最后一次点击的关闭/最小化按钮，页面无法交互。

故障现场的 ZCode 主窗口仍属于原宿主进程，窗口可见、非 iconic、启用，GUI 线程响应正常，无鼠标捕获。对已核验 HWND 取消残留输入模式后，用户确认仍锁死。单纯延迟前置或检查 `IsWindowVisible` / `GetForegroundWindow` 不能判定页面恢复。

读取已安装 ZCode 3.16.3 的主入口，确认其单实例 handler 会恢复、显示并聚焦原窗口。恢复实验先隐藏已核验的故障主 HWND，再通过原 exe 无参数正常启动，使应用自身执行 Show；短生命周期启动进程退出，原宿主和原窗口保留。用户确认：“已恢复，可以点击”。DeskPet 未发送鼠标、键盘输入，未直接改写 Agent 数据；应用自身启动的正常副作用不属于监听读写。

修改落地后，用户重启 Python 源码版 DeskPet，通过 ZCode 自身关闭按钮隐藏到托盘，再双击桌宠唤起，并确认“恢复正常，可以点击”。这次覆盖了真实应用隐藏生命周期及修改后的产品激活链；连续多次恢复和 Dashboard 原状态保持的人工交互复核仍未完成。

随后用户对 Codex 和 DSH 也通过各应用自身行为隐藏到托盘，再由桌宠双击唤起并点击页面，明确确认两者均正常。Codex 的本机 MSIX 版本为 `26.1021.3705.0`、应用版本为 `26.1021.31345`；DSH 安装版本尚未采集。这三款均完成首次真实托盘交互验收，不代表多轮压力、Dashboard 状态或资源/延迟专项已通过。

Chromium 的 Hide 同时隐藏原生窗口和 Aura 内容窗口，而外部 Win32 Show 只能恢复前者。应用自身 Show 会先恢复内容。（该调研证据 formerly 记录于 SourceLink.md，2026-10-27 起停止维护。）因此真正托盘隐藏必须走已核验的应用入口，不能先用 Win32 Show 暴露窗口后再重开；普通任务栏最小化仍可使用原生恢复。

| 应用 / 验收项 | 当前证据 | 结论 |
| --- | --- | --- |
| ZCode 3.16.3 原故障实例，经自身入口恢复后页面交互 | 原宿主/窗口身份保留、临时启动进程退出、用户确认可以点击 | PASS（恢复实验） |
| ZCode 修订后的 DeskPet 正常双击链 | 重启源码版；ZCode 自身关闭按钮隐藏到托盘；桌宠双击后用户确认恢复正常、可以点击 | PASS（首次人工验收） |
| Codex 真实托盘隐藏/恢复与页面交互 | MSIX `26.1021.3705.0` / 应用 `26.1021.31345`；应用自身隐藏、桌宠双击后用户确认正常 | PASS（首次人工验收） |
| DSH 真实托盘隐藏/恢复与页面交互 | 安装版本未采集；应用自身隐藏、桌宠双击后用户确认正常 | PASS（首次人工验收） |
| 三款应用连续多次真实托盘隐藏/恢复 | 尚未完成多轮压力复核 | NOT_TESTED |
| Dashboard 隐藏/最小化保持、连续双击合并 | 对应自动化回归通过；真实交互尚未复核 | PASS（自动化）/ NOT_TESTED（实机） |
| 真实唤起 P95、RSS、CPU 专项 | 尚未采集完整专项数据 | NOT_TESTED |
| Windows 编译产物完整托盘验收 | 尚未执行 | NOT_TESTED |

修订验收必须通过应用自身菜单/关闭行为进入真实托盘状态，不能用 Win32 `SW_HIDE` 代替。恢复后除验证原 host/HWND/进程代次、可见和非 iconic、前台 HWND，还需人工点击页面并确认交互；记录重复请求是否仅产生一个有界事务、临时启动进程是否退出，以及 Dashboard 是否保持原状态。任何未执行项保持 NOT_TESTED。

## 本次自动化结果

Windows Python 3.12 全量单元测试 **951 tests / 112.724s / OK**；`compileall`、`git diff --check` 和四项阻塞 benchmark 均通过。monitor：5000 ticks / 8.733s / 572.5 tick/s；desktop-sources：5000 ticks / 14.67s，线程 1→1、句柄 226→228；presentation 默认 8 pets、UI architecture 均 PASS。这些 benchmark 结果是自动化负载数据，不代表真实 Agent 托盘唤起的 P95、RSS 或 CPU。

本地报告位于 `.test-artifacts/`：`full-tests-tray-fix.log`、`monitor-tray-fix.json`、`desktop-tray-fix.json`、`presentation-tray-fix.json`、`ui-tray-fix.json`。编译产物、多轮真实托盘压力和资源/延迟专项仍未执行。

## 历史记录的适用范围

2026-09-12 曾对 Codex/ZCode 使用 Win32 `ShowWindow` 设置普通、最小化和隐藏状态，再检查可见性、iconic 和前台 HWND。该实验只能证明原生窗口显隐，不会触发 Electron 的内容隐藏生命周期，**不能作为真实托盘恢复 PASS 的证据**。当时的自动化数量也不代表本次修订已通过完整验收。

双击绑定桌宠本体或对应 Agent 气泡可请求激活。单宠聚合模式下本体保留互动行为，需双击对应 Agent 气泡。只唤起应用窗口，不切换应用内具体任务；Windows 拒绝前台请求时保留任务栏闪烁提示。
