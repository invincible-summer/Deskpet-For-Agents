"""UIA 观察器单元测试：FakeBackend，不需要真实 Terminal（plan.md §62）。"""
from __future__ import annotations
import time
import unittest

from agents.models import Confidence, EvidenceSource, Status
from agents.terminal_uia import (
    APPROVAL_TTL,
    DELTA_MAX,
    EVENT_QUEUE_MAX,
    RING_MAX,
    CodexTerminalRecognizer,
    ClaudeTerminalRecognizer,
    KimiTerminalRecognizer,
    PaneInfo,
    TerminalBackend,
    TerminalEvent,
    TerminalObserver,
    TerminalResolver,
    WEAK_TRIGGER_RE,
)
from agents.models import AgentInstance, AgentKind, BindingConfidence

NOW = 1_000_000.0

CODEX_APPROVAL_VISIBLE = """
user@box:~/proj$ codex
? Would you like to run the following command?

  pytest -q

  Yes, and don't ask again for commands that start with `pytest`
  Yes, proceed
  No, and tell Codex what to do differently (esc)
"""

CLAUDE_PERMISSION_VISIBLE = """
┌ Bash command ─────────────────────────┐
│ rm -rf build                          │
│ 1. Yes                                │
│ 2. Yes, and don't ask again           │
│ 3. No, and tell Claude what to do     │
│    differently (esc)                  │
└───────────────────────────────────────┘
"""

OLD_SCROLLBACK_WITH_APPROVAL = """
... lots of output ...
? Would you like to run the following command?
  Yes, proceed
  No, and tell Codex what to do differently
... scrolled far past, now compiling ...
progress: 90%
"""


class FakeBackend(TerminalBackend):
    available = True

    def __init__(self):
        self.panes: dict[tuple, PaneInfo] = {}
        self.visible: dict[tuple, str] = {}
        self.reads: list[tuple] = []

    def discover_panes(self) -> list[PaneInfo]:
        return list(self.panes.values())

    def read_visible(self, pane_id: tuple) -> str:
        self.reads.append(pane_id)
        return self.visible.get(pane_id, "")

    def emit(self, pane_id: tuple, kind: str, text: str = "", ts: float | None = None):
        self.event_sink(TerminalEvent(pane_id=pane_id, kind=kind, text=text,
                                      ts=ts if ts is not None else time.time()))


def make_observer(backend=None):
    backend = backend or FakeBackend()
    observer = TerminalObserver(backend, cfg={"terminal_observer": True})
    observer._started = True
    observer._last_discover = time.time()   # 阻止 poll 内的自动重发现
    return observer, backend


class RecognizerTests(unittest.TestCase):
    def test_codex_requires_heading_and_options(self):
        rec = CodexTerminalRecognizer()
        obs = rec.inspect(CODEX_APPROVAL_VISIBLE, "", NOW)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].status, Status.WAITING)
        self.assertEqual(obs[0].phase.value, "approval")
        self.assertEqual(obs[0].confidence, Confidence.HIGH)
        self.assertAlmostEqual(obs[0].expires_at, NOW + APPROVAL_TTL)
        # 只有标题（assistant 文字里提到审批）不触发
        self.assertEqual(rec.inspect("we discussed approval of the plan", "", NOW), [])
        # 只有选项没有标题也不触发
        self.assertEqual(rec.inspect("Yes, proceed\nNo thanks", "", NOW), [])

    def test_claude_requires_two_structural_features(self):
        rec = ClaudeTerminalRecognizer()
        obs = rec.inspect(CLAUDE_PERMISSION_VISIBLE, "", NOW)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].status, Status.WAITING)
        self.assertIn("确认", obs[0].summary)
        # assistant 文字提到 permission 但没有选项结构
        self.assertEqual(rec.inspect("the permission system works like this...", "", NOW), [])

    def test_kimi_fallback_medium_confidence(self):
        rec = KimiTerminalRecognizer()
        obs = rec.inspect("Approve this command?\n yes / no (esc)", "", NOW)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].confidence, Confidence.MEDIUM)

    def test_weak_trigger_regex(self):
        self.assertTrue(WEAK_TRIGGER_RE.search("Would you like to run the following command?"))
        self.assertTrue(WEAK_TRIGGER_RE.search("Do you want to proceed?"))
        self.assertFalse(WEAK_TRIGGER_RE.search("compiling modules... 90%"))
        self.assertFalse(WEAK_TRIGGER_RE.search(""))


