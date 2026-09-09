"""桌宠窗口：透明无边框置顶 Toplevel，画布上同时承载气泡与宠物动画。

V4.1（v4plan §8.1）：PetWindow 是 Toplevel（master 是隐藏的 controller
root）；所有 PetView 同属一个 Tk interpreter。

位置模型：窗口位置永远由"锚点"（桌宠底部中心的屏幕坐标）反推，
不读取 winfo_x/y，彻底避免气泡高度变化/缩放时的位置漂移。

placement（v4plan §12）：拖动结束时经 MonitorFromPoint/GetMonitorInfo
换算成相对 work-area 的 (u, v)，monitor 消失时回退 anchor + clamp。
"""
import tkinter as tk

from actions import winkeys

MAGIC = "#101011"  # 与 tools/convert.MAGIC 一致：画布背景色=桌面透明色


class PetWindow:
    def __init__(self, master: tk.Misc, config):
        self.master = master
        self.root = tk.Toplevel(master)
        self.config = config
        self.root.title("DeskPet")
        self.root.overrideredirect(True)
        try:
            self.root.attributes("-transparentcolor", MAGIC)
        except tk.TclError:
            pass  # 允许无 Windows 桌面的界面测试
        self._apply_topmost()
        try:
            self.root.attributes("-toolwindow", True)  # 不显示在 Alt+Tab / 任务栏
        except tk.TclError:
            pass

        self.canvas = tk.Canvas(self.root, bg=MAGIC, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self._drag_off: tuple[int, int] | None = None
        self._pos: tuple[int, int] | None = None   # 窗口左上角（自记录，不读 winfo）
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_menu)
        self.canvas.bind("<Double-Button-1>", self._on_double)

        self.on_bubble_double = None  # cb(tag)  气泡双击（激活）
        self.on_body_double = None    # cb()     body 双击（互动/激活）
        self.on_menu = None           # cb(menu) 右键菜单构建
        self.on_moved = None          # cb()     拖动结束

    @property
    def dragging(self) -> bool:
        return self._drag_off is not None

    # ---- 可见性 ----
    def hide(self):
        self.root.withdraw()

    def show(self):
        self.root.deiconify()
        if bool(self.config.get("topmost", True)):
            self._apply_topmost()

    @property
    def visible(self) -> bool:
        return bool(self.root.winfo_viewable())

    # ---- 几何（锚点制） ----
    def apply_geometry(self, w: int, h: int, x: int, y: int):
        """设置窗口尺寸与位置（由 app 依据锚点计算）。"""
        self._pos = (x, y)
        self.canvas.config(width=w, height=h)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def anchor_from_window(self, win_w: int, win_h: int) -> tuple[int, int]:
        """拖动结束后调用：由自记录的窗口位置反推锚点（桌宠底部中心）。"""
        px, py = self._pos if self._pos else (
            self.root.winfo_x(), self.root.winfo_y())
        return (px + win_w // 2, py + win_h)

    @staticmethod
    def placement_from_anchor(ax: int, ay: int) -> dict:
        """锚点 → 相对 work-area 的 placement（monitor/u/v/anchor）。

        拖动结束才调用一次（不每 mouse move 调 Win32，v4plan §12）。
        """
        monitor, work = winkeys.monitor_work_area(ax, ay)
        if not monitor:
            return {"monitor": "", "u": None, "v": None,
                    "anchor": (ax, ay), "manual": True}
        left, top, right, bottom = work
        width = max(1, right - left)
        height = max(1, bottom - top)
        u = min(1.0, max(0.0, (ax - left) / width))
        v = min(1.0, max(0.0, (ay - top) / height))
        return {"monitor": monitor, "u": round(u, 4), "v": round(v, 4),
                "anchor": (ax, ay), "manual": True}

    @staticmethod
    def anchor_from_placement(placement: dict,
                               fallback: tuple[int, int]) -> tuple[int, int]:
        """placement → 恢复锚点；monitor 不存在/无 u,v → anchor fallback。"""
        if not isinstance(placement, dict):
            return fallback
        anchor = placement.get("anchor")
        u, v = placement.get("u"), placement.get("v")
        if u is None or v is None:
            return tuple(anchor) if anchor and len(anchor) == 2 else fallback
        monitor = placement.get("monitor") or ""
        for probe in (fallback, ):
            _name, work = winkeys.monitor_work_area(probe[0], probe[1])
            if monitor and _name != monitor:
                break   # monitor 不在：fallback 位置
            if not work:
                break
            left, top, right, bottom = work
            ax = int(left + u * (right - left))
            ay = int(top + v * (bottom - top))
            # clamp 到可见 work area
            ax = min(max(ax, left + 40), right - 40)
            ay = min(max(ay, top + 40), bottom - 20)
            return (ax, ay)
        return tuple(anchor) if anchor and len(anchor) == 2 else fallback

    def _apply_topmost(self):
        try:
            self.root.attributes("-topmost", bool(self.config.get("topmost", True)))
        except tk.TclError:
            pass

    def set_topmost(self, flag: bool):
        """直接应用本次设置（v4.1.3 §17：参数必须生效）。"""
        try:
            self.root.attributes("-topmost", bool(flag))
        except tk.TclError:
            pass

    def reassert_z_order(self) -> bool:
        """无激活的 Z-order 重声明（v4.1.3 §17）。

        只用于 DeskPet 自身 Pet Toplevel（Dashboard 打开/托盘恢复后
        拉回预期层级）；绝不用于 Terminal 前台唤起。非 Windows 测试
        环境 fallback 到 root.lift()。
        """
        if not self.root.winfo_exists():
            return False
        try:
            self.root.update_idletasks()
            hwnd = int(self.root.winfo_id())
        except tk.TclError:
            return False
        ok = winkeys.reassert_window_z_order(
            hwnd, topmost=bool(self.config.get("topmost", True)))
        if not ok:
            try:
                self.root.lift()   # 无 Win32 桌面的测试环境 fallback
            except tk.TclError:
                return False
        return True

    # ---- 交互（v4.1.3 §11：气泡/body 都是双击激活；单击不激活） ----
    def _on_press(self, ev):
        tag = self.hit_button(ev.x, ev.y)
        if tag:
            # 单击气泡：不激活、也不启动拖动（等待可能的第二次点击）
            self._drag_off = None
            return "break"
        self._drag_off = (ev.x, ev.y)

    def _on_drag(self, ev):
        if self._drag_off is None or self._pos is None:
            return
        # 位置增量基于按下时的自记录位置，不读 winfo（其返回值有延迟）
        x = self._pos[0] + ev.x - self._drag_off[0]
        y = self._pos[1] + ev.y - self._drag_off[1]
        self._pos = (x, y)
        self.root.geometry(f"+{x}+{y}")

    def _on_release(self, _ev):
        if self._drag_off is not None:
            self._drag_off = None
            if self.on_moved:
                self.on_moved()

    def _on_double(self, ev):
        """双击分发：一次 double-click 只产生一次 callback（§11.3）。"""
        self._drag_off = None

        tag = self.hit_button(ev.x, ev.y)
        if tag:
            if self.on_bubble_double:
                self.on_bubble_double(tag)
            return "break"

        if self.on_body_double:
            self.on_body_double()
        return "break"

    def _on_menu(self, ev):
        menu = tk.Menu(self.root, tearoff=0)
        if self.on_menu:
            self.on_menu(menu)
        try:
            menu.tk_popup(ev.x_root, ev.y_root)
        finally:
            menu.grab_release()

    def hit_button(self, x: int, y: int):
        if self._hit_cb:
            return self._hit_cb(x, y)
        return None

    _hit_cb = None

    def bind_hit(self, cb):
        self._hit_cb = cb
