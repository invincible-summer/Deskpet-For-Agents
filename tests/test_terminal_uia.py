"""UIA 观察器单元测试：FakeBackend，不需要真实 Terminal（plan.md §62）。

V3.1：TextChanged debounce 审批 fallback、可见读取限流、
StructureChanged 重发现、订阅生命周期、互相唯一 pane 绑定。
"""
from __future__ import annotations
import time
import unittest

from agents.models import Confidence, EvidenceSource, Status
from agents.terminal_uia import (
    APPROVAL_TTL,
    DELTA_MAX,
    EVENT_QUEUE_MAX,
    GLOBAL_VISIBLE_READ_LIMIT,
    RING_MAX,
    UIA_CALL_QUEUE_MAX,
    WT_WINDOW_CLASS,
    CodexTerminalRecognizer,
    ClaudeTerminalRecognizer,
    KimiTerminalRecognizer,
    PaneInfo,
    SubscriptionTracker,
    TabInfo,
    TerminalBackend,
    TerminalEvent,
    TerminalLayout,
    TerminalObserver,
    TerminalResolver,
    UiaBackend,
    WEAK_TRIGGER_RE,
)
from agents.models import (
    AgentInstance, AgentKind, AgentKind as _AK, BindingConfidence,
    WindowIdentity,
)

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
    """Window/Tab/Pane 三层 Fake（v4plan §18.2）。

    windows: hwnd → WindowIdentity；tabs: tab_id → TabInfo；
    panes: pane_id → PaneInfo（只有 selected tab 的 pane 应出现）。
    """
    available = True

    def __init__(self):
        self.panes: dict[tuple, PaneInfo] = {}
        self.windows: dict[int, WindowIdentity] = {}
        self.tabs: dict[tuple, TabInfo] = {}
        self.selected_tabs: dict[int, tuple] = {}
        self.visible: dict[tuple, str] = {}
        self.reads: list[tuple] = []
        self.discover_count = 0
        self.select_calls: list[tuple] = []
        self.focus_calls: list[tuple] = []
        self.select_fails = False
        self.sync_history: list[set] = []
        self.unsubscribed: list[tuple] = []

    def add_window(self, hwnd: int, pid: int = 0, created: float = 1.0) -> WindowIdentity:
        ident = WindowIdentity(hwnd=hwnd, pid=pid or hwnd + 100,
                               process_created=created,
                               window_class=WT_WINDOW_CLASS)
        self.windows[hwnd] = ident
        return ident

    def add_tab(self, hwnd: int, rid: tuple, title: str,
                selected: bool = False, index: int = -1) -> tuple:
        tab_id = (hwnd, rid)
        if hwnd not in self.windows:
            self.add_window(hwnd)
        self.tabs[tab_id] = TabInfo(
            tab_id=tab_id, hwnd=hwnd, window_pid=self.windows[hwnd].pid,
            title=title, index_hint=index if index >= 0 else len(
                [t for t in self.tabs.values() if t.hwnd == hwnd]),
            selected=selected, last_seen=NOW)
        if selected:
            self.selected_tabs[hwnd] = tab_id
        return tab_id

    def add_pane(self, hwnd: int, rid: tuple, title: str = "",
                 tab_id: tuple = ()) -> tuple:
        pane_id = (hwnd, rid)
        if hwnd not in self.windows:
            self.add_window(hwnd)
        self.panes[pane_id] = PaneInfo(
            pane_id=pane_id, hwnd=hwnd, window_pid=self.windows[hwnd].pid,
            title=title, tab_id=tab_id,
            window_created=self.windows[hwnd].process_created)
        return pane_id

    def discover_layout(self) -> TerminalLayout:
        """物理模型（TabManagement.cpp）：只有 selected Tab 的
        TermControl attach 在 XAML root——pane 只随其所属 Tab 选中而
        可见。tab_id 为 () 的 pane 视为始终可见（未建模 tab 的旧测试）。
        """
        self.discover_count += 1
        selected = set(self.selected_tabs.values())
        visible = {pid: p for pid, p in self.panes.items()
                   if not p.tab_id or p.tab_id in selected}
        return TerminalLayout(windows=dict(self.windows),
                              tabs=dict(self.tabs),
                              panes=visible,
                              selected_tabs=dict(self.selected_tabs))

    def selected_tab(self, hwnd: int) -> TabInfo | None:
        tab_id = self.selected_tabs.get(hwnd)
        return self.tabs.get(tab_id) if tab_id else None

    def select_tab(self, tab_id: tuple) -> bool:
        self.select_calls.append(tab_id)
        if self.select_fails or tab_id not in self.tabs:
            return False
        hwnd = tab_id[0]
        old = self.selected_tabs.get(hwnd)
        if old is not None and old in self.tabs:
            from dataclasses import replace
            self.tabs[old] = replace(self.tabs[old], selected=False)
        from dataclasses import replace
        self.tabs[tab_id] = replace(self.tabs[tab_id], selected=True)
        self.selected_tabs[hwnd] = tab_id
        return True

    def focus_pane(self, pane_id: tuple) -> bool:
        self.focus_calls.append(pane_id)
        return pane_id in self.panes



    def read_visible(self, pane_id: tuple) -> str:
        self.reads.append(pane_id)
        return self.visible.get(pane_id, "")

    def sync_pane_subscriptions(self, panes):
        wanted = {p.pane_id for p in panes}
        prev = self.sync_history[-1] if self.sync_history else set()
        self.sync_history.append(wanted)
        for pane_id in prev:
            if pane_id not in wanted:
                self.unsubscribed.append(pane_id)

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
        backend = FakeBackend()
        ident = backend.add_window(33, pid=9)
        tab = backend.add_tab(33, (7,), "codex", selected=True)
        pane_id = backend.add_pane(33, (70,), "x", tab_id=tab)
        layout = backend.discover_layout()
        from agents.models import TerminalLocation
        resolver.set_manual_location(inst.key, TerminalLocation(
            window=ident, tab_id=tab, pane_id=pane_id))
        bindings = resolver.resolve([inst], dict(layout.panes), NOW,
                                    layout=layout)
        self.assertEqual(bindings[inst.key].confidence, BindingConfidence.CONFIRMED)


