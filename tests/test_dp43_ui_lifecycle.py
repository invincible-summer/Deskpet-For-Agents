"""DP43-R14～R22 UI 生命周期/菜单/退出/启动可靠性测试（plan §5-§13）。

Phase 0 红测试集：本文件先于实现提交，覆盖 plan §14 Phase 0 列出的
七类已确认缺陷——

  1. Fleet menu explicit view（R14：production 回调未传 view，
     `_menu_view` 从未被赋值）；
  2. rapid tray generation（R15：stop 后立即 self.tray=None，
     新 generation 在旧 worker STOPPING 期间被创建）；
  3. tray protocol duplicate/context（R15：一次右键手势产生两个
     "right"；NOTIFYICON_VERSION_4 的 WM_CONTEXTMENU 未被识别）；
  4. Dashboard FocusOut auto-collapse（R16：焦点变化被当成关闭意图）；
  5. shutdown additive timeout（R17：局部有界但总时延可叠加）；
  6. SkinCatalog indirect lock（R19：持 catalog lock 做磁盘扫描，
     Tk 读内存也要等 I/O）；
  7. startup job ordering（R18/R19：首个 view 在 bootstrap 前提交
     BUILD；first snapshot 做磁盘扫描；dashboard 顶层 import）。

实现完成后同一批测试转为常驻回归（不允许删除或放宽断言）。
"""
import copy
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pet.config import DEFAULTS

_REPO = str(Path(__file__).resolve().parents[1])


class MemoryConfig:
    def __init__(self):
        self.data = copy.deepcopy(DEFAULTS)
        self.data['tray_enabled'] = False
        self.migration_notice = False

    def get(self, path, default=None):
        node = self.data
        for p in path.split('.'):
            if not isinstance(node, dict) or p not in node:
                return default
            node = node[p]
        return node

    def set(self, path, value):
        node = self.data
        parts = path.split('.')
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value

    def save(self):
        pass


def make_app(cfg=None):
    """构造 PetApp：皮肤加载打桩（测试聚焦菜单/生命周期合同）。

    默认 disarm first-map 触发器（避免 update() 误启真实
    monitor/WSL 探测）；startup 专项测试用 arm_map=True 走真实链路。
    """
    from pet.app import PetApp
    from pet.petview import PetView
    with\
         patch.object(PetView, 'load_skin', lambda self, bm: None):
        app = PetApp(cfg or MemoryConfig())
    app._disarm_first_map_trigger()
    return app


# ================================================================ R14
class PetMenuExplicitViewRedTests(unittest.TestCase):
    """R14：Pet 菜单 builder 必须显式携带 view；_menu_view 必须消失。"""

    def test_fleet_menu_production_path_carries_view(self):
        """Fleet：production 回调链（窗口事件 → 菜单）必须产出该 view 的
        fleet 菜单，不依赖测试手工注入 _menu_view。"""
        from pet.presentation import PresentationMode
        from tests.test_fleet_ui import FleetConfig, _slot, inst, snap
        from agents.models import AgentKind
        cfg = FleetConfig([_slot("pet-1"), _slot("pet-2")])
        app = make_app(cfg)
        try:
            app.presentation.set_concurrent_mode(PresentationMode.FLEET)
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2, cwd="/w/q")
            app.monitor.instances = {a.key: a, b.key: b}
            app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
            app._aggregate()
            view = app.pet_manager.views["pet-1"]
            self.assertTrue(view.agent_key)

            class _Ev:
                x_root, y_root = 10, 20

            with patch.object(app, "quit"), \
                 patch.object(app, "activate_agent"), \
                 patch.object(app, "_open_agent_picker"), \
                 patch.object(app, "set_tray_enabled"), \
                 patch.object(app, "toggle_autostart", return_value=True), \
                 patch.object(app, "_rebuild_skin"), \
                 patch('tkinter.Menu.tk_popup',
                       lambda self, x, y, entry="": None):
                # production 链：窗口 Button-3 事件开始，不注入任何
                # 隐藏变量
                view.window._on_menu(_Ev())
                app.root.update()
            # fleet 菜单特征项（旧实现读取从未被赋值的 _menu_view，
            # production 下永远落到非 fleet 菜单）
            menu = tk.Menu(app.root, tearoff=0)
            app._build_pet_menu(menu, view)
            labels = []
            end = menu.index('end')
            if end is not None:
                for i in range(end + 1):
                    if menu.type(i) in ('command', 'cascade', 'checkbutton',
                                        'radiobutton'):
                        labels.append(menu.entrycget(i, 'label'))
            joined = "\n".join(labels)
            self.assertIn("更换 Agent", joined)
            self.assertIn("解除绑定", joined)
            try:
                menu.destroy()
            except tk.TclError:
                pass
        finally:
            app.quit()

    def test_build_pet_menu_requires_explicit_view(self):
        app = make_app()
        try:
            view = app.pet_manager.views['pet-1']
            menu = tk.Menu(app.root, tearoff=0)
            # 新合同：builder 显式参数 (menu, view)
            app._build_pet_menu(menu, view)
            self.assertIsNotNone(menu.index('end'))
        finally:
            app.quit()

    def test_petwindow_reports_context_request_not_menu(self):
        """PetWindow 只上报 (x_root, y_root)，不创建业务 menu。"""
        app = make_app()
        try:
            view = app.pet_manager.views['pet-1']
            win = view.window
            self.assertFalse(
                hasattr(win, 'on_menu') and win.on_menu is not None,
                "PetWindow 不得再持有 menu 构建回调")
            self.assertTrue(callable(getattr(win, 'on_context_menu', None)),
                            "PetWindow 必须上报 context request")
        finally:
            app.quit()


