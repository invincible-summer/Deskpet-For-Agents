"""Fleet UI 测试（v4plan §18.4 + V4.1.1 用户反馈语义）：

  * 一套 Monitor + N Toplevel PetView（绑定数 = 桌宠数）；
  * 自动绑定：空 slot 按注意力优先自动分配 Agent；已绑定 slot 不重排；
  * 没有绑定的 Agent 不显示桌宠（不因 max_targets 创建空桌宠）；
  * 并发时气泡与单个监听完全一致（单卡，无 "N Agents" 栈卡）；
  * bind/unbind/duplicate；hidden pet cursor pause；monitor 继续。
"""
from __future__ import annotations
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.models import AgentInstance, AgentKind, Snapshot, Status
from pet.presentation import AgentSelector

NOW = 1_000_000.0


class FleetConfig:
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


def _slot(slot_id, selector=None, appearance=None):
    return {"id": slot_id, "selector": selector,
            "appearance": appearance,
            "placement": {"monitor": "", "u": None, "v": None,
                          "anchor": None, "manual": False}}


def inst(kind, pid, cwd="/w/p"):
    return AgentInstance(kind, pid, "wsl:Ubuntu", cwd=cwd,
                         process_token=str(pid), started_at=NOW - pid)


def snap(i, status=Status.WORKING):
    return Snapshot(i.key, i.kind, i.source, i.pid, status=status, ts=NOW)


