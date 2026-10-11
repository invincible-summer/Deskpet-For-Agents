"""Win32 窗口原语：唤起/验证/闪炼，绝不发送输入（v4plan §5.7）。

只保留公共 Win32 的窗口能力：
  enum_windows / window_identity / validate_window /
  restore_window / try_set_foreground / flash_window

Tab/Pane 的 UIA 逻辑不放在这里（属于 agents/terminal_service）。
键盘注入 / 剪贴板路径不在产品中（V3 起移除，V4.1 不回退）。

Win32 契约（Microsoft Learn）：
  * GetWindowThreadProcessId 失败/无效 HWND 返回 0，输出变量保持不变
    ——返回 0 时读到的 PID 不是可信验证结果；
  * SetForegroundWindow 可能被 OS 拒绝（foreground policy），拒绝时
    用 FlashWindowEx 提醒，不绕过；
  * GetClassNameW 失败返回 0。
"""
import ctypes
import ctypes.wintypes as wt
import os

from agents.models import WindowIdentity

user32 = ctypes.windll.user32 if os.name == 'nt' else None
if user32:
    user32.GetForegroundWindow.restype = wt.HWND
    user32.IsWindow.argtypes = [wt.HWND]
    user32.IsWindow.restype = wt.BOOL
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindowVisible.restype = wt.BOOL
    user32.IsIconic.argtypes = [wt.HWND]
    user32.IsIconic.restype = wt.BOOL
    user32.ShowWindowAsync.argtypes = [wt.HWND, ctypes.c_int]
    user32.ShowWindowAsync.restype = wt.BOOL
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.SetForegroundWindow.restype = wt.BOOL
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
    user32.GetWindow.restype = wt.HWND


def enum_windows(*, include_hidden=False):
    if not user32:
        return []
    out = []
    callback = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    @callback
    def visit(hwnd, _):
        if include_hidden or user32.IsWindowVisible(hwnd):
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            n = user32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(n + 1)
            cls = ctypes.create_unicode_buffer(128)
            user32.GetWindowTextW(hwnd, title, n + 1)
            user32.GetClassNameW(hwnd, cls, 128)
            if title.value:
                out.append((int(hwnd), pid.value, title.value, cls.value))
        return True

    user32.EnumWindows(visit, 0)
    return out


def is_app_window(hwnd: int) -> bool:
    """Exclude owned/tool/child windows, including hidden Electron helpers."""
    if not user32 or not user32.IsWindow(hwnd):
        return False
    style = user32.GetWindowLongW(hwnd, -16)
    exstyle = user32.GetWindowLongW(hwnd, -20)
    return not (style & 0x40000000 or exstyle & 0x80
                or user32.GetWindow(hwnd, 4))  # WS_CHILD / TOOLWINDOW / OWNER


def _ancestor_pids(pid, depth=12):
    import psutil
    pids = {pid}
    try:
        cur = psutil.Process(pid)
        for _ in range(depth):
            cur = cur.parent()
            if cur is None:
                break
            pids.add(cur.pid)
    except (psutil.Error, OSError):
        pass
    return pids


def window_identity(hwnd: int) -> WindowIdentity | None:
    """hwnd → 完整 WindowIdentity（pid + process create_time + class）。

    create_time 来自 psutil（进程创建时刻）：HWND 与 PID 都可能被系统
    复用，create_time 组成 process incarnation，让旧绑定无法继承。
    任何一步读取失败返回 None（fail-closed）。
    """
    if not user32 or not user32.IsWindow(int(hwnd)):
        return None
    pid = wt.DWORD(0)
    thread_id = user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    if not thread_id or pid.value <= 0:
        return None
    buf = ctypes.create_unicode_buffer(128)
    length = user32.GetClassNameW(int(hwnd), buf, len(buf))
    if length <= 0:
        return None
    created = 0.0
    try:
        import psutil
        created = float(psutil.Process(int(pid.value)).create_time())
    except Exception:
        return None   # 进程已死或不可读：无法建立 incarnation 身份
    return WindowIdentity(hwnd=int(hwnd), pid=int(pid.value),
                          process_created=created, window_class=buf.value)


def validate_window(identity: WindowIdentity) -> bool:
    """验证目标仍是发现时的那个窗口：IsWindow + 属主 PID + create_time + class。

    期望值缺失即失败（没有期望就无法证明身份），包括
    process_created<=0——无法防 PID 复用的身份一律拒绝。绝不猜测。
    """
    if not user32:
        return False
    hwnd = int(getattr(identity, "hwnd", 0) or 0)
    if not hwnd:
        return False
    if not user32.IsWindow(hwnd):
        return False
    expected_pid = int(getattr(identity, "pid", 0) or 0)
    if expected_pid <= 0:
        return False
    expected_created = float(getattr(identity, "process_created", 0.0) or 0.0)
    if expected_created <= 0:
        return False   # fail-closed（v4.1.3 §9.1）：0 值不再穿透
    expected_class = str(getattr(identity, "window_class", "") or "")
    if not expected_class:
        return False
    pid = wt.DWORD(0)
    thread_id = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not thread_id or pid.value <= 0 or pid.value != expected_pid:
        return False
    # PID 一致仍可能是复用（新进程占了同 PID）：create_time 必须相同
    try:
        import psutil
        created = float(psutil.Process(int(pid.value)).create_time())
    except Exception:
        return False
    if abs(created - expected_created) > 0.5:
        return False
    buf = ctypes.create_unicode_buffer(128)
    length = user32.GetClassNameW(hwnd, buf, len(buf))
    if length <= 0 or buf.value != expected_class:
        return False
    return True


