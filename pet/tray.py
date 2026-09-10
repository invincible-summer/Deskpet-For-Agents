"""托盘图标：纯 ctypes Shell_NotifyIcon，零额外依赖。

独立线程里建一个隐藏 Win32 窗口接收托盘消息；点击事件写入 bounded
队列（maxsize=16，满时丢弃计数，绝不阻塞 native wndproc），UI bridge
每 tick 有界收割（app 侧 TRAY_DRAIN_MAX=8）。不跨线程碰 tkinter。

v4.3.1 DP43-R09 生命周期合同（plan2 §17）：

  * start()：spawn 线程后立即返回——绝不 `_ready.wait(5s)`；
  * request_stop()：只投递退出消息后立即返回——运行期关闭绝不 join；
  * status()：O(1) 线程安全状态（STARTING/READY/FAILED/STOPPING/
    STOPPED）；worker 致命失败经 status()/last_error() 暴露，不再靠
    click 队列承载 fatal lifecycle 事件（旧 "error" tuple 协议不匹配
    导致死图标无法恢复）；
  * join_for_shutdown(timeout)：只有最终 App 退出允许 bounded join；
  * 同一时刻进程内最多一个 live TrayIcon（wndproc 按当前实例路由）；
    旧线程 stopping 期间不得创建 replacement。
"""
import ctypes
import ctypes.wintypes as wt
import enum
import os
import queue
import threading
from dataclasses import dataclass

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

# 点击事件队列上界（§17.8）：满时丢弃并计数，绝不阻塞 wndproc
TRAY_EVENT_QUEUE_MAX = 16


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

# 进程级共享 wndproc（v4.1.4 崩溃修复）：窗口类进程内只注册一次，
# 注册进类的回调必须与类同生命周期。挂在实例上的 WNDPROC 在实例
# 被 GC 后 trampoline 即释放，而类仍指向该地址——后续实例
# CreateWindowExW 分发 WM_NCCREATE 时会跳进已释放代码
# （access violation）。产品里托盘图标开关切换同样会创建第二个
# TrayIcon，因此这里必须用模块级回调 + 当前实例路由（同一时刻
# 只有一个 TrayIcon 存活，见 TrayIcon）。
_current_icon: "TrayIcon | None" = None


@WNDPROC
def _shared_wndproc(hwnd, msg, wparam, lparam):
    icon = _current_icon
    if icon is not None:
        return icon._handle_message(hwnd, msg, wparam, lparam)
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
        ("hCursor", ctypes.c_void_p), ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
    ]


user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]


class TrayState(enum.Enum):
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True)
class TrayEvent:
    """点击事件（§17.8）：kind ∈ {"left", "right"}。"""
    kind: str


