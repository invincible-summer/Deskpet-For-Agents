"""UI 本地化标签：Status / Phase / Mode → 中文显示（plan.md §4：UI 再本地化）。"""
from agents.models import Mode, Phase, Status

STATUS_LABELS = {
    Status.IDLE: "待命", Status.WORKING: "工作中", Status.WAITING: "等待审批",
    Status.INPUT: "等待你的回复", Status.DONE: "已完成", Status.ERROR: "需要留意",
    Status.UNKNOWN: "状态暂不可读",
}
PHASE_LABELS = {
    Phase.THINKING: "思考中", Phase.PLANNING: "计划中", Phase.READING: "阅读中",
    Phase.CODING: "编码中", Phase.EXECUTING: "执行中", Phase.TESTING: "测试中",
    Phase.ANSWERING: "回答中", Phase.APPROVAL: "等待审批",
    Phase.USER_INPUT: "等待输入",
}
MODE_LABELS = {
    Mode.NONE: "", Mode.DEFAULT: "Default", Mode.PLAN: "Plan",
    Mode.ACCEPT_EDITS: "Accept Edits", Mode.AUTO: "Auto",
    Mode.DONT_ASK: "Don't Ask", Mode.BYPASS: "Bypass",
    Mode.UNKNOWN: "Unknown",
}


def status_text(snap) -> str:
    return STATUS_LABELS.get(snap.status, str(snap.status.value))


def phase_text(snap) -> str:
    if snap.status == Status.WAITING:
        return PHASE_LABELS[Phase.APPROVAL]
    if snap.status == Status.INPUT:
        return PHASE_LABELS[Phase.USER_INPUT]
    if snap.status == Status.WORKING:
        return PHASE_LABELS.get(snap.phase, "处理中")
    return ""


def environment_label(inst) -> str:
    """来源只读标签（plan2 §11）：Codex · Desktop / Codex · WSL / ZCode · Desktop。

    仅展示用；selector 持久化绝不写 runtime session id。
    """
    from agents.models import AgentSurface
    kind = getattr(inst, "kind", None)
    base = kind.label if kind is not None else ""
    if getattr(inst, "surface", AgentSurface.TERMINAL) is AgentSurface.DESKTOP:
        return f"{base} · Desktop".strip(" ·")
    distro = getattr(inst, "distro", "") or ""
    if distro:
        return f"{base} · WSL {distro}"
    return base


def mode_text(snap) -> str:
    mode = getattr(snap, "mode", Mode.NONE)
    if isinstance(mode, Mode):
        return MODE_LABELS.get(mode, mode.value)
    return str(mode or "")