# ================================================================ R15
class TrayGenerationRedTests(unittest.TestCase):
    """R15：rapid off→on 不得在旧 worker STOPPING 期间创建新 generation。"""

    def test_rapid_off_on_keeps_single_generation(self):
        from pet.tray import TrayState
        created = []

        class FakeTray:
            def __init__(self, tooltip=''):
                created.append(self)
                self.events = queue.Queue()
                self._state = TrayState.READY
                self._terminal = threading.Event()
                self.dropped_events = 0
                self.stops = 0

            def status(self):
                return self._state

            def start(self):
                pass

            def request_stop(self):
                self.stops += 1
                self._state = TrayState.STOPPING

            def join_for_shutdown(self, timeout):
                return self._terminal.is_set()

            def terminate(self):
                self._terminal.set()
                self._state = TrayState.STOPPED

            def show_icon(self):
                pass

            def update_menu_snapshot(self, *a, **k):
                pass

            def menu_open_failures(self):
                return 0

            def last_error(self):
                return ''

        with patch('pet.tray.TrayIcon', FakeTray):
            app = make_app()
            try:
                app.set_tray_enabled(True)
                self.assertEqual(len(created), 1)
                first = created[0]
                self.assertIs(app.tray, first)
                # 快速 OFF → ON（旧 worker 仍在 STOPPING）
                app.set_tray_enabled(False)
                app.set_tray_enabled(True)
                self.assertIs(app.tray, first,
                              "STOPPING 期间不得替换 owner")
                self.assertEqual(len(created), 1,
                                 "STOPPING 期间不得创建 replacement")
                self.assertGreaterEqual(first.stops, 1)
                # 旧 generation 到达终态后，reconcile 才创建新 generation
                first.terminate()
                app._reconcile_tray_runtime()
                self.assertEqual(len(created), 2)
                self.assertIs(app.tray, created[1])
            finally:
                app.quit()

    def test_stop_keeps_owner_until_terminal(self):
        from pet.tray import TrayState

        class SlowStopTray:
            def __init__(self, tooltip=''):
                self.events = queue.Queue()
                self._state = TrayState.READY

            def status(self):
                return self._state

            def start(self):
                pass

            def request_stop(self):
                self._state = TrayState.STOPPING

            def join_for_shutdown(self, timeout):
                return False

            def show_icon(self):
                pass

            def update_menu_snapshot(self, *a, **k):
                pass

        with patch('pet.tray.TrayIcon', SlowStopTray):
            app = make_app()
            try:
                app.set_tray_enabled(True)
                first = app.tray
                app.set_tray_enabled(False)
                self.assertIs(app.tray, first,
                              "request_stop 后不得立即丢弃 owner")
            finally:
                app.quit()


