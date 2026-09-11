"""轻量自定义 ttk/tk 控件（v4plan §2/§13）：只依赖 Tk/ttk。

NavButton / StatusChip / SegmentedControl / Expander / ScrollableFrame
（v4.3.1 DP43-R20：无 production caller 的旧 HelpDot / Card /
SettingCard 已删除）—— 不引入 Qt/CustomTkinter/ttkbootstrap。
"""
from __future__ import annotations

import tkinter as tk
import tkinter.ttk as ttk

from .theme import LIGHT, pick_font


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
    """纵向滚动容器（Canvas + 按需滚动条 + 页面内滚轮）。

    v4.3 §11.5：不再 `bind_all("<MouseWheel>")`（全局绑定会抢走
    其他窗口的滚轮）。滚轮由 Dashboard Toplevel 统一接收，只在
    指针位于本容器 canvas 范围内时调用 wheel_scroll()。
    内容不超出可视区时滚动条自动隐藏。
    """

    def __init__(self, master):
        super().__init__(master, bg=LIGHT.page)
        self._canvas = tk.Canvas(self, bg=LIGHT.page, highlightthickness=0)
        self._bar = ttk.Scrollbar(self, orient="vertical",
                                  command=self._canvas.yview)
        self.inner = tk.Frame(self._canvas, bg=LIGHT.page)
        self._window = self._canvas.create_window(
            (0, 0), window=self.inner, anchor="nw")
        self._canvas.configure(yscrollcommand=self._yview_changed)
        self._canvas.pack(side="left", fill="both", expand=True)
        # 滚动条初始不 pack：_sync_bar 按内容高度决定显隐
        self._bar_visible = False
        self.inner.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

    def wheel_scroll(self, delta: int, x: int, y: int) -> bool:
        """Dashboard 滚轮分发入口；指针在本 canvas 内才滚动。"""
        if not self.winfo_ismapped() or not self._bar_visible:
            return False
        try:
            self._canvas.yview_scroll(int(-1 * (delta / 120)), "units")
        except tk.TclError:
            pass
        return True

    def contains_point(self, x: int, y: int) -> bool:
        try:
            self._canvas.update_idletasks()
            left = self._canvas.winfo_rootx()
            top = self._canvas.winfo_rooty()
            return (left <= x <= left + self._canvas.winfo_width()
                    and top <= y <= top + self._canvas.winfo_height())
        except tk.TclError:
            return False

    def _on_inner_configure(self, _event):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._sync_bar()

    def _on_canvas_configure(self, event):
        self._canvas.itemconfigure(self._window, width=event.width)
        self._sync_bar()

    def _sync_bar(self):
        """内容高于可视区才显示滚动条；否则隐藏（不占宽度）。"""
        try:
            region = self._canvas.bbox("all")
            canvas_h = int(self._canvas.winfo_height())
        except tk.TclError:
            return
        content_h = region[3] - region[1] if region else 0
        needed = content_h > canvas_h + 4 and canvas_h > 1
        if needed and not self._bar_visible:
            self._bar.pack(side="right", fill="y")
            self._bar_visible = True
        elif not needed and self._bar_visible:
            self._bar.pack_forget()
            self._bar_visible = False

    def _yview_changed(self, first, last):
        self._bar.set(first, last)
        # 滚动到边界之外没有任何意义；内容不足一页时同步一次条状态
        self._sync_bar()


def bind_wraplength(widget, min_width: int = 160, pad: int = 8):
    """让带 wraplength 的 label 自适应实际宽度（窗口缩放不裁字/不撑爆）。

    绑定 <Configure>：wraplength 跟随 widget 当前宽度减 pad，不低于
    min_width。轻量无阻塞——只有尺寸变化时 Tk 才派发一次事件。
    """
    def _on_configure(event):
        try:
            widget.configure(wraplength=max(min_width, event.width - pad))
        except tk.TclError:
            pass
    widget.bind("<Configure>", _on_configure)


# ================================================================ v4.3 Dashboard 基础组件（plan2 §10-§13）

from dataclasses import dataclass


@dataclass(frozen=True)
class DashboardMetrics:
    """96-DPI 逻辑像素 → 当前 DPI 物理像素换算（plan2 §10.3）。"""
    dpi: int
    scale: float = 1.0

    @staticmethod
    def for_window(window) -> "DashboardMetrics":
        dpi = 96
        try:
            from actions import winkeys
            hwnd = int(window.winfo_id())
            dpi = winkeys.dpi_for_window(hwnd)
        except Exception:
            dpi = 96
        return DashboardMetrics(dpi=dpi, scale=dpi / 96.0)

    def px(self, logical: int) -> int:
        return max(1, int(round(logical * self.scale)))


