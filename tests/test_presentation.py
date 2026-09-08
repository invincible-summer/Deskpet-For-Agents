"""PresentationController 测试（v4plan §18.3/§18.4）。

纯逻辑（无 Tk）+ 少量 app 级 Tk smoke：Aggregate body 不激活、
卡片 exact 激活、focused/attention 分离、special 抢占。
"""
from __future__ import annotations
import copy
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.models import AgentInstance, AgentKind, Snapshot, Status
from pet.presentation import (
    AgentCardModel,
    AgentSelector,
    PresentationController,
    PresentationMode,
)

NOW = 1_000_000.0


class MemConfig:
    def __init__(self, data=None):
        self.data = data or {}
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


def concurrent_cfg(enabled=False, mode="aggregate", max_targets=3,
                   eligible=None, slots=None):
    return MemConfig({"presentation": {"concurrent": {
        "enabled": enabled,
        "mode": mode,
        "max_targets": max_targets,
        "eligible_kinds": eligible if eligible is not None else
        {"codex": True, "claude": True, "kimi": True, "pi": True},
        "slots": slots or [],
    }}})


def inst(kind, pid, source="wsl:Ubuntu", cwd="/w/proj", token=None):
    return AgentInstance(kind, pid, source, cwd=cwd,
                         process_token=token or str(pid),
                         started_at=NOW - pid * 10)


def snap(i, status=Status.WORKING, summary="工作中", **kw):
    kw.pop("ts", None)
    return Snapshot(i.key, i.kind, i.source, i.pid, status=status,
                    summary=summary, ts=NOW, **kw)


def targets(*pairs):
    out = {}
    for i, s in pairs:
        from agents.models import AgentTarget
        out[i.key] = AgentTarget(key=i.key, instance=i, snapshot=s)
    return out


class ModeTests(unittest.TestCase):
    def test_concurrency_disabled_is_single(self):
        pc = PresentationController(concurrent_cfg(enabled=False))
        self.assertIs(pc.mode, PresentationMode.SINGLE)
        state = pc.reconcile(targets((inst(AgentKind.CODEX, 1),
                                      snap(inst(AgentKind.CODEX, 1)))), NOW)
        self.assertIs(state.mode, PresentationMode.SINGLE)
        self.assertEqual(state.cards, ())

    def test_aggregate_mode_cards(self):
        pc = PresentationController(concurrent_cfg(enabled=True))
        a = inst(AgentKind.CODEX, 1)
        b = inst(AgentKind.CLAUDE, 2)
        state = pc.reconcile(targets((a, snap(a)), (b, snap(
            b, Status.WAITING, "Bash 命令需要确认"))), NOW)
        self.assertIs(state.mode, PresentationMode.AGGREGATE)
        self.assertEqual(len(state.cards), 2)
        keys = {c.agent_key for c in state.cards}
        self.assertEqual(keys, {a.key, b.key})

    def test_fleet_mode_slots(self):
        pc = PresentationController(concurrent_cfg(
            enabled=True, mode="fleet",
            slots=[{"id": "pet-1", "selector": None},
                   {"id": "pet-2", "selector": None}]))
        a = inst(AgentKind.CODEX, 1)
        state = pc.reconcile(targets((a, snap(a))), NOW)
        self.assertIs(state.mode, PresentationMode.FLEET)
        # selector=None 也自动分配（V4.1.1：尽量自动绑定现有 Agent）
        self.assertEqual(state.slot_keys, {"pet-1": a.key})
        self.assertTrue(pc.is_auto_bound("pet-1"))