class TrayProtocolRedTests(unittest.TestCase):
    """R15：一个手势 = 一个语义事件；VERSION_4 只认 WM_CONTEXTMENU。"""

    def _bare_icon(self):
        from pet.tray import TRAY_EVENT_QUEUE_MAX, TrayIcon
        icon = TrayIcon.__new__(TrayIcon)
        icon.events = queue.Queue(maxsize=TRAY_EVENT_QUEUE_MAX)
        icon.dropped_events = 0
        return icon

    def test_one_right_gesture_exactly_one_context_request(self):
        from pet.tray import WM_APP_TRAY, TrayIcon
        icon = self._bare_icon()
        v4 = TrayIcon._ICON_ID << 16   # VERSION_4：HIWORD(lParam)=icon id
        menu_opens = []
        with patch.object(icon, '_open_native_menu',
                          lambda: menu_opens.append(1)):
            # VERSION_4：Shell 对 context selection（鼠标右键/键盘）发送
            # WM_CONTEXTMENU——一次手势一个菜单
            icon._handle_message(1, WM_APP_TRAY, 0, v4 | 0x007B)
            # legacy 组合不再产生任何语义事件/菜单
            icon._handle_message(1, WM_APP_TRAY, 0, v4 | 0x0204)
            icon._handle_message(1, WM_APP_TRAY, 0, v4 | 0x0205)
        self.assertEqual(menu_opens, [1],
                         "一次右键手势必须恰好打开一个菜单")
        self.assertTrue(icon.events.empty(),
                        "legacy down/up 不得进入语义队列")

    def test_left_up_exactly_one_restore(self):
        from pet.tray import WM_APP_TRAY, TrayIcon
        icon = self._bare_icon()
        v4 = TrayIcon._ICON_ID << 16
        icon._handle_message(1, WM_APP_TRAY, 0, v4 | 0x0202)   # WM_LBUTTONUP
        icon._handle_message(1, WM_APP_TRAY, 0, v4 | 0x0200)   # LBUTTONDOWN
        drained = []
        while True:
            try:
                drained.append(icon.events.get_nowait())
            except queue.Empty:
                break
        self.assertEqual([e.command for e in drained], ['restore'])

    def test_hicon_ownership_tracked(self):
        from pet.tray import TrayIcon
        icon = TrayIcon("ownership")   # 不启动线程
        self.assertFalse(icon._owns_hicon,
                         "TrayIcon 必须显式跟踪 HICON 所有权（初始 False）")

    def test_pointer_returning_prototypes_declared(self):
        """所有 handle/指针返回的 Win32 函数必须显式声明 restype。"""
        import ctypes.wintypes as wt
        import pet.tray as tray_mod
        cases = [
            (tray_mod.user32.CreateWindowExW, wt.HWND),
            (tray_mod.kernel32.GetModuleHandleW, wt.HMODULE),
            (tray_mod.user32.LoadImageW, wt.HANDLE),
            (tray_mod.user32.LoadIconW, wt.HICON),
        ]
        for fn, expected in cases:
            self.assertIs(fn.restype, expected,
                          f"{fn.__name__} restype 必须显式声明为 {expected}")


# ================================================================ R16
class DashboardFocusRedTests(unittest.TestCase):
    """R16：FocusOut 不得改变 Dashboard 的 open 状态。"""

    def test_focusout_does_not_close_dashboard(self):
        app = make_app()
        try:
            app.open_dashboard()
            app.root.update()
            dash = app.dashboard
            own = int(dash.winfo_id())
            # 旧代码曾用 _had_focus/GetForegroundWindow 推断关闭意图；
            # 新代码该整条路径已删除（setattr 只是 inert，保证本测试
            # 在两种实现下可运行）
            dash._had_focus = True
            dash.event_generate('<FocusOut>')
            app.root.update()
            self.assertTrue(dash.is_open(),
                            "FocusOut 不得自动收起 Dashboard")
            # 二次焦点往返仍稳定
            dash.event_generate('<FocusIn>')
            dash.event_generate('<FocusOut>')
            app.root.update()
            self.assertTrue(dash.is_open())
            dash.hide_dashboard()
            self.assertFalse(dash.is_open())
            dash.open()
            self.assertTrue(dash.is_open())
        finally:
            app.quit()

    def test_no_autocollapse_symbols(self):
        app = make_app()
        try:
            app.open_dashboard()
            dash = app.dashboard
            for symbol in ('_on_focus_lost', '_maybe_auto_collapse',
                           '_native_dialog_open', '_had_focus'):
                self.assertFalse(
                    hasattr(dash, symbol) or symbol in dir(dash),
                    f"auto-collapse 路径 {symbol} 必须删除")
        finally:
            app.quit()


