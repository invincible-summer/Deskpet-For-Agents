"""UIA 观察器单元测试：FakeBackend，不需要真实 Terminal（v4.1.3）。

  * 观察流：Notification/TextChanged debounce/可见读取限流/TTL 复检/
    StructureChanged 重发现/订阅生命周期；
  * 解析器：observation-only 严格链（§7/§28）——Window 唤起候选语义
    已分离到 tests/test_terminal_window_resolver.py；
  * 删除所有 selected-tab topology 断言（产品已不承诺 Tab/Pane）。
"""
from __future__ import annotations
import time
import unittest

from agents.models import Confidence, EvidenceSource, Status
from agents.terminal_uia import (
    APPROVAL_TTL,
    CONTROL_VISIBLE_READ_MIN_INTERVAL,
    DELTA_MAX,
    EVENT_QUEUE_MAX,
    GLOBAL_VISIBLE_READ_LIMIT,
    MAX_CONTROLS,
    MAX_VISIBLE_READS_PER_POLL,
    RING_MAX,
    SCREEN_DIGEST_PER_POLL,
    SUBSCRIPTION_RETRY_SEC,
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
    VisibleReadReason,
    WEAK_TRIGGER_RE,
)
from agents.terminal_resolver import TerminalObservationResolver
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


def seed_screen_digest(observer, control_id, text="seeded", ts=NOW):
    """预置新鲜屏幕摘要：摘要补读通道跳过，测试聚焦审批/活动通道。"""
    observer._screens[control_id] = text
    observer._screen_read_at[control_id] = ts


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
        seed_screen_digest(observer, control_id)
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
        seed_screen_digest(observer, control_id)
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
        # 预算耗尽的请求保留 pending（v4.2.3 broker），下一轮再服务
        self.assertTrue(observer._pending_reads or len(backend.reads) == 8)
        self.assertLessEqual(len(observer._pending_reads), MAX_CONTROLS)

    def test_waiting_recheck_not_debounced(self):
        observer, backend = self._observer_with_control(
            visible=CODEX_APPROVAL_VISIBLE)
        backend.emit((11, (1,)), "notification",
                     "Would you like to run the following command?", ts=NOW)
        observer.poll(NOW)
        # 审批等待期间走 0.75s 复检通道，不进 debounce 队列
        self.assertIn((11, (1,)), observer._waiting_recheck)
        self.assertNotIn((11, (1,)), observer._dirty_controls)


class ScreenDigestTests(unittest.TestCase):
    """v4.1.4 屏幕摘要通道：窗口候选评分证据。

    合同：仅内存（screen_texts 只给 resolver 评分，绝不外显/持久化）；
    缺失/过期才补读；与审批通道共享全局 ≤6/s 与单 control ≥0.5s 预算；
    _inspect_visible 顺带更新；control 消失即清理。
    """

    def _observer(self, n=5):
        observer, backend = make_observer()
        for i in range(n):
            cid = (11, (i,))
            backend.controls[cid] = control(11, (i,), f"p{i}")
            backend.visible[cid] = f"screen text {i}"
        observer.refresh_controls(force=True)
        return observer, backend

    def test_poll_populates_digests_bounded_per_poll(self):
        observer, backend = self._observer(5)
        observer.poll(NOW)
        self.assertEqual(len(observer.screen_texts()), SCREEN_DIGEST_PER_POLL)
        observer.poll(NOW + 3.0)
        self.assertEqual(len(observer.screen_texts()), 4)
        observer.poll(NOW + 6.0)
        self.assertEqual(len(observer.screen_texts()), 5)
        # 摘要内容 = 可见文本（只留在内存）
        self.assertIn("screen text 0",
                      observer.screen_texts()[(11, (0,))])

    def test_digest_reads_share_global_budget(self):
        observer, backend = self._observer(8)
        observer.poll(NOW)
        observer.poll(NOW + 0.4)
        observer.poll(NOW + 0.8)
        observer.poll(NOW + 0.95)
        # 1s 窗口内总读取（含摘要通道）≤ 6
        self.assertLessEqual(len(observer.screen_texts()),
                             GLOBAL_VISIBLE_READ_LIMIT)
        # 窗口滑过后补齐
        observer.poll(NOW + 2.5)
        self.assertEqual(len(observer.screen_texts()), 8)

    def test_fresh_digest_not_reread(self):
        observer, backend = self._observer(1)
        observer.poll(NOW)
        reads = len(backend.reads)
        observer.poll(NOW + 1.0)
        self.assertEqual(len(backend.reads), reads)   # 30s 内不重复读

    def test_inspect_visible_updates_digest(self):
        observer, backend = make_observer()
        cid = (11, (1,))
        backend.controls[cid] = control(11, (1,), "codex")
        observer.refresh_controls(force=True)
        backend.visible[cid] = CODEX_APPROVAL_VISIBLE
        backend.emit(cid, "notification",
                     "Would you like to run the following command?", ts=NOW)
        observer.poll(NOW)
        # 审批通道的同一次读取顺带填充摘要，不额外读
        self.assertIn("Would you like to run",
                      observer.screen_texts()[cid])
        self.assertEqual(backend.reads.count(cid), 1)

    def test_removed_control_prunes_digest(self):
        observer, backend = self._observer(1)
        observer.poll(NOW)
        self.assertTrue(observer.screen_texts())
        del backend.controls[(11, (0,))]
        observer.refresh_controls(force=True)
        self.assertEqual(observer.screen_texts(), {})

    def test_stopped_observer_skips_digest(self):
        observer, backend = self._observer(1)
        observer._started = False
        observer.poll(NOW)
        self.assertEqual(observer.screen_texts(), {})


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