def restore_window(hwnd: int) -> bool:
    """异步恢复最小化窗口，避免失去响应的终端堵塞 Tk 主线程。"""
    if not user32 or not user32.IsWindow(int(hwnd)):
        return False
    if user32.IsIconic(int(hwnd)):
        return bool(user32.ShowWindowAsync(int(hwnd), 9))   # SW_RESTORE
    if not user32.IsWindowVisible(int(hwnd)):
        return bool(user32.ShowWindowAsync(int(hwnd), 5))   # SW_SHOW (tray hidden)
    return True


def try_set_foreground(hwnd: int) -> bool:
    """请求前台；True 仅当 GetForegroundWindow()==hwnd。

    OS foreground policy 可能拒绝（Microsoft Learn）——那是系统决定，
    DeskPet 不绕过，由调用方 Flash 提醒并返回 FOREGROUND_DENIED。
    """
    if not user32 or not user32.IsWindow(int(hwnd)):
        return False
    user32.SetForegroundWindow(int(hwnd))
    return int(user32.GetForegroundWindow()) == int(hwnd)


class FLASHWINFO(ctypes.Structure):
    _fields_ = [('cbSize', wt.UINT), ('hwnd', wt.HWND), ('dwFlags', wt.DWORD),
                ('dwCount', wt.UINT), ('dwTimeout', wt.DWORD)]


def flash_window(hwnd: int) -> bool:
    """任务栏闪烁提醒（foreground 被拒时的通知通道）。"""
    if not user32 or not user32.IsWindow(int(hwnd)):
        return False
    info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), int(hwnd), 2, 3, 0)
    return bool(user32.FlashWindowEx(ctypes.byref(info)))


# ------------------------------------------------------------ Z-order（v4.1.3 §10）

HWND_TOP = 0
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

if user32:
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wt.UINT]
    user32.SetWindowPos.restype = wt.BOOL


def reassert_window_z_order(hwnd: int, *, topmost: bool) -> bool:
    """DeskPet 自身 Pet Toplevel 的 Z-order 重声明（SWP_NOACTIVATE）。

    SetWindowPos 只改变层级不激活窗口（Microsoft Learn）——用于
    Dashboard 打开/托盘恢复后把桌宠拉回预期层级；Terminal 的用户
    显式唤起仍走 try_set_foreground（SetForegroundWindow），两者分开。
    """
    if not user32 or not user32.IsWindow(int(hwnd)):
        return False
    insert_after = HWND_TOPMOST if topmost else HWND_TOP
    flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW
    return bool(user32.SetWindowPos(int(hwnd), insert_after,
                                    0, 0, 0, 0, flags))


# ------------------------------------------------------------ 多显示器 / DPI（v4plan §12）

MONITOR_DEFAULTTONEAREST = 2


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ('cbSize', wt.DWORD), ('rcMonitor', wt.RECT), ('rcWork', wt.RECT),
        ('dwFlags', wt.DWORD), ('szDevice', wt.WCHAR * 32),
    ]


if user32:
    user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
    user32.MonitorFromPoint.restype = wt.HMONITOR
    user32.GetMonitorInfoW.argtypes = [wt.HMONITOR,
                                       ctypes.POINTER(_MONITORINFO)]
    user32.GetMonitorInfoW.restype = wt.BOOL
    user32.GetDpiForWindow.argtypes = [wt.HWND]
    user32.GetDpiForWindow.restype = wt.UINT


def monitor_work_area(x: int, y: int):
    """屏幕点 → (monitor 设备名, work_area 矩形 (l,t,r,b))。

    设备名形如 "\\\\.\\DISPLAY1"，作为 Placement.monitor 的持久身份；
    找不到返回 (None, None)。
    """
    if not user32:
        return None, None
    pt = wt.POINT(int(x), int(y))
    hmon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    if not hmon:
        return None, None
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        return None, None
    work = info.rcWork
    return info.szDevice, (work.left, work.top, work.right, work.bottom)


def dpi_for_window(hwnd: int) -> int:
    """窗口所在 monitor 的 DPI（96 基准）；失败返回 96。"""
    if not user32 or not user32.IsWindow(int(hwnd)):
        return 96
    try:
        value = int(user32.GetDpiForWindow(int(hwnd)))
        return value if value > 0 else 96
    except Exception:
        return 96
