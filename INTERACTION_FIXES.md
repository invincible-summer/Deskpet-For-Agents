# DeskPet 4.3.1 仪表盘稳定性修复记录

本次修复针对仪表盘操作后的闪烁、重复刷新和卡死。七页导航、retained 页面、Monitor 归属规则、终端激活安全语义和桌宠动画架构保持原有合同。

## 修复内容

1. **扫描按钮不再阻塞 Tk**：`Monitor.rescan()` 现在只提交合并式 Event 并唤醒现有 Monitor/ProcessProbe worker。watcher cache reset、强制 UIA topology refresh 和 terminal re-resolve 都在 Monitor worker 执行。请求 pending 时的重复点击为零 UI 工作。
2. **按钮统一走一次 render**：配置、并发展示、绑定和导航动作不再同时调用 `_aggregate()`、页面 `refresh()` 与 dirty render。同一事件批次由 `UiCoordinator` 合并成一个 render idle，并只刷新当前可见页。
3. **Configure 事件有限收敛**：滚动容器把 Canvas/inner Configure 合并到一个 `after_idle`，缓存 window width 与 scrollregion，仅在值变化时调用 geometry manager。滚轮命中判断不再用 `update_idletasks()` 重入事件循环。
4. **相同值为零工作**：`Config.set()` / `update_many()` 会识别无变化输入；外观控制器因此不会重复增加 revision、保存配置、构建皮肤或刷新 UI。导航按钮、状态标签和条件控件也跳过相同状态更新。
5. **设置页慢操作转入后台**：开机启动状态读取、切换和修复使用 Dashboard 持有的单个 transient worker。结果由现有 UI bridge 在 Tk 线程收割；busy 时拒绝重复提交，关闭后丢弃迟到结果，并在全局 3 秒 shutdown deadline 内回收。
6. **异常不会形成重试风暴**：开机启动状态读取失败后显示稳定的“不可用”状态，不会每个 bridge tick 重建 worker。配置保存结果通过 dirty render 更新设置页状态。
7. **托盘菜单真实跟踪下的确定性退出**：桌面空闲时 Windows 可能把前台授予托盘窗口，`TrackPopupMenuEx` 真实进入模态跟踪并阻塞 worker；posted `WM_CANCELMODE` 不能可靠结束菜单（官方文档注明它只是 `EndMenu` 的回退手段），`request_stop` 因此无法在 shutdown 预算内到达 STOPPED。现在 worker 在自己的 wndproc 收到 `WM_CANCELMODE` / `WM_APP_QUIT` 时调用 `EndMenu()`（一级 API，作用于调用线程），菜单跟踪被确定性解除。该路径由自动验收暴露，并以 3 项回归锁定。

## 自动验收

本轮按要求不使用 computer use、真人鼠标/键盘或人工截图。`tools/real_machine_acceptance.py` 只有显式传入 `--interactive` / `--visual` 才会进入对应诊断；普通自动模式不会生成强制人工视觉门槛。

- 完整单元回归：**670 项通过**。覆盖 slow fake UIA 下扫描回调 50ms 内返回、20 次扫描合并、UIA refresh 异常不杀 worker、同批 dirty 单 render、100 次 Configure 收敛、相同配置零工作、20 次快速外观变化合并为单保存/单 render/单最终尺寸 build、单 Dashboard worker、异常不自激、shutdown 丢弃迟到结果，以及托盘菜单真实跟踪被 EndMenu 确定性解除。
- Dashboard 自动验收检查 retained widget identity、重复 active-nav 零 render/reflow、Configure storm 收敛、同批 20 次 dirty 只 render 一次、真实几何、hide/reopen 与单实例生命周期。
- UI 架构基准新增硬断言：导航/重扫描/外观按钮回调耗时有界（<50ms，重扫描 O(1) 提交）、200 次 Configure 合并为一个 idle 并收敛、同宽零重复写入。
- 全部非交互套件以默认强度完成：200 轮 tray synthetic、100 轮 tray generation、三条退出路径、100 轮 Dashboard、warm/cold startup、300 秒 idle 和 200 轮菜单 controller；资源上限未调整。
- UI 架构、Monitor、Presentation、Bubble 四组资源基准通过。Bubble 基准已改为干净环境可运行的自包含 retained-render 检查，不依赖旧缓存、审批控件或截图。
- release-layout、Python byte-compile 和差异空白检查通过。

本地日志写入被忽略的 `.test-artifacts/`。自动验收只证明可自动观察的线程、回调、资源和 Tk 模型行为；本轮没有声明真人交互、多显示器/DPI 热切换或第三方终端真实卡死已被人工验证。