# ============================================================ observation resolver

class ObservationResolverTests(unittest.TestCase):
    """观察链保持 v4.1.2 严格语义（v4.1.3 §7/§28）：Window 唤起放宽
    （AMBIGUOUS/NONE 可携带窗口）绝不意味着 observation attribution
    放宽——低置信 Window 候选不直接授予任何 Terminal 证据。"""

    def _native(self, pid=99, token="9"):
        return AgentInstance(AgentKind.CODEX, pid, "windows",
                             process_token=token)

    def _binding_stub(self, hwnd=11, confidence=WindowBindingConfidence.NONE):
        return type("B", (), {
            "window": WindowIdentity(hwnd, hwnd + 100, 1.0, WT_WINDOW_CLASS),
            "hwnd": hwnd,
            "confidence": confidence})()

    def test_native_sole_control_confirmed(self):
        controls = {(11, (1,)): control(11, (1,), "codex")}
        inst = self._native()
        resolver = TerminalObservationResolver(enum_windows=lambda: [])
        window_bindings = {
            inst.key: self._binding_stub(
                confidence=WindowBindingConfidence.CONFIRMED)}
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
            inst.key: self._binding_stub(
                confidence=WindowBindingConfidence.CONFIRMED)}
        resolver = TerminalObservationResolver(enum_windows=lambda: [])
        out = resolver.resolve([inst], controls, window_bindings, NOW)
        self.assertEqual(out, {})

    def test_ambiguous_window_with_hwnd_never_grants_observation(self):
        """§28：Window AMBIGUOUS + valid HWND + 多 control 无 strict
        evidence → 无 TerminalObservationBinding（可唤起 ≠ 可归属）。"""
        inst = AgentInstance(AgentKind.CODEX, 1, "wsl:Ubuntu",
                             process_token="9")
        controls = {
            (11, (1,)): control(11, (1,), "x"),
            (11, (2,)): control(11, (2,), "y"),
        }
        window_bindings = {
            inst.key: self._binding_stub(
                confidence=WindowBindingConfidence.AMBIGUOUS)}
        resolver = TerminalObservationResolver(enum_windows=lambda: [])
        out = resolver.resolve([inst], controls, window_bindings, NOW)
        self.assertEqual(out, {})

    def test_sole_window_fallback_never_grants_observation(self):
        """§28：Window NONE + 唯一窗口兜底 HWND + generic profile 标题
        → 无 TerminalObservationBinding。"""
        inst = AgentInstance(AgentKind.CODEX, 1, "wsl:Ubuntu",
                             process_token="9")
        controls = {(11, (1,)): control(11, (1,), "Ubuntu")}
        window_bindings = {
            inst.key: self._binding_stub(confidence=WindowBindingConfidence.NONE)}
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

    def test_window_title_second_evidence_high(self):
        """TermControl Name 停在 "Ubuntu"，但 WT 顶层窗口标题带路径 →
        观察链仍可 HIGH（窗口标题第二证据只属于观察链，§4.3）。"""
        inst = AgentInstance(AgentKind.CODEX, 1, "wsl:Ubuntu",
                             process_token="9", cwd="/home/dev/proj/app",
                             user="dev")
        controls = {(11, (1,)): control(11, (1,), "Ubuntu")}
        resolver = TerminalObservationResolver(
            enum_windows=lambda: [(11, 5, "dev@box: ~/proj/app",
                                   WT_WINDOW_CLASS)])
        out = resolver.resolve([inst], controls, {}, NOW)
        self.assertEqual(out[inst.key].confidence,
                         ObservationBindingConfidence.HIGH)


