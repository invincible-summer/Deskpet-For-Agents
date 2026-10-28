"""UI 本地化标签：Status / Phase / Mode → 中文显示（UI 再本地化）。"""
from agents.models import Mode, Phase, Status

STATUS_LABELS = {
    Status.IDLE: "待命", Status.WORKING: "工作中", Status.WAITING: "等待审批",
    Status.INPUT: "等待你的回复", Status.DONE: "已完成", Status.ERROR: "需要留意",
    Status.UNKNOWN: "状态暂不可读",
}
PHASE_LABELS = {
    Phase.THINKING: "思考中", Phase.PLANNING: "计划中", Phase.READING: "阅读中",
    Phase.SEARCHING: "搜索中",
    Phase.CODING: "编写中", Phase.EXECUTING: "执行中", Phase.TESTING: "测试中",
    Phase.ANSWERING: "回答中", Phase.APPROVAL: "等待审批",
    Phase.USER_INPUT: "等待输入",
}
MODE_LABELS = {
    Mode.NONE: "", Mode.DEFAULT: "Default", Mode.PLAN: "Plan", Mode.GOAL: "Goal",
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
    distro = getattr(inst, "distro", "") or ""
    if getattr(inst, "surface", AgentSurface.TERMINAL) is AgentSurface.DESKTOP:
        if distro:
            return f"{base} · Desktop · WSL {distro}".strip(" ·")
        return f"{base} · Desktop".strip(" ·")
    if distro:
        return f"{base} · WSL {distro}"
    return base


def mode_text(snap) -> str:
    mode = getattr(snap, "mode", Mode.NONE)
    if isinstance(mode, Mode):
        return MODE_LABELS.get(mode, mode.value)
    return str(mode or "")


def workspace_name(target) -> str:
    """Use observed metadata only; Windows and WSL paths need no filesystem I/O."""
    from agents.summarize import fmt_command
    cwd = target.snapshot.cwd or target.instance.cwd or ""
    parts = str(cwd).replace("\\", "/").rstrip("/").split("/")
    return fmt_command(parts[-1], 60) if parts[-1] else "工作区未提供"


def activity_text(snap, detail=True, limit=120):
    """Format the observed action, never substitute the user's task or mode."""
    from agents.summarize import activity_excerpt, summarize_text
    label = status_text(snap)
    if snap.status == Status.INPUT and "等待选择回复" in (snap.summary or ""):
        label = "等待选择回复"
    elif snap.status == Status.IDLE:
        label = "等待新任务"
    elif snap.status == Status.WORKING:
        label = phase_text(snap) or label
    if not detail:
        return activity_excerpt(label, limit)
    body = (snap.waiting_detail or snap.summary) if snap.status in (Status.WAITING, Status.INPUT) else snap.summary
    body = (activity_excerpt(body, limit) if snap.status is Status.WORKING
            and snap.phase not in (Phase.THINKING, Phase.PLANNING, Phase.ANSWERING)
            else summarize_text(body, limit))
    # Older logs may already carry a presentation category. Strip only a
    # recognized category prefix, keeping the command/path/thought itself.
    prefixes = ("执行中", "执行", "测试中", "测试", "搜索中", "搜索",
                "阅读中", "读取", "编写中", "编码中", "修改", "写入",
                "查询", "思考中", "计划中", "回答中", "等待审批",
                "等待你的回复", "等待选择回复", "等待新任务", "工作中", "处理中")
    for category in prefixes:
        if body == category:
            body = ""
            break
        if body.startswith((category + "：", category + ":")):
            body = body[len(category) + 1:].strip()
            break
    if body.casefold() in {"bash", "shell", "exec_command", "read", "edit",
                            "write", "grep", "glob", "task", "run",
                            "执行", "修改", "读取", "搜索", "处理中"}:
        body = ""
    if not body:
        return activity_excerpt(label, limit)
    prefix = label + "："
    if len(prefix) >= limit:
        return activity_excerpt(label, limit)
    return prefix + activity_excerpt(body, limit - len(prefix))
