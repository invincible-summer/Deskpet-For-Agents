"""桌宠窗口：透明无边框置顶，画布上同时承载气泡与宠物动画。

位置模型：窗口位置永远由“锚点”（桌宠底部中心的屏幕坐标）反推，
不读取 winfo_x/y，彻底避免气泡高度变化/缩放时的位置漂移。
"""
import tkinter as tk

MAGIC = "#101011"  # 与 tools/convert.MAGIC 一致：画布背景色=桌面透明色


class PetWindow:
    def __init__(self, root: tk.Tk, config):
        self.root = root
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

        self.canvas = tk.Canvas(root, bg=MAGIC, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self._drag_off: tuple[int, int] | None = None
        self._pos: tuple[int, int] | None = None   # 窗口左上角（自记录，不读 winfo）
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_menu)
        self.canvas.bind("<Double-Button-1>", self._on_double)

        self.on_click_button = None   # cb(tag)  批复按钮点击
        self.on_menu = None           # cb(menu) 右键菜单构建
        self.on_interact = None       # cb()     双击互动
        self.on_moved = None          # cb(anchor_x, anchor_y) 拖动结束

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

    def _apply_topmost(self):
        try:
            self.root.attributes("-topmost", bool(self.config.get("topmost", True)))
        except tk.TclError:
            pass

    def set_topmost(self, flag: bool):
        self.config.set("topmost", bool(flag))
        self._apply_topmost()

    # ---- 交互 ----
    def _on_press(self, ev):
        tag = self.hit_button(ev.x, ev.y)
        if tag:
            if self.on_click_button:
                self.on_click_button(tag)
            return
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

    def _on_double(self, _ev):
        if self.hit_button(_ev.x, _ev.y):
            return "break"
        self._drag_off = None
        if self.on_interact:
            self.on_interact()

    def _on_menu(self, ev):
        import tkinter as tk_m
        menu = tk_m.Menu(self.root, tearoff=0)
        if self.on_menu:
            self.on_menu(menu)
        try:
            menu.tk_popup(ev.x_root, ev.y_root)
        finally:
            menu.grab_release()

    def hit_button(self, x: int, y: int) -> str | None:
        if self._hit_cb:
            return self._hit_cb(x, y)
        return None

    _hit_cb = None

    def bind_hit(self, cb):
        self._hit_cb = cb