class VisibleReadBrokerTests(unittest.TestCase):
    """v4.2.3 §5.1 统一可见读取 broker：全路径单一生产入口 + 预算。"""

    def _observer(self, n=1):
        observer, backend = make_observer()
        for i in range(n):
            cid = (11, (i,))
            backend.controls[cid] = control(11, (i,), f"p{i}")
            backend.visible[cid] = CODEX_APPROVAL_VISIBLE
        observer.refresh_controls(force=True)
        return observer, backend

    def test_backend_read_visible_only_called_from_broker(self):
        # AC-UIA-01：生产代码中 backend.read_visible 只能从
        # _read_visible_once 调用（源码静态检查，测试 Fake 除外）
        import pathlib
        src = pathlib.Path("agents", "terminal_uia.py")
        if not src.exists():
            src = pathlib.Path(__file__).resolve().parents[1] / "agents" / "terminal_uia.py"
        text = src.read_text(encoding="utf-8")
        import re as _re
        hits = [line.strip() for line in text.splitlines()
                if "read_visible(" in line and "def read_visible" not in line]
        producers = [h for h in hits
                     if "self.backend.read_visible" in h]
        self.assertEqual(len(producers), 1)
        self.assertIn("self.backend.read_visible(control_id)", producers[0])

    def test_single_control_interval_enforced(self):
        # AC-UIA-02：单 control ≥0.5s 间隔
        observer, backend = self._observer(1)
        cid = (11, (0,))
        backend.emit(cid, "notification",
                     "Would you like to run the following command?", ts=NOW)
        observer.poll(NOW)
        self.assertEqual(len(backend.reads), 1)
        # 再触发一个弱触发（不同事件流）：同 control 间隔内不读
        backend.emit(cid, "notification",
                     "Would you like to proceed? yes/no", ts=NOW + 0.2)
        observer.poll(NOW + 0.2)
        self.assertEqual(len(backend.reads), 1)
        observer.poll(NOW + 0.6)
        self.assertLessEqual(len(backend.reads), 2)

    def test_per_poll_max_three_real_reads(self):
        # AC-UIA-03 + §5.1：一次 poll 最多 3 次真实 read
        observer, backend = self._observer(8)
        for i in range(8):
            backend.emit((11, (i,)), "notification",
                         "Would you like to run the following command?",
                         ts=NOW)
        observer.poll(NOW)
        self.assertEqual(len(backend.reads), MAX_VISIBLE_READS_PER_POLL)
        # 剩余请求 pending（每 control 最多 1 个 → ≤MAX_CONTROLS）
        self.assertLessEqual(len(observer._pending_reads), MAX_CONTROLS)

    def test_priority_upgrade_and_pending_bounded(self):
        # 弱触发风暴不能扩大 pending；高优先级升级同 control 旧请求
        observer, backend = self._observer(16)
        for i in range(16):
            backend.emit((11, (i,)), "activity", "", ts=NOW)
        observer.poll(NOW + 0.2)   # 16 个 TEXT_FALLBACK 到期
        self.assertLessEqual(len(observer._pending_reads), MAX_CONTROLS)
        stats = observer.stats
        self.assertLessEqual(stats["pending_visible_reads"], MAX_CONTROLS)
        by_reason = stats["visible_reads_by_reason"]
        self.assertEqual(
            sum(by_reason.values()), stats["visible_reads"])

    def test_read_failure_not_counted_and_no_fake_waiting(self):
        # 读取失败：不消耗预算 token、不伪造 WAITING
        observer, backend = self._observer(1)
        cid = (11, (0,))

        def boom(_control_id):
            raise RuntimeError("uia broken")
        backend.read_visible = boom
        backend.emit(cid, "notification",
                     "Would you like to run the following command?", ts=NOW)
        observer.poll(NOW)
        self.assertEqual(observer.observations, {})
        self.assertEqual(len(observer._visible_read_times), 0)
        self.assertEqual(observer.stats["visible_reads"], 0)

    def test_screen_digest_shares_budget_with_approval(self):
        # 全部通道共用同一预算：任意 1s 窗口 ≤6
        observer, backend = self._observer(6)
        prev = 0
        reads_per_step = []
        for step in range(10):
            now = NOW + step * 0.25
            for i in range(6):
                backend.emit((11, (i,)), "activity", "", ts=now)
            observer.poll(now)
            total = len(backend.reads)
            reads_per_step.append(total - prev)
            prev = total
        # 任意 1s（4 个 step）窗口内的读取总数 ≤6（允许 +1 边界噪声）
        for start in range(len(reads_per_step) - 3):
            window = sum(reads_per_step[start + k] for k in range(4))
            self.assertLessEqual(window, GLOBAL_VISIBLE_READ_LIMIT + 1,
                                 f"1s window at step {start}: {window}")