# ================================================================ R17
class ShutdownDeadlineRedTests(unittest.TestCase):
    """R17：退出总等待 ≤ 单一全局 deadline（不叠加局部 timeout）。"""

    def test_quit_uses_single_global_deadline(self):
        app = make_app()
        try:
            calls = []

            class SlowMonitor:
                def __getattr__(self, name):
                    return lambda *a, **k: None

                def request_stop(self):
                    calls.append(('request_stop', time.monotonic()))

                def join_for_shutdown(self, timeout):
                    calls.append(('join', float(timeout)))
                    time.sleep(min(5.0, max(0.0, timeout)))
                    return False

                def stop(self):
                    calls.append(('legacy_stop',))
                    time.sleep(5.0)

                def get_targets(self):
                    return {}

                def rescan(self):
                    pass

                def trim(self):
                    pass

            app.monitor = SlowMonitor()
            t0 = time.monotonic()
            app.quit()
            elapsed = time.monotonic() - t0
            # 10s 级 fake worker 下总等待必须被全局 deadline 截断
            self.assertLessEqual(elapsed, 3.5,
                                 "退出等待必须由单一绝对 deadline 截断")
            self.assertIn('request_stop', [c[0] for c in calls])
            join_timeouts = [c[1] for c in calls if c[0] == 'join']
            self.assertTrue(join_timeouts)
            for t in join_timeouts:
                self.assertLessEqual(t, 3.0 + 0.05,
                                     "join 只能使用全局 deadline 的剩余量")
        finally:
            pass

    def test_double_quit_idempotent(self):
        app = make_app()
        app.quit()
        t0 = time.monotonic()
        app.quit()   # 第二次必须是 no-op
        self.assertLess(time.monotonic() - t0, 0.5)


# ================================================================ R17 §12.5
class ShutdownFaultInjectionTests(unittest.TestCase):
    """DP43-R17 §12.5：全局 deadline / 顺序 / 幂等。"""

    def test_quit_deadline_bounds_all_waits(self):
        from pet.app import SHUTDOWN_BUDGET_SEC
        app = make_app()
        timeouts = []

        def slow_join(timeout):
            timeouts.append(float(timeout))
            time.sleep(min(5.0, max(0.0, timeout)))
            return False

        class SlowMonitor:
            def __getattr__(self, name):
                return lambda *a, **k: None

            def request_stop(self):
                pass

            def join_for_shutdown(self, timeout):
                slow_join(timeout)
                return False

        app.monitor = SlowMonitor()
        with patch.object(app.pet_manager.build_manager,
                          'request_stop', lambda: None), \
             patch.object(app.pet_manager.build_manager,
                          'join_for_shutdown', side_effect=slow_join), \
             patch.object(app.config_saver, 'flush_for_shutdown',
                          side_effect=slow_join):
            t0 = time.monotonic()
            app.request_quit()
            elapsed = time.monotonic() - t0
        # 10s 级 fake worker：总等待被全局 deadline 截断
        self.assertLessEqual(elapsed, SHUTDOWN_BUDGET_SEC + 0.25)
        self.assertTrue(timeouts)
        for t in timeouts:
            self.assertLessEqual(t, SHUTDOWN_BUDGET_SEC + 0.05,
                                 "join 只能使用全局 deadline 剩余量")
        self.assertEqual(timeouts, sorted(timeouts, reverse=True),
                         "剩余量必须单调不增（同一绝对 deadline）")

    def test_hide_and_menu_teardown_before_stop_signals(self):
        app = make_app()
        order = []
        orig_hide = app.pet_manager.hide_all_for_shutdown
        app.pet_manager.hide_all_for_shutdown = (
            lambda: (order.append("hide"), orig_hide())[1])
        orig_menu = app._menu_controller.shutdown
        app._menu_controller.shutdown = (
            lambda: (order.append("menu"), orig_menu())[1])

        class OrderMonitor:
            def __getattr__(self, name):
                return lambda *a, **k: None

            def request_stop(self):
                order.append("monitor_stop")

            def join_for_shutdown(self, timeout):
                return True

        app.monitor = OrderMonitor()
        orig_lane = app.pet_manager.build_manager.request_stop
        app.pet_manager.build_manager.request_stop = (
            lambda: (order.append("lane_stop"), orig_lane())[1])
        app.request_quit()
        self.assertLess(order.index("menu"), order.index("hide"))
        self.assertLess(order.index("hide"), order.index("monitor_stop"))
        self.assertLess(order.index("monitor_stop"), order.index("lane_stop"))


