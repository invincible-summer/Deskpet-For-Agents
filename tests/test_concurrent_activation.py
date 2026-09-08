"""并发激活测试矩阵（v4.1.1 plan §20）。

  * Fleet body 双击 → 各自 exact agent_key（绝不走 focused_key）；
  * Fleet 气泡底行 → 各自 exact agent_key；
  * 不同 Agent 映射不同 HWND → try_set_foreground 用各自 exact hwnd；
  * 两个 Agent 同属一个 Terminal window → 都前置同一窗口且不切 Tab
    （window-level 语义，防止后续又当 bug 重引入 exact tab selection）；
  * Aggregate 气泡 race：点击瞬间 attention 变化不改变激活目标
    （visual identity == click identity，由 model.agent_key 固化）。
"""
from __future__ import annotations
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.models import (
    AgentInstance,
    AgentKind,
    Snapshot,
    Status,
    TerminalWindowBinding,
    WindowBindingConfidence,
    WindowIdentity,
)
from agents.terminal_service import WindowsTerminalService
from agents.terminal_uia import WT_WINDOW_CLASS

NOW = 1_000_000.0


class ConcurrentConfig:
    def __init__(self, slots):
        self.data = {
            "skin": "amiya", "scale": 1.0, "speed": 1.0, "animated": True,
            "topmost": True, "tray_enabled": False, "pet_pos": None,
            "bubble": {"enabled": True, "font_family": "Microsoft YaHei UI",
                       "font_size": 11, "height": 132, "width": 300,
                       "relative_width": 1.0, "relative_height": 1.0,
                       "relative_font": 1.0},
            "animation_cache_mb": 8,
            "presentation": {"concurrent": {
                "enabled": True, "mode": "fleet", "max_targets": 3,
                "eligible_kinds": {"codex": True, "claude": True,
                                   "kimi": True, "pi": True},
                "slots": slots}},
        }
        self.migration_notice = False
        self.saved = 0

    def get(self, path, default=None):
        node = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path, value):
        parts = path.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def save(self):
        self.saved += 1
        return type("R", (), {"ok": True, "path": "", "error": ""})()


def _slot(slot_id):
    return {"id": slot_id, "selector": None, "appearance": None,
            "placement": {"monitor": "", "u": None, "v": None,
                          "anchor": None, "manual": False}}


def inst(kind, pid, cwd="/w/p"):
    return AgentInstance(kind, pid, "wsl:Ubuntu", cwd=cwd,
                         process_token=str(pid), started_at=NOW - pid)


def snap(i, status=Status.WORKING):
    return Snapshot(i.key, i.kind, i.source, i.pid, status=status, ts=NOW)


def make_app(slots):
    from pet.app import PetApp
    from pet.petview import PetView
    cfg = ConcurrentConfig(slots)
    with patch.object(PetApp, "_reload_skins", lambda self: None), \
         patch.object(PetView, "load_skin", lambda self, bm: None):
        app = PetApp(cfg)
    return app


def bind_window(hwnd, pid=100, created=1.0):
    return TerminalWindowBinding(
        window=WindowIdentity(hwnd=hwnd, pid=pid, process_created=created,
                              window_class=WT_WINDOW_CLASS),
        confidence=WindowBindingConfidence.CONFIRMED,
        title="wt", last_seen=NOW, validated_at=NOW,
        reason="windows-ancestor")


class ForegroundRecorder:
    """记录 restore/foreground/flash 调用（mock 三件套）。"""

    def __init__(self):
        self.foreground = []
        self.restores = []
        self.flashes = []

    def patches(self):
        return [
            patch("actions.winkeys.validate_window", return_value=True),
            patch("actions.winkeys.restore_window",
                  side_effect=self.restores.append),
            patch("actions.winkeys.try_set_foreground",
                  side_effect=self._foreground),
            patch("actions.winkeys.flash_window",
                  side_effect=self.flashes.append),
        ]

    def _foreground(self, hwnd):
        self.foreground.append(hwnd)
        return True


class FleetBodyActivationTests(unittest.TestCase):
    def test_each_pet_double_click_activates_its_own_agent(self):
        app = make_app([_slot("pet-1"), _slot("pet-2")])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2)
            app.monitor.instances = {a.key: a, b.key: b}
            app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
            app._aggregate()
            view1 = app.pet_manager.views["pet-1"]
            view2 = app.pet_manager.views["pet-2"]
            activated = []
            view1._on_activate = activated.append
            view2._on_activate = activated.append
            view1._on_double()
            view2._on_double()
            # pet-1/pet-2 各自 exact key（自动分配按注意力排序）
            keys = {view1.agent_key, view2.agent_key}
            self.assertEqual(activated[0], view1.agent_key)
            self.assertEqual(activated[1], view2.agent_key)
            self.assertEqual(keys, {a.key, b.key})
        finally:
            app.quit()

    def test_activation_never_consults_focused_key(self):
        """focused_key 指向 B 时，双击 pet-1 仍激活 pet-1 的 Agent。"""
        app = make_app([_slot("pet-1"), _slot("pet-2")])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2)
            app.monitor.instances = {a.key: a, b.key: b}
            app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
            app._aggregate()
            app.presentation.set_focus(b.key)   # 用户选择 B
            view1 = app.pet_manager.views["pet-1"]
            if view1.agent_key == b.key:
                self.skipTest("pet-1 绑定的就是 focused Agent")
            activated = []
            view1._on_activate = activated.append
            view1._on_double()
            self.assertEqual(activated, [view1.agent_key])
        finally:
            app.quit()


