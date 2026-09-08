"""轻量自定义 ttk/tk 控件（v4plan §2/§13）：只依赖 Tk/ttk。

NavButton / Card / StatusChip / SegmentedControl / SettingCard /
Expander / ScrollableFrame —— 不引入 Qt/CustomTkinter/ttkbootstrap。
"""
from __future__ import annotations

import tkinter as tk
import tkinter.ttk as ttk

from .theme import LIGHT, pick_font


class HelpDot(tk.Label):
    """？帮助图标：鼠标靠近自动悬浮提示（注释不写进界面正文）。

    无边框小圆点样式；hover ~300ms 后在图标下方弹出说明气泡，
    移开即消失。不抢焦点、不阻塞输入。
    """

    def __init__(self, master, text: str, bg: str = LIGHT.surface):
        self._text = str(text or "")
        super().__init__(master, text=" ?", padx=1,
                         bg=bg, fg=LIGHT.accent,
                         disabledforeground=LIGHT.accent,
                         font=pick_font(master, 9, True),
                         cursor="arrow")
        self._tip: tk.Toplevel | None = None
        self._show_after = None
        self.bind("<Enter>", self._schedule)
        self.bind("<Leave>", self._hide)

    def _schedule(self, _event=None):
        self._cancel_show()
        self._show_after = self.after(300, self._show)

    def _cancel_show(self):
        if self._show_after is not None:
            try:
                self.after_cancel(self._show_after)
            except Exception:
                pass
            self._show_after = None

    def _show(self):
        self._show_after = None
        if self._tip is not None or not self._text:
            return
        tip = tk.Toplevel(self)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        try:
            tip.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        label = tk.Label(tip, text=self._text, justify="left",
                         wraplength=320, padx=10, pady=7,
                         bg="#202522", fg="#F7F8F6",
                         font=pick_font(self, 9))
        label.pack(fill="both", expand=True)
        self._tip = tip
        self.update_idletasks()
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 6
        sw = self.winfo_screenwidth()
        w, h = tip.winfo_reqwidth(), tip.winfo_reqheight()
        x = min(max(8, x), max(8, sw - w - 8))
        tip.geometry(f"+{x}+{y}")
        tip.deiconify()

    def _hide(self, _event=None):
        self._cancel_show()
        tip, self._tip = self._tip, None
        if tip is not None:
            try:
                tip.destroy()
            except tk.TclError:
                pass


