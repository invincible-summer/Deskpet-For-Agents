"""V3 StateReducer：会话观察 + 终端观察 → 最终 Snapshot（plan.md §26-§28）。

语义级证据的优先级严格为：
    ERROR > WAITING > INPUT > WORKING > DONE > IDLE > UNKNOWN

但必须区分两类终端证据的强弱（V3.1）：
  * semantic terminal evidence：审批识别器命中（WAITING，带 agent_kind），
    与 Session 同权参与上述优先级；
  * generic activity evidence：pane 最近有文本变化（WORKING/MEDIUM，
    agent_kind=None）。它只说明"这个 pane 在动"——可能是 final answer
    绘制、shell prompt 回来、用户自己敲命令——绝不能推翻结构化的
    turn completion。因此 Session 存在有效状态时 generic activity 只作
    融合来源标记，Session 无有效状态时才作为 WORKING fallback。

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


def _is_generic_activity(obs: Observation) -> bool:
    """泛化终端活动：pane 有文本变化，但没有任何审批语义。"""
    return (obs.source == EvidenceSource.TERMINAL
            and obs.status == Status.WORKING
            and obs.agent_kind is None)


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
        if session.mode_raw:
            snap.mode_raw = session.mode_raw

    session_live = _live(session, now)
    terminal_live = _live(terminal, now)

    # 证据强弱：generic terminal activity 不能覆盖结构化 Session 状态
    #（DONE/IDLE/ERROR/INPUT/WORKING 都算结构化状态）。
    if (terminal_live is not None and _is_generic_activity(terminal_live)
            and session_live is not None):
        winner = session_live
    else:
        winner = _pick(session_live, terminal_live, now)

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

    # 终端活动 + 会话证据并存 → 融合来源（仅限会话 WORKING；
    # DONE/IDLE 的展示不因终端还在滚动而被改写）
    if (terminal_live is not None and session_live is not None
            and _is_generic_activity(terminal_live)
            and winner is session_live
            and session_live.status == Status.WORKING):
        snap.evidence = EvidenceSource.FUSED

    if snap.status == Status.WORKING and not snap.summary:
        snap.summary = "终端活动" if winner.source == EvidenceSource.TERMINAL else "处理中"
    return snap
