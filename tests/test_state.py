"""StateReducer 测试：优先级、TTL、宽限、完成窗口（plan.md §26-§28）。"""
from __future__ import annotations
import unittest

from agents.models import (
    AgentInstance,
    AgentKind,
    Confidence,
    EvidenceSource,
    Mode,
    Observation,
    Phase,
    Status,
)
from agents.state import reduce_state


def make_instance():
    return AgentInstance(kind=AgentKind.CODEX, pid=1, source="wsl:Ubuntu",
                         process_token="9", cwd="/w")


def session_obs(status=Status.WORKING, **kw):
    defaults = dict(source=EvidenceSource.SESSION, timestamp=kw.pop("ts", 1000.0),
                    status=status, confidence=Confidence.HIGH,
                    turn_active=True, session_bound=True,
                    session_id="s1", session_file="/f.jsonl", cwd="/w")
    defaults.update(kw)
    return Observation(**defaults)


def terminal_obs(status=Status.WAITING, **kw):
    defaults = dict(source=EvidenceSource.TERMINAL, timestamp=kw.pop("ts", 1000.0),
                    status=status, phase=Phase.APPROVAL,
                    confidence=Confidence.HIGH, summary="命令需要确认",
                    expires_at=kw.pop("expires_at", 1001.5))
    defaults.update(kw)
    return Observation(**defaults)


class PriorityTests(unittest.TestCase):
    def test_error_beats_everything(self):
        inst = make_instance()
        snap = reduce_state(inst, session_obs(Status.ERROR),
                            terminal_obs(Status.WAITING), None, 1000.0)
        self.assertEqual(snap.status, Status.ERROR)

    def test_terminal_waiting_beats_session_working(self):
        inst = make_instance()
        snap = reduce_state(inst, session_obs(Status.WORKING, phase=Phase.CODING),
                            terminal_obs(), None, 1000.0)
        self.assertEqual(snap.status, Status.WAITING)
        self.assertEqual(snap.phase, Phase.APPROVAL)
        self.assertEqual(snap.evidence, EvidenceSource.TERMINAL)
        self.assertEqual(snap.waiting_detail, "命令需要确认")

    def test_input_beats_working(self):
        inst = make_instance()
        snap = reduce_state(inst, session_obs(Status.INPUT),
                            None, None, 1000.0)
        self.assertEqual(snap.status, Status.INPUT)
        self.assertEqual(snap.phase, Phase.USER_INPUT)

    def test_done_survives_generic_terminal_activity(self):
        """泛化终端活动不能推翻结构化 DONE（V3.1 证据强弱规则）。"""
        inst = make_instance()
        snap = reduce_state(inst, session_obs(Status.DONE),
                            terminal_obs(Status.WORKING, phase=Phase.NONE,
                                         expires_at=1008.0), None, 1000.0)
        self.assertEqual(snap.status, Status.DONE)

    def test_idle_survives_generic_terminal_activity(self):
        inst = make_instance()
        snap = reduce_state(inst, session_obs(Status.IDLE, turn_active=False),
                            terminal_obs(Status.WORKING, phase=Phase.NONE,
                                         expires_at=1008.0), None, 1000.0)
        self.assertEqual(snap.status, Status.IDLE)

    def test_error_and_input_survive_generic_terminal_activity(self):
        inst = make_instance()
        activity = terminal_obs(Status.WORKING, phase=Phase.NONE,
                                expires_at=1008.0)
        snap = reduce_state(inst, session_obs(Status.ERROR), activity,
                            None, 1000.0)
        self.assertEqual(snap.status, Status.ERROR)
        snap = reduce_state(inst, session_obs(Status.INPUT), activity,
                            None, 1000.0)
        self.assertEqual(snap.status, Status.INPUT)

    def test_no_session_status_activity_fallback_working(self):
        """会话无有效状态（占位观察）+ 终端活动 → WORKING/MEDIUM fallback。"""
        inst = make_instance()
        placeholder = Observation(source=EvidenceSource.SESSION, timestamp=1000.0,
                                   status=None, session_bound=False)
        snap = reduce_state(inst, placeholder,
                            terminal_obs(Status.WORKING, phase=Phase.NONE,
                                         confidence=Confidence.MEDIUM,
                                         expires_at=1008.0), None, 1000.0)
        self.assertEqual(snap.status, Status.WORKING)
        self.assertEqual(snap.confidence, Confidence.MEDIUM)
        self.assertEqual(snap.evidence, EvidenceSource.TERMINAL)

    def test_terminal_waiting_still_beats_session_done(self):
        """审批是语义级终端证据：仍然高于 DONE/IDLE（plan §26）。"""
        inst = make_instance()
        snap = reduce_state(inst, session_obs(Status.DONE),
                            terminal_obs(Status.WAITING), None, 1000.0)
        self.assertEqual(snap.status, Status.WAITING)

    def test_idle_when_session_known_over(self):
        inst = make_instance()
        snap = reduce_state(inst, session_obs(Status.IDLE, turn_active=False),
                            None, None, 1000.0)
        self.assertEqual(snap.status, Status.IDLE)


