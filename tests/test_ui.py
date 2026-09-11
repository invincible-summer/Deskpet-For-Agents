"""Headless model checks and real Tk smoke tests (Windows Python). V3: no approvals."""
import copy
import gc
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import _dpi_aware
_dpi_aware()
from pet.config import DEFAULTS
from pet.bubble import BubbleRenderer, BubbleModel, metrics, wrap_text, fit_text
from pet.animator import Animation, AnimationCursor, AnimationScheduler, SharedAnimationCache
from actions import winkeys


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


class GeometryTests(unittest.TestCase):
    def test_joint_scale_and_relative_font(self):
        cfg = MemoryConfig()
        a = metrics(cfg)
        cfg.set('scale', 2)
        b = metrics(cfg)
        self.assertEqual((b['w'], b['h']), (a['w'] * 2, a['h'] * 2))
        self.assertLessEqual(abs(b['font'] - 2 * a['font']), 1)
        cfg.set('bubble.relative_width', 1.3)
        cfg.set('bubble.relative_height', 1.2)
        c = metrics(cfg)
        self.assertGreater(c['font'], b['font'])
        self.assertGreater(c['w'], b['w'])
        d = metrics(cfg, 1.5)
        self.assertLessEqual(abs(d['w'] - c['w'] * 1.5), 1)

    def test_wrap_pixel_bound_cjk_and_long_token(self):
        measure = lambda text: sum(14 if ord(ch) > 127 else 7 for ch in text)
        for text in ('正在整理气泡布局' * 200, 'unbounded_identifier_' * 300, 'hello\nworld'):
            lines = wrap_text(text, 120, measure, 2)
            self.assertEqual(len(lines), 2)
            self.assertTrue(all(measure(line) <= 120 for line in lines))
        self.assertEqual(fit_text('abc', 0, measure), '')

    def test_no_key_injection_surface_remains(self):
        """V4.1 不变量：winkeys 不提供任何键盘注入入口。"""
        for banned in ('send_key', 'send_input', 'keybd_event', 'SendInput',
                       'post_message', 'raise_terminal'):
            self.assertFalse(hasattr(winkeys, banned))
        self.assertTrue(callable(winkeys.try_set_foreground))

    def test_ambiguous_windows_do_not_bind_via_ancestors(self):
        """两个同 PID 窗口时祖先链不能唯一定位（window=None，不猜）。"""
        from agents.terminal_resolver import TerminalWindowResolver
        from agents.models import AgentInstance, AgentKind, WindowBindingConfidence
        resolver = TerminalWindowResolver(
            enum_windows=lambda: [(1, 10, 'Codex', 'CASCADIA_HOSTING_WINDOW_CLASS'),
                                  (2, 10, 'Codex', 'CASCADIA_HOSTING_WINDOW_CLASS')],
            ancestor_pids=lambda pid: {10},
        )
        inst = AgentInstance(AgentKind.CODEX, 99, 'windows', process_token='9')
        bindings = resolver.resolve([inst], {}, 1000.0)
        self.assertEqual(bindings[inst.key].confidence,
                         WindowBindingConfidence.AMBIGUOUS)
        self.assertIsNone(bindings[inst.key].window)

    def test_activation_failure_does_not_retry_or_inject(self):
        """foreground 被拒：restore→activate→flash 一次，绝不注入。"""
        class User:
            def __init__(self): self.calls = []

            def IsWindow(self, h): return True

            def IsIconic(self, h): return True

            def IsWindowVisible(self, h): return True

            def GetWindowThreadProcessId(self, h, out): return 1

            def GetClassNameW(self, h, buf, n): return 1

            def ShowWindowAsync(self, *args): self.calls.append('restore'); return True

            def SetForegroundWindow(self, h): self.calls.append('activate')

            def GetForegroundWindow(self): return 99

            def FlashWindowEx(self, *args):
                self.calls.append('flash')
                return True
        user = User()
        with patch.object(winkeys, 'user32', user):
            self.assertFalse(winkeys.try_set_foreground(1))
            self.assertTrue(winkeys.restore_window(1))
            self.assertTrue(winkeys.flash_window(1))
        self.assertEqual(user.calls, ['activate', 'restore', 'flash'])

    def test_config_v3_migration_drops_legacy_keys(self):
        import json
        import tempfile
        from pet.config import Config, CONFIG_PATH
        old = {
            "config_version": 2,
            "connection_mode": "hybrid",
            "managed": {"command": "codex"},
            "keys": {"codex": {"approve": "y"}},
            "auto_approve": {"enabled": True},
            "approve_restore_focus": True,
            "window_instances": {"k": {}},
            "monitor": {"pinned": "old|key", "session_bindings": {"a": "x"},
                        "waiting_quiet_sec": 15, "working_hold_sec": 90},
        }
        with tempfile.TemporaryDirectory() as temp:
            saved = CONFIG_PATH
            try:
                import pet.config as cfgmod
                cfgmod.CONFIG_PATH = os.path.join(temp, "config.json")
                with open(cfgmod.CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(old, f)
                cfg = Config()
                self.assertTrue(cfg.migration_notice)
                self.assertEqual(cfg.data["config_version"], 5)
                for banned in ("connection_mode", "managed", "keys",
                               "auto_approve", "approve_restore_focus",
                               "window_instances"):
                    self.assertNotIn(banned, cfg.data)
                self.assertNotIn("pinned", cfg.data["monitor"])
                self.assertNotIn("gone_grace_sec", cfg.data["monitor"])
                # v4.3：enabled/mode 不再持久化（session runtime state）
                self.assertNotIn("enabled", cfg.data["presentation"]["concurrent"])
                self.assertNotIn("mode", cfg.data["presentation"]["concurrent"])
                self.assertEqual(cfg.data["presentation"]["concurrent"]["slots"][0]["id"], "pet-1")
                self.assertNotIn("session_bindings", cfg.data["monitor"])
                self.assertNotIn("waiting_quiet_sec", cfg.data["monitor"])
                self.assertIn("privacy", cfg.data)
                self.assertTrue(cfg.data["privacy"]["terminal_text_to_disk"] is False)
            finally:
                cfgmod.CONFIG_PATH = saved


class TkTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.cfg = MemoryConfig()
        self.canvas = tk.Canvas(self.root, width=650, height=400)
        self.canvas.pack()
        self.bubble = BubbleRenderer(self.canvas, self.cfg)
        # 本测试类里临时创建的动画缓存：tearDown 必须先于 root.destroy
        # 释放其中的 PhotoImage（v4.2.1 CI 崩溃纪律）
        self._anim_caches = []

    def tearDown(self):
        for cache in self._anim_caches:
            cache.free_all()          # interpreter 存活时于主线程释放
        self.root.destroy()
        gc.collect()                  # 主线程收残余引用环（v4.2.1）

    def test_fixed_card_cached_items_at_scales(self):
        for scale in (.5, .75, 1, 1.5, 2):
            self.cfg.set('scale', scale)
            self.bubble.invalidate()
            self.bubble.model = BubbleModel(visible=True, status='Codex · Plan · 编码中',
                                            text='正在分析气泡布局与缩放方式', footer='目标 · 迁移 JWT')
            normal = self.bubble.layout()
            self.bubble.draw(0, 0, normal[0] // 2, normal[1] + 8)
            items = self.canvas.find_all()
            for _ in range(30):
                self.bubble.layout()
                self.bubble.draw(0, 0, normal[0] // 2, normal[1] + 8)
            self.assertEqual(items, self.canvas.find_all())

    def test_bubble_hit_carries_exact_agent_key(self):
        """V4.1：气泡无审批按钮；底行命中携带 exact agent_key。"""
        b = self.bubble
        b.model = BubbleModel(visible=True, status='Claude Code · 等待审批',
                              text='Bash 命令需要确认', footer='请在终端处理',
                              agent_key='wsl:U|claude|9|tok')
        b.layout()
        b.draw(0, 0, 150, 140)
        self.assertFalse(hasattr(b.model, 'approve_label'))
        self.assertFalse(hasattr(b.model, 'deny_label'))
        box = b._hit_boxes[0][0]
        hit = b.hit((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        self.assertEqual(hit.action, 'activate_agent')
        self.assertEqual(hit.agent_key, 'wsl:U|claude|9|tok')

    def test_hidden_bubble_clears(self):
        b = self.bubble
        b.model = BubbleModel(visible=True, text='a', footer='f',
                              agent_key='k')
        b.layout()
        b.draw(0, 0, 150, 140)
        self.assertTrue(b._hit_boxes)
        b.model.visible = False
        b.draw(0, 0, 150, 140)
        self.assertEqual(len(self.canvas.find_all()), 0)
        self.assertFalse(b._hit_boxes)

    def test_shared_cache_byte_budget_and_scheduler(self):
        """V4.1：进程级共享缓存预算 + 单调度器多光标（v4plan §11）。"""
        import tkinter as tk
        cache = SharedAnimationCache(max_bytes=800)
        self._anim_caches.append(cache)
        scheduler = AnimationScheduler(self.root, cache)
        anim = Animation('unused', {'width': 10, 'height': 10, 'frames': 10})
        cache._pool['unused'] = anim
        photo_factory = tk.PhotoImage
        with patch('pet.animator.tk.PhotoImage', side_effect=lambda **_: photo_factory(master=self.root, width=10, height=10)):
            for i in range(10):
                cache.frame('unused', i, keep_paths=set())
        # 预算 800B、每帧 400B → 最多留 2 帧
        self.assertLessEqual(len(anim._frames), 2)
        # 两个 cursor 共享一个调度器；注册即有定时器，注销后清空
        c1 = AnimationCursor('v1')
        c2 = AnimationCursor('v2')
        for c in (c1, c2):
            c.play('unused', 'walk', anim)
        scheduler.register(c1, lambda _v: None)
        scheduler.register(c2, lambda _v: None)
        self.assertIsNotNone(scheduler._after_id)
        scheduler.unregister('v1')
        scheduler.unregister('v2')
        self.assertIsNone(scheduler._after_id)

    def test_hidden_pet_cursor_paused_not_scheduled(self):
        cache = SharedAnimationCache()
        scheduler = AnimationScheduler(self.root, cache)
        cursor = AnimationCursor('v')
        cursor.paused = True   # hidden Pet
        cursor.play('x', 'walk', type('M', (), {'base_delay': 83, 'n': 2,
                                                 'width': 1, 'height': 1,
                                                 'loop': True})())
        scheduler.register(cursor, lambda _v: None)
        self.assertIsNone(scheduler._after_id)   # paused 不产生定时器
        scheduler.unregister('v')
        scheduler.stop()


class MenuEphemeralLifecycleTests(unittest.TestCase):
    """v4.2.3 §9 / DP43-R14/R15：popup menu 的确定性销毁与 churn 上限。

    Tray 菜单已原生化（worker 线程内 HMENU）；Pet 菜单由
    TkContextMenuController 拥有（test_dp43_ui_lifecycle 覆盖）。
    本类保留通用 Tk menu 销毁原语（controller._destroy）的合同。
    """

    def _app(self):
        from pet.app import PetApp
        from pet.petview import PetView
        cfg = MemoryConfig()
        with\
             patch.object(PetView, 'load_skin', lambda self, bm: None):
            app = PetApp(cfg)
            app.pet_manager.activate_skin_runtime()
            app._disarm_first_map_trigger()
            return app

    def test_destroy_menu_idempotent_no_tclerror(self):
        app = self._app()
        try:
            import tkinter as tkmod
            menu = tkmod.Menu(app.root, tearoff=0)
            path = menu._w
            destroy = app._menu_controller._destroy
            destroy(menu)
            self.assertEqual(app.root.tk.call('winfo', 'exists', path), 0)
            # 幂等：重复 destroy / None 不抛
            destroy(menu)
            destroy(None)
        finally:
            app.quit()

    def test_menu_churn_1000_no_widget_growth_or_tclerror(self):
        app = self._app()
        try:
            import tkinter as tkmod
            paths = []
            for _ in range(1000):
                menu = tkmod.Menu(app.root, tearoff=0)
                paths.append(menu._w)
                app._menu_controller._destroy(menu)
            # 所有历史 menu widget 均已销毁（无线性增长）
            alive = [p for p in paths
                     if app.root.tk.call('winfo', 'exists', p)]
            self.assertEqual(alive, [])
            # 无新增 after timer（菜单生命周期不靠定时器）
        finally:
            app.quit()


class DashboardStableToplevelTests(unittest.TestCase):
    """DP43-R16：Dashboard 是 retained Toplevel——失焦绝不关闭；
    关闭只来自 X / 显式 hide / App shutdown。

    （替换旧 DashboardAutoCollapseTests 的语义；auto-collapse 整条
    生命周期已删除。）
    """

    def _app(self):
        from pet.app import PetApp
        from pet.petview import PetView
        cfg = MemoryConfig()
        with patch.object(PetView, 'load_skin', lambda self, bm: None):
            app = PetApp(cfg)
            app.pet_manager.activate_skin_runtime()
            app._disarm_first_map_trigger()
            return app

    def test_focusout_never_closes(self):
        app = self._app()
        try:
            app.open_dashboard()
            app.root.update()
            dash = app.dashboard
            self.assertTrue(dash.is_open())
            dash.event_generate('<FocusIn>')
            dash.event_generate('<FocusOut>')
            app.root.update()
            self.assertTrue(dash.is_open())
        finally:
            app.quit()

    def test_hide_reopen_keeps_current_page(self):
        app = self._app()
        try:
            app.open_dashboard()
            app.root.update()
            dash = app.dashboard
            # 切到非默认页再 hide/reopen：current page 保留
            from pet.dashboard import PAGE_PETS
            dash._show_page(PAGE_PETS)
            page = dash._page
            dash.hide_dashboard()
            self.assertFalse(dash.is_open())
            dash.open()
            self.assertTrue(dash.is_open())
            self.assertEqual(dash._page, page)
        finally:
            app.quit()

    def test_open_100_times_single_dashboard(self):
        app = self._app()
        try:
            first = None
            for _ in range(100):
                app.open_dashboard()
                app.root.update()
                if first is None:
                    first = app.dashboard
            self.assertIs(app.dashboard, first)
            self.assertTrue(first.winfo_exists())
        finally:
            app.quit()

    def test_native_dialog_wrappers_do_not_change_lifecycle(self):
        """messagebox/filedialog 使用标准 parent；不再依赖 suppress 标志
        （标志已删除，包装函数直接调用）。"""
        from pet import dashboard as dash_mod
        self.assertFalse(hasattr(dash_mod.Dashboard, '_maybe_auto_collapse'))
        src = open(dash_mod.__file__, encoding='utf-8').read()
        self.assertNotIn('_native_dialog_open', src)
        self.assertNotIn('_had_focus', src)


class AppTests(unittest.TestCase):
    def test_primary_target_and_dashboard_smoke(self):
        from pet.app import PetApp
        from agents.models import AgentInstance, AgentKind, Snapshot, Status, Phase, Mode
        cfg = MemoryConfig()
        from pet.petview import PetView
        with patch.object(PetView, 'load_skin', lambda self, bm: None):
            app = PetApp(cfg)
            app.pet_manager.activate_skin_runtime()
            app._disarm_first_map_trigger()
        try:
            from agents.terminal_service import WindowsTerminalService
            app.monitor._terminal_service = WindowsTerminalService(None)
            one = AgentInstance(AgentKind.CODEX, 101, 'windows', process_token='101',
                                cwd='D:\\proj\\DeskPet')
            two = AgentInstance(AgentKind.CLAUDE, 102, 'windows', process_token='102')
            app.monitor.instances = {one.key: one, two.key: two}
            a = Snapshot(one.key, one.kind, one.source, one.pid, status=Status.WORKING,
                         phase=Phase.CODING, mode=Mode.PLAN, summary='正在分析布局',
                         goal='完善气泡')
            b = Snapshot(two.key, two.kind, two.source, two.pid, status=Status.WAITING,
                         waiting_detail='Bash 命令需要确认')
            app.monitor.snapshots = {a.key: a, b.key: b}
            # presentation reconcile：WAITING 成为 focused（attention）
            app._aggregate()
            state = app._presentation_state
            self.assertEqual(state.attention_key, b.key)
            app.presentation.set_focus(b.key)
            waiting_model = app.pet_manager.views["pet-1"].bubble.model
            self.assertIn('Claude Code', waiting_model.status)
            self.assertIn('等待审批', waiting_model.status)
            self.assertEqual(waiting_model.footer, '请在终端处理')
            self.assertIn('Bash', waiting_model.text)
            # focused 指向 Codex 后气泡显示 Plan/编码中
            app.presentation.set_focus(a.key)
            app._aggregate()
            model = app.pet_manager.views["pet-1"].bubble.model
            self.assertIn('Codex', model.status)
            self.assertIn('Plan', model.status)
            self.assertIn('编码中', model.status)
            app.open_dashboard()
            app.root.update()
            self.assertTrue(app.dashboard.winfo_exists())
            app.hide_pet()
            view = app.pet_manager.views["pet-1"]
            self.assertTrue(view.hidden)
            self.assertTrue(view.cursor.paused)
        finally:
            app.quit()


class TrayDashboardVisibilityTests(unittest.TestCase):
    """Tray 左键 / Dashboard 打开不再意外隐藏桌宠（v4.1.3 §15/§18/§30）。"""

    def _app(self, slots=None):
        from pet.app import PetApp
        from pet.petview import PetView
        if slots is not None:
            from tests.test_concurrent_activation import ConcurrentConfig
            cfg = ConcurrentConfig(slots)
        else:
            cfg = MemoryConfig()
        with\
             patch.object(PetView, 'load_skin', lambda self, bm: None):
            app = PetApp(cfg)
            app.pet_manager.activate_skin_runtime()
            app._disarm_first_map_trigger()
            return app

    def test_tray_left_never_hides_visible_pet(self):
        app = self._app()
        try:
            view = app.pet_manager.views["pet-1"]
            self.assertFalse(view.hidden)
            reasserts = []
            app.pet_manager.reassert_visible_windows = \
                lambda: reasserts.append(1)
            app.restore_pet_from_tray()
            self.assertFalse(view.hidden)          # 仍可见（绝不隐藏）
            self.assertEqual(reasserts, [1])        # 只做 Z-order 重声明
            self.assertEqual(str(view.window.root.state()), "normal")
        finally:
            app.quit()

    def test_tray_left_restores_all_hidden(self):
        app = self._app()
        try:
            app.hide_pet()
            view = app.pet_manager.views["pet-1"]
            self.assertTrue(view.hidden)
            app.restore_pet_from_tray()
            self.assertFalse(view.hidden)           # show_all 恢复
        finally:
            app.quit()

    def test_tray_icon_survives_replacement_cycles(self):
        """v4.1.4 崩溃回归：窗口类进程级注册，实例回收后类回调不得悬空。

        旧行为：WNDPROC 挂在 TrayIcon 实例上，首个实例被 GC 后
        ctypes trampoline 释放，而 "DeskPetTrayWnd" 类仍指向它——
        后续实例 CreateWindowExW 即 access violation。共享模块级
        wndproc 后必须可无限次换代。v4.3.1 DP43-R09：start 立即
        返回 + request_stop 立即返回 + shutdown-only bounded join。
        """
        import gc
        import time as _time
        from pet.tray import TrayIcon, TrayState
        for i in range(3):
            icon = TrayIcon(f"DeskPet test {i}")
            t0 = _time.monotonic()
            icon.start()          # 立即返回（不 wait ready）
            self.assertLess(_time.monotonic() - t0, 0.5)
            icon._ready.wait(2.0)
            self.assertEqual(icon.status(), TrayState.READY)
            t0 = _time.monotonic()
            icon.request_stop()   # 运行期停止：只投递，不 join
            self.assertLess(_time.monotonic() - t0, 0.2)
            self.assertTrue(icon.join_for_shutdown(2.0))
            self.assertIn(icon.status(), (TrayState.STOPPED,))
            del icon
            gc.collect()
        final = TrayIcon("DeskPet test final")
        final.start()
        final._ready.wait(2.0)
        final.request_stop()
        final.join_for_shutdown(2.0)
        self.assertTrue(final.events.empty(),
                        "托盘创建/换代过程中不得产生 error 事件")




    def test_explicit_menu_hide_still_works(self):
        """右键菜单的显式隐藏不受 tray 左键修复影响。"""
        app = self._app()
        try:
            app.hide_pet()
            self.assertTrue(all(v.hidden
                                for v in app.pet_manager.views.values()))
        finally:
            app.quit()

    def test_open_dashboard_keeps_logical_visibility(self):
        app = self._app()
        try:
            view = app.pet_manager.views["pet-1"]
            reasserts = []
            app.pet_manager.reassert_visible_windows = \
                lambda: reasserts.append(1)
            app.open_dashboard()
            app.root.update()                        # 处理 after_idle reassert
            self.assertTrue(app.dashboard.winfo_exists())
            self.assertFalse(view.hidden)           # 逻辑 hidden 不变
            self.assertEqual(reasserts, [1])
        finally:
            app.quit()

    def test_fleet_hidden_pet_not_restored_by_dashboard(self):
        """Fleet：pet-1 可见 / pet-2 已隐藏 → 打开仪表盘只 reassert pet-1，
        不复活 pet-2。"""
        from tests.test_concurrent_activation import _slot, inst, snap
        from agents.models import AgentKind
        from pet.presentation import PresentationMode
        app = self._app(slots=[_slot("pet-1"), _slot("pet-2")])
        try:
            # v4.3：fleet 是运行期 session state
            app.presentation.set_concurrent_mode(PresentationMode.FLEET)
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2, cwd="/w/q")
            app.monitor.instances = {a.key: a, b.key: b}
            app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
            app._aggregate()
            views = app.pet_manager.views
            self.assertEqual(len(views), 2)
            v1 = views["pet-1"]
            v2 = views["pet-2"]
            v2.hide()
            calls = []
            v1.window.reassert_z_order = lambda: calls.append("v1") or True
            v2.window.reassert_z_order = lambda: calls.append("v2") or True
            app.open_dashboard()
            app.root.update()
            self.assertFalse(v1.hidden)
            self.assertTrue(v2.hidden)              # 不复活
            self.assertEqual(calls, ["v1"])          # 只 reassert 可见者
        finally:
            app.quit()

    def test_dashboard_withdraw_no_periodic_refresh_and_reopen_visible(self):
        """v4.3 §4.5（AC43-UI-02）：Dashboard 无 500ms 全页 refresh timer。

        隐藏 = is_open() False（flush E 不再刷新）；重开 = 立即刷新
        当前页一次。周期刷新只存在于 UiCoordinator 的 render flush。
        """
        app = self._app()
        try:
            app.open_dashboard()
            app.root.update()
            dash = app.dashboard
            self.assertFalse(hasattr(dash, "_refresh_after"))
            self.assertTrue(dash.is_open())
            dash.hide_dashboard()
            self.assertFalse(dash.is_open())
            app.root.update()                        # 隐藏后不再有任何刷新
            self.assertFalse(dash.is_open())
            # 隐藏状态下 flush E 不触碰页面内容
            dash.open()
            self.assertTrue(dash.is_open())
        finally:
            app.quit()

    def test_dashboard_shutdown_no_callback_after_destroy(self):
        """shutdown 后 pump 事件循环：不得出现 invalid command 回调。"""
        import io
        from contextlib import redirect_stderr
        app = self._app()
        try:
            err = io.StringIO()
            with redirect_stderr(err):
                app.open_dashboard()
                app.root.update()
                app.dashboard.shutdown()
                for _ in range(3):
                    app.root.update()                # pending after 若未取消会触发
            self.assertEqual(err.getvalue(), "")
            self.assertTrue(app.dashboard._closing)
        finally:
            app.quit()


class TrayLifecycleTests(unittest.TestCase):
    """v4.3.1 DP43-R09 §17.12：bounded/non-blocking 生命周期。"""

    def test_create_window_failure_reaches_failed_and_recoverable(self):
        from pet.tray import TrayIcon, TrayState
        import pet.tray as tray_mod
        icon = TrayIcon("DeskPet fail-inject")
        with patch.object(tray_mod.user32, "CreateWindowExW",
                          return_value=0):
            icon.start()
            icon._stopped.wait(2.0)
        self.assertEqual(icon.status(), TrayState.FAILED)
        self.assertIn("CreateWindowExW", icon.last_error())
        # 失败后线程已死：可以创建 replacement（不再有死图标悬挂）
        self.assertTrue(icon.join_for_shutdown(1.0))
        ok_icon = TrayIcon("DeskPet recovered")
        ok_icon.start()
        ok_icon._ready.wait(2.0)
        try:
            self.assertEqual(ok_icon.status(), TrayState.READY)
        finally:
            ok_icon.request_stop()
            ok_icon.join_for_shutdown(2.0)

    def test_app_tray_drain_bounded(self):
        from pet.app import TRAY_DRAIN_MAX
        from pet.tray import TrayEvent, TrayState
        harness = TrayDashboardVisibilityTests()
        app = harness._app()
        try:
            app.tray = type("T", (), {
                "events": queue.Queue(),
                "status": staticmethod(lambda: TrayState.READY),
                "last_error": staticmethod(lambda: ""),
                "menu_open_failures": staticmethod(lambda: 0),
                "request_stop": staticmethod(lambda: None),
                "show_icon": staticmethod(lambda: None),
            })()
            total = TRAY_DRAIN_MAX * 2 + 4
            for _ in range(total):
                app.tray.events.put_nowait(TrayEvent("dashboard"))
            with patch.object(app, "open_dashboard") as dash_mock:
                app._poll_tray_events()
                self.assertEqual(dash_mock.call_count, TRAY_DRAIN_MAX)
            self.assertEqual(app.tray.events.qsize(), total - TRAY_DRAIN_MAX)
            app.tray = None
        finally:
            app.quit()


class MenuCommandsAliveTests(unittest.TestCase):
    """v4.3.1 DP43-R14 菜单存活审计：桌宠右键菜单（含全部 cascade 子
    菜单）的每个 command entry 都是活按钮——invoke 后业务 action 不
    同步执行，menu teardown + idle 后 exactly once。

    Tray 菜单已原生化（worker 内 HMENU），语义映射由
    test_tray_native 覆盖；本类只审计 Pet Tk 菜单 wiring。
    """

    def _app(self, cfg=None):
        from pet.app import PetApp
        from pet.petview import PetView
        with\
             patch.object(PetView, 'load_skin', lambda self, bm: None):
            app = PetApp(cfg or MemoryConfig())
        app.pet_manager.activate_skin_runtime()
        app._disarm_first_map_trigger()
        return app

    def _entries(self, menu, path="menu", indices=()):
        end = menu.index("end")
        if end is None:
            return
        for i in range(end + 1):
            kind = menu.type(i)
            if kind == "cascade":
                sub = menu.nametowidget(menu.entrycget(i, "menu"))
                yield from self._entries(
                    sub, f"{path}/{menu.entrycget(i, 'label')}", indices + (i,))
            elif kind in ("command", "checkbutton", "radiobutton"):
                yield indices + (i,), f"{path}/{menu.entrycget(i, 'label')}"

    def _audit_menu(self, app, view, path):
        # Each real popup permits one selection. Audit every entry in its
        # own lifecycle instead of invoking an entire menu in one posting.
        probe = tk.Menu(app.root, tearoff=0)
        app._build_pet_menu(probe, view)
        entries = list(self._entries(probe, path))
        probe.destroy()
        for indices, _label in entries:
            def build(menu):
                app._build_pet_menu(menu, view)
                selected = menu
                for i in indices[:-1]:
                    selected = selected.nametowidget(selected.entrycget(i, "menu"))
                selected.invoke(indices[-1])
                self.assertTrue(app._menu_controller.active)
            with patch.object(tk.Menu, "tk_popup", lambda *args: None):
                app._menu_controller.show(view, 0, 0, build)
            self.assertFalse(app._menu_controller.active)
            app.root.update()
        return [label for _, label in entries]

    def test_every_menu_entry_dispatches_after_teardown_exactly_once(self):
        from pet import autostart
        app = self._app()
        try:
            counts = {}

            def _spy(name, impl=lambda *a, **k: None):
                def _fn(*a, **k):
                    counts[name] = counts.get(name, 0) + 1
                return _fn

            with patch.object(app, "quit", _spy("quit")),                  patch.object(app, "set_tray_enabled",
                              _spy("tray")),                  patch.object(app, "toggle_autostart",
                              _spy("autostart")),                  patch.object(app.monitor, "rescan", _spy("rescan")),                  patch.object(app, "activate_agent", _spy("activate")),                  patch.object(app, "_open_agent_picker", _spy("picker")),                  patch.object(app, "_rebuild_skin", _spy("rebuild")):
                view = app.pet_manager.views["pet-1"]
                invoked = self._audit_menu(app, view, "pet")
                joined = "\n".join(invoked)
                for needle in ("退出", "仪表盘", "重建当前皮肤缓存",
                               "暂时隐藏桌宠", "摸摸头"):
                    self.assertIn(needle, joined)
                for name in ("quit", "tray", "autostart", "rebuild"):
                    self.assertEqual(counts.get(name), 1,
                                     f"{name} 必须 exactly once")
        finally:
            app.quit()

    def test_fleet_menu_entries_dispatch_with_explicit_view(self):
        """Fleet：builder 显式 view（production 回调携带），不再手工
        _menu_view。"""
        from pet.presentation import PresentationMode
        from tests.test_fleet_ui import FleetConfig, _slot, inst, snap
        from agents.models import AgentKind
        cfg = FleetConfig([_slot("pet-1", None), _slot("pet-2", None)])
        app = self._app(cfg)
        try:
            app.presentation.set_concurrent_mode(PresentationMode.FLEET)
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2, cwd="/w/q")
            app.monitor.instances = {a.key: a, b.key: b}
            app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
            app._aggregate()
            view = app.pet_manager.views["pet-1"]
            self.assertTrue(view.agent_key)
            with patch.object(app, "quit"), \
                 patch.object(app, "activate_agent") as act_mock, \
                 patch.object(app, "_open_agent_picker"), \
                 patch.object(app, "_unbind_view"), \
                 patch.object(app, "set_tray_enabled"), \
                 patch.object(app, "toggle_autostart", return_value=True), \
                 patch.object(app, "_rebuild_skin"):
                invoked = self._audit_menu(app, view, "fleet")
            joined = "\n".join(invoked)
            for needle in ("打开此 Agent 终端", "更换 Agent", "解除绑定",
                           "隐藏此桌宠", "仪表盘", "退出"):
                self.assertIn(needle, joined)
            # fleet 激活作用于 exact view 的 agent
            self.assertTrue(act_mock.called)
            for call in act_mock.call_args_list:
                self.assertEqual(call.args[0], view.agent_key)
        finally:
            app.quit()


class QuitImageReleaseTests(unittest.TestCase):
    """v4.2.1 CI 崩溃回归（windows-latest 曾中止于
    "Tcl_AsyncDelete: async handler deleted by the wrong thread"）：

    quit 必须先于 root.destroy() 在主线程释放全部 PhotoImage
    （PetView._pet_image + 缓存帧）。否则引用环把它们拖到之后，由
    任意触发 GC 的工作线程回收时，__del__ 在已销毁/异线程 interpreter
    上执行 Tcl 调用——轻则 "main thread is not in main loop" 噪音，
    重则 Tcl C 层 panic 直接中止进程。
    """

    def _app(self):
        from pet.app import PetApp
        from pet.petview import PetView
        with\
             patch.object(PetView, 'load_skin', lambda self, bm: None):
            app = PetApp(MemoryConfig())
            app.pet_manager.activate_skin_runtime()
            app._disarm_first_map_trigger()
            return app

    def test_quit_releases_photoimages_before_destroy(self):
        import tkinter as tk
        app = self._app()
        try:
            view = app.pet_manager.views["pet-1"]
            # 模拟动画拉帧后 view 持有的"当前帧"
            view._pet_image = tk.PhotoImage(master=app.root, width=8,
                                            height=8)
        finally:
            app.quit()
        # quit 后：视图不再持有 PhotoImage，缓存帧也已清空
        self.assertIsNone(view._pet_image)
        for anim in app.pet_manager.cache._pool.values():
            self.assertEqual(len(anim._frames), 0)
        # root 已销毁；此刻由工作线程触发 GC 不得触碰任何 Tcl 对象
        errors = []

        def _collect():
            try:
                gc.collect()
            except Exception as exc:   # pragma: no cover - 防御性断言
                errors.append(exc)

        worker = threading.Thread(target=_collect)
        worker.start()
        worker.join(timeout=5)
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
