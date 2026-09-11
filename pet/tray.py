"""托盘：原生的 Notification Area owner（纯 ctypes，零额外依赖）。

单一 tray worker 线程拥有全部原生资源——Shell icon、隐藏 HWND、
native HMENU、HICON。Tk 线程只 start / request_stop / 读状态 /
更新 immutable 菜单 snapshot；语义事件进 bounded 队列由 UiCoordinator
bridge 有界收割（app 侧 TRAY_DRAIN_MAX=8）。

v4.3.1 DP43-R15 生命周期与协议合同（plan §6）：

  * NOTIFYICON_VERSION_4：NIM_ADD 成功 + NIM_SETVERSION(4) 成功才
    READY；任一失败 → NIM_DELETE 回滚 + FAILED + last_error，绝不
    伪装 READY，也不退回 legacy right-down/up 双协议；
  * 一个手势 = 一个语义事件：context menu 只认 WM_CONTEXTMENU
    （Shell 对鼠标右键与键盘 context selection 都发它）；左键/
    键盘激活认 WM_LBUTTONUP / NIN_SELECT / NIN_KEYSELECT；
  * version-4 callback 组成（Microsoft Learn / NOTIFYICONDATAW）：
    LOWORD(lParam)=通知事件，HIWORD(lParam)=icon id（16 位），
    wParam=锚点坐标（仅 NIN_POPUPOPEN/NIN_SELECT/NIN_KEYSELECT 与
    WM_MOUSEFIRST..WM_MOUSELAST 有定义）；
  * native menu：CreatePopupMenu → 前台准备（SetForegroundWindow +
    GetForegroundWindow 复核）→ TrackPopupMenuEx(TPM_RETURNCMD) →
    finally DestroyMenu(root)（递归释放 submenu）→ NIM_SETFOCUS →
    非零 command id 映射成 TrayEvent 入队。前台准备失败不开菜单
    （menu_open_failures++），绝不留下无法 click-away 的菜单；
  * request_stop：先 PostMessage(WM_CANCELMODE) 结束可能 active 的
    native menu（EndMenu 只作用于调用线程，不得跨线程调用），再投
    递 WM_APP_QUIT / 线程 WM_QUIT；
  * HWND 路由：module-level WNDPROC（与窗口类同生命周期）+ hwnd →
    TrayIcon registry；未登记 HWND 一律 DefWindowProc。App 保证同
    一时刻最多一个 live generation（plan §6.6）；
  * HICON ownership：文件 LoadImageW（无 LR_SHARED）的 icon 由本
    进程释放；LoadIconW shared icon 绝不 DestroyIcon（Microsoft
    Learn / DestroyIcon）；
  * worker 绝不触碰 Tk；Tk 绝不创建/销毁 tray HMENU。
"""
import ctypes
import ctypes.wintypes as wt
import enum
import os
import queue
import threading
from dataclasses import dataclass, field

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

# ================================================================ Win32 原型
# DP43-R15 §3.4：所有用到的 Win32/Shell32 函数集中声明 argtypes/restype，
# 不留任何"指针/handle 返回函数依赖 ctypes 默认 restype（按 C int 截断）"
# 的例外——64 位 ABI 合同。
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetLastError.argtypes = []
kernel32.GetLastError.restype = wt.DWORD

user32.RegisterClassW.restype = wt.ATOM   # argtypes 在 WNDCLASSW 定义后声明
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND,
                               wt.UINT, wt.UINT]
user32.GetMessageW.restype = ctypes.c_int   # -1=error, 0=WM_QUIT
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.TranslateMessage.restype = wt.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM,
                                      wt.LPARAM]
user32.PostThreadMessageW.restype = wt.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None
user32.DestroyWindow.argtypes = [wt.HWND]
user32.DestroyWindow.restype = wt.BOOL
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                              ctypes.c_int, ctypes.c_int, wt.UINT]
user32.LoadImageW.restype = wt.HANDLE
user32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
user32.LoadIconW.restype = wt.HICON
user32.DestroyIcon.argtypes = [wt.HICON]
user32.DestroyIcon.restype = wt.BOOL
user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t,
                               wt.LPCWSTR]
user32.AppendMenuW.restype = wt.BOOL
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.DestroyMenu.restype = wt.BOOL
user32.TrackPopupMenuEx.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int,
                                    ctypes.c_int, wt.HWND, wt.LPVOID]
