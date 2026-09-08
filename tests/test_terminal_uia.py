"""UIA 观察器单元测试：FakeBackend，不需要真实 Terminal（v4.1.1 §21）。

  * 观察流：Notification/TextChanged debounce/可见读取限流/TTL 复检/
    StructureChanged 重发现/订阅生命周期；
  * 解析器：window-only 与 observation-only 双链（§5/§7）；
  * 删除所有 selected-tab topology 断言（产品已不承诺 Tab/Pane）。
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
    ObservedTerminalControl,
    SubscriptionTracker,
    TerminalBackend,
    TerminalEvent,
    TerminalLayout,
    TerminalObserver,
    UiaBackend,
    WEAK_TRIGGER_RE,
)
from agents.terminal_resolver import (
    TerminalObservationResolver,
    TerminalWindowResolver,
)
from agents.models import (
    AgentInstance, AgentKind, ObservationBindingConfidence,
    WindowBindingConfidence, WindowIdentity,
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


def control(hwnd: int, rid, title: str = "") -> ObservedTerminalControl:
    return ObservedTerminalControl(control_id=(hwnd, tuple(rid)), hwnd=hwnd,
                                   window_pid=hwnd + 100, title=title)


class FakeBackend(TerminalBackend):
    """Window + TermControl 两层 Fake（v4.1.1：没有 Tab 拓扑）。"""

    available = True

    def __init__(self):
        self.controls: dict[tuple, ObservedTerminalControl] = {}
        self.windows: dict[int, WindowIdentity] = {}
        self.visible: dict[tuple, str] = {}
        self.reads: list[tuple] = []
        self.discover_count = 0
        self.sync_history: list[set] = []
        self.unsubscribed: list[tuple] = []

    def add_window(self, hwnd: int, pid: int = 0,
                   created: float = 1.0) -> WindowIdentity:
        ident = WindowIdentity(hwnd=hwnd, pid=pid or hwnd + 100,
                               process_created=created,
                               window_class=WT_WINDOW_CLASS)
        self.windows[hwnd] = ident
        return ident

    def add_control(self, hwnd: int, rid: tuple, title: str = "") -> tuple:
        control_id = (hwnd, tuple(rid))
        if hwnd not in self.windows:
            self.add_window(hwnd)
        self.controls[control_id] = control(hwnd, rid, title)
        return control_id

    def discover_layout(self) -> TerminalLayout:
        self.discover_count += 1
        return TerminalLayout(windows=dict(self.windows),
                              controls=dict(self.controls))

    def read_visible(self, control_id: tuple) -> str:
        self.reads.append(control_id)
        return self.visible.get(control_id, "")

    def sync_control_subscriptions(self, controls):
        wanted = {c.control_id for c in controls}
        prev = self.sync_history[-1] if self.sync_history else set()
        self.sync_history.append(wanted)
        for control_id in prev:
            if control_id not in wanted:
                self.unsubscribed.append(control_id)

    def emit(self, control_id: tuple, kind: str, text: str = "",
             ts: float | None = None):
        self.event_sink(TerminalEvent(control_id=control_id, kind=kind,
                                      text=text,
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
        control_id = (11, (1,))
        backend.controls[control_id] = control(11, (1,), "codex")
        observer.refresh_controls(force=True)
        backend.visible[control_id] = CODEX_APPROVAL_VISIBLE
        backend.emit(control_id, "notification", "Would you like to run the following command?")
        observer.poll(NOW)
        self.assertIn(control_id, observer.observations)
        obs = observer.observations[control_id]
        self.assertEqual(obs.status, Status.WAITING)
        self.assertIn(control_id, backend.reads)   # 弱触发后才读可见区域

    def test_plain_output_delta_does_not_read_visible(self):
        observer, backend = make_observer()
        control_id = (11, (1,))
        backend.controls[control_id] = control(11, (1,), "x")
        observer.refresh_controls(force=True)
        backend.emit(control_id, "notification", "progress: compiling 42%")
        observer.poll(NOW)
        self.assertEqual(observer.observations, {})
        self.assertEqual(backend.reads, [])   # 没有弱触发就不做可见读取
        # 活动时间被记录（Claude transcript 回归时的 WORKING 证据）
        self.assertIn(control_id, observer.activity)

    def test_approval_disappears_after_ttl_recheck(self):
        observer, backend = make_observer()
        control_id = (11, (1,))
        backend.controls[control_id] = control(11, (1,), "codex")
        observer.refresh_controls(force=True)
        backend.visible[control_id] = CODEX_APPROVAL_VISIBLE
        backend.emit(control_id, "notification", "Would you like to run the following command?")
        observer.poll(NOW)
        self.assertIn(control_id, observer.observations)
        # 用户在终端处理后 overlay 消失 → 复检清除（TTL 不残留）
        backend.visible[control_id] = "user@box:~/proj$ "
        observer.poll(NOW + 0.8)
        self.assertNotIn(control_id, observer.observations)

    def test_old_scrollback_approval_does_not_persist(self):
        """scrollback 里的旧审批文案不会造成永久 WAITING（plan §21）。"""
        observer, backend = make_observer()
        control_id = (11, (1,))
        backend.controls[control_id] = control(11, (1,), "codex")
        observer.refresh_controls(force=True)
        # 可见区域里审批 UI 已滚出（只剩普通输出），delta 是无关文本
        backend.visible[control_id] = "progress: 95%\nalmost done"
        backend.emit(control_id, "notification", "progress: 95%")
        observer.poll(NOW)
        self.assertEqual(observer.observations, {})
        # ring buffer 里即使残留旧审批 delta，没有弱触发 + 可见区域无 UI
        observer._push_delta(control_id, "Would you like to run? Yes, proceed")
        backend.visible[control_id] = "plain output"
        observer.poll(NOW + 1.0)
        self.assertEqual(observer.observations, {})

    def test_ring_buffer_bounded(self):
        observer, _ = make_observer()
        control_id = (11, (1,))
        for i in range(200):
            observer._push_delta(control_id, "x" * DELTA_MAX)
        self.assertLessEqual(observer._ring_len[control_id], RING_MAX)
        self.assertLessEqual(len(observer.rings[control_id]), RING_MAX // DELTA_MAX + 2)

    def test_event_queue_overflow_drops_oldest(self):
        observer, _ = make_observer()
        for i in range(EVENT_QUEUE_MAX + 50):
            observer._on_event(TerminalEvent(control_id=(i % 5,), kind="activity"))
        with observer._event_q_lock:
            size = len(observer._events)
        self.assertLessEqual(size, EVENT_QUEUE_MAX)
        self.assertEqual(observer.stats["dropped"], 50)

    def test_multiple_controls_tracked_independently(self):
        observer, backend = make_observer()
        c1, c2 = (11, (1,)), (11, (2,))
        backend.controls[c1] = control(11, (1,), "codex")
        backend.controls[c2] = control(11, (2,), "claude")
        observer.refresh_controls(force=True)
        backend.visible[c1] = CODEX_APPROVAL_VISIBLE
        backend.visible[c2] = CLAUDE_PERMISSION_VISIBLE
        backend.emit(c1, "notification", "Would you like to run")
        backend.emit(c2, "notification", "Do you want to proceed")
        observer.poll(NOW)
        self.assertEqual(observer.observations[c1].summary, "命令执行需要确认")
        self.assertEqual(observer.observations[c2].summary, "Bash 命令需要确认")

    def test_disappeared_control_state_cleaned(self):
        observer, backend = make_observer()
        control_id = (11, (1,))
        backend.controls[control_id] = control(11, (1,), "x")
        observer.refresh_controls(force=True)
        observer.observations[control_id] = None  # placeholder
        observer.activity[control_id] = NOW
        # control 关闭 → 重发现后清理
        observer._last_discover = 0
        backend.controls.clear()
        observer.refresh_controls(force=True)
        self.assertNotIn(control_id, observer.observations)
        self.assertNotIn(control_id, observer.activity)

    def test_control_activity_observation(self):
        observer, backend = make_observer()
        control_id = (11, (1,))
        backend.controls[control_id] = control(11, (1,), "x")
        observer.refresh_controls(force=True)
        backend.emit(control_id, "activity", "", ts=NOW)
        observer.poll(NOW)
        obs = observer.control_activity_observation(control_id, NOW + 1, 10.0)
        self.assertIsNotNone(obs)
        self.assertEqual(obs.status, Status.WORKING)
        self.assertEqual(obs.confidence, Confidence.MEDIUM)
        self.assertIsNone(observer.control_activity_observation(control_id, NOW + 60, 10.0))


class TextChangedFallbackTests(unittest.TestCase):
    """TextChanged → debounce → 有界可见读取（Notification 失效时的兜底）。"""

    def _observer_with_control(self, control_id=(11, (1,)), visible="plain output"):
        observer, backend = make_observer()
        backend.controls[control_id] = control(11, (1,), "x")
        observer.refresh_controls(force=True)
        backend.visible[control_id] = visible
        return observer, backend

    def test_text_changed_schedules_bounded_visible_read(self):
        observer, backend = self._observer_with_control()
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
        observer, backend = self._observer_with_control()
        for i in range(100):
            backend.emit((11, (1,)), "activity", "", ts=NOW)
        observer.poll(NOW)
        observer.poll(NOW + 0.2)
        # 100 个 TextChanged 合并为 1 次可见读取
        self.assertEqual(len(backend.reads), 1)
        observer.poll(NOW + 0.4)
        observer.poll(NOW + 0.6)
        # 单 control 限流（≥0.5s 间隔）后也只再有界增长
        self.assertLessEqual(len(backend.reads), 2)

    def test_text_changed_fallback_detects_approval(self):
        observer, backend = self._observer_with_control(
            visible=CODEX_APPROVAL_VISIBLE)
        backend.emit((11, (1,)), "activity", "", ts=NOW)
        observer.poll(NOW + 0.2)
        self.assertIn((11, (1,)), observer.observations)
        self.assertEqual(observer.observations[(11, (1,))].status,
                         Status.WAITING)

    def test_global_visible_read_budget(self):
        observer, backend = make_observer()
        for i in range(8):
            control_id = (11, (i,))
            backend.controls[control_id] = control(11, (i,), f"p{i}")
        observer.refresh_controls(force=True)
        for i in range(8):
            backend.emit((11, (i,)), "activity", "", ts=NOW)
        observer.poll(NOW)
        observer.poll(NOW + 0.2)
        # 8 个 dirty control，全局预算 6/s：最多读 6 次
        self.assertLessEqual(len(backend.reads), GLOBAL_VISIBLE_READ_LIMIT)
        # 预算耗尽的 control 被推迟，下一轮（1s 窗口滑过后）再读
        self.assertTrue(observer._dirty_controls or len(backend.reads) == 8)

    def test_waiting_recheck_not_debounced(self):
        observer, backend = self._observer_with_control(
            visible=CODEX_APPROVAL_VISIBLE)
        backend.emit((11, (1,)), "notification",
                     "Would you like to run the following command?", ts=NOW)
        observer.poll(NOW)
        # 审批等待期间走 0.75s 复检通道，不进 debounce 队列
        self.assertIn((11, (1,)), observer._waiting_recheck)
        self.assertNotIn((11, (1,)), observer._dirty_controls)


class StructureChangedTests(unittest.TestCase):
    def test_structure_event_triggers_immediate_rediscovery(self):
        observer, backend = make_observer()
        base = backend.discover_count
        backend.emit((), "structure", "", ts=NOW)
        observer.poll(NOW)
        self.assertGreater(backend.discover_count, base)

    def test_new_control_discovered_without_waiting(self):
        observer, backend = make_observer()
        backend.emit((), "structure", "", ts=NOW)
        observer.poll(NOW)      # 重发现：control 集合尚空
        control_id = (11, (9,))
        backend.controls[control_id] = control(11, (9,), "new")
        observer._last_discover = time.time()   # 阻止周期性重发现
        backend.emit((), "structure", "", ts=NOW + 1)
        observer.poll(NOW + 1)
        self.assertIn(control_id, observer.controls)

    def test_removed_control_unsubscribed(self):
        observer, backend = make_observer()
        c1 = (11, (1,))
        backend.controls[c1] = control(11, (1,), "x")
        observer.refresh_controls(force=True)
        self.assertEqual(len(backend.sync_history), 1)
        del backend.controls[c1]
        observer.refresh_controls(force=True)
        self.assertEqual(backend.unsubscribed, [c1])


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

    def test_control_activity_observation_has_no_kind(self):
        observer, backend = make_observer()
        control_id = (11, (1,))
        backend.controls[control_id] = control(11, (1,), "x")
        observer.refresh_controls(force=True)
        backend.emit(control_id, "activity", "", ts=NOW)
        observer.poll(NOW)
        obs = observer.control_activity_observation(control_id, NOW + 1, 10.0)
        self.assertIsNone(obs.agent_kind)


# ============================================================ window resolver

class WindowResolverTests(unittest.TestCase):
    def _wsl(self, kind=AgentKind.CODEX, cwd="/w/x", user="", pid=1):
        return AgentInstance(kind=kind, pid=pid, source="wsl:Ubuntu",
                             process_token=str(pid), cwd=cwd, user=user)

    def test_windows_native_ancestor_chain_confirmed(self):
        resolver = TerminalWindowResolver(
            enum_windows=lambda: [(11, 50, "codex", WT_WINDOW_CLASS)],
            ancestor_pids=lambda pid: {pid, 50},
        )
        inst = AgentInstance(AgentKind.CODEX, 99, "windows", process_token="9")
        bindings = resolver.resolve([inst], {}, NOW)
        b = bindings[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.CONFIRMED)
        self.assertEqual(b.hwnd, 11)
        self.assertEqual(b.reason, "windows-ancestor")

    def test_windows_native_multi_control_window_still_confirmed(self):
        """window-only 语义：窗口唯一即 CONFIRMED，不要求唯一 control
        （旧"窗口唯一但多 pane = AMBIGUOUS"是 exact-pane 残留，已删除）。"""
        resolver = TerminalWindowResolver(
            enum_windows=lambda: [(11, 50, "wt", WT_WINDOW_CLASS)],
            ancestor_pids=lambda pid: {pid, 50},
        )
        inst = AgentInstance(AgentKind.CODEX, 99, "windows", process_token="9")
        controls = {
            (11, (1,)): control(11, (1,), "codex"),
            (11, (2,)): control(11, (2,), "claude"),
        }
        bindings = resolver.resolve([inst], controls, NOW)
        self.assertEqual(bindings[inst.key].confidence,
                         WindowBindingConfidence.CONFIRMED)

    def test_windows_native_multi_window_ambiguous_no_window(self):
        resolver = TerminalWindowResolver(
            enum_windows=lambda: [(1, 10, "wt", WT_WINDOW_CLASS),
                                  (2, 10, "wt", WT_WINDOW_CLASS)],
            ancestor_pids=lambda pid: {10},
        )
        inst = AgentInstance(AgentKind.CODEX, 99, "windows", process_token="9")
        bindings = resolver.resolve([inst], {}, NOW)
        b = bindings[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.AMBIGUOUS)
        self.assertIsNone(b.window)
        self.assertEqual(b.reason, "multi-window-ancestor")

    def test_wsl_title_scoring_high_when_unique(self):
        resolver = TerminalWindowResolver(
            enum_windows=lambda: [(11, 5, "u@box:~/DeskPet", WT_WINDOW_CLASS)])
        inst = self._wsl(cwd="/home/u/DeskPet", user="u")
        controls = {
            (11, (1,)): control(11, (1,), "u@box:~/DeskPet"),
            (11, (2,)): control(11, (2,), "PowerShell"),
        }
        bindings = resolver.resolve([inst], controls, NOW)
        b = bindings[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.HIGH)
        self.assertEqual(b.hwnd, 11)
        self.assertIn("cwd", b.reason)

    def test_wsl_window_title_is_second_evidence_source(self):
        """TermControl Name 停在 "Ubuntu"，但 WT 顶层窗口标题带路径 → HIGH。"""
        resolver = TerminalWindowResolver(
            enum_windows=lambda: [(11, 5, "dev@box: ~/proj/app",
                                   WT_WINDOW_CLASS)])
        inst = self._wsl(cwd="/home/dev/proj/app", user="dev")
        controls = {(11, (1,)): control(11, (1,), "Ubuntu")}
        bindings = resolver.resolve([inst], controls, NOW)
        b = bindings[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.HIGH)
        self.assertIn("cwd", b.reason)

    def test_wsl_two_similar_controls_ambiguous(self):
        resolver = TerminalWindowResolver(
            enum_windows=lambda: [(11, 5, "u@box:~/DeskPet", WT_WINDOW_CLASS),
                                  (22, 6, "u@box:~/DeskPet",
                                   WT_WINDOW_CLASS)])
        inst = self._wsl(cwd="/home/u/DeskPet", user="u")
        controls = {
            (11, (1,)): control(11, (1,), "u@box:~/DeskPet"),
            (22, (2,)): control(22, (2,), "u@box:~/DeskPet"),
        }
        bindings = resolver.resolve([inst], controls, NOW)
        self.assertEqual(bindings[inst.key].confidence,
                         WindowBindingConfidence.AMBIGUOUS)
        self.assertIsNone(bindings[inst.key].window)

    def test_wsl_kind_in_title_strong(self):
        resolver = TerminalWindowResolver(
            enum_windows=lambda: [(11, 5, "claude", WT_WINDOW_CLASS)])
        inst = self._wsl(kind=AgentKind.CLAUDE, cwd="/x")
        controls = {(11, (1,)): control(11, (1,), "claude")}
        bindings = resolver.resolve([inst], controls, NOW)
        self.assertEqual(bindings[inst.key].confidence,
                         WindowBindingConfidence.HIGH)

    def test_single_window_fallback_allows_window_but_not_high(self):
        """唯一 WT 窗口兜底：FALLBACK 允许唤起窗口（plan §5.3）。"""
        resolver = TerminalWindowResolver(
            enum_windows=lambda: [(11, 5, "shell", WT_WINDOW_CLASS)])
        inst = self._wsl(cwd="/home/dev/proj/app", user="dev")
        controls = {(11, (1,)): control(11, (1,), "Ubuntu")}
        bindings = resolver.resolve([inst], controls, NOW)
        b = bindings[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.FALLBACK)
        self.assertEqual(b.hwnd, 11)
        self.assertEqual(b.reason, "single-window-fallback")

    def test_no_window_no_binding(self):
        resolver = TerminalWindowResolver(enum_windows=lambda: [])
        inst = self._wsl()
        bindings = resolver.resolve([inst], {}, NOW)
        b = bindings[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.NONE)
        self.assertEqual(b.hwnd, 0)

    def test_multiple_windows_no_evidence_ambiguous(self):
        resolver = TerminalWindowResolver(
            enum_windows=lambda: [(11, 5, "a", WT_WINDOW_CLASS),
                                  (22, 6, "b", WT_WINDOW_CLASS)])
        inst = self._wsl()
        bindings = resolver.resolve([inst], {}, NOW)
        b = bindings[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.AMBIGUOUS)
        self.assertIsNone(b.window)
        self.assertEqual(b.reason, "multiple terminal windows")


class MutualBindingTests(unittest.TestCase):
    def test_weak_single_control_not_high(self):
        """唯一 control + 1 分弱提示（只命中 user@）→ 绝不能 HIGH。"""
        resolver = TerminalWindowResolver(
            enum_windows=lambda: [(11, 5, "u@box", WT_WINDOW_CLASS)])
        inst = AgentInstance(AgentKind.CODEX, 1, "wsl:Ubuntu",
                             process_token="9", cwd="/x/y", user="u")
        controls = {(11, (1,)): control(11, (1,), "u@box")}
        bindings = resolver.resolve([inst], controls, NOW)
        b = bindings[inst.key]
        # 单窗口兜底也不够格（有两个窗口才 AMBIGUOUS；这里唯一窗口）
        self.assertEqual(b.confidence, WindowBindingConfidence.FALLBACK)
        self.assertLess(b.score, 3)

    def test_two_agents_two_controls_mutual_unique(self):
        def enum():
            return [(11, 5, "wt", WT_WINDOW_CLASS)]
        codex = AgentInstance(AgentKind.CODEX, 1, "wsl:Ubuntu",
                              process_token="9", cwd="/w/alpha", user="u1")
        claude = AgentInstance(AgentKind.CLAUDE, 2, "wsl:Ubuntu",
                               process_token="10", cwd="/w/beta", user="u2")
        controls = {
            (11, (1,)): control(11, (1,), "codex u1@box:~/alpha"),
            (11, (2,)): control(11, (2,), "claude u2@box:~/beta"),
        }
        r1 = TerminalWindowResolver(enum_windows=enum).resolve(
            [codex, claude], controls, NOW)
        r2 = TerminalWindowResolver(enum_windows=enum).resolve(
            [claude, codex], controls, NOW)   # 顺序无关
        self.assertEqual(r1[codex.key].confidence, WindowBindingConfidence.HIGH)
        self.assertEqual(r1[claude.key].confidence, WindowBindingConfidence.HIGH)
        self.assertEqual(
            {k: v.hwnd for k, v in r1.items()},
            {k: v.hwnd for k, v in r2.items()})

    def test_close_competition_with_margin_still_high(self):
        resolver = TerminalWindowResolver(
            enum_windows=lambda: [(11, 5, "wt", WT_WINDOW_CLASS)])
        a = AgentInstance(AgentKind.CODEX, 1, "wsl:Ubuntu",
                          process_token="9", cwd="/w/alpha", user="u")
        b = AgentInstance(AgentKind.CODEX, 2, "wsl:Ubuntu",
                          process_token="10", cwd="/w/beta", user="u")
        # 两个 control 都含 "codex"（+3）与 "u@"（+1），各自 cwd 差异化（+2）
        controls = {
            (11, (1,)): control(11, (1,), "codex alpha u@box"),
            (11, (2,)): control(11, (2,), "codex beta u@box"),
        }
        bindings = resolver.resolve([a, b], controls, NOW)
        # A: X=6, Y=4；B: X=4, Y=6 → 双向唯一，margin=2 → 都 HIGH
        self.assertEqual(bindings[a.key].confidence, WindowBindingConfidence.HIGH)
        self.assertEqual(bindings[b.key].confidence, WindowBindingConfidence.HIGH)


class ObservationResolverTests(unittest.TestCase):
    def _native(self, pid=99, token="9"):
        return AgentInstance(AgentKind.CODEX, pid, "windows",
                             process_token=token)

    def test_native_sole_control_confirmed(self):
        controls = {(11, (1,)): control(11, (1,), "codex")}
        windows = {11: WindowIdentity(11, 50, 1.0, WT_WINDOW_CLASS)}
        bindings = {self._native().key: None}
        inst = self._native()
        resolver = TerminalObservationResolver(enum_windows=lambda: [])
        window_bindings = {
            inst.key: type("B", (), {
                "window": windows[11], "hwnd": 11,
                "confidence": WindowBindingConfidence.CONFIRMED})()}
        out = resolver.resolve([inst], controls, window_bindings, NOW)
        self.assertIn(inst.key, out)
        self.assertEqual(out[inst.key].confidence,
                         ObservationBindingConfidence.CONFIRMED)
        self.assertEqual(out[inst.key].control_id, (11, (1,)))

    def test_native_multiple_controls_no_attribution_without_evidence(self):
        """窗口 CONFIRMED 但有两个 control 且无标题证据 → 不归属（fail-closed）。"""
        inst = self._native()
        controls = {
            (11, (1,)): control(11, (1,), "x"),
            (11, (2,)): control(11, (2,), "y"),
        }
        window_bindings = {
            inst.key: type("B", (), {
                "window": WindowIdentity(11, 50, 1.0, WT_WINDOW_CLASS),
                "hwnd": 11,
                "confidence": WindowBindingConfidence.CONFIRMED})()}
        resolver = TerminalObservationResolver(enum_windows=lambda: [])
        out = resolver.resolve([inst], controls, window_bindings, NOW)
        self.assertEqual(out, {})

    def test_fallback_window_binding_never_grants_observation(self):
        """FALLBACK 窗口兜底允许唤起，但不赋予 terminal evidence（§5.3）。"""
        inst = AgentInstance(AgentKind.CODEX, 1, "wsl:Ubuntu",
                             process_token="9")
        controls = {(11, (1,)): control(11, (1,), "Ubuntu")}
        window_bindings = {
            inst.key: type("B", (), {
                "window": WindowIdentity(11, 5, 1.0, WT_WINDOW_CLASS),
                "hwnd": 11,
                "confidence": WindowBindingConfidence.FALLBACK})()}
        resolver = TerminalObservationResolver(enum_windows=lambda: [])
        out = resolver.resolve([inst], controls, window_bindings, NOW)
        self.assertEqual(out, {})

    def test_wsl_title_evidence_high(self):
        inst = AgentInstance(AgentKind.CODEX, 1, "wsl:Ubuntu",
                             process_token="9", cwd="/home/dev/proj/app",
                             user="dev")
        controls = {(11, (1,)): control(11, (1,), "dev@box: ~/proj/app")}
        resolver = TerminalObservationResolver(
            enum_windows=lambda: [(11, 5, "dev@box: ~/proj/app",
                                   WT_WINDOW_CLASS)])
        out = resolver.resolve([inst], controls, {}, NOW)
        self.assertEqual(out[inst.key].confidence,
                         ObservationBindingConfidence.HIGH)
        self.assertEqual(out[inst.key].control_id, (11, (1,)))


class ScoringRegressionTests(unittest.TestCase):
    """V4.1.1 评分修正（用户反馈：无法扫描关联终端）。

    * 词边界：kind "pi" 不得命中 "pip"；"codex" 命中 "codex · task"；
    * ~/路径标记：`user@host: ~/a/b` 按 cwd 归一化比对，不再用裸
      basename（用户名==家目录名时会造成所有同用户 control 假命中）；
    * WT 顶层窗口标题是第二条证据：TermControl Name 停在 profile 名
      时仍可通过窗口标题达成 HIGH；
    * "Ubuntu" 这类无路径标题不产生假 HIGH（fail-closed）。
    """

    def _inst(self, kind=AgentKind.CODEX, cwd="/w/x", user=""):
        return AgentInstance(kind=kind, pid=1, source="wsl:Ubuntu",
                             process_token="9", cwd=cwd, user=user)

    def test_pi_word_boundary_not_pip(self):
        resolver = TerminalWindowResolver(enum_windows=lambda: [])
        inst = self._inst(kind=AgentKind.PI)
        score, reason = resolver._scorer.score(inst, "pip install requests")
        self.assertEqual(score, 0)
        self.assertNotIn("kind", reason)

    def test_kind_word_boundary_matches(self):
        resolver = TerminalWindowResolver(enum_windows=lambda: [])
        inst = self._inst(kind=AgentKind.CODEX)
        score, _ = resolver._scorer.score(inst, "codex · 编码中")
        self.assertGreaterEqual(score, 3)

    def test_tilde_path_marker_scores_cwd(self):
        resolver = TerminalWindowResolver(enum_windows=lambda: [])
        inst = self._inst(cwd="/home/dev/proj/app", user="dev")
        score, reason = resolver._scorer.score(inst, "dev@box: ~/proj/app")
        self.assertIn("cwd", reason)
        self.assertIn("user@", reason)

    def test_home_dir_basename_does_not_match_user_host(self):
        """Agent 在家目录：不得因 basename==用户名 命中所有 user@host 标题。"""
        resolver = TerminalWindowResolver(enum_windows=lambda: [])
        inst = self._inst(cwd="/home/dev", user="dev")
        score, reason = resolver._scorer.score(inst, "dev@box: ~/proj/app")
        # 只有 user@ 弱证据，绝不能有 cwd
        self.assertNotIn("cwd", reason)
        self.assertLess(score, 3)

    def test_generic_profile_title_stays_fallback_or_ambiguous(self):
        resolver = TerminalWindowResolver(
            enum_windows=lambda: [(11, 5, "a", WT_WINDOW_CLASS),
                                  (22, 6, "b", WT_WINDOW_CLASS)])
        inst = self._inst(cwd="/home/dev/proj/app", user="dev")
        controls = {(11, (1,)): control(11, (1,), "Ubuntu")}
        bindings = resolver.resolve([inst], controls, NOW)
        self.assertNotEqual(bindings[inst.key].confidence,
                            WindowBindingConfidence.HIGH)

    def test_two_distinct_paths_both_high(self):
        """两个不同项目的 control + 两个对应 Agent → 双向唯一都 HIGH。"""
        resolver = TerminalWindowResolver(
            enum_windows=lambda: [(11, 5, "wt", WT_WINDOW_CLASS)])
        a = self._inst(kind=AgentKind.CODEX, cwd="/home/dev/alpha", user="dev")
        b = self._inst(kind=AgentKind.CLAUDE, cwd="/home/dev/beta", user="dev")
        controls = {
            (11, (1,)): control(11, (1,), "dev@box: ~/alpha"),
            (11, (2,)): control(11, (2,), "dev@box: ~/beta"),
        }
        bindings = resolver.resolve([a, b], controls, NOW)
        self.assertEqual(bindings[a.key].confidence, WindowBindingConfidence.HIGH)
        self.assertEqual(bindings[b.key].confidence, WindowBindingConfidence.HIGH)


if __name__ == "__main__":
    unittest.main()