# ================================================================ R19
class SkinCatalogLockRedTests(unittest.TestCase):
    """R19：snapshot() 纯内存；磁盘扫描永远在锁外。"""

    def test_ui_read_never_waits_for_disk_scan(self):
        import pet.skins as skins_mod
        cat = skins_mod.SkinCatalog()
        entered = threading.Event()
        release = threading.Event()
        orig_scan = skins_mod._scan_skins

        def slow_scan():
            entered.set()
            release.wait(5.0)
            return orig_scan()

        def worker_refresh():
            # DP43-R19 新合同：worker 锁外扫描 → 短临界区 swap
            fresh = slow_scan()
            cat.replace(fresh)

        with patch.object(skins_mod, '_scan_skins', slow_scan):
            worker = threading.Thread(target=worker_refresh)
            worker.start()
            self.assertTrue(entered.wait(2.0))
            t0 = time.perf_counter()
            snapshot = cat.snapshot()   # 模拟 Tk 线程读内存
            elapsed = time.perf_counter() - t0
            release.set()
            worker.join(6.0)
        self.assertLess(elapsed, 0.5,
                        "UI 读 snapshot 不得等待后台磁盘扫描（锁外 I/O）")
        self.assertIn(skins_mod.BUILTIN_SKIN, snapshot)

    def test_initial_snapshot_requires_no_disk_io(self):
        import pet.skins as skins_mod
        cat = skins_mod.SkinCatalog()

        def forbidden():
            raise AssertionError("snapshot() 不得触发磁盘扫描")

        with patch.object(skins_mod, '_scan_skins', forbidden):
            snapshot = cat.snapshot()
        self.assertIn(skins_mod.BUILTIN_SKIN, snapshot)


# ================================================================ R18
class StartupOrderingRedTests(unittest.TestCase):
    """R18/R19：首个 view 先于后台 runtime；首帧路径无磁盘扫描。"""

    def test_initial_view_no_build_request_before_bootstrap(self):
        import pet.skins as skins_mod
        requests = []
        from pet.app import PetApp
        from pet.petview import PetView
        from pet.skins import SkinBuildManager
        orig_request = SkinBuildManager.request

        def spy_request(self, view_id, skin, height, fps):
            requests.append((view_id, skin, height, fps))
            return orig_request(self, view_id, skin, height, fps)

        scans = []
        orig_scan = skins_mod._scan_skins

        def spy_scan():
            scans.append(1)
            return orig_scan()

        with\
             patch.object(SkinBuildManager, 'request', spy_request), \
             patch.object(skins_mod, '_scan_skins', spy_scan), \
             patch.object(PetView, 'redraw', lambda self: None):
            cfg = MemoryConfig()
            app = PetApp(cfg)
        try:
            self.assertEqual(
                requests, [],
                "bootstrap 激活前 ensure_view 不得提交 BUILD")
            self.assertEqual(
                scans, [],
                "首个 view 创建路径不得触发 skin 目录扫描")
        finally:
            app.quit()

    def test_dashboard_import_is_lazy(self):
        """import pet.app 不得连带加载 dashboard（千行级 UI 模块）、
        Pillow 或 comtypes（§12.6：first map 前不 import）。"""
        code = ("import sys; sys.path.insert(0, r'%s'); "
                "import pet.app; "
                "mods = sys.modules; "
                "assert 'pet.dashboard' not in mods, "
                "'dashboard must be lazy-imported'; "
                "assert 'PIL' not in mods, 'Pillow must stay lazy'; "
                "assert 'comtypes' not in mods, 'comtypes must stay lazy'; "
                "print('OK')") % _REPO
        result = subprocess.run(
            [sys.executable, '-c', code], capture_output=True,
            text=True, timeout=60, cwd=_REPO)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('OK', result.stdout)