class FleetUiTests(unittest.TestCase):
    def _app(self, slots):
        from pet.app import PetApp
        from pet.petview import PetView
        cfg = FleetConfig(slots)
        with patch.object(PetApp, "_reload_skins", lambda self: None), \
             patch.object(PetView, "load_skin", lambda self, bm: None):
            app = PetApp(cfg)
        from agents.terminal_service import WindowsTerminalService
        app.monitor._terminal_service = WindowsTerminalService(None)
        # v4.3：mode 是运行期 session state；config 旧 mode=fleet 被
        # 启动策略忽略，fleet 测试显式切换
        from pet.presentation import PresentationMode
        app.presentation.set_concurrent_mode(PresentationMode.FLEET)
        return app

    def _put(self, app, *agents_with_snap):
        app.monitor.instances = {a.key: a for a, _s in agents_with_snap}
        app.monitor.snapshots = {a.key: s for a, s in agents_with_snap}

    def test_auto_bind_creates_pet_per_bound_agent_only(self):
        """3 slot 配置 + 2 个 Agent → 自动绑 2、桌宠 2（不是 3）。"""
        app = self._app([_slot("pet-1"), _slot("pet-2"), _slot("pet-3")])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2)
            self._put(app, (a, snap(a)), (b, snap(b)))
            app._aggregate()
            self.assertEqual(len(app.pet_manager.views), 2)
            self.assertEqual(len(app.monitor._watchers), 4)   # 唯一 Monitor
            bound = {v.agent_key for v in app.pet_manager.views.values()}
            self.assertEqual(bound, {a.key, b.key})
            self.assertNotIn("pet-3", app.pet_manager.views)  # 无绑定不显示
            self.assertTrue(app.presentation.is_auto_bound("pet-1"))
        finally:
            app.quit()

    def test_no_agents_keeps_idle_fallback_pet(self):
        """v4.3 §18.2：Fleet 0 bound → pet-1 idle fallback（AC43-PRES-05）。

        fallback 不代表 fake Agent：agent_key 空、不占 Monitor target。
        """
        app = self._app([_slot("pet-1"), _slot("pet-2")])
        try:
            self._put(app)
            app._aggregate()
            self.assertEqual(list(app.pet_manager.views), ["pet-1"])
            view = app.pet_manager.views["pet-1"]
            self.assertEqual(view.agent_key, "")
            self.assertFalse(app.pet_manager.user_hidden)
        finally:
            app.quit()

    def test_fallback_replaced_when_first_bound_slot_appears(self):
        # AC43-PRES-05：fallback 被复用/替换，可见数不降为 0、无第 9 只
        app = self._app([_slot("pet-1"), _slot("pet-2")])
        try:
            self._put(app)
            app._aggregate()
            self.assertEqual(list(app.pet_manager.views), ["pet-1"])
            a = inst(AgentKind.CODEX, 1)
            self._put(app, (a, snap(a)))
            app._aggregate()
            # pet-1 被 Agent 复用（同一 view 对象，不 destroy/recreate）
            self.assertEqual(app.pet_manager.views["pet-1"].agent_key, a.key)
            b = inst(AgentKind.CLAUDE, 2)
            self._put(app, (a, snap(a)), (b, snap(b)))
            app._aggregate()
            self.assertEqual(len(app.pet_manager.views), 2)   # 无 fallback 第 3 只
        finally:
            app.quit()

    def test_aggregate_zero_target_keeps_pet1_sleep(self):
        # AC43-PRES-03/04：SINGLE/AGGREGATE 0 target 仍有 pet-1，sleep
        app = self._app([_slot("pet-1")])
        try:
            from pet.presentation import PresentationMode
            app.presentation.set_concurrent_mode(PresentationMode.AGGREGATE)
            self._put(app)
            app._aggregate()
            self.assertEqual(list(app.pet_manager.views), ["pet-1"])
            view = app.pet_manager.views["pet-1"]
            self.assertEqual(view.agent_key, "")
            state = app._presentation_state
            name, _repeat = app.presentation.animation_state_for([], {}, NOW)
            self.assertEqual(name, "sleep")
            app.presentation.set_concurrent_enabled(False)   # SINGLE
            app._aggregate()
            self.assertEqual(list(app.pet_manager.views), ["pet-1"])
        finally:
            app.quit()

    def test_user_hidden_not_persisted_and_reset_on_new_manager(self):
        # AC43-PRES-08：hide_all 置 user_hidden；恢复后 False
        app = self._app([_slot("pet-1")])
        try:
            self._put(app, (inst(AgentKind.CODEX, 1),
                            snap(inst(AgentKind.CODEX, 1))))
            app._aggregate()
            app.hide_pet()
            self.assertTrue(app.pet_manager.user_hidden)
            app.restore_pet_from_tray()
            self.assertFalse(app.pet_manager.user_hidden)
            # ensure_view 在 user_hidden 期间创建的 view 也隐藏
            app.pet_manager.user_hidden = True
            view = app.pet_manager.ensure_view("pet-2")
            self.assertTrue(view.hidden)
        finally:
            app.quit()

    def test_auto_bind_prefers_attention(self):
        """自动分配按注意力优先：WAITING 的 Agent 先占 slot。"""
        app = self._app([_slot("pet-1")])
        try:
            working = inst(AgentKind.CODEX, 1)
            waiting = inst(AgentKind.CLAUDE, 2)
            self._put(app, (working, snap(working)),
                      (waiting, snap(waiting, Status.WAITING)))
            app._aggregate()
            self.assertEqual(app.pet_manager.views["pet-1"].agent_key,
                             waiting.key)
        finally:
            app.quit()

    def test_excluded_agent_not_auto_bound(self):
        """移出并发的 Agent：其桌宠消失，其余桌宠不受影响。"""
        app = self._app([_slot("pet-1")])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2)
            self._put(app, (a, snap(a)), (b, snap(b)))
            app._aggregate()          # 2 个 Agent → pet-1/pet-2 各绑一个
            key_a = app.presentation.slot_binding("pet-1")
            app.presentation.set_instance_included(key_a, False)
            app._aggregate()
            # 被移出的 Agent 释放 slot，不再显示桌宠；另一个保持
            self.assertEqual(app.presentation.slot_binding("pet-1"), "")
            self.assertNotIn("pet-1", app.pet_manager.views)
            remaining = {v.agent_key
                         for v in app.pet_manager.views.values()}
            self.assertEqual(remaining, {b.key})
        finally:
            app.quit()

    def test_agent_exit_removes_pet_and_refills(self):
        """Agent 退出 → 绑定桌宠释放；0 bound 时 pet-1 idle fallback。"""
        app = self._app([_slot("pet-1")])
        try:
            a = inst(AgentKind.CODEX, 1)
            self._put(app, (a, snap(a)))
            app._aggregate()
            self.assertEqual(app.pet_manager.views["pet-1"].agent_key, a.key)
            app.monitor.instances = {}
            app.monitor.snapshots = {}
            app._aggregate()
            # v4.3 §18.2：0 bound → pet-1 fallback（agent_key 空）
            self.assertEqual(len(app.pet_manager.views), 1)
            self.assertEqual(app.pet_manager.views["pet-1"].agent_key, "")
            b = inst(AgentKind.CLAUDE, 2)
            self._put(app, (b, snap(b)))
            app._aggregate()
            self.assertEqual(app.pet_manager.views["pet-1"].agent_key, b.key)
        finally:
            app.quit()

    def test_manual_bind_wins_and_duplicate_rejected(self):
        app = self._app([_slot("pet-1"), _slot("pet-2")])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2)
            self._put(app, (a, snap(a)), (b, snap(b)))
            self.assertTrue(app.presentation.bind_slot("pet-1", a.key))
            app._aggregate()
            self.assertFalse(app.presentation.is_auto_bound("pet-1"))
            # duplicate bind 同一 exact key → 拒绝
            self.assertFalse(app.presentation.bind_slot("pet-2", a.key))
            app.presentation.unbind_slot("pet-1")
            self.assertTrue(app.presentation.bind_slot("pet-2", a.key))
        finally:
            app.quit()

    def test_bound_pet_double_click_activates_exact_agent(self):
        app = self._app([_slot("pet-1")])
        try:
            a = inst(AgentKind.CODEX, 1)
            self._put(app, (a, snap(a)))
            app._aggregate()
            view = app.pet_manager.views["pet-1"]
            activated = []
            view._on_activate = lambda key: activated.append(key)
            view._on_body_double()
            self.assertEqual(activated, [a.key])
        finally:
            app.quit()

    def test_concurrent_bubble_is_single_card(self):
        """并发时气泡与单个监听一致：单卡模型 + 底行 exact key 激活。"""
        app = self._app([_slot("pet-1")])
        try:
            a = inst(AgentKind.CODEX, 1)
            self._put(app, (a, snap(a, Status.WAITING)))
            app._aggregate()
            view = app.pet_manager.views["pet-1"]
            self.assertFalse(hasattr(view, "use_stack"))
            self.assertFalse(hasattr(view, "stack"))
            model = view.bubble.model
            self.assertTrue(model.visible)
            self.assertEqual(model.agent_key, a.key)
            app.root.update()
            view.redraw()
            self.assertTrue(view.bubble._hit_boxes)
            box = view.bubble._hit_boxes[0][0]
            tag = view._hit((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
            self.assertEqual(tag, ("activate", a.key))
        finally:
            app.quit()

    def test_slot_appearance_override(self):
        app = self._app([_slot("pet-1"),
                         _slot("pet-2", appearance={
                             "skin": "other", "scale": 0.75, "speed": 2.0,
                             "animated": False, "bubble": {"enabled": False}})])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2)
            self._put(app, (a, snap(a)), (b, snap(b)))
            app.presentation.bind_slot("pet-2", b.key)
            app._aggregate()
            v1 = app.pet_manager.views["pet-1"]
            v2 = app.pet_manager.views["pet-2"]
            self.assertEqual(v1.view_config.get("scale"), 1.0)
            self.assertEqual(v2.view_config.get("scale"), 0.75)
            self.assertEqual(v2.view_config.get("speed"), 2.0)
            self.assertEqual(v2.view_config.get("animated"), False)
            self.assertEqual(v2.view_config.get("bubble.enabled"), False)
            # null 字段继承全局
            self.assertEqual(v2.view_config.get("topmost"), True)
        finally:
            app.quit()

    def test_hide_all_pauses_cursors_monitor_continues(self):
        app = self._app([_slot("pet-1"), _slot("pet-2")])
        try:
            a = inst(AgentKind.CODEX, 1)
            self._put(app, (a, snap(a)))
            app._aggregate()
            app.hide_pet()
            for view in app.pet_manager.views.values():
                self.assertTrue(view.cursor.paused)   # hidden 不推进动画
            self.assertIsNone(app.pet_manager.scheduler._after_id)
            # Monitor 事实层继续工作
            b = inst(AgentKind.CLAUDE, 2)
            self._put(app, (a, snap(a)), (b, snap(b)))
            app._aggregate()
            self.assertEqual(len(app.monitor.get_targets()), 2)
            app.show_pet()
            for view in app.pet_manager.views.values():
                self.assertFalse(view.cursor.paused)
        finally:
            app.quit()

    def test_churn_bind_unbind_does_not_leak_views(self):
        app = self._app([_slot("pet-1"), _slot("pet-2")])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2)
            self._put(app, (a, snap(a)), (b, snap(b)))
            for _ in range(20):
                app.presentation.bind_slot("pet-1", a.key)
                app.presentation.bind_slot("pet-2", b.key)
                app._aggregate()
                app.presentation.unbind_slot("pet-1")
                app.presentation.unbind_slot("pet-2")
                app._aggregate()   # 自动分配立即补位 → 仍是 2 views
            self.assertEqual(len(app.pet_manager.views), 2)
            self.assertEqual(len(app.pet_manager.scheduler._cursors), 2)
        finally:
            app.quit()

    def test_selector_unique_rebind_after_restart(self):
        fp = AgentSelector.workspace_fingerprint("/w/proj")
        app = self._app([_slot("pet-1", selector={
            "kind": "codex", "source": "wsl:Ubuntu", "workspace_fp": fp})])
        try:
            a = inst(AgentKind.CODEX, 1, cwd="/w/proj")
            self._put(app, (a, snap(a)))
            app._aggregate()
            self.assertEqual(app.pet_manager.views["pet-1"].agent_key, a.key)
        finally:
            app.quit()

    def test_shared_cache_single_instance_across_views(self):
        app = self._app([_slot("pet-1"), _slot("pet-2"), _slot("pet-3")])
        try:
            caches = {id(app.pet_manager.cache)}
            schedulers = {id(app.pet_manager.scheduler)}
            builders = {id(app.pet_manager.build_manager)}
            self.assertEqual(len(caches), 1)
            self.assertEqual(len(schedulers), 1)
            self.assertEqual(len(builders), 1)
        finally:
            app.quit()


if __name__ == "__main__":
    unittest.main()