class FleetBubbleActivationTests(unittest.TestCase):
    def test_bubble_hit_carries_view_agent_key(self):
        app = make_app([_slot("pet-1"), _slot("pet-2")])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2, cwd="/w/q")
            app.monitor.instances = {a.key: a, b.key: b}
            app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
            app._aggregate()
            app.root.update()
            for view in app.pet_manager.views.values():
                view.redraw()
                # 气泡模型 key == view key（visual identity == click identity）
                self.assertEqual(view.bubble.model.agent_key, view.agent_key)
                self.assertTrue(view.bubble._hit_boxes)
                box = view.bubble._hit_boxes[0][0]
                tag = view._hit((box[0] + box[2]) // 2,
                                (box[1] + box[3]) // 2)
                self.assertEqual(tag, ("activate", view.agent_key))
        finally:
            app.quit()


class WindowIndependenceTests(unittest.TestCase):
    """§20.3：不同 Agent 不同 HWND → 各自前置；绝不串窗口。"""

    def _prepare(self, app, a, b, hwnd_a, hwnd_b):
        service = WindowsTerminalService(None)
        service._window_bindings = {
            a.key: bind_window(hwnd_a), b.key: bind_window(hwnd_b)}
        service._last_instances = [a, b]
        app.monitor._terminal_service = service
        app.monitor.instances = {a.key: a, b.key: b}
        app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
        app._aggregate()
        return service

    def test_pet_click_foregrounds_its_own_hwnd(self):
        app = make_app([_slot("pet-1"), _slot("pet-2")])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2, cwd="/w/q")
            self._prepare(app, a, b, hwnd_a=101, hwnd_b=202)
            rec = ForegroundRecorder()
            with rec.patches()[0], rec.patches()[1], rec.patches()[2], \
                 rec.patches()[3]:
                for view in app.pet_manager.views.values():
                    app.activate_agent(view.agent_key)
            hwnd_by_key = {a.key: 101, b.key: 202}
            # 每个 Agent 的 key 恰好前置自己的 hwnd
            self.assertEqual(len(rec.foreground), 2)
            self.assertNotEqual(rec.foreground[0], rec.foreground[1])
            self.assertEqual(sorted(rec.foreground), [101, 202])
            # 反向验证：再次点击 pet-1 只动 101
            view1 = next(v for v in app.pet_manager.views.values()
                         if hwnd_by_key[v.agent_key] == 101)
            rec2 = ForegroundRecorder()
            with rec2.patches()[0], rec2.patches()[1], rec2.patches()[2], \
                 rec2.patches()[3]:
                app.activate_agent(view1.agent_key)
            self.assertEqual(rec2.foreground, [101])
        finally:
            app.quit()

    def test_same_window_semantics_documented(self):
        """§20.4：两个 Agent 同属一个 WT 窗口 → 都前置同一窗口。

        这是 window-level 语义的正确行为：DeskPet 不承诺自动切换到
        各自的 Tab/Pane。该断言显式记录语义，防止后续开发把"点击
        两个 Pet 都前置同一窗口"当成 bug 重引入 exact tab selection。
        """
        app = make_app([_slot("pet-1"), _slot("pet-2")])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2, cwd="/w/q")
            self._prepare(app, a, b, hwnd_a=101, hwnd_b=101)   # 同一窗口
            rec = ForegroundRecorder()
            with rec.patches()[0], rec.patches()[1], rec.patches()[2], \
                 rec.patches()[3]:
                for view in app.pet_manager.views.values():
                    app.activate_agent(view.agent_key)
            self.assertEqual(rec.foreground, [101, 101])
        finally:
            app.quit()


class AggregateBubbleRaceTests(unittest.TestCase):
    """§20.5：Aggregate 气泡使用绘制时固化的 exact key。"""

    def test_click_uses_drawn_key_not_new_attention(self):
        app = make_app([_slot("pet-1")])
        try:
            # aggregate 模式
            app.config.data["presentation"]["concurrent"]["mode"] = "aggregate"
            app.config.data["presentation"]["concurrent"]["enabled"] = True
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2, cwd="/w/q")
            app.monitor.instances = {a.key: a, b.key: b}
            app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
            app._aggregate()      # 绘制：attention（无 focused）= 先启动的 a
            view = app.pet_manager.views["pet-1"]
            drawn_key = view.bubble.model.agent_key
            other = b if drawn_key == a.key else a
            self.assertTrue(view.bubble._hit_boxes)
            box = view.bubble._hit_boxes[0][0]
            # 绘制之后、点击之前 attention 变化（另一个 Agent 变 WAITING）；
            # 不触发重绘——点击必须使用绘制时固化的 key，不能现场重算。
            app.monitor.snapshots[other.key] = snap(other, Status.WAITING)
            activated = []
            view._on_activate = activated.append
            tag = view._hit((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
            view._on_hit_tag(tag)
            self.assertEqual(tag, ("activate", drawn_key))
            self.assertEqual(activated, [drawn_key])
        finally:
            app.quit()


if __name__ == "__main__":
    unittest.main()
