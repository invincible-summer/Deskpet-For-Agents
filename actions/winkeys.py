"""Terminal window activation through public Win32 APIs; never sends input."""
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


def window_identity(hwnd):
    import psutil
    for h,pid,title,cls in enum_windows():
        if h==hwnd:
            try: created=psutil.Process(pid).create_time()
            except (psutil.Error,OSError): return None
            return dict(hwnd=h,pid=pid,created=created,title=title,window_class=cls)
    return None


def valid_binding(binding,wins=None):
    if not isinstance(binding,dict): return None
    for hwnd,pid,_,cls in (enum_windows() if wins is None else wins):
        if hwnd==binding.get('hwnd') and pid==binding.get('pid') and cls==binding.get('window_class'):
            import psutil
            try:
                if abs(psutil.Process(pid).create_time()-float(binding['created']))<.01: return hwnd
            except (psutil.Error,OSError,ValueError,KeyError): pass
    return None


def terminal_candidates():
    import psutil
    out=[]
    for win in enum_windows():
        hwnd,pid,title,cls=win
        try: name=psutil.Process(pid).name().lower()
        except (psutil.Error,OSError): continue
        if cls in ('ConsoleWindowClass','CASCADIA_HOSTING_WINDOW_CLASS','mintty','VirtualConsoleClass') or name in (
            'windowsterminal.exe','openconsole.exe','conemu64.exe','conemu.exe','code.exe','wezterm-gui.exe','alacritty.exe'):
            out.append(win)
    return out


def find_terminal_window(instance_key,pid,source,hints,saved_title):
    wins=enum_windows()
    if isinstance(saved_title,dict):
        return valid_binding(saved_title,wins)  # stale binding must never retarget silently
    if saved_title:
        return None  # legacy titles require a fresh, explicit verified binding
    if source=='windows' and pid:
        parents=_ancestor_pids(pid)
        matches=[h for h,p,_,_ in wins if p in parents]
        if len(matches)==1: return matches[0]
    return None  # title substring does not establish ownership


def foreground_window_title():
    if not user32: return None
    hwnd=user32.GetForegroundWindow()
    return next((title for h,_,title,_ in enum_windows() if h==hwnd),None)


def foreground_identity():
    return window_identity(user32.GetForegroundWindow()) if user32 else None


class FLASHWINFO(ctypes.Structure):
    _fields_=[('cbSize',wt.UINT),('hwnd',wt.HWND),('dwFlags',wt.DWORD),('uCount',wt.UINT),('dwTimeout',wt.DWORD)]


def raise_window(hwnd):
    if not user32 or not user32.IsWindow(hwnd): return False
    if user32.IsIconic(hwnd): user32.ShowWindow(hwnd,9)
    user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow()==hwnd: return True
    info=FLASHWINFO(ctypes.sizeof(FLASHWINFO),hwnd,2,3,0)
    user32.FlashWindowEx(ctypes.byref(info))
    return False