# ================================================================ R14 §12.3
class PetContextMenuControllerTests(unittest.TestCase):
    """DP43-R14 §12.3：TkContextMenuController 生命周期/deferred 合同。"""

    class _Ev:
        x_root, y_root = 10, 20

    def test_repeated_show_destroys_previous_single_active(self):
        from pet.context_menu import TkContextMenuController
        app = make_app()
        try:
            ctrl = app._menu_controller
            menus = []
            active_flags = []

            def build(menu):
                active_flags.append(ctrl.active)   # build 阶段必然 active
                menus.append(menu)
                menu.add_command(label="x")

            with patch('tkinter.Menu.tk_popup',
                       lambda self, x, y, entry="": None):
                ctrl.show("owner", 1, 2, build)
                self.assertEqual(active_flags, [True])
                path = menus[0]._w
                # show 返回 = popup 已确定性销毁
                self.assertFalse(ctrl.active)
                ctrl.show("owner2", 3, 4, build)
            # 旧 popup 不因第二次 show 复活/残留
            self.assertEqual(app.root.tk.call('winfo', 'exists', path), 0)
            self.assertEqual(len(menus), 2)
            ctrl.dismiss()
            self.assertFalse(ctrl.active)
            ctrl.dismiss()   # 幂等
        finally:
            app.quit()

    def test_builder_exception_still_destroys(self):
        from pet.context_menu import TkContextMenuController
        app = make_app()
        try:
            ctrl = app._menu_controller

            def bad_builder(menu):
                menu.add_command(label="x")
                raise RuntimeError("builder boom")

            with patch('tkinter.Menu.tk_popup',
                       lambda self, x, y, entry="": None):
                ctrl.show("owner", 1, 2, bad_builder)   # 不向外抛
            self.assertFalse(ctrl.active)
        finally:
            app.quit()

    def test_deferred_waits_for_teardown_and_runs_once(self):
        from pet.context_menu import TkContextMenuController
        app = make_app()
        try:
            ctrl = app._menu_controller
            calls = []

            def build(menu):
                menu.add_command(label="go",
                                 command=ctrl.deferred(calls.append, "ran"))
                # 菜单 active（native/Tk 交互阶段）时 invoke：
                # 只发布 idle，不同步执行业务
                menu.invoke(0)
                self.assertEqual(calls, [])
                self.assertTrue(ctrl.active)

            with patch('tkinter.Menu.tk_popup',
                       lambda self, x, y, entry="": None):
                ctrl.show("owner", 1, 2, build)
            # show 返回 = teardown 完成；idle 后 exactly once
            app.root.update()
            self.assertEqual(calls, ["ran"])
            app.root.update()
            self.assertEqual(calls, ["ran"])   # 不重复
        finally:
            app.quit()

    def test_deferred_discarded_when_closing_except_quit(self):
        from pet.context_menu import TkContextMenuController
        app = make_app()
        try:
            ctrl = app._menu_controller
            calls = []
            app._closing = True
            ctrl.deferred(calls.append, "normal")()
            ctrl.deferred(calls.append, "quit", allow_when_closing=True)()
            app.root.update()
            self.assertEqual(calls, ["quit"])   # closing 只放行 quit
            app._closing = False
        finally:
            app.quit()

    def test_fleet_context_request_carries_own_view(self):
        """Fleet pet-1/pet-2 各自右键 → builder 收到 exact view。"""
        from pet.presentation import PresentationMode
        from tests.test_fleet_ui import FleetConfig, _slot, inst, snap
        from agents.models import AgentKind
        cfg = FleetConfig([_slot("pet-1"), _slot("pet-2")])
        app = make_app(cfg)
        try:
            app.presentation.set_concurrent_mode(PresentationMode.FLEET)
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2, cwd="/w/q")
            app.monitor.instances = {a.key: a, b.key: b}
            app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
            app._aggregate()
            v1 = app.pet_manager.views["pet-1"]
            v2 = app.pet_manager.views["pet-2"]
            seen = []
            with patch.object(app, "_build_pet_menu",
                              side_effect=lambda menu, view:
                              seen.append(view)), \
                 patch('tkinter.Menu.tk_popup',
                       lambda self, x, y, entry="": None):
                v1.window._on_menu(self._Ev())
                app.root.update()
                v2.window._on_menu(self._Ev())
                app.root.update()
            self.assertEqual(seen, [v1, v2])   # 各自携带 exact view
        finally:
            app.quit()

    def test_controller_churn_leaves_no_after_timers(self):
        app = make_app()
        try:
            ctrl = app._menu_controller
            with patch('tkinter.Menu.tk_popup',
                       lambda self, x, y, entry="": None):
                for i in range(100):
                    ctrl.show("owner", 1, 2,
                              lambda m: m.add_command(label=str(i)))
            ctrl.dismiss()
            app.root.update()
            # Tk after 队列为空（菜单生命周期不靠定时器）
            afters = app.root.tk.splitlist(
                app.root.tk.call('after', 'info'))
            self.assertEqual(len(afters), 0)
        finally:
            app.quit()