class SurfacePanel(tk.Frame):
    """轻量卡片（plan2 §11.1）：1px 边框 frame + body（padding 16）。

    不用 Canvas/圆角——设置行数量多时 Canvas+window item 的布局回调
    成本不可接受。
    """

    def __init__(self, master, padding: int = 16,
                 body_bg: str | None = None):
        super().__init__(master, bg=LIGHT.border, bd=1,
                         highlightthickness=0)
        self.body = tk.Frame(self, bg=body_bg or LIGHT.surface)
        self.body.pack(fill="both", expand=True,
                       padx=padding - 1, pady=padding - 1)


class TooltipController:
    """单共享 tooltip（plan2 §11.2）：一个 Dashboard 只有一个实例。

    280ms hover delay；max 外宽 360 / wrap 336 / 内边距 10；贴边翻转；
    不抢 focus、不 topmost、无周期 timer（单次 after）。
    """

    DELAY_MS = 280
    MAX_W = 360
    WRAP_W = 336
    PAD = 10
    MARGIN = 12

    def __init__(self, toplevel):
        self._toplevel = toplevel
        self._after = None
        self._win = None
        self._label = None

    def schedule(self, anchor, text: str):
        self.hide()
        if not text:
            return
        self._after = self._toplevel.after(
            self.DELAY_MS, lambda: self._show(anchor, text))

    def hide(self):
        if self._after is not None:
            try:
                self._toplevel.after_cancel(self._after)
            except Exception:
                pass
            self._after = None
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:
                pass
            self._win = None
            self._label = None

    def _show(self, anchor, text: str):
        self._after = None
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:
                pass
            self._win = None
        try:
            ax = anchor.winfo_rootx()
            ay = anchor.winfo_rooty()
            ah = anchor.winfo_height()
            aw = anchor.winfo_width()
        except Exception:
            return
        self._win = tk.Toplevel(self._toplevel)
        self._win.withdraw()
        self._win.overrideredirect(True)
        self._win.configure(bg=LIGHT.border)
        self._label = tk.Label(
            self._win, text=text, justify="left", wraplength=self.WRAP_W,
            bg=LIGHT.surface, fg=LIGHT.text, padx=self.PAD, pady=self.PAD,
            font=pick_font(self._toplevel, 9))
        self._label.pack(fill="both", expand=True, padx=1, pady=1)
        self._win.update_idletasks()
        w = min(self.MAX_W, self._win.winfo_reqwidth())
        h = self._win.winfo_reqheight()
        x = min(max(ax - (w - aw) // 2, self.MARGIN),
                self._toplevel.winfo_screenwidth() - w - self.MARGIN)
        y = ay + ah + 6
        if y + h > self._toplevel.winfo_screenheight() - self.MARGIN:
            y = max(self.MARGIN, ay - h - 6)   # 底部不足 → 上方
        self._win.wm_geometry(f"{w}x{h}+{x}+{y}")
        self._win.deiconify()


class InfoButton(tk.Label):
    """18×18 ⓘ 说明按钮（plan2 §11.2）：与 label 同一 cell，间隔 6px。"""

    SIZE = 18

    def __init__(self, master, text: str, tooltip: TooltipController):
        self._tooltip = tooltip
        super().__init__(master, text="ⓘ", width=2, height=1,
                         bg=master["bg"] if "bg" in master.keys() else
                         LIGHT.surface,
                         fg=LIGHT.text_secondary, cursor="hand2",
                         font=pick_font(master, 10))
        self._text = text
        self.bind("<Enter>", lambda _e: tooltip.schedule(self, text))
        self.bind("<Leave>", lambda _e: tooltip.hide())
        self.bind("<Button-1>", lambda _e: tooltip.schedule(self, text))


class Stepper(tk.Frame):
    """`-  3  +` 步进器（plan2 §12.3 展示上限 1..8，非连续 slider）。"""

    def __init__(self, master, value: int, lo: int, hi: int,
                 command=None, width_chars: int = 3):
        super().__init__(master, bg=LIGHT.surface_subtle)
        self._command = command
        self._value = max(lo, min(hi, int(value)))
        self._lo, self._hi = lo, hi
        self._minus = tk.Label(self, text="－", width=3, cursor="hand2",
                               bg=LIGHT.surface_subtle, fg=LIGHT.text,
                               font=pick_font(master, 10))
        self._value_label = tk.Label(
            self, text=str(self._value), width=width_chars,
            bg=LIGHT.surface, fg=LIGHT.text,
            font=pick_font(master, 10, bold=True))
        self._plus = tk.Label(self, text="＋", width=3, cursor="hand2",
                              bg=LIGHT.surface_subtle, fg=LIGHT.text,
                              font=pick_font(master, 10))
        self._minus.pack(side="left", padx=(6, 2), pady=2)
        self._value_label.pack(side="left", pady=2)
        self._plus.pack(side="left", padx=(2, 6), pady=2)
        self._minus.bind("<Button-1>", lambda _e: self.step(-1))
        self._plus.bind("<Button-1>", lambda _e: self.step(1))

    def value(self) -> int:
        return self._value

    def set_external(self, value: int):
        value = max(self._lo, min(self._hi, int(value)))
        if value != self._value:
            self._value = value
            self._value_label.configure(text=str(value))

    def step(self, delta: int):
        new = max(self._lo, min(self._hi, self._value + delta))
        if new == self._value:
            return
        self._value = new
        self._value_label.configure(text=str(new))
        if self._command is not None:
            self._command(new)


class DiscreteSlider(tk.Frame):
    """离散值 slider（plan2 §13.1）。

    ttk.Scale 逻辑轴是整数 index 0..len(values)-1；拖动/键盘永远
    snap 到合法 step；每跨一个 step 立即 command(values[index])，
    不等 ButtonRelease。set_external 只更新 UI（invoke=False 不触发
    command，防 refresh 回环）；用户值不在 step 中时 thumb 停最近
    step、由 format_value 呈现"自定义"原值。
    """

    def __init__(self, master, values: tuple, *, format_value=None,
                 command=None, show_value_label: bool = True):
        super().__init__(master, bg=LIGHT.surface)
        self._values = tuple(values)
        self._format = format_value or (lambda v: f"{v:g}×")
        self._command = command
        self._index = 0
        self._custom_text = ""    # 用户非 step 值的"自定义"标注
        row = tk.Frame(self, bg=LIGHT.surface)
        row.pack(fill="x")
        self._scale_var = tk.DoubleVar(value=0)
        self._scale = ttk.Scale(row, from_=0, to=len(self._values) - 1,
                                value=0, command=self._on_drag)
        self._scale.pack(side="left", fill="x", expand=True)
        if show_value_label:
            self._value_label = tk.Label(
                row, text=self._format(self._values[0]), width=12,
                anchor="e", bg=LIGHT.surface, fg=LIGHT.text,
                font=pick_font(master, 10, bold=True))
            self._value_label.pack(side="left", padx=(10, 0))
        else:
            self._value_label = None
        # 首/默认/末 tick 文字（等距 tick dot 略去：3 点文字已足够定位，
        # 避免每个 slider 一个 canvas 布局回调）
        ticks = tk.Frame(self, bg=LIGHT.surface)
        ticks.pack(fill="x")
        tk.Label(ticks, text=self._format(self._values[0]),
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(master, 8)).pack(side="left")
        mid = self._values[len(self._values) // 2]
        tk.Label(ticks, text=self._format(mid), bg=LIGHT.surface,
                 fg=LIGHT.text_secondary,
                 font=pick_font(master, 8)).pack(side="top")
        tk.Label(ticks, text=self._format(self._values[-1]),
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(master, 8)).pack(side="right")
        for key, delta in (("<Left>", -1), ("<Down>", -1),
                           ("<Right>", 1), ("<Up>", 1)):
            self._scale.bind(key, lambda _e, d=delta: self._step(d))
        self._scale.bind("<Home>",
                         lambda _e: self._set_index(0, invoke=True))
        self._scale.bind("<End>",
                         lambda _e: self._set_index(
                             len(self._values) - 1, invoke=True))

    # ------------------------------------------------------------ 值
    def value(self):
        return self._values[self._index]

    def set_external(self, value, *, invoke: bool = False):
        """外部（config 同步）设值；默认不触发 command（防回环）。"""
        nearest, idx = None, 0
        for i, v in enumerate(self._values):
            if nearest is None or abs(v - value) < abs(nearest - value):
                nearest, idx = v, i
        custom = "" if nearest == value else f"自定义 {value:g}×"
        changed = (idx != self._index or custom != self._custom_text)
        self._index = idx
        self._custom_text = custom
        try:
            self._scale.configure(value=idx)
        except Exception:
            pass
        self._render_value()
        if invoke and changed and self._command is not None:
            self._command(self._values[self._index])

    # ------------------------------------------------------------ 内部
    def _render_value(self):
        if self._value_label is None:
            return
        if self._custom_text:
            self._value_label.configure(text=self._custom_text)
        else:
            self._value_label.configure(
                text=self._format(self._values[self._index]))

    def _on_drag(self, raw):
        try:
            index = int(round(float(raw)))
        except (TypeError, ValueError):
            return
        index = max(0, min(len(self._values) - 1, index))
        if float(raw) != float(index):
            # 用户永远不能停在两个合法值之间
            try:
                self._scale.configure(value=index)
            except Exception:
                pass
        if index != self._index:
            self._set_index(index, invoke=True)

    def _step(self, delta: int):
        self._set_index(self._index + delta, invoke=True)
        return "break"

    def _set_index(self, index: int, *, invoke: bool):
        index = max(0, min(len(self._values) - 1, index))
        if index == self._index:
            try:
                self._scale.configure(value=index)
            except Exception:
                pass
            return
        self._index = index
        self._custom_text = ""
        try:
            self._scale.configure(value=index)
        except Exception:
            pass
        self._render_value()
        if invoke and self._command is not None:
            self._command(self._values[self._index])


class SettingRow(tk.Frame):
    """四列设置行（plan2 §11.3）。

    wide：| label+info 220 | control flex(min 300) | value 72 | side 96 |
    compact（<720）：label+info 一行；control 满宽；value/side 随行。
    reflow 只 re-grid，不 destroy/recreate。
    """

    LABEL_W = 220
    CONTROL_MIN_W = 300
    VALUE_W = 72
    SIDE_W = 96
    COL_GAP = 12
    MIN_H = 48
    COMPACT_BREAK = 720

    def __init__(self, master, label: str, info: str = "",
                 tooltip: TooltipController | None = None):
        super().__init__(master, bg=LIGHT.surface)
        self._mode = None
        self.label_cell = tk.Frame(self, bg=LIGHT.surface)
        self._label = tk.Label(self.label_cell, text=label, anchor="w",
                               bg=LIGHT.surface, fg=LIGHT.text,
                               font=pick_font(master, 10))
        self._label.pack(side="left")
        if info and tooltip is not None:
            InfoButton(self.label_cell, info, tooltip).pack(
                side="left", padx=(6, 0))
        self._control_cell = tk.Frame(self, bg=LIGHT.surface)
        self._value_cell = tk.Frame(self, bg=LIGHT.surface)
        self._side_cell = tk.Frame(self, bg=LIGHT.surface)

    def set_control(self, widget):
        widget.pack(fill="x", expand=True, in_=self._control_cell)

    def set_value_widget(self, widget):
        widget.pack(in_=self._value_cell)

    def set_side_action(self, widget):
        widget.pack(in_=self._side_cell)

    def reflow(self, mode: str):
        """mode: wide | compact —— 只 re-grid 已有 cell。"""
        if mode == self._mode:
            return
        self._mode = mode
        for cell in (self.label_cell, self._control_cell,
                     self._value_cell, self._side_cell):
            cell.grid_forget()
        if mode == "wide":
            self.label_cell.grid(row=0, column=0, sticky="w",
                                 padx=(0, self.COL_GAP), pady=6)
            self._control_cell.grid(row=0, column=1, sticky="ew",
                                    padx=(0, self.COL_GAP), pady=6)
            self._value_cell.grid(row=0, column=2, sticky="e", pady=6)
            self._side_cell.grid(row=0, column=3, sticky="e",
                                 padx=(self.COL_GAP, 0), pady=6)
            self.grid_columnconfigure(0, minsize=self.LABEL_W, weight=0)
            self.grid_columnconfigure(1, weight=1,
                                      minsize=self.CONTROL_MIN_W)
            self.grid_columnconfigure(2, minsize=self.VALUE_W, weight=0)
            self.grid_columnconfigure(3, minsize=self.SIDE_W, weight=0)
        else:
            self.label_cell.grid(row=0, column=0, sticky="w", pady=(6, 0))
            self._control_cell.grid(row=1, column=0, sticky="ew", pady=4)
            self._value_cell.grid(row=1, column=1, sticky="e", pady=4)
            self._side_cell.grid(row=2, column=1, sticky="e", pady=(0, 6))
            self.grid_columnconfigure(0, weight=1)
            self.grid_columnconfigure(1, weight=0)