class SelectionTests(unittest.TestCase):
    def test_max_targets_overflow(self):
        pc = PresentationController(concurrent_cfg(enabled=True, max_targets=3))
        pairs = [(inst(AgentKind.CODEX, i), snap(inst(AgentKind.CODEX, i)))
                 for i in range(5)]
        state = pc.reconcile(targets(*pairs), NOW)
        self.assertEqual(len(state.cards), 3)
        self.assertEqual(state.overflow_count, 2)

    def test_runtime_exclude_removes_card_but_target_alive(self):
        pc = PresentationController(concurrent_cfg(enabled=True))
        a = inst(AgentKind.CODEX, 1)
        b = inst(AgentKind.CLAUDE, 2)
        t = targets((a, snap(a)), (b, snap(b)))
        pc.set_instance_included(a.key, False)
        state = pc.reconcile(t, NOW)
        keys = {c.agent_key for c in state.cards}
        self.assertEqual(keys, {b.key})
        self.assertIn(a.key, t)   # Monitor target 仍在

    def test_runtime_include_adds_back(self):
        pc = PresentationController(concurrent_cfg(enabled=True))
        a = inst(AgentKind.CODEX, 1)
        t = targets((a, snap(a)))
        pc.set_instance_included(a.key, False)
        self.assertEqual(pc.reconcile(t, NOW).cards, ())
        pc.set_instance_included(a.key, True)
        self.assertEqual(len(pc.reconcile(t, NOW).cards), 1)

    def test_eligible_kinds_filter_only_presentation(self):
        pc = PresentationController(concurrent_cfg(
            enabled=True, eligible={"codex": True, "claude": False,
                                    "kimi": False, "pi": False}))
        a = inst(AgentKind.CODEX, 1)
        b = inst(AgentKind.CLAUDE, 2)
        t = targets((a, snap(a)), (b, snap(b)))
        state = pc.reconcile(t, NOW)
        self.assertEqual({c.agent_key for c in state.cards}, {a.key})
        self.assertEqual(len(t), 2)   # Monitor 不受影响

    def test_agent_exit_removes_card_and_display_follows_attention(self):
        pc = PresentationController(concurrent_cfg(enabled=True))
        a = inst(AgentKind.CODEX, 1)
        b = inst(AgentKind.CLAUDE, 2)
        t = targets((a, snap(a)), (b, snap(b)))
        pc.set_focus(a.key)
        state = pc.reconcile(t, NOW)
        self.assertEqual(state.focused_key, a.key)
        # a 退出 → 显示焦点回到 attention（b）；不再残留已退出的 key
        del t[a.key]
        state = pc.reconcile(t, NOW)
        self.assertEqual(state.focused_key, b.key)
        self.assertEqual({c.agent_key for c in state.cards}, {b.key})
        pc.set_instance_included(a.key, True)   # 退出后 include 无效
        state = pc.reconcile(t, NOW)
        self.assertEqual({c.agent_key for c in state.cards}, {b.key})

    def test_deterministic_ordering(self):
        """排序不依赖 dict iteration：两次 reconcile（同输入）卡片顺序一致。"""
        pc = PresentationController(concurrent_cfg(enabled=True, max_targets=8))
        pairs = [(inst(AgentKind.CODEX, i), snap(inst(AgentKind.CODEX, i)))
                 for i in range(6)]
        t = targets(*pairs)
        s1 = pc.reconcile(t, NOW)
        s2 = pc.reconcile(t, NOW)
        self.assertEqual([c.agent_key for c in s1.cards],
                         [c.agent_key for c in s2.cards])