class SubscriptionRetryTests(unittest.TestCase):
    """v4.2.3 §5.2：订阅瞬时失败在 topology 不变时自动重试。"""

    class FlakyBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.failing = False
            self.sync_calls = 0

        def sync_control_subscriptions(self, controls):
            self.sync_calls += 1
            super().sync_control_subscriptions(controls)

        def subscription_retry_needed(self):
            return self.failing

    def _observer(self):
        backend = self.FlakyBackend()
        observer = TerminalObserver(backend,
                                    cfg={"terminal_observer": True})
        observer._started = True
        observer._last_discover = time.time()
        control_id = (11, (1,))
        backend.controls[control_id] = control(11, (1,), "x")
        observer.refresh_controls(force=True)
        return observer, backend, control_id

    def test_retry_when_flag_set_and_interval_elapsed(self):
        observer, backend, cid = self._observer()
        base = backend.sync_calls
        backend.failing = True
        observer.poll(NOW)
        self.assertEqual(backend.sync_calls, base + 1)
        self.assertEqual(observer.stats["subscription_retry_count"], 1)
        # 2s 内不重复重试（无空 UIA command 风暴）
        observer.poll(NOW + 1.0)
        self.assertEqual(backend.sync_calls, base + 1)
        observer.poll(NOW + SUBSCRIPTION_RETRY_SEC + 0.1)
        self.assertEqual(backend.sync_calls, base + 2)

    def test_healthy_backend_never_retries(self):
        # AC-UIA-04：healthy 时 retry counter 保持 0
        observer, backend, cid = self._observer()
        base = backend.sync_calls
        for step in range(5):
            observer.poll(NOW + step * 0.5)
        self.assertEqual(backend.sync_calls, base)
        self.assertEqual(observer.stats["subscription_retry_count"], 0)

    def test_uia_backend_flag_requires_full_coverage(self):
        # UiaBackend 覆盖判定：desired control/window 全部有活跃订阅才清除
        backend = UiaBackend()
        a = ObservedTerminalControl(control_id=(1, (1,)), hwnd=1,
                                    window_pid=5, title="a")
        backend._control_subscriptions[(1, (1,))] = object()
        backend._window_subscriptions[1] = object()
        self.assertTrue(backend._subscriptions_covered([a]))
        b_missing = ObservedTerminalControl(control_id=(1, (2,)), hwnd=1,
                                            window_pid=5, title="b")
        self.assertFalse(backend._subscriptions_covered([a, b_missing]))
        w_missing = ObservedTerminalControl(control_id=(1, (1,)), hwnd=9,
                                            window_pid=5, title="a")
        self.assertFalse(backend._subscriptions_covered([w_missing]))
        # 空列表 = 全部覆盖（没有期望订阅）
        self.assertTrue(backend._subscriptions_covered([]))


if __name__ == "__main__":
    unittest.main()
