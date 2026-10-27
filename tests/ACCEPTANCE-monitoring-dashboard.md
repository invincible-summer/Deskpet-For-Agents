# 2026-10-27 监听与仪表盘源码验收

环境：Windows，仓库 `.venv/Scripts/python.exe`，Python 3.12.14。未发布版本或
编译产物；下述合成数据和自动化结果不能替代真实 Agent 交互验收。

## 自动化

所有实现完成后集中运行完整套件：**916 tests / 112.442s / OK**。测试清理前为
41 个模块、951 项；现为 40 个模块、916 项。删除重复概况页、重复菜单/仪表盘
生命周期、旧架构源码字符串断言；动画/摘要微用例合并为行为合同。真实进程
churn 从 500 轮收敛至 24 轮，保留注册/退出/注销和句柄泄漏检查。

最终静态审查补齐工具包装、数组、Kimi/pi 摘要边界，相关 167 项通过；包装参数
顺序和命令文本的最后收口由摘要/活动 16 项复验通过。截图发现的长文本、高 DPI
侧栏和状态芯片裁切修正后，仪表盘/组件 24 项逐项验证通过。未重复运行完整套件。
最终 `compileall` 与 `git diff --check` 通过。

四项既有 blocking benchmark 均 PASS：

| 门禁 | 结果 |
| --- | --- |
| Monitor | 5000 ticks，11.152s；队列/读取预算有界，关闭后扫描增量为 0 |
| Desktop sources | 5000 ticks，16.1s；线程 1→1，句柄 197→199；busy 检出及恢复通过 |
| Presentation | 8 pets；共享缓存、调度器与 view/cursor 回收通过 |
| UI architecture | 当前页刷新、200 次 Configure 合并、按钮提交时延 <50ms、单 worker 和关闭期限通过 |

本地日志与 JSON 报告位于 `.test-artifacts/20261027-*`，其中完整套件为
`20261027-unit.log`，相关复验为 `20261027-focused.log` / `20261027-summary.log`。
这是自动化负载数据，不是实机 RSS/CPU 或 Agent 激活延迟结论。

## 界面

使用纯合成会话验证：工作台与外观在 1120×760、860×560 逻辑尺寸正常；详情
长文本明确省略、最长中文状态完整、侧栏状态无高 DPI 裁切。页面 hover/focus
不改变控件坐标或大小，当前页与 retained 行复用检查通过。

气泡显示工作区、Agent/模式/状态、动作与目标；默认、0.5 倍及大字号下正文与
底行无重叠，不展示思考强度。截图为 `.test-artifacts/product-preview/` 下七张
PNG；生成脚本仅在 `.test-artifacts/` 内，不进入运行产品。

## 实机边界

只读探测时 Ubuntu 正在运行，但没有 ZCode Remote Development runtime。
因此真实连接、断开、切用户与远程 Agent 退出交互为 **NOT_TESTED**；相应
UID/HOME/starttime、新鲜度、缓存续租、server 残留及分数据面增退已有合成回归。
未扩大未经证实的 Node 路径识别，也不使用控制接口或唤醒停止的发行版。
