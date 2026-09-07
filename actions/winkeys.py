"""批复通道：定位 Agent 所在的终端窗口 → 前置 → 发送批准/拒绝按键。

只用公开 Win32 API（SetForegroundWindow / SendInput），不注入目标进程，
不使用 hooks；WSL 里的 Agent 复用其宿主 Windows Terminal 窗口。
"""
import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.windll.user32

VK_MAP = {
    "return": 0x0D, "enter": 0x0D, "escape": 0x1B, "esc": 0x1B,
    "space": 0x20, "tab": 0x09, "backspace": 0x08, "delete": 0x2E,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
}

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    # 必须包含最大的成员 MOUSEINPUT，否则 x64 上 INPUT 尺寸不对，
    # SendInput 会因 cbSize 不匹配而静默失败
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _INPUTUNION)]


def _send_inputs(seq: list[INPUT]):
    arr = (INPUT * len(seq))(*seq)
    sent = user32.SendInput(len(arr), arr, ctypes.sizeof(INPUT))
    if sent != len(arr):
        err = ctypes.get_last_error() or ctypes.windll.kernel32.GetLastError()
        raise OSError(f"SendInput 失败: {sent}/{len(arr)}, GetLastError={err}")


def _press_vk(vk: int):
    down = INPUT(type=INPUT_KEYBOARD)
    down.union.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=0, time=0, dwExtraInfo=None)
    up = INPUT(type=INPUT_KEYBOARD)
    up.union.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0,
                             dwExtraInfo=None)
    _send_inputs([down, up])


def _press_char(ch: str):
    down = INPUT(type=INPUT_KEYBOARD)
    down.union.ki = KEYBDINPUT(wVk=0, wScan=ord(ch), dwFlags=KEYEVENTF_UNICODE,
                               time=0, dwExtraInfo=None)
    up = INPUT(type=INPUT_KEYBOARD)
    up.union.ki = KEYBDINPUT(wVk=0, wScan=ord(ch),
                             dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                             time=0, dwExtraInfo=None)
    _send_inputs([down, up])


def send_key(key: str):
    k = key.strip()
    if not k:
        return
    vk = VK_MAP.get(k.lower())
    if vk is not None:
        _press_vk(vk)
    elif len(k) == 1:
        _press_char(k)
    else:
        for ch in k:
            _press_char(ch)


# ---- 窗口枚举 ----
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def enum_windows() -> list[tuple[int, int, str, str]]:
    """(hwnd, pid, title, class)，仅可见窗口。"""
    out: list[tuple[int, int, str, str]] = []

    @EnumWindowsProc
    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        out.append((hwnd, pid.value, buf.value, cls.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def _ancestor_pids(pid: int, depth: int = 8) -> set[int]:
    import psutil
    pids = {pid}
    try:
        cur = psutil.Process(pid)
        for _ in range(depth):
            cur = cur.parent()
            if cur is None:
                break
            pids.add(cur.pid)
    except Exception:
        pass
    return pids


def find_terminal_window(instance_key: str, pid: int | None, source: str,
                         hints: list[str], saved_title: str | None) -> int | None:
    """返回目标终端窗口 hwnd。hints: cwd 目录名 / agent 名等标题提示。"""
    wins = enum_windows()
    # 0) 用户手动绑定过的窗口标题
    if saved_title:
        for hwnd, _pid, title, _cls in wins:
            if title == saved_title:
                return hwnd
    # 1) Windows 原生进程：沿父链找拥有窗口的终端
    if source == "windows" and pid:
        pids = _ancestor_pids(pid)
        for hwnd, wpid, title, _cls in wins:
            if wpid in pids and title.strip():
                return hwnd
    # 2) 标题提示匹配（WSL 场景主路径）
    lowered = [(hwnd, (title or "").lower()) for hwnd, _p, title, _c in wins]
    for hint in hints:
        h = hint.lower().strip()
        if len(h) < 3:
            continue
        for hwnd, title in lowered:
            if h in title:
                return hwnd
    return None


def foreground_window_title() -> str | None:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value or None


def _unlock_foreground():
    """注入一次 ALT 按下/抬起，解除 Windows 前台锁（SetForegroundWindow 的公开替代做法）。"""
    alt_down = INPUT(type=INPUT_KEYBOARD)
    alt_down.union.ki = KEYBDINPUT(wVk=0x12, wScan=0, dwFlags=0, time=0,
                                   dwExtraInfo=None)
    alt_up = INPUT(type=INPUT_KEYBOARD)
    alt_up.union.ki = KEYBDINPUT(wVk=0x12, wScan=0, dwFlags=KEYEVENTF_KEYUP,
                                 time=0, dwExtraInfo=None)
    _send_inputs([alt_down, alt_up])


def _force_foreground(hwnd: int, tries: int = 3) -> bool:
    """把 hwnd 变为前台窗口：ALT 解锁 → AttachThreadInput → SwitchToThisWindow
    多手段递进并重试，全部失败才返回 False。"""
    for attempt in range(tries):
        if user32.GetForegroundWindow() == hwnd:
            return True
        _unlock_foreground()
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.08)
        if user32.GetForegroundWindow() == hwnd:
            return True
        fg = user32.GetForegroundWindow()
        if fg:
            t_fg = user32.GetWindowThreadProcessId(fg, None)
            t_me = kernel32.GetCurrentThreadId()
            if t_fg and t_fg != t_me:
                user32.AttachThreadInput(t_me, t_fg, True)
                try:
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                finally:
                    user32.AttachThreadInput(t_me, t_fg, False)
        time.sleep(0.08)
        if user32.GetForegroundWindow() == hwnd:
            return True
        # 兜底：系统内置切换函数（专为“切到该窗口”设计，一般不受前台锁限制）
        user32.SwitchToThisWindow(hwnd, True)
        time.sleep(0.15)
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.2 * (attempt + 1))
    return user32.GetForegroundWindow() == hwnd


def focus_and_send(hwnd: int, keys: list[str], restore_focus: bool = False) -> bool:
    """把窗口带到前台并发送按键序列；可选发送后把焦点还给原先的窗口。
    前置失败时不发送任何按键（避免误触别的窗口）。"""
    try:
        prev = user32.GetForegroundWindow()
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        if not _force_foreground(hwnd):
            return False
        for k in keys:
            send_key(k)
            time.sleep(0.06)
        if restore_focus and prev and prev != hwnd:
            time.sleep(0.15)
            _force_foreground(prev)
        return True
    except Exception:
        return False


def raise_window(hwnd: int) -> bool:
    """把窗口强制置顶到最上层（TOPMOST→NOTOPMOST 技巧）并给焦点。"""
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
        SWP = 0x0001 | 0x0002 | 0x0040  # NOSIZE | NOMOVE | SHOWWINDOW
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, SWP)   # HWND_TOPMOST
        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, SWP)   # HWND_NOTOPMOST（强制提到最上）
        return _force_foreground(hwnd)
    except Exception:
        return False