class AttentionTests(unittest.TestCase):
    def test_waiting_becomes_attention_without_stealing_focus(self):
        pc = PresentationController(concurrent_cfg(enabled=True))
        a = inst(AgentKind.CODEX, 1)
        b = inst(AgentKind.CLAUDE, 2)
        pc.set_focus(a.key)
        t = targets((a, snap(a)), (b, snap(b)))
        state = pc.reconcile(t, NOW)
        # 同优先级稳定排序：focused 优先（v4plan §6.4）
        self.assertEqual(state.attention_key, a.key)
        # B 变 WAITING：attention=B，但 focused 仍是 A（不偷偷改）
        t[b.key] = type("T", (), {"key": b.key, "instance": b,
                                  "snapshot": snap(b, Status.WAITING),
                                  "terminal": None})()
        state = pc.reconcile(t, NOW)
        self.assertEqual(state.attention_key, b.key)
        self.assertEqual(state.focused_key, a.key)

    def test_waiting_interrupts_special_animation(self):
        pc = PresentationController(concurrent_cfg(enabled=True))
        a = inst(AgentKind.CODEX, 1)
        t = targets((a, snap(a, Status.DONE, ts=NOW)))
        state = pc.reconcile(t, NOW)
        name, repeat = pc.animation_state(state, t, NOW)
        self.assertEqual((name, repeat), ("special", 3))
        # special 期间出现 WAITING：立即抢占
        t2 = targets((a, snap(a, Status.WAITING)),)
        state2 = pc.reconcile(t2, NOW + 1)
        name2, _ = pc.animation_state(state2, t2, NOW + 1)
        self.assertEqual(name2, "die")
        # 抢占后 special 窗口已被清除
        self.assertEqual(pc.special_until, 0.0)

    def test_error_and_input_also_interrupt(self):
        pc = PresentationController(concurrent_cfg(enabled=True))
        for status in (Status.INPUT, Status.ERROR):
            pc2 = pc
            a = inst(AgentKind.CODEX, 1)
            t = targets((a, snap(a, status)),)
            state = pc2.reconcile(t, NOW)
            name, _ = pc2.animation_state(state, t, NOW)
            self.assertEqual(name, "die", msg=str(status))


class FleetTests(unittest.TestCase):
    def _fleet_pc(self, slots):
        return PresentationController(concurrent_cfg(
            enabled=True, mode="fleet", slots=slots))

    def test_bind_unbind_and_duplicate_reject(self):
        pc = self._fleet_pc([{"id": "pet-1"}, {"id": "pet-2"}])
        a = inst(AgentKind.CODEX, 1)
        self.assertTrue(pc.bind_slot("pet-1", a.key))
        # 第二只 Pet 绑同一个 exact Agent → 拒绝
        self.assertFalse(pc.bind_slot("pet-2", a.key))
        pc.unbind_slot("pet-1")
        self.assertTrue(pc.bind_slot("pet-2", a.key))

    def test_agent_exit_frees_slot(self):
        pc = self._fleet_pc([{"id": "pet-1"}])
        a = inst(AgentKind.CODEX, 1)
        pc.bind_slot("pet-1", a.key)
        state = pc.reconcile(targets((a, snap(a))), NOW)
        self.assertEqual(state.slot_keys, {"pet-1": a.key})
        # Agent 退出 → slot 立即 vacant
        state = pc.reconcile({}, NOW + 1)
        self.assertEqual(state.slot_keys, {})
        self.assertEqual(state.slot_vacant_reason.get("pet-1"), "agent-exited")

    def test_selector_unique_auto_rebind(self):
        fp = AgentSelector.workspace_fingerprint("/w/proj")
        pc = self._fleet_pc([{"id": "pet-1", "selector": {
            "kind": "codex", "source": "wsl:Ubuntu", "workspace_fp": fp}}])
        a = inst(AgentKind.CODEX, 1, cwd="/w/proj")
        state = pc.reconcile(targets((a, snap(a))), NOW)
        self.assertEqual(state.slot_keys, {"pet-1": a.key})

    def test_selector_ambiguous_auto_bind_deterministic(self):
        """selector 模糊时由自动分配兜底：确定性（注意力→started_at→key）。

        这是纯展示层的分配（哪只桌宠显示哪个 Agent），不是终端身份绑定，
        不受"不猜"约束；终端 exact activation 仍走 CONFIRMED/HIGH 门槛。
        """
        fp = AgentSelector.workspace_fingerprint("/w/proj")
        pc = self._fleet_pc([{"id": "pet-1", "selector": {
            "kind": "codex", "workspace_fp": fp}}])
        a = inst(AgentKind.CODEX, 1, cwd="/w/proj")
        b = inst(AgentKind.CODEX, 2, cwd="/w/proj")
        s1 = pc.reconcile(targets((a, snap(a)), (b, snap(b))), NOW)
        self.assertEqual(s1.slot_keys.get("pet-1"), a.key)
        # 同输入重跑/换顺序 → 同一结果（确定性）
        s2 = pc.reconcile(targets((b, snap(b)), (a, snap(a))), NOW)
        self.assertEqual(s1.slot_keys, s2.slot_keys)

    def test_selector_fingerprint_normalizes_path(self):
        self.assertEqual(
            AgentSelector.workspace_fingerprint("D:\\Work\\Proj"),
            AgentSelector.workspace_fingerprint("d:/work/proj"))
        self.assertEqual(AgentSelector.workspace_fingerprint(""), "")