class TrayIcon:
    """同一时刻进程内只应有一个 TrayIcon 存活（消息按当前实例路由）。"""

    def __init__(self, tooltip: str = "DeskPet"):
        self.events: "queue.Queue[TrayEvent]" = queue.Queue(
            maxsize=TRAY_EVENT_QUEUE_MAX)
        self.dropped_events = 0
        self.tooltip = tooltip
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._state_lock = threading.Lock()
        self._state = TrayState.STOPPED
        self._last_error = ""
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._hwnd = None
        self._nid: NOTIFYICONDATAW | None = None
        self._hicon = None

    # ---- 生命周期（DP43-R09 §17.6） ----
    @property
    def menu_hwnd(self):
        """托盘隐藏窗口 HWND（app 弹托盘菜单前的前台准备用，v4.3）。"""
        return self._hwnd

    def start(self) -> None:
        """Spawn worker 线程并立即返回（绝不 wait ready）。"""
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._state = TrayState.STARTING
            self._last_error = ""
        self._ready.clear()
        self._stopped.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="deskpet-tray")
        self._thread.start()

    def request_stop(self) -> None:
        """投递退出消息并立即返回（运行期关闭也绝不 join）。幂等。"""
        with self._state_lock:
            if self._state in (TrayState.STOPPING, TrayState.STOPPED):
                return
            self._state = TrayState.STOPPING
        thread_id = self._thread_id
        if thread_id:
            # WM_QUIT 属于目标线程消息队列；thread-id 投递是隐藏窗口
            # 正在被拆除时也可靠的退出路径。
            user32.PostThreadMessageW(thread_id, 0x0012, 0, 0)
        hwnd = self._hwnd
        if hwnd:
            user32.PostMessageW(hwnd, WM_APP_QUIT, 0, 0)

    def status(self) -> TrayState:
        """O(1)、线程安全。"""
        with self._state_lock:
            return self._state

    def last_error(self) -> str:
        with self._state_lock:
            return self._last_error

    def join_for_shutdown(self, timeout: float) -> bool:
        """只有最终 App 退出允许调用（bounded join）。返回线程是否退出。"""
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))
            thread = self._thread
        return thread is None or not thread.is_alive()

    # ---- worker 线程 ----
    def _run(self):
        global _current_icon
        _current_icon = self
        self._thread_id = int(kernel32.GetCurrentThreadId())
        # request_stop 抢在 thread_id 可见前到达：本线程自行退出
        if self.status() is TrayState.STOPPING:
            user32.PostQuitMessage(0)
        hinst = kernel32.GetModuleHandleW(None)
        cls = "DeskPetTrayWnd"

        wc = WNDCLASSW()
        wc.lpfnWndProc = _shared_wndproc   # 模块级：与窗口类同生命周期
        wc.hInstance = hinst
        wc.lpszClassName = cls
        if not user32.RegisterClassW(ctypes.byref(wc)):
            # 已注册（重复启动托盘）：类仍指向共享 wndproc，安全复用
            pass
        try:
            if self.status() is not TrayState.STOPPING:
                hwnd = user32.CreateWindowExW(
                    0, cls, "DeskPetTray", 0, 0, 0, 0, 0,
                    None, None, hinst, None)
                if not hwnd:
                    # worker 致命失败：经 status()/last_error() 暴露，
                    # _ready/_stopped 置位（不靠 click 队列承载 lifecycle）
                    with self._state_lock:
                        self._state = TrayState.FAILED
                        self._last_error = (
                            f"CreateWindowExW failed "
                            f"(GetLastError={kernel32.GetLastError()})")
                    self._ready.set()
                    self._stopped.set()
                    return
                self._hwnd = hwnd
                self._add_icon()
                with self._state_lock:
                    if self._state is TrayState.STARTING:
                        self._state = TrayState.READY
                self._ready.set()

                msg = wt.MSG()
                lpmsg = ctypes.byref(msg)
                while user32.GetMessageW(lpmsg, None, 0, 0) > 0:
                    user32.TranslateMessage(lpmsg)
                    user32.DispatchMessageW(lpmsg)
                user32.DestroyWindow(hwnd)
        finally:
            if self._hicon:
                user32.DestroyIcon(self._hicon)
                self._hicon = None
            self._hwnd = None
            with self._state_lock:
                if self._state is not TrayState.FAILED:
                    self._state = TrayState.STOPPED
                # FAILED 保持原样：app 据此知道 worker 致命失败可重建
            self._stopped.set()
            self._ready.set()
            if _current_icon is self:
                _current_icon = None

    def _handle_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_APP_TRAY:
            # lParam 低字 = 鼠标消息
            ev = {0x0202: "left", 0x0205: "right", 0x0204: "right"}.get(
                lparam & 0xFFFF)
            if ev:
                try:
                    self.events.put_nowait(TrayEvent(ev))
                except queue.Full:
                    # 队列满：丢弃/合并点击并计数，绝不阻塞 native wndproc
                    self.dropped_events += 1
            return 0
        if msg == WM_APP_QUIT:
            self._remove()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _load_icon(self) -> int:
        # V4.1.5：托盘图标 = 程序化绘制的原创小猫（pet/icon.py），
        # 不再使用桌宠形象/皮肤素材；生成失败回退系统默认图标。
        ico = None
        try:
            from .icon import ensure_icon_ico
            ico = ensure_icon_ico()
        except Exception:
            ico = None
        if ico and os.path.isfile(ico):
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
