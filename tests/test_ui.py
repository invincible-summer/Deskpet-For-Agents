"""Headless model checks and real Tk smoke tests (Windows Python). V3: no approvals."""
import copy
import os
from pathlib import Path
import sys
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
                self.assertEqual(cfg.data["config_version"], 4)
                for banned in ("connection_mode", "managed", "keys",
                               "auto_approve", "approve_restore_focus",
                               "window_instances"):
                    self.assertNotIn(banned, cfg.data)
                self.assertNotIn("pinned", cfg.data["monitor"])
                self.assertNotIn("gone_grace_sec", cfg.data["monitor"])
                self.assertFalse(cfg.data["presentation"]["concurrent"]["enabled"])
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

    def tearDown(self):
        self.root.destroy()

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
        app = self._app(slots=[_slot("pet-1"), _slot("pet-2")])
        try:
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

    def test_dashboard_withdraw_stops_refresh_timer_and_reopen_restarts(self):
        app = self._app()
        try:
            app.open_dashboard()
            app.root.update()
            self.assertIsNotNone(app.dashboard._refresh_after)
            app.dashboard.hide_dashboard()
            self.assertIsNone(app.dashboard._refresh_after)
            app.root.update()                        # 隐藏后绝不自启
            self.assertIsNone(app.dashboard._refresh_after)
            app.dashboard.open()
            app.root.update()
            self.assertIsNotNone(app.dashboard._refresh_after)  # 重开重启
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
                self.assertIsNotNone(app.dashboard._refresh_after)
                app.dashboard.shutdown()
                for _ in range(3):
                    app.root.update()                # pending after 若未取消会触发
            self.assertEqual(err.getvalue(), "")
            self.assertTrue(app.dashboard._closing)
            self.assertIsNone(app.dashboard._refresh_after)
        finally:
            app.quit()


if __name__ == '__main__':
    unittest.main()