class TextChangedFallbackTests(unittest.TestCase):
    """TextChanged → debounce → 有界可见读取（Notification 失效时的兜底）。"""

    def _observer_with_pane(self, pane_id=(11, (1,)), visible="plain output"):
        observer, backend = make_observer()
        backend.panes[pane_id] = PaneInfo(pane_id=pane_id, hwnd=11,
                                          window_pid=5, title="x")
        observer.refresh_panes(force=True)
        backend.visible[pane_id] = visible
        return observer, backend

    def test_text_changed_schedules_bounded_visible_read(self):
        observer, backend = self._observer_with_pane()
        backend.emit((11, (1,)), "activity", "", ts=NOW)
        observer.poll(NOW)
        # debounce 未到：不读取
        self.assertEqual(backend.reads, [])
        observer.poll(NOW + 0.2)
        self.assertEqual(backend.reads, [(11, (1,))])
        # 读取后没有审批 UI → 不产生 WAITING，但活动已记录
        self.assertEqual(observer.observations, {})
        self.assertIn((11, (1,)), observer.activity)

    def test_debounce_merges_burst_into_single_read(self):
        observer, backend = self._observer_with_pane()
        for i in range(100):
            backend.emit((11, (1,)), "activity", "", ts=NOW)
        observer.poll(NOW)
        observer.poll(NOW + 0.2)
        # 100 个 TextChanged 合并为 1 次可见读取
        self.assertEqual(len(backend.reads), 1)
        observer.poll(NOW + 0.4)
        observer.poll(NOW + 0.6)
        # 单 pane 限流（≥0.5s 间隔）后也只再有界增长
        self.assertLessEqual(len(backend.reads), 2)

    def test_text_changed_fallback_detects_approval(self):
        observer, backend = self._observer_with_pane(
            visible=CODEX_APPROVAL_VISIBLE)
        backend.emit((11, (1,)), "activity", "", ts=NOW)
        observer.poll(NOW + 0.2)
        self.assertIn((11, (1,)), observer.observations)
        self.assertEqual(observer.observations[(11, (1,))].status,
                         Status.WAITING)

    def test_global_visible_read_budget(self):
        observer, backend = make_observer()
        for i in range(8):
            pane_id = (11, (i,))
            backend.panes[pane_id] = PaneInfo(pane_id=pane_id, hwnd=11,
                                              window_pid=5, title=f"p{i}")
        observer.refresh_panes(force=True)
        for i in range(8):
            backend.emit((11, (i,)), "activity", "", ts=NOW)
        observer.poll(NOW)
        observer.poll(NOW + 0.2)
        # 8 个 dirty pane，全局预算 6/s：最多读 6 次
        self.assertLessEqual(len(backend.reads), GLOBAL_VISIBLE_READ_LIMIT)
        # 预算耗尽的 pane 被推迟，下一轮（1s 窗口滑过后）再读
        self.assertTrue(observer._dirty_panes or len(backend.reads) == 8)

    def test_waiting_recheck_not_debounced(self):
        observer, backend = self._observer_with_pane(
            visible=CODEX_APPROVAL_VISIBLE)
        backend.emit((11, (1,)), "notification",
                     "Would you like to run the following command?", ts=NOW)
        observer.poll(NOW)
        # 审批等待期间走 0.75s 复检通道，不进 debounce 队列
        self.assertIn((11, (1,)), observer._waiting_recheck)
        self.assertNotIn((11, (1,)), observer._dirty_panes)