class NavButton(tk.Frame):
    """左侧导航按钮（高亮态用背景色 + 前景色表达，不只靠颜色）。"""

    def __init__(self, master, text: str, command, indent: int = 0):
        super().__init__(master, bg=LIGHT.page, cursor="hand2")
        self._text = text
        self._command = command
        self._active = False
        self._font = pick_font(master, 10)
        self._label = tk.Label(self, text=("  " * (indent // 8)) + text,
                               font=self._font, bg=LIGHT.page,
                               fg=LIGHT.text_secondary, anchor="w",
                               padx=8 + indent, pady=7)
        self._label.pack(fill="x")
        for widget in (self, self._label):
            widget.bind("<Button-1>", lambda _e: self._command())

    def set_active(self, active: bool):
        self._active = active
        bg = LIGHT.nav_active_bg if active else LIGHT.page
        fg = LIGHT.nav_active_fg if active else LIGHT.text_secondary
        self.configure(bg=bg)
        self._label.configure(bg=bg,
                              fg=LIGHT.text if active else fg,
                              font=pick_font(self, 10, bold=active))


class Card(tk.Frame):
    """卡片：内容（body）的请求尺寸决定高度，宽度随容器。

    修复：旧实现构造时立即把 canvas configure 成 winfo 尺寸（布局前为
    1×1），而 canvas 窗口项内的内容不会贡献 canvas 的请求尺寸 → 卡片
    永远塌成 1px 高。现在由 body 的 <Configure> 反向驱动 canvas 的请求
    尺寸，再由 pack 自然传播给卡片。
    """

    def __init__(self, master, padding: int = 16):
        super().__init__(master, bg=LIGHT.border, bd=0,
                         highlightthickness=0)
        self._pad = padding
        self._body_bg = LIGHT.surface
        self._canvas = tk.Canvas(self, bg=LIGHT.border, highlightthickness=0,
                                 height=2)
        self._canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self._canvas, bg=self._body_bg)
        # 两个 item 只创建一次（禁止 delete("all")：会连带删掉挂 body 的
        # window item，卡片内容会整个消失）
        self._rect = self._canvas.create_rectangle(
            0, 0, 2, 2, fill=self._body_bg, outline=LIGHT.border)
        self._window = self._canvas.create_window(0, 0, anchor="nw",
                                                  window=self.body)
        self.body.bind("<Configure>", lambda _e: self._sync_request())
        self._canvas.bind("<Configure>", self._on_canvas_resize)

    def _sync_request(self):
        """body 请求尺寸变化 → 更新 canvas 请求尺寸（含内边距）。"""
        w = self.body.winfo_reqwidth() + 2 * self._pad
        h = self.body.winfo_reqheight() + 2 * self._pad
        if (w, h) != (int(self._canvas["width"]), int(self._canvas["height"])):
            self._canvas.configure(width=w, height=h)

    def _on_canvas_resize(self, event):
        w, h = max(2, event.width), max(2, event.height)
        c = self._canvas
        c.coords(self._rect, 0, 0, w - 1, h - 1)
        c.coords(self._window, self._pad, self._pad)
        c.itemconfigure(self._window, width=max(10, w - 2 * self._pad))


class StatusChip(tk.Label):
    """状态徽标：颜色 + 文字同时表达（v4plan §13.1 可访问性）。"""

    def __init__(self, master, text: str = "", color: str = ""):
        self._color = color or LIGHT.unknown
        super().__init__(master, text=text, font=pick_font(master, 9, True),
                         bg=LIGHT.surface_subtle, fg=self._color,
                         padx=8, pady=2)

    def set(self, text: str, color: str = ""):
        self.configure(text=text, fg=color or self._color)


class SegmentedControl(tk.Frame):
    """分段选择（如 单宠聚合 / 多宠分离）。"""

    def __init__(self, master, options: list[str], command=None):
        super().__init__(master, bg=LIGHT.surface_subtle)
        self._command = command
        self._buttons: list[tk.Label] = []
        self._selected = -1
        self._initialized = False
        for i, text in enumerate(options):
            label = tk.Label(self, text=text, padx=12, pady=4,
                             cursor="hand2",
                             font=pick_font(master, 10, bold=False))
            label.pack(side="left")
            label.bind("<Button-1>", lambda _e, idx=i: self.select(idx))
            self._buttons.append(label)
        self._render(0)
        self._initialized = True

    def select(self, index: int):
        if index == self._selected:
            return
        self._render(index)
        if self._initialized and self._command:
            self._command(index)

    def _render(self, index: int):
        self._selected = index
        for i, label in enumerate(self._buttons):
            active = i == index
            label.configure(
                bg=LIGHT.surface if active else LIGHT.surface_subtle,
                fg=LIGHT.text if active else LIGHT.text_secondary,
                font=pick_font(self, 10, bold=active))

    def selected(self) -> int:
        return self._selected


class SettingCard(tk.Frame):
    """一行设置：左标签 + 右控件（对齐网格）。"""

    def __init__(self, master, label: str, widget, hint: str = ""):
        super().__init__(master, bg=LIGHT.surface)
        tk.Label(self, text=label, bg=LIGHT.surface, fg=LIGHT.text,
                 font=pick_font(master, 10)).pack(side="left")
        if hint:
            tk.Label(self, text=hint, bg=LIGHT.surface,
                     fg=LIGHT.text_secondary,
                     font=pick_font(master, 9)).pack(side="left", padx=8)
        widget.pack(side="right")

    def set_hint(self, hint: str):
        pass


class Expander(tk.Frame):
    """高级选项折叠区（默认收起）。"""

    def __init__(self, master, title: str):
        super().__init__(master, bg=LIGHT.surface)
        self._open = False
        self._title = tk.Label(self, text="▸ " + title, bg=LIGHT.surface,
                               fg=LIGHT.text_secondary, cursor="hand2",
                               font=pick_font(master, 10))
        self._title.pack(fill="x")
        self._title.bind("<Button-1>", lambda _e: self.toggle())
        self.body = tk.Frame(self, bg=LIGHT.surface)
        # body 由 add 后 pack

    def toggle(self):
        self._open = not self._open
        self._title.configure(
            text=("▾ " if self._open else "▸ ") + self._title["text"][2:])
        if self._open:
            self.body.pack(fill="x", pady=(6, 0))
        else:
            self.body.pack_forget()


class ScrollableFrame(tk.Frame):
    """纵向滚动容器（Canvas + 滚动条 + 鼠标滚轮）。"""

    def __init__(self, master):
        super().__init__(master, bg=LIGHT.page)
        self._canvas = tk.Canvas(self, bg=LIGHT.page, highlightthickness=0)
        self._bar = ttk.Scrollbar(self, orient="vertical",
                                  command=self._canvas.yview)
        self.inner = tk.Frame(self._canvas, bg=LIGHT.page)
        self._window = self._canvas.create_window(
            (0, 0), window=self.inner, anchor="nw")
        self._canvas.configure(yscrollcommand=self._bar.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._bar.pack(side="right", fill="y")
        self.inner.bind(
            "<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfigure(
                self._window, width=e.width))
        self._canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, event):
        if not self.winfo_ismapped():
            return
        try:
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except tk.TclError:
            pass
