"""Terminal window activation through public Win32 APIs; never sends input.

V3（plan.md §43/§51）：只保留公共 Win32 的窗口唤起能力；
SendInput / 键盘注入 / 剪贴板路径已从产品中彻底移除。
"""
import ctypes
import ctypes.wintypes as wt
import os

user32 = ctypes.windll.user32 if os.name == 'nt' else None
if user32:
    user32.GetForegroundWindow.restype = wt.HWND
    user32.IsWindow.argtypes = [wt.HWND]
    user32.IsWindow.restype = wt.BOOL
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsIconic.argtypes = [wt.HWND]
    user32.ShowWindow.argtypes = [wt.HWND,ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextW.argtypes = [wt.HWND,wt.LPWSTR,ctypes.c_int]
    user32.GetClassNameW.argtypes = [wt.HWND,wt.LPWSTR,ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int


def enum_windows():
    if not user32: return []
    out=[]
    callback=ctypes.WINFUNCTYPE(wt.BOOL,wt.HWND,wt.LPARAM)
    @callback
    def visit(hwnd,_):
        if user32.IsWindowVisible(hwnd):
            pid=wt.DWORD(); user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
            n=user32.GetWindowTextLengthW(hwnd)
            title=ctypes.create_unicode_buffer(n+1); cls=ctypes.create_unicode_buffer(128)
            user32.GetWindowTextW(hwnd,title,n+1); user32.GetClassNameW(hwnd,cls,128)
            if title.value: out.append((int(hwnd),pid.value,title.value,cls.value))
        return True
    user32.EnumWindows(visit,0)
    return out


def _ancestor_pids(pid,depth=12):
    import psutil
    pids={pid}
    try:
        cur=psutil.Process(pid)
        for _ in range(depth):
            cur=cur.parent()
            if cur is None: break
            pids.add(cur.pid)
    except (psutil.Error,OSError): pass
    return pids


def validate_terminal_window(binding) -> bool:
    """唤起前的 HWND 复用验证：IsWindow + 属主 PID + 窗口类三者一致。

    Windows 会复用 HWND；终端已关闭而 resolver 尚未更新时，旧 HWND
    可能指向别的窗口。本函数的职责是证明"目标仍是发现阶段记录的那个
    终端窗口"——任何一步无法完成验证即失败（fail-closed），绝不
    SendInput/PostMessage，也绝不在 action 层做第二套 heuristic 推测。

    Win32 契约（Microsoft Learn）：
      * GetWindowThreadProcessId 失败/无效 HWND 返回 0，且输出变量
        保持不变——返回 0 时读到的 PID 不是可信验证结果；
      * GetClassNameW 失败返回 0。
    因此：thread_id==0、actual_pid<=0（0 是"未完成验证"不是"匹配"）、
    GetClassNameW 返回 <=0、期望 PID/class 缺失，全部拒绝。
    """
    if not user32:
        return False
    hwnd = int(getattr(binding, "hwnd", 0) or 0)
    if not hwnd:
        return False
    if not user32.IsWindow(hwnd):
        return False
    expected_pid = int(getattr(binding, "window_pid", 0) or 0)
    if expected_pid <= 0:
        return False    # 没有期望属主就无法完成身份验证
    expected_class = str(getattr(binding, "window_class", "") or "")
    if not expected_class:
        return False
    pid = wt.DWORD(0)
    thread_id = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not thread_id:
        return False
    if pid.value <= 0:
        return False
    if pid.value != expected_pid:
        return False
    buf = ctypes.create_unicode_buffer(128)
    length = user32.GetClassNameW(hwnd, buf, len(buf))
    if length <= 0:
        return False
    if buf.value != expected_class:
        return False
    return True


def raise_terminal(binding) -> bool:
    """唤起 TerminalBinding 指向的终端窗口（plan §43 的公共 API 路径）。"""
    if not validate_terminal_window(binding):
        return False
    hwnd = int(getattr(binding, "hwnd", 0) or 0)
    return raise_window(hwnd)


class FLASHWINFO(ctypes.Structure):
    _fields_=[('cbSize',wt.UINT),('hwnd',wt.HWND),('dwFlags',wt.DWORD),('dwCount',wt.UINT),('dwTimeout',wt.DWORD)]


def raise_window(hwnd):
    if not user32 or not user32.IsWindow(hwnd): return False
    if user32.IsIconic(hwnd): user32.ShowWindow(hwnd,9)
    user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow()==hwnd: return True
    info=FLASHWINFO(ctypes.sizeof(FLASHWINFO),hwnd,2,3,0)
    user32.FlashWindowEx(ctypes.byref(info))
    return False
