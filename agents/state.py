"""V3 StateReducer：会话观察 + 终端观察 → 最终 Snapshot（plan.md §26-§28）。

优先级严格为：
    ERROR > WAITING > INPUT > WORKING > DONE > IDLE > UNKNOWN

关键不变量：
  * 静默永远不能推断 WAITING（WAITING 只能来自 wire ApprovalRequest
    或终端当前可见审批 UI，两处都不会在这里凭空生成）。
  * 已知 active turn → 无限保持 WORKING，直到显式完成事件。
  * 只有活动证据（无 turn 生命周期）→ 宽限 8~10s 后回到 UNKNOWN，
    不伪造 WORKING 也不伪造 IDLE。
"""
from .models import (
    AgentInstance,
    Confidence,
    EvidenceSource,
    Observation,
    Phase,
    Snapshot,
    Status,
)

# plan.md §26 状态优先级（数值越小越优先）
_STATUS_PRIORITY = {
    Status.ERROR: 0,
    Status.WAITING: 1,
    Status.INPUT: 2,
    Status.WORKING: 3,
    Status.DONE: 4,
    Status.IDLE: 5,
    Status.UNKNOWN: 6,
}

_CONFIDENCE_RANK = {
    Confidence.EXACT: 3,
    Confidence.HIGH: 2,
    Confidence.MEDIUM: 1,
    Confidence.UNKNOWN: 0,
}


def _live(observation: Observation | None, now: float) -> Observation | None:
    if observation is None:
        return None
    if observation.status is None:
        return None
    if not observation.live(now):
        return None
    return observation


def _pick(session: Observation | None, terminal: Observation | None,
          now: float) -> Observation | None:
    """选出来源观察：状态优先级 → 置信度 → 会话（结构化）优先。"""
    candidates = [o for o in (_live(session, now), _live(terminal, now)) if o]
    if not candidates:
        return None

    def rank(obs: Observation):
        source_bias = 0 if obs.source == EvidenceSource.SESSION else 1
        return (_STATUS_PRIORITY.get(obs.status, 9),
                -_CONFIDENCE_RANK.get(obs.confidence, 0),
                source_bias)

    return sorted(candidates, key=rank)[0]


def reduce_state(instance: AgentInstance,
                 session: Observation | None,
                 terminal: Observation | None,
                 previous: Snapshot | None,
                 now: float,
                 policy: str = "") -> Snapshot:
    """融合单个 Agent 的观察证据，产出 UI 快照。"""
    snap = Snapshot(
        key=instance.key, kind=instance.kind, source=instance.source,
        pid=instance.pid, ts=now,
        cwd=instance.cwd or (session.cwd if session else ""),
        session_id=(session.session_id if session else "") or instance.session_id,
        session_bound=bool(session and session.session_bound),
        policy=policy,
    )
    if session is not None:
        snap.session_file = session.session_file
        snap.freshness = session.timestamp
        if session.goal:
            snap.goal = session.goal
        if session.summary:
            snap.summary = session.summary
        if session.title:
            snap.title = session.title

    winner = _pick(session, terminal, now)

    if winner is None:
        # 没有任何存活证据：进程存活但状态未知（绝不伪装 IDLE）。
        # 活动型证据刚过期时按 plan §28 回到 UNKNOWN。
        snap.status = Status.UNKNOWN
        snap.evidence = EvidenceSource.PROCESS
        snap.confidence = Confidence.UNKNOWN
        snap.summary = snap.summary or "进程存活，等待会话证据"
        return snap

    snap.status = winner.status or Status.UNKNOWN
    snap.evidence = winner.source
    snap.confidence = winner.confidence
    if winner.mode:
        snap.mode = winner.mode

    # Phase：优先采用胜出观察自己的 phase（如 WAITING→APPROVAL）；
    # 胜出是终端活动证据（无 phase）时保留会话结构化 phase，
    # 不伪造具体编码/测试阶段（plan §12 Claude 融合规则）。
    if winner.phase and winner.phase is not Phase.NONE:
        snap.phase = winner.phase
    elif session is not None and session.phase and session.phase is not Phase.NONE:
        if snap.status == Status.WORKING and winner.source == EvidenceSource.TERMINAL:
            snap.phase = session.phase
            snap.evidence = EvidenceSource.FUSED
        elif snap.status == Status.WORKING:
            snap.phase = session.phase

    if snap.status == Status.WAITING:
        snap.phase = Phase.APPROVAL
        detail = (winner.summary or "").strip()
        if winner.source == EvidenceSource.TERMINAL:
            snap.summary = "终端显示审批请求"
        snap.waiting_detail = detail or snap.summary or "等待审批"
    elif snap.status == Status.INPUT:
        snap.phase = Phase.USER_INPUT
        snap.waiting_detail = snap.summary or "等待输入"

    # 终端活动 + 会话证据并存 → 融合来源
    if (terminal is not None and session is not None
            and terminal.source == EvidenceSource.TERMINAL
            and terminal.live(now) and terminal.status == Status.WORKING
            and winner is session):
        snap.evidence = EvidenceSource.FUSED

    if snap.status == Status.WORKING and not snap.summary:
        snap.summary = "终端活动" if winner.source == EvidenceSource.TERMINAL else "处理中"
    return snap