class TtlAndGraceTests(unittest.TestCase):
    def test_expired_terminal_waiting_is_ignored(self):
        inst = make_instance()
        snap = reduce_state(inst, session_obs(Status.WORKING),
                            terminal_obs(expires_at=999.0), None, 1000.0)
        self.assertEqual(snap.status, Status.WORKING)

    def test_activity_only_working_expires_to_unknown(self):
        """活动型证据过期 → UNKNOWN，不伪造 WORKING/IDLE（plan §28）。"""
        inst = make_instance()
        session = session_obs(Status.WORKING, turn_active=False,
                               expires_at=990.0, ts=980.0,
                               confidence=Confidence.MEDIUM)
        snap = reduce_state(inst, session, None, None, 1000.0)
        self.assertEqual(snap.status, Status.UNKNOWN)

    def test_active_turn_working_is_sticky(self):
        """已知 active turn → 无限保持 WORKING（plan §28）。"""
        inst = make_instance()
        snap = reduce_state(inst, session_obs(Status.WORKING, turn_active=True,
                                              ts=1.0), None, None, 10_000.0)
        self.assertEqual(snap.status, Status.WORKING)

    def test_no_evidence_is_unknown_not_idle(self):
        inst = make_instance()
        snap = reduce_state(inst, None, None, None, 1000.0)
        self.assertEqual(snap.status, Status.UNKNOWN)
        self.assertEqual(snap.evidence, EvidenceSource.PROCESS)


class FusionFieldTests(unittest.TestCase):
    def test_terminal_activity_keeps_session_phase_but_fuses_source(self):
        """终端活动 + 会话证据 → WORKING 且保留结构化 phase（plan §12）。"""
        inst = make_instance()
        session = session_obs(Status.WORKING, turn_active=True, phase=Phase.READING)
        terminal = terminal_obs(Status.WORKING, phase=Phase.NONE,
                                confidence=Confidence.MEDIUM, summary="终端活动",
                                expires_at=1008.0)
        snap = reduce_state(inst, session, terminal, None, 1000.0)
        self.assertEqual(snap.status, Status.WORKING)
        self.assertEqual(snap.phase, Phase.READING)
        self.assertEqual(snap.evidence, EvidenceSource.FUSED)

    def test_fields_carried_from_session(self):
        inst = make_instance()
        snap = reduce_state(inst, session_obs(Status.WORKING, mode=Mode.PLAN,
                                              goal="迁移 JWT", summary="修改 codex.py",
                                              phase=Phase.CODING, title="T"),
                            None, None, 1000.0)
        self.assertEqual(snap.mode, Mode.PLAN)
        self.assertEqual(snap.goal, "迁移 JWT")
        self.assertEqual(snap.summary, "修改 codex.py")
        self.assertEqual(snap.session_id, "s1")
        self.assertEqual(snap.session_file, "/f.jsonl")
        self.assertTrue(snap.session_bound)
        self.assertEqual(snap.cwd, "/w")

    def test_unbound_session_is_unknown_with_placeholder(self):
        inst = make_instance()
        placeholder = Observation(source=EvidenceSource.SESSION, timestamp=1000.0,
                                   status=None, session_bound=False,
                                   summary="Session：未解析")
        snap = reduce_state(inst, placeholder, None, None, 1000.0)
        self.assertEqual(snap.status, Status.UNKNOWN)
        self.assertFalse(snap.session_bound)