# ================================================================ R18 §12.6
class StartupFlowTests(unittest.TestCase):
    """first-map 优先 / bootstrap 顺序 / warm-cold cache 行为。"""

    @staticmethod
    def _fake_monitor_module():
        import agents.monitor as monitor_mod

        class FakeTerminalService:
            observer = None

            def request_stop(self):
                pass

            def join_for_shutdown(self, timeout):
                return True

            def stop(self):
                pass

        class FakeMonitor:
            terminal_available = staticmethod(lambda: False)

            def __init__(self, config):
                self.events = []
                self._terminal_service = FakeTerminalService()

            def start(self):
                self.events.append(("monitor_start", time.perf_counter()))

            def get_targets(self):
                return {}

            def get_target(self, key):
                return None

            def get_targets_if_changed(self, revision):
                return revision, None

            def rescan(self):
                pass

            def trim(self):
                pass

            def request_stop(self):
                pass

            def join_for_shutdown(self, timeout):
                return True

            def stop(self):
                pass

        return monitor_mod, FakeMonitor

    def _make_armed_app(self):
        from pet.app import PetApp
        from pet.petview import PetView
        monitor_mod, FakeMonitor = self._fake_monitor_module()
        with patch.object(monitor_mod, "Monitor", FakeMonitor), \
