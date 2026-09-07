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

    def test_working_beats_done(self):
        inst = make_instance()
        snap = reduce_state(inst, session_obs(Status.DONE),
                            terminal_obs(Status.WORKING, phase=Phase.NONE,
                                         expires_at=1008.0), None, 1000.0)
        self.assertEqual(snap.status, Status.WORKING)

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


if __name__ == "__main__":
    unittest.main()
