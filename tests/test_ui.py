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
from pet.animator import Animation, Animator
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
        """V3 不变量：winkeys 不提供任何键盘注入入口。"""
        for banned in ('send_key', 'send_input', 'keybd_event', 'SendInput'):
            self.assertFalse(hasattr(winkeys, banned))
        self.assertTrue(callable(winkeys.raise_window))

    def test_ambiguous_windows_do_not_bind_via_ancestors(self):
        """两个同 PID 窗口时祖先链不能唯一定位（由 TerminalResolver 处理 AMBIGUOUS）。"""
        from agents.terminal_uia import TerminalResolver, PaneInfo
        from agents.models import AgentInstance, AgentKind, BindingConfidence
        resolver = TerminalResolver(
            enum_windows=lambda: [(1, 10, 'Codex', 'CASCADIA_HOSTING_WINDOW_CLASS'),
                                  (2, 10, 'Codex', 'CASCADIA_HOSTING_WINDOW_CLASS')],
            ancestor_pids=lambda pid: {10},
        )
        inst = AgentInstance(AgentKind.CODEX, 99, 'windows', process_token='9')
        bindings = resolver.resolve([inst], {}, 1000.0)
        self.assertEqual(bindings[inst.key].confidence, BindingConfidence.AMBIGUOUS)

    def test_activation_failure_does_not_retry_or_inject(self):
        class User:
            def __init__(self): self.calls = []

            def IsWindow(self, h): return True

            def IsIconic(self, h): return True

            def ShowWindow(self, *args): self.calls.append('restore')

            def SetForegroundWindow(self, h): self.calls.append('activate')

            def GetForegroundWindow(self): return 99

            def FlashWindowEx(self, *args): self.calls.append('flash')
        user = User()
        with patch.object(winkeys, 'user32', user):
            self.assertFalse(winkeys.raise_window(1))
        self.assertEqual(user.calls, ['restore', 'activate', 'flash'])

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
                self.assertEqual(cfg.data["config_version"], 3)
                for banned in ("connection_mode", "managed", "keys",
                               "auto_approve", "approve_restore_focus",
                               "window_instances"):
                    self.assertNotIn(banned, cfg.data)
                self.assertEqual(cfg.data["monitor"]["pinned"], "")
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

    def test_bubble_has_no_approval_buttons(self):
        """V3：气泡彻底没有 [批准]/[拒绝] 按钮（plan §41）。"""
        b = self.bubble
        b.model = BubbleModel(visible=True, status='Claude Code · 等待审批',
                              text='Bash 命令需要确认', footer='请在终端处理')
        b.layout()
        b.draw(0, 0, 150, 140)
        self.assertEqual(set(b.btn_boxes), {'details'})
        self.assertFalse(hasattr(b.model, 'approve_label'))
        self.assertFalse(hasattr(b.model, 'deny_label'))

    def test_hidden_bubble_clears(self):
        b = self.bubble
        b.model = BubbleModel(visible=True, text='a', footer='f')
        b.layout()
        b.draw(0, 0, 150, 140)
        self.assertTrue(b.btn_boxes)
        b.model.visible = False
        b.draw(0, 0, 150, 140)
        self.assertEqual(len(self.canvas.find_all()), 0)
        self.assertFalse(b.btn_boxes)

    def test_animation_byte_budget_and_pause(self):
        import tkinter as tk
        animator = Animator(self.root)
        anim = Animation('unused', {'width': 10, 'height': 10, 'frames': 10})
        animator._pool['unused'] = anim
        animator.current = anim
        animator.cache_bytes = 800
        photo_factory = tk.PhotoImage
        with patch('pet.animator.tk.PhotoImage', side_effect=lambda **_: photo_factory(master=self.root, width=10, height=10)):
            for i in range(10):
                animator._idx = i
                animator.frame_image()
        self.assertLessEqual(len(anim._frames), 2)
        animator._schedule()
        self.assertIsNotNone(animator._after_id)
        animator.set_paused(True)
        self.assertIsNone(animator._after_id)
        animator.set_paused(False)
        self.assertIsNotNone(animator._after_id)
        animator.stop()


class AppTests(unittest.TestCase):
    def test_primary_target_and_dashboard_smoke(self):
        from pet.app import PetApp
        from agents.models import AgentInstance, AgentKind, Snapshot, Status, Phase, Mode
        cfg = MemoryConfig()
        with patch.object(PetApp, '_apply_skin', lambda self: None):
            app = PetApp(cfg)
        try:
            app.monitor._terminal = None
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
            app.monitor._select_primary()
            # 自动跟随：WAITING 抢占 WORKING（plan §31）
            self.assertEqual(app.monitor.primary_key, b.key)
            waiting_model = app._bubble_model()
            self.assertIn('Claude Code', waiting_model.status)
            self.assertIn('等待审批', waiting_model.status)
            self.assertEqual(waiting_model.footer, '请在终端处理')
            self.assertIn('Bash', waiting_model.text)
            # 钉住 Codex 后气泡显示 Plan/编码中
            app.monitor.set_primary(a.key, manual=True)
            target = app.monitor.primary_target()
            self.assertIsNotNone(target)
            model = app._bubble_model()
            self.assertIn('Codex', model.status)
            self.assertIn('Plan', model.status)
            self.assertIn('编码中', model.status)
            app.open_dashboard()
            app.root.update()
            self.assertTrue(app.dashboard.winfo_exists())
            app.hide_pet()
            self.assertTrue(app.animator.paused)
        finally:
            app.quit()


if __name__ == '__main__':
    unittest.main()
