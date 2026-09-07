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


def mode_text(snap) -> str:
    mode = getattr(snap, "mode", Mode.NONE)
    if isinstance(mode, Mode):
        return MODE_LABELS.get(mode, mode.value)
    return str(mode or "")
