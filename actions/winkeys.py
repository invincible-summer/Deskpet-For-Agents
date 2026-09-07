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
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsIconic.argtypes = [wt.HWND]
    user32.ShowWindow.argtypes = [wt.HWND,ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND,ctypes.POINTER(wt.DWORD)]
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextW.argtypes = [wt.HWND,wt.LPWSTR,ctypes.c_int]
    user32.GetClassNameW.argtypes = [wt.HWND,wt.LPWSTR,ctypes.c_int]


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


def raise_terminal(binding) -> bool:
    """唤起 TerminalBinding 指向的终端窗口（plan §43 的公共 API 路径）。"""
    hwnd = int(getattr(binding, "hwnd", 0) or 0)
    if not hwnd:
        return False
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
