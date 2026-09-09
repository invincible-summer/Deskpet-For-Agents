"""Headless model checks and real Tk smoke tests (Windows Python). V3: no approvals."""
import copy
import gc
import os
from pathlib import Path
import sys
import threading
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

            def ShowWindow(self, *args): self.calls.append('restore')

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
    """v4.2.3 §9：tray/pet popup menu 的确定性销毁与 churn 上限。"""

    def _app(self):
        from pet.app import PetApp
        from pet.petview import PetView
        cfg = MemoryConfig()
        with patch.object(PetApp, '_reload_skins', lambda self: None), \
             patch.object(PetView, 'load_skin', lambda self, bm: None):
            return PetApp(cfg)

    def test_dismiss_destroys_and_resets_active_menu(self):
        app = self._app()
        try:
            import tkinter as tkmod
            menu = tkmod.Menu(app.root, tearoff=0)
            app._active_menu = menu
            path = menu._w
            app._dismiss_active_menu()
            self.assertIsNone(app._active_menu)
            self.assertEqual(app.root.tk.call('winfo', 'exists', path), 0)
            # 幂等：重复 dismiss 不抛
            app._dismiss_active_menu()
            app._destroy_menu(None)
        finally:
            app.quit()

    def test_tray_menu_uses_tk_popup_and_destroys_in_finally(self):
        app = self._app()
        try:
            destroyed = []
            popups = []
            # v4.3 修复：spy 只记录调用，绝不递归真实 tk_popup——
            # 测试进程无前台状态，真弹的菜单点击外部不收起，会在
            # 屏幕上留下悬浮幽灵菜单（用户实测卡屏）。
            popup_args = []

            def spy_popup(self, x, y, entry=""):
                popups.append(self._w)
                popup_args.append((x, y))
            with patch('tkinter.Menu.tk_popup', spy_popup):
                app._tray_menu()
            self.assertEqual(len(popups), 1)          # tk_popup 而非 post
            self.assertEqual(len(popup_args), 1)      # 恰一次、带坐标
            self.assertTrue(popup_args[0][0] > 0 and popup_args[0][1] > 0)
            self.assertIsNone(app._active_menu)       # finally 清空
            # 菜单 widget 已销毁
            self.assertEqual(app.root.tk.call('winfo', 'exists', popups[0]), 0)
        finally:
            app.quit()

    def test_second_tray_menu_destroys_previous(self):
        app = self._app()
        try:
            seen = []

            def spy_popup(self, x, y, entry=""):
                seen.append(self._w)
            with patch('tkinter.Menu.tk_popup', spy_popup):
                app._tray_menu()
                first = seen[0]
                self.assertEqual(app.root.tk.call('winfo', 'exists', first), 0)
                app._tray_menu()
            # 旧 popup 已确定性销毁：任何时刻 _active_menu 最多 1 个
            self.assertEqual(app.root.tk.call('winfo', 'exists', first), 0)
            self.assertIsNone(app._active_menu)
        finally:
            app.quit()

    def test_menu_churn_1000_no_widget_growth_or_tclerror(self):
        app = self._app()
        try:
            import tkinter as tkmod
            paths = []
            for _ in range(1000):
                menu = tkmod.Menu(app.root, tearoff=0)
                app._active_menu = menu
                paths.append(menu._w)
                app._dismiss_active_menu()
            self.assertIsNone(app._active_menu)
            # 所有历史 menu widget 均已销毁（无线性增长）
            alive = [p for p in paths
                     if app.root.tk.call('winfo', 'exists', p)]
            self.assertEqual(alive, [])
            # 无新增 after timer（菜单生命周期不靠定时器）
        finally:
            app.quit()


