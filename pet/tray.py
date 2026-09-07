"""托盘图标：纯 ctypes Shell_NotifyIcon，零额外依赖。

独立线程里建一个隐藏 Win32 窗口接收托盘消息，事件写入队列，
UI 线程轮询队列处理（左键=显示/隐藏，右键=菜单），不跨线程碰 tkinter。
"""
import ctypes
import ctypes.wintypes as wt
import os
import queue
import threading

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32
kernel32.GetCurrentThreadId.restype = wt.DWORD
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]

# 明确签名：回调里会收到 64 位 wParam/lparam，缺省转换会溢出
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]

WM_APP_TRAY = 0x8100            # 托盘回调消息
WM_APP_QUIT = 0x8101
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4


class NOTIFYICONDATAW(ctypes.Structure):
    class _UNION(ctypes.Union):
        _fields_ = [("uTimeout", wt.UINT), ("uVersion", wt.UINT)]
    _fields_ = [
        ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
        ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128), ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
        ("union", _UNION), ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_ubyte * 16), ("hBalloonIcon", wt.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
        ("hCursor", ctypes.c_void_p), ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
    ]


user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]


class TrayIcon:
    def __init__(self, tooltip: str = "DeskPet"):
        self.events: "queue.Queue" = queue.Queue()
        self.tooltip = tooltip
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._hwnd = None
        self._wndproc_ref = None          # 保住回调引用防 GC
        self._nid: NOTIFYICONDATAW | None = None
        self._hicon = None

    # ---- 生命周期 ----
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="deskpet-tray")
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop(self):
        thread_id = self._thread_id
        if thread_id:
            # WM_QUIT belongs to the target thread's message queue.  This is
            # the reliable shutdown path even when the hidden window is being
            # torn down at the same time.
            user32.PostThreadMessageW(thread_id, 0x0012, 0, 0)
        if self._hwnd:
            # WM_QUIT is a thread-queue message and cannot be delivered
            # reliably with PostMessage(hwnd, ...).  Send a private window
            # message and let the tray thread call PostQuitMessage itself.
            user32.PostMessageW(self._hwnd, WM_APP_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None
        self._hwnd = None
        self._thread_id = 0

    # ---- worker 线程 ----
    def _run(self):
        self._thread_id = int(kernel32.GetCurrentThreadId())
        hinst = kernel32.GetModuleHandleW(None)
        cls = "DeskPetTrayWnd"

        @WNDPROC
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_APP_TRAY:
                # lParam 低字 = 鼠标消息
                ev = {0x0202: "left", 0x0205: "right", 0x0204: "right"}.get(
                    lparam & 0xFFFF)
                if ev:
                    self.events.put(ev)
                return 0
            if msg == WM_APP_QUIT:
                self._remove()
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = wndproc
        wc = WNDCLASSW()
        wc.lpfnWndProc = wndproc
        wc.hInstance = hinst
        wc.lpszClassName = cls
        if not user32.RegisterClassW(ctypes.byref(wc)):
            # 已注册（重复启动托盘）
            pass
        hwnd = user32.CreateWindowExW(0, cls, "DeskPetTray", 0, 0, 0, 0, 0,
                                      None, None, hinst, None)
        if not hwnd:
            self.events.put(("error", "CreateWindowExW failed"))
            return
        self._hwnd = hwnd
        self._add_icon()
        self._ready.set()

        msg = wt.MSG()
        lpmsg = ctypes.byref(msg)
        while user32.GetMessageW(lpmsg, None, 0, 0) > 0:
            user32.TranslateMessage(lpmsg)
            user32.DispatchMessageW(lpmsg)
        user32.DestroyWindow(hwnd)
        if self._hicon:
            user32.DestroyIcon(self._hicon)

    def _load_icon(self) -> int:
        ico = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "assets", "icon.ico")
        if os.path.isfile(ico):
            hicon = user32.LoadImageW(None, ico, 1, 0, 0,
                                      0x10 | 0x00000040)  # LR_LOADFROMFILE|LR_DEFAULTSIZE
            if hicon:
                return hicon
        return user32.LoadIconW(None, 32512)  # IDI_APPLICATION

    def _add_icon(self):
        self._hicon = self._load_icon()
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_APP_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self.tooltip or "DeskPet"
        self._nid = nid
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

    def _remove(self):
        if self._nid:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._nid = None

    def set_tooltip(self, tip: str):
        self.tooltip = tip
        if self._nid:
            self._nid.szTip = tip
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    def hide_icon(self):
        self._remove()

    def show_icon(self):
        if self._hwnd and not self._nid:
            self._add_icon()