user32.TrackPopupMenuEx.restype = ctypes.c_int   # TPM_RETURNCMD 时为 cmd id
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wt.HWND
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetCursorPos.restype = wt.BOOL

shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, wt.LPVOID]
shell32.Shell_NotifyIconW.restype = wt.BOOL

# ================================================================ 常量
WM_APP_TRAY = 0x8100            # 托盘回调消息（uCallbackMessage）
WM_APP_QUIT = 0x8101
WM_QUIT = 0x0012
WM_CANCELMODE = 0x001B          # 结束 active menu 的标准路径（EndMenu
                                # 只作用于调用线程，不能跨线程用）

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIM_SETFOCUS, NIM_SETVERSION = 3, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
NIF_SHOWTIP = 0x80              # VERSION_4 下保留标准 tooltip
NOTIFYICON_VERSION_4 = 4

WM_CONTEXTMENU = 0x007B         # v4：鼠标右键与键盘 context selection
WM_LBUTTONUP = 0x0202           # v4：鼠标左键激活
NIN_SELECT = 0x0400             # v4：鼠标选中后 ENTER 激活
NIN_KEYSELECT = 0x0401          # v4：SPACE/ENTER 键盘激活

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
LR_DEFAULTSIZE = 0x40
IDI_APPLICATION = 32512

TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

MF_STRING = 0x0000
MF_GRAYED = 0x0001
MF_POPUP = 0x0010
MF_SEPARATOR = 0x0800

# 语义事件队列上界（plan §6.3）：满时丢弃并计数，绝不阻塞 wndproc
TRAY_EVENT_QUEUE_MAX = 16

# 固定 command id（动态 agent 项从 _AGENT_CMD_BASE 起）
_CMD_TOGGLE_VISIBLE = 101
_CMD_DASHBOARD = 102
_CMD_RESCAN = 103
_CMD_QUIT = 104
_AGENT_CMD_BASE = 200


class NOTIFYICONDATAW(ctypes.Structure):
    class _UNION(ctypes.Union):
        _fields_ = [("uTimeout", wt.UINT), ("uVersion", wt.UINT)]
    _anonymous_ = ("union",)
    _fields_ = [
        ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
        ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128), ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
        ("union", _UNION), ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_ubyte * 16), ("hBalloonIcon", wt.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT,
                             wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
        ("hCursor", ctypes.c_void_p), ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
    ]


# ================================================================ HWND registry
# DP43-R15 §6.6：module-level WNDPROC 保持进程级生命周期（窗口类进程内
# 只注册一次，注册进类的回调必须与类同生命周期——挂在实例上的 WNDPROC
# 在实例 GC 后 trampoline 释放，后续 CreateWindowExW 分发 WM_NCCREATE
# 即 access violation）。实例路由用 hwnd → TrayIcon registry，替换"当前
# 实例"概念：registry 常态最多 1 项（App 保证单 live generation），它只
# 是正确的 ownership，不允许多实例。
_hwnd_lock = threading.Lock()
_hwnd_registry: dict[int, "TrayIcon"] = {}


def _register_hwnd(hwnd, icon: "TrayIcon") -> None:
    with _hwnd_lock:
        _hwnd_registry[int(hwnd)] = icon


def _unregister_hwnd(hwnd) -> None:
    with _hwnd_lock:
        _hwnd_registry.pop(int(hwnd), None)


def registry_size() -> int:
    """诊断用：当前登记的 tray HWND 数（常态 <=1）。"""
    with _hwnd_lock:
        return len(_hwnd_registry)


@WNDPROC
def _shared_wndproc(hwnd, msg, wparam, lparam):
    with _hwnd_lock:
        icon = _hwnd_registry.get(int(hwnd))
    if icon is not None:
        return icon._handle_message(hwnd, msg, wparam, lparam)
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]


class TrayState(enum.Enum):
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


_TERMINAL_STATES = (TrayState.STOPPED, TrayState.FAILED)


@dataclass(frozen=True)
class TrayAgentItem:
    """Agents 子菜单的一项（immutable；Tk 线程构建）。"""
    key: str
    label: str