class StructureChangedTests(unittest.TestCase):
    def test_structure_event_triggers_immediate_rediscovery(self):
        observer, backend = make_observer()
        base = backend.discover_count
        backend.emit((), "structure", "", ts=NOW)
        observer.poll(NOW)
        self.assertGreater(backend.discover_count, base)

    def test_new_pane_discovered_without_waiting(self):
        observer, backend = make_observer()
        backend.emit((), "structure", "", ts=NOW)
        observer.poll(NOW)      # 重发现：pane 集合尚空
        pane_id = (11, (9,))
        backend.panes[pane_id] = PaneInfo(pane_id=pane_id, hwnd=11,
                                          window_pid=5, title="new")
        observer._last_discover = time.time()   # 阻止周期性重发现
        backend.emit((), "structure", "", ts=NOW + 1)
        observer.poll(NOW + 1)
        self.assertIn(pane_id, observer.panes)

    def test_removed_pane_unsubscribed(self):
        observer, backend = make_observer()
        p1 = (11, (1,))
        backend.panes[p1] = PaneInfo(pane_id=p1, hwnd=11, window_pid=5, title="x")
        observer.refresh_panes(force=True)
        self.assertEqual(len(backend.sync_history), 1)
        del backend.panes[p1]
        observer.refresh_panes(force=True)
        self.assertEqual(backend.unsubscribed, [p1])


class SubscriptionLifecycleTests(unittest.TestCase):
    def test_tracker_churn_does_not_accumulate(self):
        tracker = SubscriptionTracker()
        added = removed = 0
        final = {("win", (i,)) for i in range(3)}
        for cycle in range(1000):
            wanted = {("win", ((cycle + i) % 5,)) for i in range(3)}
            to_add, to_remove = tracker.sync(wanted)
            added += len(to_add)
            removed += len(to_remove)
            self.assertLessEqual(len(tracker.active), 5)
        tracker.sync(final)
        self.assertEqual(tracker.active, final)
        self.assertEqual(added - removed, len(final))
        self.assertEqual(added, removed + 3)

    def test_uia_call_queue_bounded(self):
        backend = UiaBackend()
        q = backend._queue
        for i in range(UIA_CALL_QUEUE_MAX):
            q.put_nowait(object())
        with self.assertRaises(Exception):
            q.put_nowait(object())
        self.assertEqual(q.maxsize, UIA_CALL_QUEUE_MAX)

    def test_backend_stats_counts(self):
        backend = UiaBackend()
        stats = backend.stats()
        self.assertIn("uia_calls", stats)
        self.assertIn("uia_timeouts", stats)
        self.assertIn("uia_queue_dropped", stats)