class ModeSwitchTests(unittest.TestCase):
    def test_mode_switch_keeps_monitor_state(self):
        """并发模式切换只是 presentation 状态：Monitor/UIA 无关（§18.3-10）。"""
        pc = PresentationController(concurrent_cfg(enabled=False))
        a = inst(AgentKind.CODEX, 1)
        t = targets((a, snap(a)))
        self.assertIs(pc.reconcile(t, NOW).mode, PresentationMode.SINGLE)
        pc.config.set("presentation.concurrent.enabled", True)
        self.assertIs(pc.reconcile(t, NOW).mode, PresentationMode.AGGREGATE)
        pc.config.set("presentation.concurrent.mode", "fleet")
        pc.config.set("presentation.concurrent.slots",
                      [{"id": "pet-1", "selector": None}])
        self.assertIs(pc.reconcile(t, NOW).mode, PresentationMode.FLEET)


class AggregateUiSmokeTests(unittest.TestCase):
    """app 级 Tk smoke：aggregate body 不激活；卡片点击 exact 激活。"""

    def test_aggregate_body_and_card_click(self):
        from pet.app import PetApp
        from pet.config import DEFAULTS

        class UiConfig:
            def __init__(self):
                self.data = copy.deepcopy(DEFAULTS)
                self.data["tray_enabled"] = False
                self.data["presentation"] = {"concurrent": {
                    "enabled": True, "mode": "aggregate", "max_targets": 3,
                    "eligible_kinds": {"codex": True, "claude": True,
                                       "kimi": True, "pi": True},
                    "slots": []}}
                self.migration_notice = False

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
                pass

        from pet.petview import PetView
        with patch.object(PetApp, "_reload_skins", lambda self: None),              patch.object(PetView, "load_skin", lambda self, bm: None):
            app = PetApp(UiConfig())
        try:
            app.monitor._terminal_service = __import__(
                "agents.terminal_service", fromlist=["x"]).WindowsTerminalService(None)
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2)
            app.monitor.instances = {a.key: a, b.key: b}
            app.monitor.snapshots = {
                a.key: snap(a, Status.WORKING),
                b.key: snap(b, Status.WAITING, "Bash 命令需要确认")}
            app._aggregate()
            state = app._presentation_state
            self.assertIs(state.mode, PresentationMode.AGGREGATE)
            self.assertEqual(len(state.cards), 2)

            # body 双击（aggregate）→ 只有互动，绝不激活
            activated = []
            view = app.pet_manager.views["pet-1"]
            view._on_activate = lambda key: activated.append(key)
            view._on_double()
            self.assertEqual(activated, [])

            # 气泡与单个监听一致：单卡（显示 attention=WAITING 的 b），
            # 底行命中 → exact key 激活
            self.assertFalse(hasattr(view, "stack"))
            self.assertEqual(view.bubble.model.agent_key, b.key)
            app.root.update()
            view.redraw()
            self.assertTrue(view.bubble._hit_boxes)
            box = view.bubble._hit_boxes[0][0]
            tag = view._hit((box[0] + box[2]) // 2,
                            (box[1] + box[3]) // 2)
            self.assertEqual(tag, ("activate", b.key))
            view._on_hit_tag(tag)
            self.assertEqual(activated, [b.key])
        finally:
            app.quit()


if __name__ == "__main__":
    unittest.main()