class ModeOrthogonalityTests(unittest.TestCase):
    """Status / Phase / Mode 是三个独立维度（V3.1.1）。

    终端 WAITING（审批识别器命中）成为状态胜者时，无权擦除 Session
    已解析出的独立 Mode——WAITING + APPROVAL + PLAN 是合法且必要的状态。
    """

    def test_terminal_waiting_preserves_session_plan_mode(self):
        inst = make_instance()
        snap = reduce_state(inst,
                            session_obs(Status.WORKING, mode=Mode.PLAN),
                            terminal_obs(Status.WAITING), None, 1000.0)
        self.assertEqual(snap.status, Status.WAITING)
        self.assertEqual(snap.phase, Phase.APPROVAL)
        self.assertEqual(snap.mode, Mode.PLAN)

    def test_terminal_waiting_preserves_unknown_mode_raw(self):
        inst = make_instance()
        session = session_obs(Status.WORKING, mode=Mode.UNKNOWN,
                              mode_raw="delegate")
        snap = reduce_state(inst, session, terminal_obs(Status.WAITING),
                            None, 1000.0)
        self.assertEqual(snap.status, Status.WAITING)
        self.assertEqual(snap.mode, Mode.UNKNOWN)
        self.assertEqual(snap.mode_raw, "delegate")

    def test_terminal_waiting_preserves_default_mode(self):
        # 不只为 PLAN 特判：任何 Session 结构化 Mode 都保留
        inst = make_instance()
        session = session_obs(Status.WORKING, mode=Mode.ACCEPT_EDITS)
        snap = reduce_state(inst, session, terminal_obs(Status.WAITING),
                            None, 1000.0)
        self.assertEqual(snap.status, Status.WAITING)
        self.assertEqual(snap.mode, Mode.ACCEPT_EDITS)

    def test_explicit_winner_mode_can_override_none(self):
        # 为未来携带 Mode 的 semantic observation 留接口语义：
        # 胜者自身明确携带 Mode 时覆盖 Session 的 NONE。
        inst = make_instance()
        session = session_obs(Status.WORKING, mode=Mode.NONE)
        terminal = terminal_obs(Status.WAITING, mode=Mode.PLAN,
                                mode_raw="plan")
        snap = reduce_state(inst, session, terminal, None, 1000.0)
        self.assertEqual(snap.status, Status.WAITING)
        self.assertEqual(snap.mode, Mode.PLAN)
        self.assertEqual(snap.mode_raw, "plan")

    def test_generic_activity_keeps_session_mode(self):
        # 泛化终端活动胜出（无会话状态时）也不伪造/清除 Mode
        inst = make_instance()
        session = session_obs(Status.WORKING, turn_active=False,
                              expires_at=990.0, ts=980.0, mode=Mode.PLAN,
                              confidence=Confidence.MEDIUM)
        terminal = terminal_obs(Status.WORKING, phase=Phase.NONE,
                                confidence=Confidence.MEDIUM, summary="终端活动",
                                expires_at=1008.0)
        snap = reduce_state(inst, session, terminal, None, 1000.0)
        self.assertEqual(snap.mode, Mode.PLAN)


if __name__ == "__main__":
    unittest.main()