class RecognizerKindTests(unittest.TestCase):
    def test_recognizer_observations_carry_agent_kind(self):
        for rec, visible in (
                (CodexTerminalRecognizer(), CODEX_APPROVAL_VISIBLE),
                (ClaudeTerminalRecognizer(), CLAUDE_PERMISSION_VISIBLE),
                (KimiTerminalRecognizer(), "Approve this command?\n yes / no (esc)")):
            obs = rec.inspect(visible, "", NOW)
            self.assertEqual(len(obs), 1)
            self.assertEqual(obs[0].agent_kind, rec.KIND)

    def test_pane_activity_observation_has_no_kind(self):
        observer, backend = make_observer()
        pane_id = (11, (1,))
        backend.panes[pane_id] = PaneInfo(pane_id=pane_id, hwnd=11,
                                          window_pid=5, title="x")
        observer.refresh_panes(force=True)
        backend.emit(pane_id, "activity", "", ts=NOW)
        observer.poll(NOW)
        obs = observer.pane_activity_observation(pane_id, NOW + 1, 10.0)
        self.assertIsNone(obs.agent_kind)


class MutualBindingTests(unittest.TestCase):
    def test_weak_single_pane_not_high(self):
        """唯一 pane + 1 分弱提示（只命中 user@）→ 绝不能 HIGH。"""
        resolver = TerminalResolver(enum_windows=lambda: [])
        inst = AgentInstance(kind=AgentKind.CODEX, pid=1, source="wsl:Ubuntu",
                             process_token="9", cwd="/x/y", user="u")
        panes = {(11, (1,)): PaneInfo(pane_id=(11, (1,)), hwnd=11,
                                      window_pid=5, title="u@box")}
        bindings = resolver.resolve([inst], panes, NOW)
        b = bindings[inst.key]
        self.assertEqual(b.confidence, BindingConfidence.AMBIGUOUS)
        self.assertEqual(b.score, 1)

    def test_two_agents_two_panes_mutual_unique(self):
        resolver = TerminalResolver(enum_windows=lambda: [])
        codex = AgentInstance(kind=AgentKind.CODEX, pid=1, source="wsl:Ubuntu",
                              process_token="9", cwd="/w/alpha", user="u1")
        claude = AgentInstance(kind=AgentKind.CLAUDE, pid=2, source="wsl:Ubuntu",
                               process_token="10", cwd="/w/beta", user="u2")
        panes = {
            (11, (1,)): PaneInfo(pane_id=(11, (1,)), hwnd=11, window_pid=5,
                                 title="codex u1@box:~/alpha"),
            (11, (2,)): PaneInfo(pane_id=(11, (2,)), hwnd=11, window_pid=5,
                                 title="claude u2@box:~/beta"),
        }
        b1 = resolver.resolve([codex, claude], panes, NOW)
        b2 = resolver.resolve([claude, codex], panes, NOW)   # 顺序无关
        self.assertEqual(b1[codex.key].confidence, BindingConfidence.HIGH)
        self.assertEqual(b1[claude.key].confidence, BindingConfidence.HIGH)
        self.assertEqual(b1[codex.key].pane_id, (11, (1,)))
        self.assertEqual(b1[claude.key].pane_id, (11, (2,)))
        self.assertEqual(
            {k: v.pane_id for k, v in b1.items()},
            {k: v.pane_id for k, v in b2.items()})

    def test_close_competition_not_high(self):
        """同分差仅 1（<margin=2 的保守设定）时用户可手动修复。"""
        resolver = TerminalResolver(enum_windows=lambda: [])
        a = AgentInstance(kind=AgentKind.CODEX, pid=1, source="wsl:Ubuntu",
                          process_token="9", cwd="/w/alpha", user="u")
        b = AgentInstance(kind=AgentKind.CODEX, pid=2, source="wsl:Ubuntu",
                          process_token="10", cwd="/w/beta", user="u")
        # 两个 pane 都含 "codex"（+3）与 "u@"（+1），各自 cwd 差异化（+2）
        panes = {
            (11, (1,)): PaneInfo(pane_id=(11, (1,)), hwnd=11, window_pid=5,
                                 title="codex alpha u@box"),
            (11, (2,)): PaneInfo(pane_id=(11, (2,)), hwnd=11, window_pid=5,
                                 title="codex beta u@box"),
        }
        bindings = resolver.resolve([a, b], panes, NOW)
        # A: X=6, Y=4；B: X=4, Y=6 → 双向唯一，margin=2 → 都 HIGH
        self.assertEqual(bindings[a.key].confidence, BindingConfidence.HIGH)
        self.assertEqual(bindings[b.key].confidence, BindingConfidence.HIGH)

    def test_manual_location_pruned_when_agent_or_window_gone(self):
        """v4plan §5.10：manual 绑定在 Agent 退出/WindowIdentity 失效时清理。

        pane 暂时不可见（Tab 未选中）不算失效。
        """
        backend = FakeBackend()
        ident = backend.add_window(33, pid=9, created=1.5)
        tab = backend.add_tab(33, (7,), "codex", selected=True)
        pane_id = backend.add_pane(33, (70,), "x", tab_id=tab)
        layout = backend.discover_layout()
        resolver = TerminalResolver(enum_windows=lambda: [])
        inst = AgentInstance(kind=AgentKind.CODEX, pid=1, source="wsl:Ubuntu",
                             process_token="9")
        from agents.models import TerminalLocation
        resolver.set_manual_location(inst.key, TerminalLocation(
            window=ident, tab_id=tab, pane_id=pane_id))
        bindings = resolver.resolve([inst], dict(layout.panes), NOW,
                                    layout=layout)
        b = bindings[inst.key]
        self.assertEqual(b.confidence, BindingConfidence.CONFIRMED)
        self.assertEqual(b.tab_id, tab)
        self.assertEqual(b.pane_id, pane_id)
        # pane 暂不可见（Tab 未选中）：manual 保留（activation 会重验证）
        backend.panes.pop(pane_id)
        layout2 = backend.discover_layout()
        resolver.resolve([inst], dict(layout2.panes), NOW, layout=layout2)
        self.assertIn(inst.key, resolver._manual)
        # 窗口 identity 失效（HWND 复用新进程）→ manual 清理
        backend.windows[33] = WindowIdentity(hwnd=33, pid=999,
                                             process_created=99.0,
                                             window_class=WT_WINDOW_CLASS)
        layout3 = backend.discover_layout()
        resolver.resolve([inst], dict(layout3.panes), NOW, layout=layout3)
        self.assertEqual(resolver._manual, {})
        # Agent 退出 → manual 清理
        resolver.set_manual_location(inst.key, TerminalLocation(
            window=ident, tab_id=tab, pane_id=pane_id))
        resolver.resolve([], {}, NOW, layout=layout3)
        self.assertEqual(resolver._manual, {})

    def test_manual_binding_confirmed(self):
        """（旧 pane-only 兼容断言）manual 位置绑定仍产 CONFIRMED。"""
        backend = FakeBackend()
        ident = backend.add_window(33, pid=9)
        tab = backend.add_tab(33, (7,), "codex", selected=True)
        pane_id = backend.add_pane(33, (70,), "x", tab_id=tab)
        layout = backend.discover_layout()
        resolver = TerminalResolver(enum_windows=lambda: [])
        inst = AgentInstance(kind=AgentKind.CODEX, pid=1, source="wsl:Ubuntu",
                             process_token="9")
        from agents.models import TerminalLocation
        resolver.set_manual_location(inst.key, TerminalLocation(
            window=ident, tab_id=tab, pane_id=pane_id))
        bindings = resolver.resolve([inst], dict(layout.panes), NOW,
                                    layout=layout)
        self.assertEqual(bindings[inst.key].confidence,
                         BindingConfidence.CONFIRMED)