@dataclass(frozen=True)
class TrayMenuSnapshot:
    """native 菜单的 immutable 模型（plan §6.3）。

    Tk 线程构建后整只换入 worker（原子引用交换）；worker 在打开菜单
    时临时构建 HMENU，动态 command id 只活在这一次 HMENU 生命周期内。
    """
    pet_visible: bool = True
    agents: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class TrayEvent:
    """语义事件：command ∈ {"restore","toggle_visible","dashboard",
    "rescan","quit","activate"}；activate 携带 exact agent_key。"""
    command: str
    agent_key: str = ""


class TrayIcon:
    """同一时刻进程内只应有一个 live TrayIcon（App reconcile 保证）。"""

    _ICON_ID = 1   # NOTIFYICONDATA.uID；v4 callback HIWORD(lParam)

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
        # DP43-R15 §6.8：文件 LoadImageW（无 LR_SHARED）→ 需要本进程
        # DestroyIcon；LoadIconW shared icon → 绝不 Destroy。
        self._owns_hicon = False
        # immutable 菜单模型（Tk 线程换入；worker 打开菜单时读取）
        self._menu_snapshot = TrayMenuSnapshot()
        self._menu_open_failures = 0
        self._menu_active = False
        self.quit_requested = threading.Event()

    # ---- 生命周期（start 立即返回；request_stop 只投递不 join） ----
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
        """投递退出消息并立即返回（幂等）。

        顺序：WM_CANCELMODE（结束可能 active 的 native menu——
        TrackPopupMenuEx 的模态循环会先返回）→ WM_APP_QUIT（删 icon +
        PostQuitMessage）→ 线程 WM_QUIT（窗口正在拆除时的兜底路径）。
        """
        with self._state_lock:
            if self._state in (TrayState.STOPPING, TrayState.STOPPED):
                return
            self._state = TrayState.STOPPING
        hwnd = self._hwnd
        if hwnd:
            user32.PostMessageW(hwnd, WM_CANCELMODE, 0, 0)
            user32.PostMessageW(hwnd, WM_APP_QUIT, 0, 0)
        thread_id = self._thread_id
        if thread_id:
            user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)

    def status(self) -> TrayState:
        """O(1)、线程安全。"""
        with self._state_lock:
            return self._state

    def is_terminated(self) -> bool:
        """worker 是否已到终态（线程已退出/即将回收）。"""
        return self.status() in _TERMINAL_STATES

    def last_error(self) -> str:
        with self._state_lock:
            return self._last_error

    def menu_open_failures(self) -> int:
        with self._state_lock:
            return self._menu_open_failures

    def join_for_shutdown(self, timeout: float) -> bool:
        """只有最终 App 退出允许调用（bounded join）。返回线程是否退出。"""
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))
            thread = self._thread
        return thread is None or not thread.is_alive()

    def update_menu_snapshot(self, snapshot: TrayMenuSnapshot) -> None:
        """Tk 线程换入 immutable 菜单模型（原子引用交换）。"""
        self._menu_snapshot = snapshot

    # ---- worker 线程 ----
    def _run(self):
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
        hwnd = None
        try:
            if self.status() is not TrayState.STOPPING:
                hwnd = user32.CreateWindowExW(
                    0, cls, "DeskPetTray", 0, 0, 0, 0, 0,
                    None, None, hinst, None)
                if not hwnd:
                    with self._state_lock:
                        self._state = TrayState.FAILED
                        self._last_error = (
                            f"CreateWindowExW failed "
                            f"(GetLastError={kernel32.GetLastError()})")
                    self._ready.set()
                    self._stopped.set()
                    return
                self._hwnd = hwnd
                _register_hwnd(hwnd, self)
                icon_ok = self._add_icon()
                with self._state_lock:
                    if self._state is TrayState.STARTING:
                        self._state = (TrayState.READY if icon_ok
                                       else TrayState.FAILED)
                self._ready.set()

                if icon_ok and self.status() is not TrayState.STOPPING:
                    msg = wt.MSG()
                    lpmsg = ctypes.byref(msg)
                    while user32.GetMessageW(lpmsg, None, 0, 0) > 0:
                        user32.TranslateMessage(lpmsg)
                        user32.DispatchMessageW(lpmsg)
        finally:
            # WM_QUIT can bypass WM_APP_QUIT (including a startup race).
            # The resource owner must remove the icon on every exit path.
            self._remove()
            if hwnd:
                _unregister_hwnd(hwnd)
                user32.DestroyWindow(hwnd)
            self._hwnd = None
            self._thread_id = 0
            self._destroy_owned_hicon()
            with self._state_lock:
                if self._state is not TrayState.FAILED:
                    self._state = TrayState.STOPPED
                # FAILED 保持原样：app 据此知道 worker 致命失败可重建
            self._stopped.set()
            self._ready.set()

    def _handle_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_APP_TRAY:
            # VERSION_4：LOWORD(lParam)=通知事件，HIWORD(lParam)=icon id
            notification = lparam & 0xFFFF
            icon_id = (lparam >> 16) & 0xFFFF
            if icon_id != self._ICON_ID:
                return 0
            if notification == WM_CONTEXTMENU:
                self._open_native_menu()
                return 0
            if notification in (WM_LBUTTONUP, NIN_SELECT, NIN_KEYSELECT):
                self._enqueue(TrayEvent("restore"))
            # 其他鼠标 down/move/up 不转成业务语义（plan §6.2）
            return 0
        if msg == WM_APP_QUIT:
            self._remove()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _enqueue(self, event: TrayEvent) -> None:
        if event.command == "quit":
            self.quit_requested.set()  # exit cannot be lost behind a full queue
        try:
            self.events.put_nowait(event)
        except queue.Full:
            # 队列满：丢弃并计数，绝不阻塞 native wndproc
            self.dropped_events += 1

    # ---- native context menu（worker 线程；plan §6.4） ----
    def _open_native_menu(self):
        if self._menu_active or self.status() is TrayState.STOPPING:
            return
        self._menu_active = True
        try:
            self._track_native_menu()
        finally:
            self._menu_active = False

    def _track_native_menu(self):
        snapshot = self._menu_snapshot
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            self._record_menu_open_failure("CreatePopupMenu failed "
                                           f"(GetLastError="
                                           f"{kernel32.GetLastError()})")
            return
        cmd_by_id: dict[int, TrayEvent] = {
            _CMD_TOGGLE_VISIBLE: TrayEvent("toggle_visible"),
            _CMD_DASHBOARD: TrayEvent("dashboard"),
            _CMD_RESCAN: TrayEvent("rescan"),
            _CMD_QUIT: TrayEvent("quit"),
        }
        next_id = _AGENT_CMD_BASE
        agents_menu = None
        agents_attached = False
        try:
            self._append(hmenu, MF_STRING,
                         _CMD_TOGGLE_VISIBLE,
                         "隐藏桌宠" if snapshot.pet_visible else "显示桌宠")
            agents_menu = user32.CreatePopupMenu()
            if not agents_menu:
                raise OSError("CreatePopupMenu(agents) failed")
            if snapshot.agents:
                for item in snapshot.agents:
                    cid = next_id
                    next_id += 1
                    self._append(agents_menu, MF_STRING, cid, item.label)
                    cmd_by_id[cid] = TrayEvent("activate", item.key)
            else:
                self._append(agents_menu, MF_STRING | MF_GRAYED, 0,
                             "（当前没有发现 Agent）")
            self._append(hmenu, MF_STRING | MF_POPUP, int(agents_menu),
                         "Agents")
            agents_attached = True
            self._append(hmenu, MF_STRING, _CMD_DASHBOARD, "仪表盘")
            self._append(hmenu, MF_STRING, _CMD_RESCAN, "重新扫描")
            self._append(hmenu, MF_SEPARATOR, 0, None)
            self._append(hmenu, MF_STRING, _CMD_QUIT, "退出")

            # 前台准备：TrackPopupMenu 的 owner 必须是前台窗口
            # （Microsoft Learn / notification area）。失败绝不开一个
            # 可能无法 dismiss 的菜单。
            hwnd = self._hwnd
            user32.SetForegroundWindow(hwnd)
            if int(user32.GetForegroundWindow() or 0) != int(hwnd or 0):
                self._record_menu_open_failure(
                    "SetForegroundWindow denied before menu")
                return
            pt = wt.POINT()
            if not user32.GetCursorPos(ctypes.byref(pt)):
                self._record_menu_open_failure("GetCursorPos failed")
                return
            cmd = user32.TrackPopupMenuEx(
                hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                pt.x, pt.y, hwnd, None)
        except OSError as exc:
            self._record_menu_open_failure(str(exc)[:120])
            return
        finally:
            # Microsoft TrackPopupMenu contract: this benign message fixes
            # immediate dismissal on the next notification-area invocation.
            user32.PostMessageW(self._hwnd, 0x0000, 0, 0)  # WM_NULL
            if agents_menu and not agents_attached:
                user32.DestroyMenu(agents_menu)
            # HMENU 生命周期固定在本次 popup 内：root destroy 递归释放
            # submenus，不依赖 Python GC（Microsoft Learn / DestroyMenu）。
            user32.DestroyMenu(hmenu)
            self._set_focus_tray()
        if cmd and self.status() is not TrayState.STOPPING:
            event = cmd_by_id.get(int(cmd))
            if event is not None:
                self._enqueue(event)

    def _append(self, hmenu, flags: int, cmd_id: int, text) -> None:
        if not user32.AppendMenuW(hmenu, flags, cmd_id, text):
            raise OSError(f"AppendMenuW failed (GetLastError="
                          f"{kernel32.GetLastError()})")

    def _record_menu_open_failure(self, reason: str) -> None:
        with self._state_lock:
            self._menu_open_failures += 1
            self._last_error = reason

    def _set_focus_tray(self) -> None:
        """菜单结束后把焦点还给通知区（Microsoft Learn / NIM_SETFOCUS）。"""
        nid = self._nid
        if nid is not None:
            shell32.Shell_NotifyIconW(NIM_SETFOCUS, ctypes.byref(nid))

    # ---- icon ----
    def _load_icon(self):
        """(hicon, owns)：文件 icon 需要本进程释放；shared 不释放。

        V4.1.5：托盘图标 = 程序化绘制的原创小猫（pet/icon.py），不再
        使用桌宠形象/皮肤素材；生成失败回退系统 shared 默认图标。
        """
        ico = None
        try:
            from .icon import ensure_icon_ico
            ico = ensure_icon_ico()
        except Exception:
            ico = None
        if ico and os.path.isfile(ico):
            hicon = user32.LoadImageW(
                None, ico, IMAGE_ICON, 0, 0,
                LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if hicon:
                return hicon, True
        # MAKEINTRESOURCEW：按官方宏语义把资源 id 转成宽字符串指针
        return (user32.LoadIconW(
            None, ctypes.cast(IDI_APPLICATION, ctypes.c_wchar_p)), False)

    def _add_icon(self) -> bool:
        """NIM_ADD + NIM_SETVERSION(VERSION_4)；两步都成功才 True。

        失败路径（plan §3.5）：NIM_ADD 失败 → FAILED；NIM_SETVERSION
        失败 → NIM_DELETE 回滚 + FAILED。last_error 记录阶段与
        GetLastError。
        """
        self._hicon, self._owns_hicon = self._load_icon()
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = self._ICON_ID
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        nid.uCallbackMessage = WM_APP_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self.tooltip or "DeskPet"
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            with self._state_lock:
                self._state = TrayState.FAILED
                self._last_error = (
                    f"Shell_NotifyIconW(NIM_ADD) failed (GetLastError="
                    f"{kernel32.GetLastError()})")
            self._destroy_owned_hicon()
            return False
        self._nid = nid
        ver = NOTIFYICONDATAW()
        ver.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        ver.hWnd = self._hwnd
        ver.uID = self._ICON_ID
        ver.uVersion = NOTIFYICON_VERSION_4
        if not shell32.Shell_NotifyIconW(NIM_SETVERSION,
                                         ctypes.byref(ver)):
            error = (f"Shell_NotifyIconW(NIM_SETVERSION=4) failed "
                     f"(GetLastError={kernel32.GetLastError()})")
            self._remove()   # 回滚：不留一个语义模糊的 icon
            with self._state_lock:
                self._state = TrayState.FAILED
                self._last_error = error
            self._destroy_owned_hicon()
            return False
        return True

    def _destroy_owned_hicon(self) -> None:
        hicon, owns = self._hicon, self._owns_hicon
        self._hicon = None
        self._owns_hicon = False
        if hicon and owns:
            user32.DestroyIcon(hicon)

    def _remove(self):
        if self._nid:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._nid = None

    def set_tooltip(self, tip: str):
        self.tooltip = tip
        if self._nid:
            self._nid.szTip = tip
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    def show_icon(self):
        """READY 状态下重新补挂 icon（icon 曾被移除时）。"""
        if self._hwnd and not self._nid:
            self._add_icon()