class MenuForegroundPrepTests(unittest.TestCase):
    """v4.3：tk_popup 前的 Win32 前台准备。

    无前台状态的 TrackPopupMenu（Tk 菜单 grab）点击菜单外不收起——
    菜单滞留且抓住全部 Tk 输入，必须点菜单本身才能消掉（用户实测
    卡死）。prepare 必须在 popup 前、finish 在 finally 中。
    """

    def _app(self):
        from pet.app import PetApp
        from pet.petview import PetView
        cfg = MemoryConfig()
        with patch.object(PetApp, '_reload_skins', lambda self: None), \
             patch.object(PetView, 'load_skin', lambda self, bm: None):
            return PetApp(cfg)

    def test_tray_menu_prepares_foreground_before_popup(self):
        from actions import winkeys as wk
        app = self._app()
        try:
            calls = []

            def spy_popup(self, x, y, entry=""):
                calls.append("popup")

            class _FakeTray:
                menu_hwnd = 4321

            app.tray = _FakeTray()
            with patch.object(wk, 'prepare_menu_popup',
                              lambda h: calls.append(("prepare", h))
                              or True), \
                 patch.object(wk, 'finish_menu_popup',
                              lambda h: calls.append(("finish", h))), \
                 patch('tkinter.Menu.tk_popup', spy_popup):
                app._tray_menu()
            self.assertEqual(calls[0], ("prepare", 4321))
            self.assertEqual(calls[1], "popup")
            self.assertEqual(calls[-1], ("finish", 4321))
            self.assertIsNone(app._active_menu)   # finally 仍确定性销毁
        finally:
            app.tray = None
            app.quit()

    def test_tray_menu_without_tray_skips_dance(self):
        from actions import winkeys as wk
        app = self._app()
        try:
            calls = []
            with patch.object(wk, 'prepare_menu_popup',
                              lambda h: calls.append(h)), \
                 patch.object(wk, 'finish_menu_popup',
                              lambda h: calls.append(h)), \
                 patch('tkinter.Menu.tk_popup',
                       lambda self, x, y, entry="": None):
                app._tray_menu()
            self.assertEqual(calls, [])   # 无托盘句柄 → 不做前台操作
        finally:
            app.quit()

    def test_pet_menu_prepares_foreground_before_popup(self):
        from actions import winkeys as wk
        app = self._app()
        try:
            view = app.pet_manager.views["pet-1"]
            view.window.on_menu = lambda menu: None
            calls = []

            class _Ev:
                x_root, y_root = 10, 20

            with patch.object(wk, 'prepare_menu_popup',
                              lambda h: calls.append(("prepare", h))
                              or True), \
                 patch.object(wk, 'finish_menu_popup',
                              lambda h: calls.append(("finish", h))), \
                 patch('tkinter.Menu.tk_popup',
                       lambda self, x, y, entry="":
                       calls.append("popup")):
                view.window._on_menu(_Ev())
            kinds = [c if isinstance(c, str) else c[0]
                     for c in calls]
            self.assertEqual(kinds, ["prepare", "popup", "finish"])
        finally:
            app.quit()


class DashboardAutoCollapseTests(unittest.TestCase):
    """v4.3 用户反馈：仪表盘失去焦点时自动收起（_maybe_auto_collapse）。

    只有真正拿到过焦点（_had_focus）且前台已离开本窗口才收起；
    原生对话框期间与从未获焦的窗口（CI/测试）不收起。
    """

    def _app(self):
        from pet.app import PetApp
        from pet.petview import PetView
        cfg = MemoryConfig()
        with patch.object(PetApp, '_reload_skins', lambda self: None), \
             patch.object(PetView, 'load_skin', lambda self, bm: None):
            return PetApp(cfg)

    def test_collapses_when_foreground_left_after_focus(self):
        from actions import winkeys as wk
        app = self._app()
        try:
            app.open_dashboard()
            dash = app.dashboard
            dash._had_focus = True
            own = int(dash.winfo_id())
            with patch.object(wk, 'foreground_window',
                              lambda: own + 404):
                dash._maybe_auto_collapse()
            self.assertFalse(dash.is_open())
        finally:
            app.quit()

    def test_stays_open_when_foreground_is_self(self):
        from actions import winkeys as wk
        app = self._app()
        try:
            app.open_dashboard()
            dash = app.dashboard
            dash._had_focus = True
            own = int(dash.winfo_id())
            with patch.object(wk, 'foreground_window', lambda: own):
                dash._maybe_auto_collapse()
            self.assertTrue(dash.is_open())
        finally:
            app.quit()

    def test_never_focused_dashboard_stays_open(self):
        """CI/测试环境窗口从未获得焦点 → 绝不误收起。"""
        from actions import winkeys as wk
        app = self._app()
        try:
            app.open_dashboard()
            dash = app.dashboard
            dash._had_focus = False
            own = int(dash.winfo_id())
            with patch.object(wk, 'foreground_window',
                              lambda: own + 404):
                dash._maybe_auto_collapse()
            self.assertTrue(dash.is_open())
        finally:
            app.quit()

    def test_native_dialog_blocks_collapse(self):
        from actions import winkeys as wk
        app = self._app()
        try:
            app.open_dashboard()
            dash = app.dashboard
            dash._had_focus = True
            dash._native_dialog_open = True
            own = int(dash.winfo_id())
            with patch.object(wk, 'foreground_window',
                              lambda: own + 404):
                dash._maybe_auto_collapse()
            self.assertTrue(dash.is_open())
        finally:
            app.quit()


class AppTests(unittest.TestCase):
    def test_primary_target_and_dashboard_smoke(self):
        from pet.app import PetApp
        from agents.models import AgentInstance, AgentKind, Snapshot, Status, Phase, Mode
        cfg = MemoryConfig()
        from pet.petview import PetView
        with patch.object(PetApp, '_reload_skins', lambda self: None), patch.object(PetView, 'load_skin', lambda self, bm: None):
            app = PetApp(cfg)
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
        with patch.object(PetApp, '_reload_skins', lambda self: None), \
             patch.object(PetView, 'load_skin', lambda self, bm: None):
            return PetApp(cfg)

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
        wndproc 后必须可无限次换代。
        """
        import gc
        from pet.tray import TrayIcon
        for i in range(3):
            icon = TrayIcon(f"DeskPet test {i}")
            icon.start()
            icon.stop()
            del icon
            gc.collect()
        final = TrayIcon("DeskPet test final")
        final.start()
        final.stop()
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
        with patch.object(PetApp, '_reload_skins', lambda self: None), \
             patch.object(PetView, 'load_skin', lambda self, bm: None):
            return PetApp(MemoryConfig())

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