class ObserverFlowTests(unittest.TestCase):
    def test_notification_delta_triggers_visible_read_and_approval(self):
        observer, backend = make_observer()
        pane_id = (11, (1,))
        backend.panes[pane_id] = PaneInfo(pane_id=pane_id, hwnd=11, window_pid=5,
                                          title="codex")
        observer.refresh_panes(force=True)
        backend.visible[pane_id] = CODEX_APPROVAL_VISIBLE
        backend.emit(pane_id, "notification", "Would you like to run the following command?")
        observer.poll(NOW)
        self.assertIn(pane_id, observer.observations)
        obs = observer.observations[pane_id]
        self.assertEqual(obs.status, Status.WAITING)
        self.assertIn(pane_id, backend.reads)   # 弱触发后才读可见区域

    def test_plain_output_delta_does_not_read_visible(self):
        observer, backend = make_observer()
        pane_id = (11, (1,))
        backend.panes[pane_id] = PaneInfo(pane_id=pane_id, hwnd=11, window_pid=5, title="x")
        observer.refresh_panes(force=True)
        backend.emit(pane_id, "notification", "progress: compiling 42%")
        observer.poll(NOW)
        self.assertEqual(observer.observations, {})
        self.assertEqual(backend.reads, [])   # 没有弱触发就不做可见读取
        # 活动时间被记录（Claude transcript 回归时的 WORKING 证据）
        self.assertIn(pane_id, observer.activity)

    def test_approval_disappears_after_ttl_recheck(self):
        observer, backend = make_observer()
        pane_id = (11, (1,))
        backend.panes[pane_id] = PaneInfo(pane_id=pane_id, hwnd=11, window_pid=5, title="codex")
        observer.refresh_panes(force=True)
        backend.visible[pane_id] = CODEX_APPROVAL_VISIBLE
        backend.emit(pane_id, "notification", "Would you like to run the following command?")
        observer.poll(NOW)
        self.assertIn(pane_id, observer.observations)
        # 用户在终端处理后 overlay 消失 → 复检清除（TTL 不残留）
        backend.visible[pane_id] = "user@box:~/proj$ "
        observer.poll(NOW + 0.8)
        self.assertNotIn(pane_id, observer.observations)

    def test_old_scrollback_approval_does_not_persist(self):
        """scrollback 里的旧审批文案不会造成永久 WAITING（plan §21）。"""
        observer, backend = make_observer()
        pane_id = (11, (1,))
        backend.panes[pane_id] = PaneInfo(pane_id=pane_id, hwnd=11, window_pid=5, title="codex")
        observer.refresh_panes(force=True)
        # 可见区域里审批 UI 已滚出（只剩普通输出），delta 是无关文本
        backend.visible[pane_id] = "progress: 95%\nalmost done"
        backend.emit(pane_id, "notification", "progress: 95%")
        observer.poll(NOW)
        self.assertEqual(observer.observations, {})
        # ring buffer 里即使残留旧审批 delta，没有弱触发 + 可见区域无 UI
        observer._push_delta(pane_id, "Would you like to run? Yes, proceed")
        backend.visible[pane_id] = "plain output"
        observer.poll(NOW + 1.0)
        self.assertEqual(observer.observations, {})

    def test_ring_buffer_bounded(self):
        observer, _ = make_observer()
        pane_id = (11, (1,))
        for i in range(200):
            observer._push_delta(pane_id, "x" * DELTA_MAX)
        self.assertLessEqual(observer._ring_len[pane_id], RING_MAX)
        self.assertLessEqual(len(observer.rings[pane_id]), RING_MAX // DELTA_MAX + 2)

    def test_event_queue_overflow_drops_oldest(self):
        observer, _ = make_observer()
        for i in range(EVENT_QUEUE_MAX + 50):
            observer._on_event(TerminalEvent(pane_id=(i % 5,), kind="activity"))
        with observer._event_q_lock:
            size = len(observer._events)
        self.assertLessEqual(size, EVENT_QUEUE_MAX)
        self.assertEqual(observer.stats["dropped"], 50)

    def test_multiple_panes_tracked_independently(self):
        observer, backend = make_observer()
        p1, p2 = (11, (1,)), (11, (2,))
        backend.panes[p1] = PaneInfo(pane_id=p1, hwnd=11, window_pid=5, title="codex")
        backend.panes[p2] = PaneInfo(pane_id=p2, hwnd=11, window_pid=5, title="claude")
        observer.refresh_panes(force=True)
        backend.visible[p1] = CODEX_APPROVAL_VISIBLE
        backend.visible[p2] = CLAUDE_PERMISSION_VISIBLE
        backend.emit(p1, "notification", "Would you like to run")
        backend.emit(p2, "notification", "Do you want to proceed")
        observer.poll(NOW)
        self.assertEqual(observer.observations[p1].summary, "命令执行需要确认")
        self.assertEqual(observer.observations[p2].summary, "Bash 命令需要确认")

    def test_disappeared_pane_state_cleaned(self):
        observer, backend = make_observer()
        pane_id = (11, (1,))
        backend.panes[pane_id] = PaneInfo(pane_id=pane_id, hwnd=11, window_pid=5, title="x")
        observer.refresh_panes(force=True)
        observer.observations[pane_id] = None  # placeholder
        observer.activity[pane_id] = NOW
        # pane 关闭 → 重发现后清理
        observer._last_discover = 0
        backend.panes.clear()
        observer.refresh_panes(force=True)
        self.assertNotIn(pane_id, observer.observations)
        self.assertNotIn(pane_id, observer.activity)

    def test_pane_activity_observation(self):
        observer, backend = make_observer()
        pane_id = (11, (1,))
        backend.panes[pane_id] = PaneInfo(pane_id=pane_id, hwnd=11, window_pid=5, title="x")
        observer.refresh_panes(force=True)
        backend.emit(pane_id, "activity", "", ts=NOW)
        observer.poll(NOW)
        obs = observer.pane_activity_observation(pane_id, NOW + 1, 10.0)
        self.assertIsNotNone(obs)
        self.assertEqual(obs.status, Status.WORKING)
        self.assertEqual(obs.confidence, Confidence.MEDIUM)
        self.assertIsNone(observer.pane_activity_observation(pane_id, NOW + 60, 10.0))


class ResolverTests(unittest.TestCase):
    def test_windows_native_ancestor_chain_confirmed(self):
        resolver = TerminalResolver(
            enum_windows=lambda: [(11, 50, "codex", "CASCADIA_HOSTING_WINDOW_CLASS")],
            ancestor_pids=lambda pid: {pid, 50},
        )
        inst = AgentInstance(kind=AgentKind.CODEX, pid=99, source="windows",
                             process_token="9")
        pane_id = (11, (1,))
        panes = {pane_id: PaneInfo(pane_id=pane_id, hwnd=11, window_pid=50, title="codex")}
        bindings = resolver.resolve([inst], panes, NOW)
        b = bindings[inst.key]
        self.assertEqual(b.confidence, BindingConfidence.CONFIRMED)
        self.assertEqual(b.pane_id, pane_id)

    def test_windows_native_multi_pane_ambiguous(self):
        resolver = TerminalResolver(
            enum_windows=lambda: [(11, 50, "wt", "CASCADIA_HOSTING_WINDOW_CLASS")],
            ancestor_pids=lambda pid: {pid, 50},
        )
        inst = AgentInstance(kind=AgentKind.CODEX, pid=99, source="windows",
                             process_token="9")
        panes = {
            (11, (1,)): PaneInfo(pane_id=(11, (1,)), hwnd=11, window_pid=50, title="codex"),
            (11, (2,)): PaneInfo(pane_id=(11, (2,)), hwnd=11, window_pid=50, title="claude"),
        }
        bindings = resolver.resolve([inst], panes, NOW)
        self.assertEqual(bindings[inst.key].confidence, BindingConfidence.AMBIGUOUS)

    def test_wsl_title_scoring_high_when_unique(self):
        resolver = TerminalResolver(enum_windows=lambda: [])
        inst = AgentInstance(kind=AgentKind.CODEX, pid=1, source="wsl:Ubuntu",
                             process_token="9", cwd="/home/u/DeskPet", user="u")
        panes = {
            (11, (1,)): PaneInfo(pane_id=(11, (1,)), hwnd=11, window_pid=5,
                                 title="u@box:~/DeskPet"),
            (11, (2,)): PaneInfo(pane_id=(11, (2,)), hwnd=11, window_pid=5,
                                 title="PowerShell"),
        }
        bindings = resolver.resolve([inst], panes, NOW)
        b = bindings[inst.key]
        self.assertEqual(b.confidence, BindingConfidence.HIGH)
        self.assertEqual(b.pane_id, (11, (1,)))

    def test_wsl_two_similar_panes_ambiguous(self):
        resolver = TerminalResolver(enum_windows=lambda: [])
        inst = AgentInstance(kind=AgentKind.CODEX, pid=1, source="wsl:Ubuntu",
                             process_token="9", cwd="/home/u/DeskPet", user="u")
        panes = {
            (11, (1,)): PaneInfo(pane_id=(11, (1,)), hwnd=11, window_pid=5,
                                 title="u@box:~/DeskPet"),
            (22, (2,)): PaneInfo(pane_id=(22, (2,)), hwnd=22, window_pid=6,
                                 title="u@box:~/DeskPet"),
        }
        bindings = resolver.resolve([inst], panes, NOW)
        self.assertEqual(bindings[inst.key].confidence, BindingConfidence.AMBIGUOUS)

    def test_wsl_comm_in_title_strong(self):
        resolver = TerminalResolver(enum_windows=lambda: [])
        inst = AgentInstance(kind=AgentKind.CLAUDE, pid=1, source="wsl:Ubuntu",
                             process_token="9", cwd="/x")
        panes = {
            (11, (1,)): PaneInfo(pane_id=(11, (1,)), hwnd=11, window_pid=5, title="claude"),
        }
        bindings = resolver.resolve([inst], panes, NOW)
        self.assertEqual(bindings[inst.key].confidence, BindingConfidence.HIGH)

    def test_no_pane_no_binding(self):
        resolver = TerminalResolver(enum_windows=lambda: [])
        inst = AgentInstance(kind=AgentKind.CODEX, pid=1, source="wsl:Ubuntu",
                             process_token="9")
        bindings = resolver.resolve([inst], {}, NOW)
        self.assertEqual(bindings[inst.key].confidence, BindingConfidence.NONE)
        self.assertEqual(bindings[inst.key].hwnd, 0)

    def test_manual_binding_confirmed(self):
        resolver = TerminalResolver(enum_windows=lambda: [])
        inst = AgentInstance(kind=AgentKind.CODEX, pid=1, source="wsl:Ubuntu",
                             process_token="9")
        pane_id = (33, (7,))
        panes = {pane_id: PaneInfo(pane_id=pane_id, hwnd=33, window_pid=9, title="x")}
        resolver.set_manual_binding(inst.key, pane_id)
        bindings = resolver.resolve([inst], panes, NOW)
        self.assertEqual(bindings[inst.key].confidence, BindingConfidence.CONFIRMED)


if __name__ == "__main__":
    unittest.main()