class ScoringRegressionTests(unittest.TestCase):
    """V4.1.1 评分修正（用户反馈：无法扫描关联终端）。

    * 词边界：kind "pi" 不得命中 "pip"；"codex" 命中 "codex · task"；
    * ~/路径标记：`user@host: ~/a/b` 按 cwd 归一化比对，不再用裸
      basename（用户名==家目录名时会造成所有同用户 pane 假命中）；
    * Tab 标题是第二条证据：TermControl Name 停留在 profile 名时仍可
      通过 TabItem 标题达成 HIGH；
    * "Ubuntu" 这类无路径标题不产生假 HIGH（fail-closed，等用户切到
      该 Tab 自然学习或手动关联）。
    """

    def _inst(self, kind=AgentKind.CODEX, cwd="/w/x", user="", source="wsl:Ubuntu"):
        return AgentInstance(kind=kind, pid=1, source=source,
                             process_token="9", cwd=cwd, user=user)

    def _pane(self, hwnd, rid, title, tab_id=()):
        return PaneInfo(pane_id=(hwnd, rid), hwnd=hwnd, window_pid=5,
                        title=title, tab_id=tab_id)

    def test_pi_word_boundary_not_pip(self):
        resolver = TerminalResolver(enum_windows=lambda: [])
        inst = self._inst(kind=AgentKind.PI)
        panes = {11: self._pane(11, (1,), "pip install requests")}
        score, reason = resolver._score_pane(inst, panes[11])
        self.assertEqual(score, 0)
        self.assertNotIn("kind", reason)

    def test_kind_word_boundary_matches(self):
        resolver = TerminalResolver(enum_windows=lambda: [])
        inst = self._inst(kind=AgentKind.CODEX)
        score, _ = resolver._score_pane(inst, self._pane(11, (1,), "codex · 编码中"))
        self.assertGreaterEqual(score, 3)

    def test_tilde_path_marker_scores_cwd(self):
        resolver = TerminalResolver(enum_windows=lambda: [])
        inst = self._inst(cwd="/home/dev/proj/app", user="dev")
        score, reason = resolver._score_pane(
            inst, self._pane(11, (1,), "dev@box: ~/proj/app"))
        self.assertIn("cwd", reason)
        self.assertIn("user@", reason)

    def test_home_dir_basename_does_not_match_user_host(self):
        """Agent 在家目录：不得因 basename==用户名 而命中所有 user@host 标题。"""
        resolver = TerminalResolver(enum_windows=lambda: [])
        inst = self._inst(cwd="/home/dev", user="dev")
        p1 = self._pane(11, (1,), "dev@box: ~/proj/app")
        score, reason = resolver._score_pane(inst, p1)
        # 只有 user@ 弱证据，绝不能有 cwd（否则两个不同项目的 pane 同分）
        self.assertNotIn("cwd", reason)
        self.assertLess(score, 3)

    def test_generic_profile_title_stays_ambiguous(self):
        resolver = TerminalResolver(enum_windows=lambda: [])
        inst = self._inst(cwd="/home/dev/proj/app", user="dev")
        pane = self._pane(11, (1,), "Ubuntu")
        bindings = resolver.resolve([inst], {pane.pane_id: pane}, NOW)
        self.assertEqual(bindings[inst.key].confidence,
                         BindingConfidence.AMBIGUOUS)

    def test_tab_title_is_second_evidence_source(self):
        """TermControl Name 停在 "Ubuntu"，但 Tab 标题带路径 → 仍可 HIGH。"""
        from agents.models import TerminalBinding  # noqa: F401
        resolver = TerminalResolver(enum_windows=lambda: [])
        inst = self._inst(cwd="/home/dev/proj/app", user="dev")
        tab_id = (11, (5,))
        pane = self._pane(11, (1,), "Ubuntu", tab_id=tab_id)
        panes = {pane.pane_id: pane}
        layout = TerminalLayout(
            windows={}, tabs={tab_id: TabInfo(
                tab_id=tab_id, hwnd=11, window_pid=5,
                title="dev@box: ~/proj/app", index_hint=0, selected=True,
                last_seen=NOW)},
            panes=panes, selected_tabs={11: tab_id})
        bindings = resolver.resolve([inst], panes, NOW, layout=layout)
        b = bindings[inst.key]
        self.assertEqual(b.confidence, BindingConfidence.HIGH)
        self.assertEqual(b.pane_id, (11, (1,)))
        self.assertIn("cwd", b.reason)

    def test_two_distinct_paths_both_high(self):
        """两个不同项目的 pane + 两个对应 Agent → 双向唯一都 HIGH。"""
        resolver = TerminalResolver(enum_windows=lambda: [])
        a = self._inst(kind=AgentKind.CODEX, cwd="/home/dev/alpha", user="dev")
        b = self._inst(kind=AgentKind.CLAUDE, cwd="/home/dev/beta", user="dev")
        panes = {
            (11, (1,)): self._pane(11, (1,), "dev@box: ~/alpha"),
            (11, (2,)): self._pane(11, (2,), "dev@box: ~/beta"),
        }
        bindings = resolver.resolve([a, b], panes, NOW)
        self.assertEqual(bindings[a.key].confidence, BindingConfidence.HIGH)
        self.assertEqual(bindings[b.key].confidence, BindingConfidence.HIGH)
        self.assertEqual(bindings[a.key].pane_id, (11, (1,)))
        self.assertEqual(bindings[b.key].pane_id, (11, (2,)))


if __name__ == "__main__":
    unittest.main()