\
             patch.object(PetView, 'load_skin', lambda self, bm: None):
            app = PetApp(MemoryConfig())
        return app

    def test_first_map_precedes_background_runtime(self):
        app = self._make_armed_app()
        try:
            monitor = app.monitor
            bm = app.pet_manager.build_manager
            # 未 map：后台 runtime 未启动，无任何 skin job
            self.assertEqual(monitor.events, [])
            self.assertFalse(app._background_started)
            self.assertFalse(bm.building())
            # 模拟窗口映射（production 路径：<Map> 一次性回调）
            view = app.pet_manager.views["pet-1"]
            view.window.root.event_generate("<Map>")
            app.root.update()
            self.assertTrue(monitor.events, "map 后必须启动 Monitor")
            metrics = app.startup_metrics()
            self.assertIn("first_pet_mapped", metrics)
            self.assertIn("background_runtime_started", metrics)
            self.assertLess(metrics["first_pet_mapped"],
                            metrics["background_runtime_started"])
            # bootstrap 已提交；Map 期间不产生 BUILD
            self.assertTrue(
                bm._bootstrap_pending is not None
                or (bm._active_job is not None
                    and bm._active_job.kind.value == "bootstrap")
                or bm.results_pending(),
                "map 后应提交 BOOTSTRAP job")
            self.assertEqual(bm._pending_builds, [])
        finally:
            app.quit()

    def test_bootstrap_result_activates_runtime_and_queues_maintenance(self):
        app = self._make_armed_app()
        try:
            bm = app.pet_manager.build_manager
            activated = []
            app.pet_manager.activate_skin_runtime = (
                lambda: activated.append(1))
            # 直接以 worker 结果驱动 poll（跳过真实磁盘扫描）
            from pet.skins import SkinJobKind, SkinJobResult
            bm._put_result(SkinJobResult(
                1, SkinJobKind.BOOTSTRAP, True, None,
                {"catalog": {"builtin-cat": {"name": "builtin-cat"}},
                 "ready": {}, "fallback_keys": []}, ""))
            bm._active_job = None   # 模拟 worker 已结束
            bm.poll_results()
            # on_bootstrap_result（app 回调）→ activate skin runtime
            self.assertEqual(activated, [1])
            metrics = app.startup_metrics()
            self.assertIn("skin_bootstrap_finished", metrics)
        finally:
            app.quit()

    def test_bootstrap_no_converter_warm_cache_and_build_after_cold(self):
        """BOOTSTRAP 只读校验（warm cache 命中不转换）；真正 miss 的
        BUILD 只在 activate 之后才产生。"""
        import json as json_mod
        import tempfile
        from pathlib import Path
        import pet.skins as skins_mod
        from pet.skins import (SkinBuildManager, cache_dir, cache_files_ok,
                               write_cache_manifest)

        with tempfile.TemporaryDirectory() as root:
            cache = Path(root, "cache")
            cache.mkdir()
            with patch.object(skins_mod, "CACHE_DIR", str(cache)):
                bm = SkinBuildManager()
                key = (skins_mod.BUILTIN_SKIN, 240, 12)
                # cold：无 cache → bootstrap 全 miss
                bm.request_bootstrap([key])
                self._wait_lane(bm)
                out = bm.poll_results()
                payload = out[0][2]
                self.assertEqual(payload["fallback_keys"], [key])
                self.assertEqual(payload["ready"], {})
                self.assertFalse(bm._pending_builds,
                                 "bootstrap 本身绝不提交 BUILD/转换")
                # 手工构造合法 warm cache（builtin 固定签名；不跑 Pillow）
                d = cache_dir(key[0], key[1])
                os.makedirs(d, exist_ok=True)
                for s in skins_mod.STATES:
                    Path(d, s + ".gif").write_bytes(b"GIF89a")
                    Path(d, s + ".gif.json").write_text(
                        json_mod.dumps({"frames": 1, "width": 4,
                                        "height": 4, "delay_ms": 1000,
                                        "loop": True}))
                write_cache_manifest(d, key[0], key[1], key[2],
                                     skins_mod.source_signature_for(key[0]))
                # warm：bootstrap 命中 ready，不产生任何 BUILD/转换
                bm.request_bootstrap([key])
                self._wait_lane(bm)
                out = bm.poll_results()
                payload = out[0][2]
                self.assertEqual(list(payload["ready"].keys()), [key])
                self.assertEqual(payload["fallback_keys"], [])
                self.assertFalse(bm._pending_builds)
                self.assertTrue(bm.ready_paths(*key))
                # ready index 命中：request 立即入队 ok 结果（无 worker）
                bm.request("pet-1", *key)
                self.assertFalse(bm._pending_builds)
                results = bm.poll_results()
                self.assertEqual(results[0][1], "ok")

    @staticmethod
    def _wait_lane(bm, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if bm.results_pending():
                return True
            if bm._active_job is None and bm._bootstrap_pending is None:
                return True
            time.sleep(0.01)
        return False

    def test_import_result_carries_catalog_swap(self):
        """import 成功：worker 锁外扫描的 catalog 经 result 携带，
        Tk 侧 poll 短临界区 swap（§12.6）。"""
        import tempfile
        from pathlib import Path
        import pet.skins as skins_mod
        from pet.skins import SkinBuildManager
        with tempfile.TemporaryDirectory() as src, \
                tempfile.TemporaryDirectory() as pets:
            for state in skins_mod.STATES:
                Path(src, state + ".gif").write_bytes(b"GIF89a")
            with patch.object(skins_mod, "PETS_DIR", pets):
                bm = SkinBuildManager()
                bm.submit_import(src, "catalogswap")
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if bm.poll_results():
                        break
                    time.sleep(0.01)
                self.assertIn("catalogswap", skins_mod.list_skins())
        # 清理：恢复真实 catalog（模块单例）
        with patch.object(skins_mod, "_scan_skins",
                          lambda: {skins_mod.BUILTIN_SKIN:
                                   {"name": skins_mod.BUILTIN_SKIN,
                                    "builtin": True}}):
            skins_mod.refresh_skin_catalog()


if __name__ == '__main__':
    unittest.main()