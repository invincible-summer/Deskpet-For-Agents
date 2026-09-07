"""批复动作：气泡按钮 → 定位终端窗口 → 发键。"""
from agents.models import AgentKind, Snapshot
from . import winkeys

# 各 Agent 默认批复键（可被 config.keys 覆盖）
DEFAULT_KEYS = {
    AgentKind.CLAUDE: {"approve": ["Return"], "deny": ["Escape"]},
    AgentKind.CODEX:  {"approve": ["y"],      "deny": ["Escape"]},
    AgentKind.KIMI:   {"approve": ["Return"], "deny": ["Escape"]},
    AgentKind.PI:     {"approve": ["Return"], "deny": ["Escape"]},
}


def keys_for(config, kind: AgentKind, action: str) -> list[str]:
    raw = config.get(f"keys.{kind.value}.{action}") or DEFAULT_KEYS[kind][action]
    return [s.strip() for s in str(raw).split(",") if s.strip()]


def window_hints(snapshot: Snapshot) -> list[str]:
    hints = [snapshot.kind.label]
    sf = snapshot.session_file or ""
    for sep in ("/", "\\"):
        if sep in sf:
            hints.append(sf.split(sep)[-2])
            break
    if snapshot.title and len(snapshot.title) >= 4:
        hints.append(snapshot.title[:20])
    return hints


def send_approval(config, snapshot: Snapshot, action: str,
                  saved_title: str | None, restore_focus: bool | None = None) -> tuple[bool, str]:
    """action: approve / deny。返回 (成功?, 描述)。
    restore_focus: 发送后把焦点还给原先窗口（默认读 config.approve_restore_focus）。"""
    keys = keys_for(config, snapshot.kind, action)
    hwnd = winkeys.find_terminal_window(
        snapshot.key, snapshot.pid, snapshot.source,
        window_hints(snapshot), saved_title)
    if not hwnd:
        return False, f"未找到 {snapshot.kind.label} 的终端窗口，请在仪表盘手动绑定"
    if restore_focus is None:
        restore_focus = bool(config.get("approve_restore_focus", True))
    ok = winkeys.focus_and_send(hwnd, keys, restore_focus=restore_focus)
    label = "+".join(keys)
    if ok:
        return True, f"已发送 {label} → {snapshot.kind.label}"
    return False, "按键发送失败"


def raise_terminal(config, snapshot: Snapshot, saved_title: str | None) -> tuple[bool, str]:
    """把 Agent 所在终端窗口强制置顶并给焦点。"""
    hwnd = winkeys.find_terminal_window(
        snapshot.key, snapshot.pid, snapshot.source,
        window_hints(snapshot), saved_title)
    if not hwnd:
        return False, f"未找到 {snapshot.kind.label} 的终端窗口，请在仪表盘手动绑定"
    ok = winkeys.raise_window(hwnd)
    return (ok, f"已唤起 {snapshot.kind.label} 的终端" if ok else "唤起终端失败")
