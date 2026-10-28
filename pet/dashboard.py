"""Native desktop workspace with retained pages and a single layout owner.

The Agent workspace owns session counts, filtering and details. Settings pages
have separate responsibilities. Viewport width drives page layout; hover and
runtime updates never renegotiate control metrics or rebuild the current page.
"""
from __future__ import annotations
import os
import tkinter as tk
import threading
import time
from tkinter import filedialog, messagebox, ttk

from agents.models import AgentSurface, Status
from agents.summarize import summarize_text

from . import autostart, skins
from .labels import (activity_text, environment_label, mode_text, phase_text,
                     status_text, workspace_name)
from .presentation import PresentationMode
from .theme import LIGHT, STATUS_COLOR, pick_font, configure_dashboard_styles
from .ui_coordinator import UiDirty
from .version import APP_LABEL as APP_VERSION
from .widgets import (
    BoundedLabel,
    DashboardMetrics,
    DiscreteSlider,
    Expander,
    InfoButton,
    NavButton,
    SettingRow,
    StatusChip,
    Stepper,
    SurfacePanel,
    TooltipController,
    bind_wraplength,
)

PAGE_AGENTS = "Agent 工作台"
PAGE_PETS = "桌宠"
PAGE_LOOK = "外观"
PAGE_MONITOR = "监听与隐私"
PAGE_DIAG = "诊断"
PAGE_SETTINGS = "设置"

# §10.2 shell 常量（96-DPI 逻辑像素）
DEFAULT_WINDOW_W = 1120
DEFAULT_WINDOW_H = 760
NAV_WIDTH = 184
BRAND_HEIGHT = 64
NAV_ITEM_HEIGHT = 40
PAGE_HEADER_HEIGHT = 64
PAGE_CONTENT_MAX_WIDTH = 1040
PAGE_PAD_X = 24
PAGE_PAD_Y = 20
SECTION_GAP = 12
ROW_GAP = 8

# 视觉层级只在这里定义，页面名称仍然保持原有公开键值。
PAGE_DESCRIPTIONS = {
    PAGE_AGENTS: "所有会话，一个工作台。查看状态并回到正在运行的 Agent。",
    PAGE_PETS: "管理并发桌宠与 Agent 绑定",
    PAGE_LOOK: "调整桌宠和气泡的显示方式",
    PAGE_MONITOR: "控制观察来源与隐私边界",
    PAGE_DIAG: "检查运行健康与性能指标",
    PAGE_SETTINGS: "启动、窗口与本地配置",
}
# §13.2 固定离散值（默认点 1.00）
SCALE_STEPS = (0.50, 0.75, 1.00, 1.25, 1.50, 1.75, 2.00)
SPEED_STEPS = (0.50, 0.75, 1.00, 1.25, 1.50, 2.00, 3.00)
BUBBLE_W_STEPS = (0.70, 0.85, 1.00, 1.15, 1.30, 1.45, 1.60)
BUBBLE_H_STEPS = (0.80, 0.90, 1.00, 1.15, 1.30, 1.45, 1.60)
BUBBLE_FONT_STEPS = (0.75, 0.85, 1.00, 1.10, 1.20, 1.30, 1.40)


def _binding_conf_value(binding):
    value = getattr(binding, "confidence", None)
    return value.value if hasattr(value, "value") else str(value)


def _section_title(parent, text: str, *, trailing=None):
    """Render a compact section heading without adding a second card layer."""
    row = tk.Frame(parent, bg=parent["bg"])
    row.pack(fill="x", pady=(0, ROW_GAP))
    tk.Label(row, text=text, bg=parent["bg"], fg=LIGHT.text,
             font=pick_font(parent, 11, True), anchor="w").pack(
        side="left")
    if trailing is not None:
        trailing.pack(side="right")
    return row


def _pack_once(widget, **options):
    if not widget.winfo_manager():
        widget.pack(**options)


def _pack_forget_once(widget):
    if widget.winfo_manager():
        widget.pack_forget()


def _configure_changed(widget, **options):
    changed = {}
    for key, value in options.items():
        try:
            current = widget.cget(key)
        except tk.TclError:
            current = object()
        if str(current) != str(value):
            changed[key] = value
    if changed:
        widget.configure(**changed)


def _activation_label(instance) -> str:
    """Use the action wording that matches the target's runtime surface."""
    return ("打开应用" if getattr(instance, "surface", None)
            is AgentSurface.DESKTOP else "打开终端")


def _ellipsize(value, limit: int = 64) -> str:
    """Bound dynamic labels so a new summary never changes card height."""
    text = " ".join(str(value or "").split())
    if not text:
        return "—"
    if len(text) <= limit:
        return text
    return text[:max(1, limit - 1)].rstrip() + "…"


class DashboardPage:
    """统一页面接口（plan2 §15）：lazy build + retained 复用。"""

    name = ""

    def __init__(self, dash: "Dashboard"):
        self.dash = dash
        self.built = False
        self.holder = None      # 页面容器（grid 进 content 区）

    # 子类契约
    def build(self, parent): ...
    def on_show(self): ...
    def on_hide(self): ...
    def refresh(self, reason: UiDirty): ...
    def reflow(self, width: int): ...

    def ensure_built(self, parent):
        if not self.built:
            self.built = True
            self.holder = tk.Frame(parent, bg=LIGHT.page)
            self.build(self.holder)
            self._polish(self.holder)
        return self.holder

    def _polish(self, parent):
        """Style lazy-built controls once; text wraps to its allocated width."""
        for widget in parent.winfo_children():
            kind = widget.winfo_class()
            if kind in ("TButton", "TCheckbutton", "TRadiobutton", "TCombobox", "TSpinbox", "TScale"):
                if not widget.cget("style"):
                    widget.configure(style="Dashboard.Horizontal.TScale" if kind == "TScale"
                                     else "Dashboard." + kind)
            # Wrapping is opt-in for descriptive text. Applying a Configure
            # handler to every packed label makes each status/title a second
            # layout owner and feeds requested widths back into the Canvas.
            self._polish(widget)


class AgentsPage(DashboardPage):
    """One workspace: summary, filtered retained rows, selected-session details."""

    name = PAGE_AGENTS
    FILTERS = ("全部", "需要关注", "工作中", "待命")

    def build(self, parent):
        header = self.dash.page_header(parent, PAGE_AGENTS)
        header.add_action("重新扫描", self.dash.request_rescan)
        body = self.dash.page_body(parent)
        summary = SurfacePanel(body, padding=16)
        summary.pack(fill="x", pady=(0, SECTION_GAP))
        self._metrics = {}
        for col, (key, title, color) in enumerate((
                ("live", "在线会话", LIGHT.text),
                ("working", "正在工作", LIGHT.working),
                ("attention", "需要关注", LIGHT.waiting))):
            summary.body.columnconfigure(col, weight=1, uniform="metric")
            cell = tk.Frame(summary.body, bg=LIGHT.surface)
            cell.grid(row=0, column=col, sticky="ew", padx=(0, 12))
            tk.Label(cell, text=title, bg=LIGHT.surface,
                     fg=LIGHT.text_secondary, anchor="w",
                     font=pick_font(cell, 9)).pack(fill="x")
            value = tk.Label(cell, text="0", width=1, height=1,
                             bg=LIGHT.surface, fg=color, anchor="w",
                             font=pick_font(cell, 22, True))
            value.pack(fill="x", pady=(3, 0))
            self._metrics[key] = value

        self._list_panel = SurfacePanel(body, padding=16)
        self._list_panel.pack(fill="x", pady=(0, SECTION_GAP))
        toolbar = tk.Frame(self._list_panel.body, bg=LIGHT.surface)
        toolbar.pack(fill="x", pady=(0, 12))
        from .widgets import SegmentedControl
        self._filter = 0
        self._filters = SegmentedControl(
            toolbar, list(self.FILTERS), command=self._set_filter)
        self._filters.pack(side="left")
        self._list_count = tk.Label(toolbar, text="0 个会话", width=18,
                                    anchor="e", bg=LIGHT.surface,
                                    fg=LIGHT.text_secondary,
                                    font=pick_font(toolbar, 9))
        self._list_count.pack(side="right")
        self._list_frame = tk.Frame(self._list_panel.body, bg=LIGHT.surface)
        self._list_frame.pack(fill="x")
        self._empty = tk.Frame(self._list_frame, bg=LIGHT.surface, height=108)
        self._empty.pack_propagate(False)
        self._empty_title = tk.Label(
            self._empty, text="暂无在线 Agent", bg=LIGHT.surface,
            fg=LIGHT.text, font=pick_font(parent, 12, True))
        self._empty_title.pack(pady=(20, 6))
        tk.Label(self._empty, text="启动受支持的 Agent 后，会话会自动出现在这里。",
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(parent, 9)).pack()
        self._rows: dict[str, dict] = {}
        self._row_order: tuple[str, ...] = ()
        self._detail_key = ""

        self._detail_panel = SurfacePanel(body, padding=16)
        self._detail_panel.pack(fill="x")
        _section_title(self._detail_panel.body, "会话详情")
        head = tk.Frame(self._detail_panel.body, bg=LIGHT.surface)
        head.pack(fill="x")
        head.columnconfigure(0, weight=1)
        self._detail_title = BoundedLabel(
            head, text="选择一个会话查看详情", width=1, height=1,
            bg=LIGHT.surface, fg=LIGHT.text,
            font=pick_font(head, 12, True), anchor="w")
        self._detail_title.grid(row=0, column=0, sticky="ew")
        self._detail_chip = StatusChip(head, "—")
        self._detail_chip.grid(row=0, column=1, padx=(12, 0))
        actions = tk.Frame(self._detail_panel.body, bg=LIGHT.surface)
        actions.pack(fill="x", pady=(12, 8))
        self._activate_btn = ttk.Button(
            actions, text="打开终端", width=12,
            style="Dashboard.Primary.TButton", state="disabled",
            command=self._activate_selected)
        self._activate_btn.pack(side="left")
        self._include_btn = ttk.Button(
            actions, text="在桌宠中隐藏", width=16, state="disabled",
            command=self._toggle_include)
        self._include_btn.pack(side="left", padx=(8, 0))
        InfoButton(actions, "打开操作只恢复并前置所属窗口；不会切换终端标签、"
                   "发送输入或改变桌宠当前关注的 Agent。",
                   self.dash.tooltip).pack(side="left", padx=(8, 0))
        self._fields_frame = tk.Frame(self._detail_panel.body,
                                      bg=LIGHT.surface)
        self._fields_frame.pack(fill="x")
        self._fields = {}
        self._fields_frame.columnconfigure(1, weight=1)
        for index, text in enumerate(("目标", "当前活动", "等待内容", "模式 / 阶段",
                                      "环境", "项目路径", "所属窗口")):
            tk.Label(self._fields_frame, text=text, width=12, anchor="nw",
                     bg=LIGHT.surface, fg=LIGHT.text_secondary,
                     font=pick_font(parent, 9)).grid(
                         row=index, column=0, sticky="nw", pady=5)
            value = BoundedLabel(self._fields_frame, text="—",
                             max_lines=2 if index in (0, 1, 2, 5) else 1,
                             anchor="nw", justify="left", bg=LIGHT.surface,
                             fg=LIGHT.text, font=pick_font(parent, 10))
            value.grid(row=index, column=1, sticky="ew", pady=5)
            self._fields[text] = value
        self._advanced = Expander(self._detail_panel.body, "连接与解析信息")
        self._advanced.pack(fill="x", pady=(12, 0))
        self._advanced_label = tk.Label(
            self._advanced.body, text="未选择会话", width=1,
            bg=LIGHT.surface, fg=LIGHT.text_secondary,
            font=pick_font(parent, 9, mono=True), justify="left", anchor="nw")
        self._advanced_label.pack(fill="x")
        bind_wraplength(self._advanced_label)

    def _set_filter(self, index):
        self._filter = index
        self.dash.app.ui.request(UiDirty.DASHBOARD)

    def _matches(self, target):
        status = target.snapshot.status
        return (self._filter == 0
                or self._filter == 1 and status in (
                    Status.WAITING, Status.INPUT, Status.ERROR)
                or self._filter == 2 and status is Status.WORKING
                or self._filter == 3 and status in (Status.IDLE, Status.DONE))

    def _activate_selected(self):
        if self._detail_key and self.dash.app.monitor.is_live_key(self._detail_key):
            self.dash.app.activate_agent(self._detail_key)

    def _toggle_include(self):
        if not self._detail_key:
            return
        app = self.dash.app
        current = app.presentation.instance_included(self._detail_key)
        app.presentation.set_instance_included(self._detail_key, current is False)
        app.ui.request(UiDirty.PRESENTATION)

    def select(self, key):
        if key != self._detail_key:
            self._detail_key = key
            self.dash.app.ui.request(UiDirty.DASHBOARD)

    def _create_row(self, key):
        frame = tk.Frame(self._list_frame, height=self.dash.metrics.px(66),
                         bg=LIGHT.surface, cursor="hand2",
                         highlightthickness=1, highlightbackground=LIGHT.border)
        frame.grid_propagate(False)
        frame.columnconfigure(0, weight=1)
        title = BoundedLabel(frame, text="", anchor="w",
                         bg=LIGHT.surface, fg=LIGHT.text,
                         font=pick_font(frame, 10, True))
        title.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 0))
        meta = BoundedLabel(frame, text="", anchor="w",
                        bg=LIGHT.surface, fg=LIGHT.text_secondary,
                        font=pick_font(frame, 9))
        meta.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 8))
        chip = StatusChip(frame)
        chip.grid(row=0, column=1, rowspan=2, padx=10)
        open_btn = ttk.Button(frame, text="打开终端", width=10,
                              style="Dashboard.TButton",
                              command=lambda k=key: self.dash.app.activate_agent(k))
        open_btn.grid(row=0, column=2, rowspan=2, padx=(0, 12))
        for widget in (frame, title, meta, chip):
            widget.bind("<Button-1>", lambda _e, k=key: self.select(k))
        title.configure(takefocus=True)
        title.bind("<Return>", lambda _e, k=key: self.select(k))
        title.bind("<space>", lambda _e, k=key: self.select(k))
        self._rows[key] = {"frame": frame, "title": title, "meta": meta,
                           "chip": chip, "open_btn": open_btn}

    def refresh(self, reason: UiDirty):
        targets = self.dash.app.monitor.get_targets()
        counts = {
            "live": len(targets),
            "working": sum(t.snapshot.status is Status.WORKING
                           for t in targets.values()),
            "attention": sum(t.snapshot.status in (
                Status.WAITING, Status.INPUT, Status.ERROR)
                for t in targets.values()),
        }
        for key, value in counts.items():
            _configure_changed(self._metrics[key], text=str(value))
        for key in tuple(self._rows):
            if key not in targets:
                self._rows.pop(key)["frame"].destroy()
        for key in sorted(targets):
            if key not in self._rows:
                self._create_row(key)
        visible = tuple(key for key in sorted(targets) if self._matches(targets[key]))
        _configure_changed(self._list_count,
                           text=f"显示 {len(visible)} / 在线 {len(targets)}")
        if visible != self._row_order:
            for row in self._rows.values():
                _pack_forget_once(row["frame"])
            for key in visible:
                self._rows[key]["frame"].pack(fill="x", pady=(0, 6))
            self._row_order = visible
        if visible:
            _pack_forget_once(self._empty)
        else:
            _configure_changed(self._empty_title,
                               text="暂无在线 Agent" if not targets
                               else "没有符合筛选条件的会话")
            _pack_once(self._empty, fill="x")
        if self._detail_key not in visible:
            self._detail_key = visible[0] if visible else ""
        for key in visible:
            target = targets[key]
            row = self._rows[key]
            _configure_changed(row["title"], text=_ellipsize(
                f"{target.snapshot.kind.label} · {workspace_name(target)}", 80))
            _configure_changed(row["meta"], text=_ellipsize(
                f"{environment_label(target.instance)}  ·  "
                f"{activity_text(target.snapshot)}", 100))
            row["chip"].set(status_text(target.snapshot), STATUS_COLOR.get(
                target.snapshot.status.value, LIGHT.unknown))
            _configure_changed(row["open_btn"], text=_activation_label(target.instance))
            selected = key == self._detail_key
            bg = LIGHT.accent_soft if selected else LIGHT.surface
            _configure_changed(row["frame"], bg=bg, highlightbackground=(
                LIGHT.accent if selected else LIGHT.border))
            for part in ("title", "meta"):
                _configure_changed(row[part], bg=bg)
        self._refresh_detail(targets.get(self._detail_key))

    def _refresh_detail(self, target):
        if target is None:
            _configure_changed(self._detail_title, text="选择一个会话查看详情")
            self._detail_chip.set("—", LIGHT.unknown)
            for label in self._fields.values():
                _configure_changed(label, text="—")
            _configure_changed(self._advanced_label, text="未选择会话")
            self._activate_btn.state(["disabled"])
            self._include_btn.state(["disabled"])
            return
        snap, inst, binding = target.snapshot, target.instance, target.terminal_window
        _configure_changed(self._detail_title, text=_ellipsize(
            f"{snap.kind.label} · {workspace_name(target)}", 80))
        self._detail_chip.set(status_text(snap), STATUS_COLOR.get(
            snap.status.value, LIGHT.unknown))
        self._activate_btn.state(["!disabled"])
        self._include_btn.state(["!disabled"])
        _configure_changed(self._activate_btn, text=_activation_label(inst))
        included = self.dash.app.presentation.instance_included(self._detail_key)
        _configure_changed(self._include_btn,
                           text="在桌宠中隐藏" if included is not False
                           else "在桌宠中显示")
        fields = {
            "目标": summarize_text(snap.goal or snap.title, 300) or "—",
            "当前活动": activity_text(snap, limit=300),
            "等待内容": summarize_text(snap.waiting_detail, 300) or "—",
            "模式 / 阶段": " / ".join(x for x in (
                mode_text(snap), phase_text(snap)) if x) or "—",
            "环境": environment_label(inst), "项目路径": inst.cwd or "—",
            "所属窗口": "可打开" if binding is not None and binding.wakeable else "未定位",
        }
        for key, text in fields.items():
            _configure_changed(self._fields[key], text=_ellipsize(text, 300))
        detail = [f"kind={snap.kind.value}", f"source={inst.source}",
                  f"pid={inst.pid}", f"process_token={inst.process_token}",
                  f"started_at={inst.started_at:.0f}"]
        if binding is not None and binding.window is not None:
            detail.extend((f"hwnd={binding.window.hwnd}",
                           f"binding={_binding_conf_value(binding)}"))
        detail.append(f"parser={snap.parser_health or 'UNKNOWN'}"
                      + (f" ({snap.parser_detail})" if snap.parser_detail else ""))
        _configure_changed(self._advanced_label, text="\n".join(detail))


class PetsPage(DashboardPage):
    """§12.3 桌宠页：并发显示 + 当前展示（retained slot cards）。"""

    name = PAGE_PETS

    def build(self, parent):
        self.dash.page_header(parent, PAGE_PETS)
        body = self.dash.page_body(parent)
        from .presentation import PresentationMode
        self._mode_enum = PresentationMode
        app = self.dash.app
        # ---- Section A 并发显示
        panel_a = SurfacePanel(body)
        panel_a.pack(fill="x", pady=(0, SECTION_GAP))
        _section_title(panel_a.body, "并发显示")
        r1 = SettingRow(panel_a.body, "同时展示多个 Agent",
                        "多宠并发不会增加每 Agent 线程：全部桌宠共享同一"
                        "个 Monitor/调度器/动画缓存。",
                        self.dash.tooltip)
        self.concurrent_var = tk.BooleanVar(
            value=app.presentation.concurrent_enabled)
        self.concurrent_toggle = ttk.Checkbutton(
            r1._control_cell, variable=self.concurrent_var,
            command=self._toggle_concurrent)
        r1.set_control(self.concurrent_toggle)
        r1.reflow("wide")
        r1.pack(fill="x")
        r2 = SettingRow(panel_a.body, "展示方式", "", self.dash.tooltip)
        from .widgets import SegmentedControl
        self.mode_segment = SegmentedControl(
            r2._control_cell, ["单宠聚合", "多宠分离"],
            command=self._on_mode_segment)
        r2.set_control(self.mode_segment)
        r2.reflow("wide")
        r2.pack(fill="x")
        self.mode_segment.select(1 if app.presentation.mode
                                 is PresentationMode.FLEET else 0)
        tk.Label(panel_a.body,
                 text="并发开关和展示方式仅对本次运行有效；下次启动恢复"
                      "“并行监听 + 单宠聚合”",
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(panel_a, 9), anchor="w").pack(
            fill="x", pady=(4, 0))
        r3 = SettingRow(panel_a.body, "展示上限", "最多同时显示的桌宠数"
                        "（1..8）；slot 配置持久化，绑定只在本运行期。",
                        self.dash.tooltip)
        self.max_stepper = Stepper(
            r3._control_cell,
            int(app.config.get("presentation.concurrent.max_targets", 3)
                or 3), 1, 8, command=self._save_max_targets)
        r3.set_control(self.max_stepper)
        r3.reflow("wide")
        r3.pack(fill="x")
        r4 = SettingRow(panel_a.body, "参与展示",
                        "勾选的 Agent 类型才会参与并发展示（发现监听仍"
                        "继续）。", self.dash.tooltip)
        grid = tk.Frame(r4._control_cell, bg=LIGHT.surface)
        grid.pack(fill="x")
        self.eligible_vars = {}
        for i, (kind, label) in enumerate(
                (("codex", "Codex"), ("claude", "Claude"),
                 ("kimi", "Kimi"), ("pi", "pi"),
                 ("zcode", "ZCode"), ("opencode", "OpenCode"),
                 ("dsh", "DSH"))):
            var = tk.BooleanVar(value=bool(app.config.get(
                f"presentation.concurrent.eligible_kinds.{kind}", True)))
            self.eligible_vars[kind] = var
            ttk.Checkbutton(grid, text=label, variable=var,
                            command=lambda k=kind: self._save_eligible(
                                k)).grid(row=i // 2, column=i % 2,
                                         sticky="w", padx=(0, 24),
                                         pady=2)
        r4.set_control(grid)
        r4.reflow("wide")
        r4.pack(fill="x")
        # ---- Section B 当前展示
        panel_b = SurfacePanel(body)
        panel_b.pack(fill="x")
        _section_title(panel_b.body, "当前展示")
        self.summary_label = tk.Label(panel_b.body, text="", bg=LIGHT.surface,
                                      fg=LIGHT.text,
                                      font=pick_font(panel_b, 10), anchor="w")
        self.summary_label.pack(fill="x", pady=(0, ROW_GAP))
        self.fallback_label = tk.Label(
            panel_b.body, text="", bg=LIGHT.surface,
            fg=LIGHT.text_secondary, font=pick_font(panel_b, 9), anchor="w")
        self.cards_frame = tk.Frame(panel_b.body, bg=LIGHT.surface)
        self.cards_frame.pack(fill="x")
        self._slot_cards: dict[str, dict] = {}
        self._slot_order: tuple[str, ...] = ()
        self._card_cols = 1
        self._rows: list[SettingRow] = [r1, r2, r3, r4]

    # ------------------------------------------------------------ actions
    def _toggle_concurrent(self):
        app = self.dash.app
        enabled = self.concurrent_var.get()
        app.presentation.set_concurrent_enabled(enabled)
        app.toast("并发监听已" + ("开启" if enabled else "关闭")
                  + "（仅本次运行有效）", 4)
        app.ui.request(UiDirty.PRESENTATION)

    def _on_mode_segment(self, index: int):
        app = self.dash.app
        mode = (PresentationMode.FLEET if index == 1
                else PresentationMode.AGGREGATE)
        if mode is PresentationMode.FLEET:
            slots = list(app.config.get(
                "presentation.concurrent.slots") or [])
            if len(slots) < 2:
                app.config.ensure_fleet_slots(3)
                app.config_saver.request_save()
        app.presentation.set_concurrent_mode(mode)
        app.ui.request(UiDirty.PRESENTATION)

    def _save_max_targets(self, value: int):
        app = self.dash.app
        slots_changed = app.config.ensure_fleet_slots(value)
        value_changed = app.config.set(
            "presentation.concurrent.max_targets", value)
        if slots_changed or value_changed is not False:
            app.config_saver.request_save()
        app.ui.request(UiDirty.PRESENTATION)

    def _save_eligible(self, kind: str):
        app = self.dash.app
        changed = app.config.set(
            f"presentation.concurrent.eligible_kinds.{kind}",
            bool(self.eligible_vars[kind].get()))
        if changed is not False:
            app.config_saver.request_save()
        app.ui.request(UiDirty.PRESENTATION)

    def _unbind_slot(self, slot_id: str):
        app = self.dash.app
        key = app.presentation.slot_binding(slot_id)
        app.presentation.unbind_slot(slot_id)
        if key:
            app.presentation.set_instance_included(key, False)
        app.ui.request(UiDirty.PRESENTATION)

    # ------------------------------------------------------------ refresh
    def refresh(self, reason: UiDirty):
        app = self.dash.app
        state = app._presentation_state
        if state is None:
            return
        self.concurrent_var.set(app.presentation.concurrent_enabled)
        target_index = (1 if app.presentation.mode is PresentationMode.FLEET
                        else 0)
        if self.mode_segment.selected() != target_index:
            initialized = self.mode_segment._initialized
            self.mode_segment._initialized = False
            self.mode_segment.select(target_index)
            self.mode_segment._initialized = initialized
        max_targets = int(app.config.get(
            "presentation.concurrent.max_targets", 3) or 3)
        self.max_stepper.set_external(max_targets)
        for kind, var in self.eligible_vars.items():
            var.set(bool(app.config.get(
                f"presentation.concurrent.eligible_kinds.{kind}", True)))
        if state.mode is PresentationMode.FLEET:
            bound = len(state.slot_keys)
            summary = (f"{bound or 1} 只桌宠 · Fleet 模式"
                       + (f" · 绑定 {bound}/{max_targets}"
                          if bound else ""))
            overflow = max(0, len(state.cards) - max_targets)
            if overflow:
                summary += f" · overflow {overflow}"
            _configure_changed(self.summary_label, text=summary)
            if not state.slot_keys:
                _configure_changed(self.fallback_label,
                    text="1 只 idle fallback 桌宠（pet-1）：不占用 Agent "
                         "slot，Agent 出现后自动复用/替换。")
                _pack_once(self.fallback_label, fill="x",
                           pady=(0, ROW_GAP))
            else:
                _pack_forget_once(self.fallback_label)
            self._refresh_slot_cards(state, max_targets)
        else:
            if (app.presentation.concurrent_enabled
                    and state.mode is PresentationMode.AGGREGATE):
                overflow = max(0, len(state.cards) - max_targets)
                text = (f"1 只桌宠 / 最多 {max_targets} 张卡"
                        + (f" / overflow {overflow}" if overflow else ""))
                if not state.cards:
                    text = "1 只桌宠 · 暂无 Agent"
            elif not app.presentation.concurrent_enabled:
                text = "1 只桌宠 · 单目标模式"
            else:
                text = "1 只桌宠"
            _configure_changed(self.summary_label, text=text)
            _pack_forget_once(self.fallback_label)
            self._clear_slot_cards()

    def _clear_slot_cards(self):
        for card in self._slot_cards.values():
            card["panel"].destroy()
        self._slot_cards.clear()
        self._slot_order = ()

    def _refresh_slot_cards(self, state, max_targets: int):
        app = self.dash.app
        slot_ids = app.presentation.slot_ids()[:max(max_targets, 1)]
        layout_changed = tuple(slot_ids) != self._slot_order
        for slot_id in list(self._slot_cards):
            if slot_id not in slot_ids:
                self._slot_cards.pop(slot_id)["panel"].destroy()
        targets = app.monitor.get_targets()
        for index, slot_id in enumerate(slot_ids, start=1):
            card = self._slot_cards.get(slot_id)
            if card is None:
                card = self._create_slot_card(slot_id, index)
            key = state.slot_keys.get(slot_id, "")
            target = targets.get(key) if key else None
            # header chips
            if target is not None:
                agent = (f"{target.snapshot.kind.label} · "
                         f"{target.instance.project or ''}")
                chip = ("自动分配" if app.presentation.is_auto_bound(slot_id)
                        else "手动绑定")
                chip_color = LIGHT.done
            elif key:
                agent = "绑定的 Agent 已退出"
                chip = "vacant"
                chip_color = LIGHT.unknown
            else:
                agent = "未绑定"
                chip = "空闲"
                chip_color = LIGHT.unknown
            if card["agent"]["text"] != agent:
                card["agent"].configure(text=agent)
            if card["chip"]["text"] != chip:
                card["chip"].set(chip, chip_color)
            # skin 状态
            view = app.pet_manager.views.get(slot_id)
            if view is not None:
                build_state = view.skin_build_state
                skin_text = {"ready": "已就绪", "queued": "排队中",
                             "building": "构建中", "error": "构建失败",
                             "fallback": "缺失（已回退内置猫）"}.get(
                                build_state, build_state)
                if view.skin_build_error and build_state in (
                        "error", "fallback"):
                    skin_text += f" · {view.skin_build_error[:40]}"
            else:
                skin_text = "—"
            if card["skin_state"]["text"] != skin_text:
                card["skin_state"].configure(text=skin_text)
            # combobox 当前值
            override = app.pet_manager.slot_skin_overridden(slot_id)
            current = (view.skin_requested_name
                       if view is not None and override else
                       "跟随全局")
            if card["skin_var"].get() != current:
                card["skin_var"].set(current)
        self._slot_order = tuple(slot_ids)
        if layout_changed:
            self._arrange_slot_cards()

    def _create_slot_card(self, slot_id: str, index: int) -> dict:
        app = self.dash.app
        panel = SurfacePanel(self.cards_frame)
        head = tk.Frame(panel.body, bg=LIGHT.surface)
        head.pack(fill="x")
        tk.Label(head, text=f"桌宠 {index}", bg=LIGHT.surface,
                 fg=LIGHT.text, font=pick_font(head, 10, True)).pack(
            side="left")
        chip = StatusChip(head)
        chip.pack(side="left", padx=(8, 0))
        ttk.Button(head, text="更换 Agent", width=12,
                   command=lambda s=slot_id: app._open_agent_picker(
                       s)).pack(side="right")
        ttk.Button(head, text="解除绑定", width=10,
                   command=lambda s=slot_id: self._unbind_slot(
                       s)).pack(side="right", padx=(0, 8))
        agent = tk.Label(panel.body, text="", bg=LIGHT.surface,
                         fg=LIGHT.text_secondary,
                         font=pick_font(panel, 9), anchor="w")
        agent.pack(fill="x", pady=(4, 0))
        skin_row = tk.Frame(panel.body, bg=LIGHT.surface)
        skin_row.pack(fill="x", pady=(4, 0))
        tk.Label(skin_row, text="皮肤", bg=LIGHT.surface,
                 fg=LIGHT.text, font=pick_font(skin_row, 10)).pack(
            side="left")
        InfoButton(skin_row, "本桌宠独立皮肤；同一皮肤可被多个桌宠重复"
                   "选择。选择后立即请求构建，完成前保持当前画面。",
                   self.dash.tooltip).pack(side="left", padx=(6, 0))
        skin_var = tk.StringVar(value="跟随全局")
        values = ["跟随全局"] + sorted(skins.list_skins())
        combo = ttk.Combobox(skin_row, textvariable=skin_var, width=18,
                             values=values, state="readonly")
        combo.pack(side="left", padx=(6, 0))
        skin_state = tk.Label(skin_row, text="—", bg=LIGHT.surface,
                              fg=LIGHT.text_secondary,
                              font=pick_font(skin_row, 9))
        skin_state.pack(side="left", padx=(6, 0))

        def _on_select(_evt=None, s=slot_id, v=skin_var):
            choice = v.get()
            app.appearance.set_slot_skin(
                s, None if choice == "跟随全局" else choice)

        combo.bind("<<ComboboxSelected>>", _on_select)
        footer = tk.Frame(panel.body, bg=LIGHT.surface)
        footer.pack(fill="x")
        reset_btn = ttk.Button(footer, text="恢复跟随全局",
                               command=lambda s=slot_id:
                               app.appearance.set_slot_skin(s, None))
        reset_btn.pack(side="right")
        card = {"panel": panel, "chip": chip, "agent": agent,
                "skin_var": skin_var, "skin_combo": combo,
                "skin_state": skin_state, "reset_btn": reset_btn}
        self._slot_cards[slot_id] = card
        return card

    def _arrange_slot_cards(self):
        """Arrange retained cards only when the width mode changes."""
        if not self._slot_cards:
            return
        panels = [card["panel"] for card in self._slot_cards.values()]
        for panel in panels:
            panel.pack_forget()
            panel.grid_forget()
        for column in range(2):
            self.cards_frame.grid_columnconfigure(
                column, weight=1 if column < self._card_cols else 0)
        if self._card_cols == 2:
            for index, panel in enumerate(panels):
                panel.grid(row=index // 2, column=index % 2,
                           sticky="nsew",
                           padx=(0 if index % 2 == 0 else 6,
                                 6 if index % 2 == 0 else 0), pady=4)
        else:
            for panel in panels:
                panel.pack(fill="x", pady=4)

    def reflow(self, width: int):
        for row in getattr(self, "_rows", ()):
            row.reflow(SettingRow.mode_for_width(width, row._mode))
        cols = 2 if width >= 820 else 1
        if cols != self._card_cols:
            self._card_cols = cols
            self._arrange_slot_cards()


class AppearancePage(DashboardPage):
    """§12.4 外观页：皮肤 / 桌宠 / 气泡；无 Apply 按钮，live apply。"""

    name = PAGE_LOOK

    def build(self, parent):
        header = self.dash.page_header(parent, PAGE_LOOK)
        self.save_status = tk.Label(header.frame, text="已自动保存",
                                    bg=LIGHT.page, fg=LIGHT.text_secondary,
                                    font=pick_font(header.frame, 9))
        self.save_status.pack(side="right", padx=(0, 12))
        header.add_action("重置全部外观", self._confirm_reset, primary=True)
        body = self.dash.page_body(parent)
        app = self.dash.app
        cfg = app.config
        self._rows: list[SettingRow] = []

        def row(parent_panel, label, info=""):
            r = SettingRow(parent_panel, label, info, self.dash.tooltip)
            r.pack(fill="x")
            self._rows.append(r)
            return r

        # ---- Section A 皮肤
        panel_skin = SurfacePanel(body)
        panel_skin.pack(fill="x", pady=(0, SECTION_GAP))
        _section_title(panel_skin.body, "皮肤")
        r = row(panel_skin.body, "全局皮肤", "所有未单独设置皮肤的桌宠"
                "使用此皮肤；选择立即生效，构建完成前保持当前画面。")
        self.skin_var = tk.StringVar(
            value=str(cfg.get("skin", skins.BUILTIN_SKIN)))
        self.skin_combo = ttk.Combobox(
            r._control_cell, textvariable=self.skin_var,
            values=sorted(skins.list_skins()), state="readonly")
        self.skin_combo.pack(fill="x")
        self.skin_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: app.appearance.set_global(
                "skin", self.skin_var.get()))
        r.reflow("wide")
        ir = row(panel_skin.body, "导入", "选择包含 5 段素材的文件夹；"
                 "复制/校验/构建全部在后台进行，不阻塞界面。")
        ttk.Button(ir._control_cell, text="导入皮肤…", width=12,
                   command=self._import_skin).pack(side="left")
        self.skin_build_state = tk.Label(
            ir._value_cell, text="", bg=LIGHT.surface,
            fg=LIGHT.text_secondary, font=pick_font(ir._value_cell, 9))
        self.skin_build_state.pack()
        ir.reflow("wide")
        self._skin_catalog_revision = skins.catalog_revision()
        # ---- Section B 桌宠
        panel_pet = SurfacePanel(body)
        panel_pet.pack(fill="x", pady=(0, SECTION_GAP))
        _section_title(panel_pet.body, "桌宠")
        r = row(panel_pet.body, "整体大小")
        self.scale_slider = DiscreteSlider(
            r._control_cell, SCALE_STEPS,
            command=lambda v: app.appearance.set_global("scale", v))
        r.set_control(self.scale_slider)
        r.reflow("wide")
        r = row(panel_pet.body, "动画速度")
        self.speed_slider = DiscreteSlider(
            r._control_cell, SPEED_STEPS,
            command=lambda v: app.appearance.set_global("speed", v))
        r.set_control(self.speed_slider)
        r.reflow("wide")
        r = row(panel_pet.body, "播放动画", "关闭后显示第 0 帧静态图。")
        self.animated_var = tk.BooleanVar(
            value=bool(cfg.get("animated", True)))
        ttk.Checkbutton(r._control_cell, variable=self.animated_var,
                        command=lambda: app.appearance.set_global(
                            "animated", self.animated_var.get())
                        ).pack(side="left")
        r.reflow("wide")
        r = row(panel_pet.body, "动画状态", "锁定后不随监听状态切换"
                "（walk/attack/die/special/sleep）。")
        self.force_var = tk.StringVar(
            value=str(cfg.get("force_state") or "") or "自动")
        combo = ttk.Combobox(
            r._control_cell, textvariable=self.force_var, width=12,
            values=["自动", "walk", "attack", "die", "special", "sleep"],
            state="readonly")
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>",
                   lambda _e: app.appearance.set_global(
                       "force_state",
                       "" if self.force_var.get() == "自动"
                       else self.force_var.get()))
        r.reflow("wide")
        # ---- Section C 气泡
        panel_bub = SurfacePanel(body)
        panel_bub.pack(fill="x")
        _section_title(panel_bub.body, "气泡")
        r = row(panel_bub.body, "显示气泡", "关闭后桌宠仍显示动画。")
        self.bubble_var = tk.BooleanVar(
            value=bool(cfg.get("bubble.enabled", True)))
        ttk.Checkbutton(r._control_cell, variable=self.bubble_var,
                        command=lambda: app.appearance.set_global(
                            "bubble.enabled", self.bubble_var.get())
                        ).pack(side="left")
        r.reflow("wide")
        r = row(panel_bub.body, "工作详情", "开启显示精简命令或问题，关闭仅显示当前状态。")
        self.bubble_details_var = tk.BooleanVar(value=bool(cfg.get("bubble.show_details", True)))
        ttk.Checkbutton(r._control_cell, variable=self.bubble_details_var,
                        command=lambda: app.appearance.set_global(
                            "bubble.show_details", self.bubble_details_var.get())).pack(side="left")
        r.reflow("wide")
        r = row(panel_bub.body, "气泡宽度")
        self.bw_slider = DiscreteSlider(
            r._control_cell, BUBBLE_W_STEPS,
            command=lambda v: app.appearance.set_global(
                "bubble.relative_width", v))
        r.set_control(self.bw_slider)
        r.reflow("wide")
        r = row(panel_bub.body, "气泡高度")
        self.bh_slider = DiscreteSlider(
            r._control_cell, BUBBLE_H_STEPS,
            command=lambda v: app.appearance.set_global(
                "bubble.relative_height", v))
        r.set_control(self.bh_slider)
        r.reflow("wide")
        r = row(panel_bub.body, "文字缩放")
        self.bf_slider = DiscreteSlider(
            r._control_cell, BUBBLE_FONT_STEPS,
            command=lambda v: app.appearance.set_global(
                "bubble.relative_font", v))
        r.set_control(self.bf_slider)
        r.reflow("wide")
        r = row(panel_bub.body, "字体")
        from .theme import _available_families
        families = ["Microsoft YaHei UI", "Segoe UI", "SimHei", "Consolas"]
        available = sorted(f for f in _available_families(self.dash)
                           if f in set(families)) or families[:1]
        self.font_var = tk.StringVar(
            value=str(cfg.get("bubble.font_family",
                              "Microsoft YaHei UI")))
        # DP43-R11：字体下拉必须写回 AppearanceController——旧实现只改
        # UI variable，Config/桌宠/保存全部不动（死选择器）。程序化
        # font_var.set() 不触发 <<ComboboxSelected>>，sync 无副作用。
        self.font_combo = ttk.Combobox(
            r._control_cell, textvariable=self.font_var,
            values=available, width=22, state="readonly")
        self.font_combo.pack(side="left")
        self.font_combo.bind(
            "<<ComboboxSelected>>",
            lambda _e: app.appearance.set_global(
                "bubble.font_family", self.font_var.get()))
        r.reflow("wide")
        r = row(panel_bub.body, "字号")
        self.size_stepper = Stepper(
            r._control_cell, int(cfg.get("bubble.font_size", 11) or 11),
            8, 24, command=lambda v: app.appearance.set_global(
                "bubble.font_size", v), width_chars=4)
        r.set_control(self.size_stepper)
        r.reflow("wide")
        self._sync_from_config(initial=True)

    # ------------------------------------------------------------ actions
    def _confirm_reset(self):
        app = self.dash.app
        # DP43-R16：native dialog 用标准 Tk parent 关系即可——已无
        # auto-collapse，也就不需要任何 suppress 标志
        ok = self.dash.run_dialog(messagebox.askyesno,
            "重置全部外观",
            "恢复默认皮肤（内置猫）与全部视觉参数；不影响监听、隐私、"
            "slot 绑定与摆放。确定重置？",
            parent=self.dash)
        if ok:
            app.appearance.reset_all()

    def _import_skin(self):
        dash = self.dash
        src = dash.run_dialog(filedialog.askdirectory,
            title="选择包含 5 个素材文件的文件夹", parent=dash)
        if not src:
            return
        import re
        default = re.split(r"[\\/]+", src.rstrip("/\\"))[-1] or "myskin"
        name = default.strip() or "myskin"
        dash.app.begin_skin_import(src, name)
        self.skin_var.set(name)

    # ------------------------------------------------------------ refresh
    def on_show(self):
        pass   # 字体枚举已由 theme 全局缓存（首次调用发生在页面 build）

    def refresh(self, reason: UiDirty):
        self._sync_from_config()
        self._refresh_skin_state()
        saver = self.dash.app.config_saver
        if saver.pending():
            text, color = "正在保存…", LIGHT.waiting
        else:
            last = saver.last_result or getattr(
                self.dash.app.config, "last_save_result", None)
            if last is not None and getattr(last, "ok", True) is False:
                text, color = "保存失败（设置页可重试）", LIGHT.error
            else:
                text, color = "已自动保存", LIGHT.text_secondary
        _configure_changed(self.save_status, text=text, fg=color)

    def _refresh_skin_state(self):
        revision = skins.catalog_revision()
        if revision != self._skin_catalog_revision:
            self._skin_catalog_revision = revision
            self.skin_combo.configure(values=sorted(skins.list_skins()))
        app = self.dash.app
        state_text = ""
        for view in app.pet_manager.views.values():
            s = view.skin_build_state
            if s in ("queued", "building", "error"):
                state_text = {"queued": "排队构建中…",
                              "building": "构建中…",
                              "error": view.skin_build_error[:40] or
                              "构建失败"}.get(s, s)
                break
        if self.skin_build_state["text"] != state_text:
            self.skin_build_state.configure(text=state_text)

    def _sync_from_config(self, initial: bool = False):
        cfg = self.dash.app.config
        self.scale_slider.set_external(float(cfg.get("scale", 1.0) or 1.0))
        self.speed_slider.set_external(float(cfg.get("speed", 1.0) or 1.0))
        self.bw_slider.set_external(float(
            cfg.get("bubble.relative_width", 1.0) or 1.0))
        self.bh_slider.set_external(float(
            cfg.get("bubble.relative_height", 1.0) or 1.0))
        self.bf_slider.set_external(float(
            cfg.get("bubble.relative_font", 1.0) or 1.0))
        self.size_stepper.set_external(int(
            cfg.get("bubble.font_size", 11) or 11))
        self.animated_var.set(bool(cfg.get("animated", True)))
        self.bubble_var.set(bool(cfg.get("bubble.enabled", True)))
        self.bubble_details_var.set(bool(cfg.get("bubble.show_details", True)))
        self.force_var.set(str(cfg.get("force_state") or "") or "自动")
        self.font_var.set(str(cfg.get("bubble.font_family",
                                      "Microsoft YaHei UI")))
        skin = str(cfg.get("skin", skins.BUILTIN_SKIN))
        if self.skin_var.get() != skin:
            self.skin_var.set(skin)

    def reflow(self, width: int):
        for r in self._rows:
            r.reflow(SettingRow.mode_for_width(width, r._mode))


class MonitorPage(DashboardPage):
    """§12.5 监听与隐私：来源/类型/终端观察/隐私/高级节奏（折叠）。"""

    name = PAGE_MONITOR

    def build(self, parent):
        self.dash.page_header(parent, PAGE_MONITOR)
        body = self.dash.page_body(parent)
        app = self.dash.app
        cfg = app.config

        def save(path, value):
            if cfg.set(path, value) is not False:
                app.config_saver.request_save()

        # A 监听来源
        panel = SurfacePanel(body)
        panel.pack(fill="x", pady=(0, SECTION_GAP))
        _section_title(panel.body, "监听来源")
        r = SettingRow(panel.body, "来源",
                       "Windows=原生进程（psutil 枚举）；WSL=各发行版内"
                       "只读 /proc。关闭立即停止发现。", self.dash.tooltip)
        cell = tk.Frame(r._control_cell)
        cell.pack(fill="x")
        self.windows_var = tk.BooleanVar(
            value=bool(cfg.get("monitor.windows_enabled", True)))
        ttk.Checkbutton(cell, text="Windows",
                        variable=self.windows_var,
                        command=lambda: save("monitor.windows_enabled",
                                             self.windows_var.get())
                        ).pack(side="left", padx=(0, 16))
        self.wsl_var = tk.BooleanVar(
            value=bool(cfg.get("monitor.wsl_enabled", True)))
        ttk.Checkbutton(cell, text="WSL", variable=self.wsl_var,
                        command=lambda: save("monitor.wsl_enabled",
                                             self.wsl_var.get())
                        ).pack(side="left")
        r.reflow("wide")
        r.pack(fill="x")
        # B Agent 类型
        panel = SurfacePanel(body)
        panel.pack(fill="x", pady=(0, SECTION_GAP))
        _section_title(panel.body, "Agent 类型")
        r = SettingRow(panel.body, "参与发现解析",
                       "勾选的 Agent 类型才会被扫描/解析；取消立即停止"
                       "发现（现有实例按退出清理）。", self.dash.tooltip)
        grid = tk.Frame(r._control_cell)
        grid.pack(fill="x")
        self.kind_vars = {}
        for i, (kind, label) in enumerate(
                (("codex", "Codex"), ("claude", "Claude"),
                 ("kimi", "Kimi"), ("pi", "pi"),
                 ("zcode", "ZCode"), ("opencode", "OpenCode"),
                 ("dsh", "DSH"))):
            var = tk.BooleanVar(
                value=bool(cfg.get(f"monitor.agents.{kind}", True)))
            self.kind_vars[kind] = var
            ttk.Checkbutton(grid, text=label, variable=var,
                            command=lambda k=kind: save(
                                f"monitor.agents.{k}",
                                self.kind_vars[k].get())).grid(
                row=i // 2, column=i % 2, sticky="w", padx=(0, 24), pady=2)
        r.reflow("wide")
        r.pack(fill="x")
        # C 监听目标路径（数据根）
        panel = SurfacePanel(body)
        panel.pack(fill="x", pady=(0, SECTION_GAP))
        _section_title(panel.body, "监听目标路径（数据根）")
        tk.Label(panel.body, text="各 Agent 的会话数据根。留空使用默认"
                 "（环境变量 → 内置路径）；填入或选择自定义目录后立即"
                 "生效并重新扫描。自定义只影响 Windows 监听，WSL 始终跟随"
                 "各发行版内进程的真实 HOME。路径仅保存在本机用户配置"
                 "（%LOCALAPPDATA% 下），不写入其他任何位置。",
                 bg=LIGHT.surface, fg=LIGHT.text_secondary, justify="left",
                 wraplength=560,
                 font=pick_font(panel, 9)).pack(fill="x")
        from agents import paths as agent_paths

        def _apply_roots():
            agent_paths.set_data_root_overrides(
                cfg.get("monitor.data_roots") or {})
            app.monitor.rescan()

        self.data_root_vars: dict[str, tk.StringVar] = {}
        self.data_root_entries: dict[str, ttk.Entry] = {}
        self.data_root_savers: dict[str, object] = {}
        for key, label in (("codex", "Codex"), ("claude", "Claude"),
                           ("kimi", "Kimi"), ("pi", "pi"),
                           ("zcode", "ZCode"), ("opencode", "OpenCode"),
                           ("dsh", "DSH")):
            hint = agent_paths.default_data_root_hint(key)
            r = SettingRow(panel.body, label,
                           f"默认：{hint}" if hint else "", self.dash.tooltip)
            cell = tk.Frame(r._control_cell)
            cell.pack(fill="x")
            var = tk.StringVar(
                value=str(cfg.get(f"monitor.data_roots.{key}", "") or ""))
            self.data_root_vars[key] = var
            entry = ttk.Entry(cell, textvariable=var)
            entry.pack(side="left", fill="x", expand=True)
            self.data_root_entries[key] = entry

            def _save(key=key, var=var, label=label):
                value = var.get().strip()
                if value and not os.path.isabs(value):
                    app.toast("监听目标需要绝对路径，未保存")
                    return
                if cfg.set(f"monitor.data_roots.{key}", value) is False:
                    return
                app.config_saver.request_save()
                _apply_roots()
                app.toast(f"{label} 已恢复默认监听目标" if not value
                          else f"{label} 监听目标已更新")

            def _browse(key=key, var=var, label=label, _save=_save):
                initial = var.get().strip() or \
                    agent_paths.default_data_root_hint(key)
                chosen = filedialog.askdirectory(
                    initialdir=initial, title=f"选择 {label} 数据根")
                if chosen:
                    var.set(os.path.normpath(chosen))
                    _save()

            self.data_root_savers[key] = _save
            entry.bind("<Return>", lambda _e, _s=_save: _s())
            entry.bind("<FocusOut>", lambda _e, _s=_save: _s())
            ttk.Button(cell, text="浏览…", style="Dashboard.TButton",
                       command=lambda _f=_browse: _f(),
                       ).pack(side="left", padx=(8, 0))
            ttk.Button(cell, text="默认", style="Dashboard.TButton",
                       command=lambda _f=_save, _v=var: (_v.set(""), _f()),
                       ).pack(side="left", padx=(6, 0))
            r.reflow("wide")
            r.pack(fill="x")
        # C 终端观察
        panel = SurfacePanel(body)
        panel.pack(fill="x", pady=(0, SECTION_GAP))
        _section_title(panel.body, "终端观察")
        r = SettingRow(panel.body, "终端交互观察（UIA）",
                       "用系统官方 UI Automation 被动观察 Windows Terminal "
                       "当前可见区域（识别“等待审批”等）。只读可见文本、"
                       "有频率上限；不模拟键盘、不截图、不读 scrollback。"
                       "关闭需重启 DeskPet 生效。",
                       self.dash.tooltip)
        self.uia_var = tk.BooleanVar(
            value=bool(cfg.get("monitor.terminal_observer", True)))
        ttk.Checkbutton(r._control_cell, variable=self.uia_var,
                        command=lambda: save("monitor.terminal_observer",
                                             self.uia_var.get())
                        ).pack(side="left")
        r.reflow("wide")
        r.pack(fill="x")
        self.uia_state = tk.Label(panel.body, text="", bg=LIGHT.surface,
                                  fg=LIGHT.text_secondary,
                                  font=pick_font(panel, 9), anchor="w")
        self.uia_state.pack(fill="x", pady=(4, 0))
        # D 隐私
        panel = SurfacePanel(body)
        panel.pack(fill="x", pady=(0, SECTION_GAP))
        _section_title(panel.body, "隐私")
        tk.Label(panel.body, text="DeskPet 只做被动观察：读 cwd、进程启动"
                 " token、uid/HOME 与 allowlist 内环境变量；终端文本只做"
                 "字符串匹配归类，绝不写进磁盘/日志/配置；不启动 Agent、"
                 "不配置 hooks、不发送键盘、不自动审批。",
                 bg=LIGHT.surface, fg=LIGHT.text, justify="left",
                 wraplength=560,
                 font=pick_font(panel, 9)).pack(fill="x")
        expander = Expander(panel.body, "高级")
        expander.pack(fill="x", pady=(8, 0))
        erow = tk.Frame(expander.body, bg=LIGHT.surface)
        erow.pack(fill="x")
        self.root_meta_var = tk.BooleanVar(
            value=bool(cfg.get("privacy.wsl_root_metadata_fallback",
                               False)))

        def _set_root_metadata(flag):
            # DP43-R03：隐私开关运行期必须立即生效——
            # Config 内存 → runtime 权限（Monitor → probe）→ 异步持久化；
            # 磁盘保存失败也不回滚运行期隐私意图。
            changed = cfg.set(
                "privacy.wsl_root_metadata_fallback", bool(flag))
            if changed is False:
                return
            try:
                app.monitor.set_wsl_root_metadata_fallback(bool(flag))
            except Exception:
                pass
            app.config_saver.request_save()

        self.root_meta_check = ttk.Checkbutton(
            erow, text="允许 WSL root metadata fallback（默认关闭）",
            variable=self.root_meta_var,
            command=lambda: _set_root_metadata(
                self.root_meta_var.get()))
        self.root_meta_check.pack(side="left")
        InfoButton(erow, "WSL 里 root 用户的 /proc 元数据默认拒绝读取。"
                   "开启后用受控 fallback 读取 root Agent 元数据；关闭时"
                   "这类 Agent 仍被发现，只是详情较少。",
                   self.dash.tooltip).pack(side="left", padx=(6, 0))
        # E 高级节奏
        panel = SurfacePanel(body)
        panel.pack(fill="x")
        _section_title(panel.body, "高级节奏")
        adv = Expander(panel.body, "扫描间隔")
        adv.pack(fill="x")
        arow = tk.Frame(adv.body, bg=LIGHT.surface)
        arow.pack(fill="x")
        InfoButton(arow, "扫描节奏微调（秒）：加大更省电，减小响应更快。"
                   "值保持 plan1 的 clamp 范围。", self.dash.tooltip).pack(
            side="left")
        self.interval_vars = {}
        for path, label, lo, hi in (
                ("windows_scan_sec", "Windows 扫描", 1.0, 30.0),
                ("wsl_scan_sec", "WSL 扫描", 1.0, 60.0),
                ("file_poll_sec", "文件轮询", 0.2, 5.0)):
            interval_row = tk.Frame(adv.body, bg=LIGHT.surface)
            interval_row.pack(fill="x", pady=6)
            tk.Label(interval_row, text=label, bg=LIGHT.surface,
                     fg=LIGHT.text, font=pick_font(panel, 10)).pack(side="left")
            var = tk.DoubleVar(value=float(
                cfg.get(f"monitor.{path}", 3.0)))
            self.interval_vars[path] = var
            spin = ttk.Spinbox(interval_row, from_=lo, to=hi, increment=0.5,
                               textvariable=var, width=5,
                               command=lambda p=path: save(
                                   f"monitor.{p}",
                                   self.interval_vars[p].get()))
            spin.pack(side="right", padx=2)

    def refresh(self, reason: UiDirty):
        available = self.dash.app.monitor.terminal_available()
        _configure_changed(self.uia_state,
            text=f"当前状态：{'可用' if available else '不可用'}"
                 f"（开关更改在重启 DeskPet 后生效）")

    def reflow(self, width: int): ...


class DiagnosticsPage(DashboardPage):
    """§12.6 诊断：Health / Performance / Log（1s 仅当前页可见）。"""

    name = PAGE_DIAG

    def build(self, parent):
        self.dash.page_header(parent, PAGE_DIAG)
        body = self.dash.page_body(parent)
        health = SurfacePanel(body)
        health.pack(fill="x", pady=(0, SECTION_GAP))
        _section_title(health.body, "健康")
        self.diag_health = tk.Label(health.body, text="", bg=LIGHT.surface,
                                    fg=LIGHT.text,
                                    font=pick_font(health, 9, mono=True),
                                    anchor="nw", justify="left")
        self.diag_health.pack(fill="x")
        perf = SurfacePanel(body)
        perf.pack(fill="x", pady=(0, SECTION_GAP))
        _section_title(perf.body, "性能")
        self.diag_perf = tk.Label(perf.body, text="", bg=LIGHT.surface,
                                  fg=LIGHT.text,
                                  font=pick_font(perf, 9, mono=True),
                                  anchor="nw", justify="left")
        self.diag_perf.pack(fill="x")
        logs = SurfacePanel(body)
        logs.pack(fill="both", expand=True)
        bar = tk.Frame(logs.body, bg=LIGHT.surface)
        bar.pack(fill="x")
        _section_title(bar, "日志（不含终端原文）")
        ttk.Button(bar, text="清空诊断",
                   command=self._clear).pack(side="right")
        self.log_text = tk.Text(logs.body, wrap="word", bg=LIGHT.surface,
                                fg=LIGHT.text,
                                font=pick_font(logs, 9, mono=True),
                                height=12, state="disabled", relief="flat")
        self.log_text.pack(fill="both", expand=True, pady=(6, 0))
        self._logs_signature = None
        self._health_signature = None

    def _clear(self):
        monitor = self.dash.app.monitor
        monitor._log_ring.clear()
        try:
            while True:
                monitor.log_q.get_nowait()
        except Exception:
            pass
        self._logs_signature = None
        self.dash.app.ui.request(UiDirty.DASHBOARD)

    def refresh(self, reason: UiDirty):
        # ≥1s 节流时间戳（bridge 规则 5 读取）
        self.dash.last_diag_refresh = time.monotonic()
        app = self.dash.app
        monitor = app.monitor
        stats = dict(monitor.stats())
        stats.update(app.pet_manager.stats())
        targets = monitor.get_targets()

        def _count(value: str) -> int:
            return sum(1 for t in targets.values()
                       if t.terminal_window
                       and _binding_conf_value(t.terminal_window) == value)

        windows_err = getattr(monitor._probe, "windows_probe_error", "")
        lines = ["Process", f"  Windows        "
                 f"{'OK' if not windows_err else 'FAIL'}"]
        probe_snap = monitor._probe.snapshot()
        for key in sorted(k for k in probe_snap if k.startswith("wsl:")):
            sp = probe_snap.get(key)
            lines.append(f"  {key:<14} "
                         f"{'OK' if sp.authoritative else 'FAIL'}")
        lines.append("Session")
        for kind in ("codex", "claude", "kimi", "pi", "zcode",
                    "opencode", "dsh"):
            for t in targets.values():
                if t.snapshot.kind.value == kind:
                    lines.append(f"  {kind:<14} "
                                 f"{t.snapshot.parser_health or 'UNKNOWN'}")
                    break
        lines.append("Terminal")
        lines.append(f"  UIA            "
                     f"{'OK' if monitor.terminal_available() else '不可用'}")

        def _sole_window_count() -> int:
            return sum(1 for t in targets.values()
                       if t.terminal_window
                       and _binding_conf_value(t.terminal_window) == "none"
                       and t.terminal_window.window is not None)

        lines.append(f"  Bindings       confirmed {_count('confirmed')}"
                     f" / high {_count('high')}"
                     f" / 候选(ambiguous) {_count('ambiguous')}"
                     f" / 唯一窗口兜底 {_sole_window_count()}")
        # DP43-R15 §3.5：tray 生命周期状态可见（state/last_error/
        # menu-open 失败计数/丢弃事件计数）
        lines.append("Tray")
        tray = app.tray
        tray_state = tray.status().value if tray is not None else "off"
        lines.append(f"  state          {tray_state}")
        tray_err = tray.last_error() if tray is not None else ""
        if tray_err:
            lines.append(f"  last_error     {tray_err[:60]}")
        lines.append(
            f"  menu_fail      {tray.menu_open_failures() if tray is not None else 0}"
            f" · dropped {tray.dropped_events if tray is not None else 0}")
        text = "\n".join(lines)
        if text != self._health_signature:
            self._health_signature = text
            self.diag_health.configure(text=text)

        ui = app.ui
        saver = app.config_saver
        scheduler = app.pet_manager.scheduler
        perf = (
            f"monitor ticks={stats.get('ticks', 0)}"
            f" · wsl调用={stats.get('wsl_spawn_count', 0)}"
            f" · 可见读取={stats.get('visible_reads', 0)}"
            f"（pending {stats.get('pending_visible_reads', 0)}"
            f" · retry {stats.get('subscription_retry_count', 0)}）\n"
            f"ui_bridge_ticks={ui.bridge_count}"
            f"（last {ui.bridge_last_ms:.2f}ms）"
            f" · render_flushes={ui.render_count}"
            f"（last {ui.render_last_ms:.2f}ms"
            f" · dirty_views {ui.render_dirty_views_last}）\n"
            f"cold_decode_queue={scheduler.decode_queue_len()}"
            f" · cold_decodes={scheduler.cold_decode_count}"
            f" · cache {stats.get('cache_bytes', 0) // 1024}KB"
            f"/{stats.get('cache_budget', 0) // 1024}KB"
            f"（{stats.get('cache_frames', 0)} 帧）"
            f" · build队列={stats.get('skin_build_pending', 0)}\n"
            f"config_save_pending={1 if saver.pending() else 0}"
            f" · config_saves={saver.save_count}"
            f" · exit_watched={stats.get('exit_watched', 0)}"
            f" · detached过滤={stats.get('detached_filtered_count', 0)}")
        # DP43-R18 §9.9：startup 里程碑（相对 process 基准的毫秒）
        sm = app.startup_metrics()
        base = sm.get("process_start")
        if base is not None:
            order = ("config_loaded", "tk_created", "first_pet_created",
                     "first_pet_mapped", "background_runtime_started",
                     "skin_bootstrap_finished", "first_real_skin_frame")
            parts = [f"{k}={int((sm[k] - base) * 1000)}ms"
                     for k in order if k in sm]
            if parts:
                perf += "\nstartup " + " → ".join(parts)
        _configure_changed(self.diag_perf, text=perf)

        logs = "\n".join(monitor.recent_logs())
        if logs != self._logs_signature:
            self._logs_signature = logs
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("1.0", logs)
            self.log_text.configure(state="disabled")

    def reflow(self, width: int): ...


class SettingsPage(DashboardPage):
    """§12.7 设置：自启/窗口托盘/保存健康/版本；无周期 polling。"""

    name = PAGE_SETTINGS

    def build(self, parent):
        self.dash.page_header(parent, PAGE_SETTINGS)
        body = self.dash.page_body(parent)
        from .runtime_paths import get_runtime_paths
        app = self.dash.app
        runtime_paths = get_runtime_paths()
        auto = SurfacePanel(body)
        auto.pack(fill="x", pady=(0, SECTION_GAP))
        _section_title(auto.body, "开机启动")
        self.autostart_state = tk.Label(auto.body, text="", bg=LIGHT.surface,
                                        fg=LIGHT.text,
                                        font=pick_font(auto, 10))
        self.autostart_state.pack(anchor="w")
        self.autostart_detail = tk.Label(
            auto.body, text="", bg=LIGHT.surface, fg=LIGHT.text_secondary,
            font=pick_font(auto, 9), wraplength=480, justify="left",
            height=2, anchor="nw")
        self.autostart_detail.pack(anchor="w")
        bind_wraplength(self.autostart_detail)
        arow = tk.Frame(auto.body, bg=LIGHT.surface)
        arow.pack(fill="x", pady=(6, 0))
        self._autostart_row = arow
        self._autostart_status = None
        self._autostart_loading = False
        self.autostart_btn = ttk.Button(arow, text="开启",
                                        command=self._toggle_autostart)
        self.autostart_btn.pack(side="left")
        self.autostart_repair_btn = ttk.Button(arow, text="修复",
                                               command=self._repair)
        InfoButton(arow, "注册表 HKCU Run 键（当前用户级）。移动 DeskPet "
                   "位置后显示\"需要修复\"，一键重新登记。",
                   self.dash.tooltip).pack(side="left", padx=(6, 0))
        win = SurfacePanel(body)
        win.pack(fill="x", pady=(0, SECTION_GAP))
        _section_title(win.body, "窗口与托盘")
        wrow = tk.Frame(win.body, bg=LIGHT.surface)
        wrow.pack(fill="x", pady=2)
        topmost = tk.BooleanVar(value=bool(app.config.get("topmost", True)))
        ttk.Checkbutton(wrow, text="窗口置顶", variable=topmost,
                        command=lambda: app._set_topmost(
                            topmost.get())).pack(side="left")
        InfoButton(wrow, "桌宠窗口始终保持在其他窗口之上。",
                   self.dash.tooltip).pack(side="left", padx=(6, 0))
        tray = tk.BooleanVar(value=bool(
            app.config.get("tray_enabled", True)))
        ttk.Checkbutton(wrow, text="托盘图标", variable=tray,
                        command=lambda: app.set_tray_enabled(
                            tray.get())).pack(side="left", padx=(16, 0))
        InfoButton(wrow, "左键显示桌宠（幂等恢复），右键完整菜单；隐藏"
                   "桌宠后托盘是唯一恢复入口。", self.dash.tooltip).pack(
            side="left", padx=(6, 0))
        hrow = tk.Frame(win.body, bg=LIGHT.surface)
        hrow.pack(fill="x", pady=2)
        ttk.Button(hrow, text="隐藏全部桌宠",
                   command=app.hide_pet).pack(side="left")
        InfoButton(hrow, "暂时隐藏所有桌宠（托盘保留，左键恢复；监听不受"
                   "影响）。", self.dash.tooltip).pack(side="left",
                                                       padx=(6, 0))
        savep = SurfacePanel(body)
        savep.pack(fill="x", pady=(0, SECTION_GAP))
        _section_title(savep.body, "配置保存状态")
        self.save_state = tk.Label(savep.body, text="", bg=LIGHT.surface,
                                   fg=LIGHT.text,
                                   font=pick_font(savep, 10))
        self.save_state.pack(anchor="w")
        srow = tk.Frame(savep.body, bg=LIGHT.surface)
        srow.pack(fill="x", pady=(6, 0))
        self._retry_btn = ttk.Button(srow, text="重试保存",
                                     command=self._retry_save)
        InfoButton(srow, "所有设置经 650ms debounce 原子写入 config.json"
                   "（临时文件+替换）；失败绝不静默——此处标红并可重试。",
                   self.dash.tooltip).pack(side="left", padx=(6, 0))
        drow = tk.Frame(savep.body, bg=LIGHT.surface)
        drow.pack(fill="x", pady=(6, 0))
        tk.Label(drow, text=f"配置与素材目录：{runtime_paths.data_root}",
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(savep, 9), anchor="w").pack(
            side="left", fill="x", expand=True)
        ttk.Button(drow, text="打开数据目录",
                   command=self._open_data_root).pack(side="right")
        abt = SurfacePanel(body)
        abt.pack(fill="x")
        _section_title(abt.body, "关于")
        tk.Label(abt.body, text=APP_VERSION, bg=LIGHT.surface,
                 fg=LIGHT.text, font=pick_font(abt, 10)).pack(anchor="w")

    def _open_data_root(self):
        from .runtime_paths import open_data_root
        result = open_data_root()
        if not result.ok:
            self.dash.app.toast(
                f"无法打开数据目录：{result.error or result.path}", 5)

    def _toggle_autostart(self):
        self._submit_autostart("autostart-toggle", autostart.toggle)

    def _repair(self):
        self._submit_autostart("autostart-repair", autostart.repair)

    def _submit_autostart(self, key, operation):
        if self._autostart_loading:
            return
        self._autostart_loading = True
        self.dash.app.ui.request(UiDirty.DASHBOARD)

        def work():
            result = operation()
            return result, autostart.status()

        if not self.dash.submit_action(key, work, self._autostart_done):
            self._autostart_loading = False

    def _request_autostart_status(self):
        if self._autostart_loading or self._autostart_status is not None:
            return
        self._autostart_loading = True
        if not self.dash.submit_action(
                "autostart-status", autostart.status,
                self._autostart_status_done):
            self._autostart_loading = False

    def _autostart_status_done(self, ok, payload):
        self._autostart_loading = False
        if ok:
            self._autostart_status = payload
        else:
            from .autostart import AutostartState, AutostartStatus
            self._autostart_status = AutostartStatus(
                state=AutostartState.UNAVAILABLE, registered=False,
                healthy=False, expected_command="", registered_command="",
                reason=str(payload))
        self.dash.app.ui.request(UiDirty.DASHBOARD)

    def _autostart_done(self, ok, payload):
        self._autostart_loading = False
        if not ok:
            self.dash.app.toast(f"开机自启动操作失败：{payload}", 5)
        else:
            result, status = payload
            self._autostart_status = status
            self.dash.app.toast(
                "开机自启动已" + ("开启" if result.enabled else "关闭")
                + ("" if result.ok else f"（{result.reason}）"), 4)
        self.dash.app.ui.request(UiDirty.DASHBOARD)

    def _retry_save(self):
        # DP43-R02：显式重试不同步写盘——immediate 强制快照 + 单
        # transient worker；成功/失败 toast 统一走 saver 的 result
        # callback（on_result），此处只负责触发。
        self.dash.app.config_saver.request_save(
            immediate=True, force=True)
        self.dash.app.ui.request(UiDirty.DASHBOARD)

    def on_show(self):
        self._request_autostart_status()

    def refresh(self, reason: UiDirty):
        from .autostart import AutostartState
        st = self._autostart_status
        if st is None:
            _configure_changed(self.autostart_state, text="正在读取…",
                               fg=LIGHT.text_secondary)
            _configure_changed(self.autostart_detail, text="")
            _configure_changed(self.autostart_btn, state="disabled")
            _pack_forget_once(self.autostart_repair_btn)
            self._request_autostart_status()
        else:
            _configure_changed(self.autostart_btn,
                state="disabled" if self._autostart_loading else "normal")
        if st is None:
            pass
        elif st.state == AutostartState.HEALTHY:
            _configure_changed(self.autostart_state, text="已开启",
                               fg=LIGHT.done)
            _configure_changed(self.autostart_detail, text="")
            _pack_forget_once(self.autostart_repair_btn)
        elif st.state == AutostartState.MISSING:
            _configure_changed(self.autostart_state, text="未开启",
                               fg=LIGHT.text_secondary)
            _configure_changed(self.autostart_detail, text="")
            _pack_forget_once(self.autostart_repair_btn)
        elif st.state == AutostartState.STALE:
            _configure_changed(self.autostart_state, text="需要修复",
                               fg=LIGHT.waiting)
            _configure_changed(self.autostart_detail,
                text=f"注册路径与当前 DeskPet 路径不一致：\n"
                     f"{_ellipsize(st.registered_command, 120)}")
            _pack_once(self.autostart_repair_btn,
                       in_=self._autostart_row, side="left", padx=6)
            _configure_changed(
                self.autostart_repair_btn,
                state="disabled" if self._autostart_loading else "normal")
        elif st is not None:
            _configure_changed(self.autostart_state, text="不可用",
                               fg=LIGHT.text_secondary)
            _configure_changed(self.autostart_detail,
                               text=getattr(st, "reason", ""))
            _pack_forget_once(self.autostart_repair_btn)
        if st is not None:
            _configure_changed(self.autostart_btn,
                text="关闭" if st.state == AutostartState.HEALTHY else "开启")
        saver = self.dash.app.config_saver
        last = saver.last_result or getattr(
            self.dash.app.config, "last_save_result", None)
        if saver.pending():
            _configure_changed(self.save_state, text="正在保存…",
                               fg=LIGHT.waiting)
        elif last is not None and getattr(last, "ok", True) is False:
            _configure_changed(self.save_state,
                text=f"上次保存失败：{getattr(last, 'error', '')}",
                fg=LIGHT.error)
            _pack_once(self._retry_btn, side="left")
        else:
            _configure_changed(self.save_state, text="已保存", fg=LIGHT.done)
            _pack_forget_once(self._retry_btn)

    def reflow(self, width: int): ...


class _PageHeader:
    """Stable page header: title hierarchy on the left, actions on the right."""

    def __init__(self, parent, title: str, metrics: DashboardMetrics,
                 description: str = ""):
        self.frame = tk.Frame(parent, bg=LIGHT.page,
                              height=metrics.px(PAGE_HEADER_HEIGHT))
        self.frame.pack(fill="x", padx=metrics.px(PAGE_PAD_X),
                        pady=(metrics.px(14), metrics.px(8)))
        self.frame.pack_propagate(False)
        self._actions = tk.Frame(self.frame, bg=LIGHT.page)
        self._actions.pack(side="right", padx=(12, 0))
        title_group = tk.Frame(self.frame, bg=LIGHT.page)
        title_group.pack(side="left", fill="both", expand=True)
        self._title = tk.Label(title_group, text=title, bg=LIGHT.page,
                               fg=LIGHT.text, anchor="w", width=1,
                               font=pick_font(parent, 17, True))
        self._title.pack(fill="x")
        if description:
            tk.Label(title_group, text=description, bg=LIGHT.page,
                     fg=LIGHT.text_secondary, anchor="w", width=1,
                     font=pick_font(parent, 9)).pack(fill="x", pady=(2, 0))

    def add_action(self, text: str, command, primary: bool = False):
        button = ttk.Button(self._actions, text=text, width=14 if primary
                            else 10, command=command,
                            style="Dashboard.Primary.TButton" if primary
                            else "Dashboard.TButton")
        button.pack(side="left", padx=4)
        return button


class Dashboard(tk.Toplevel):
    """Retained desktop shell with one Agent workspace and focused settings."""

    def __init__(self, app):
        self.app = app
        super().__init__(app.root)
        self.title("DeskPet · 仪表盘")
        self.metrics = DashboardMetrics.for_window(self)
        configure_dashboard_styles(self)
        self._apply_window_geometry()
        self.configure(bg=LIGHT.page)
        self.protocol("WM_DELETE_WINDOW", self.hide_dashboard)
        self.selected_key = ""
        self._page = PAGE_AGENTS
        self._closing = False
        self._dialog_active = False
        self._visibility_epoch = 0
        # 一个 Dashboard 只有一个 transient external-action worker。
        # worker 只执行注册表/路径类阻塞工作；结果由 UiCoordinator bridge
        # 在 Tk 线程收割，绝不从 worker 触碰 widget。
        self._action_lock = threading.Lock()
        self._action_worker = None
        self._action_result = None
        self._action_token = 0
        self._actions_accepting = True
        # Configure/Canvas 几何变化合并到短期 idle pass；无周期 timer。
        self._last_center_geometry = None
        # 诊断页 ≥1s 节流时间戳（bridge 规则 5 读取，monotonic）
        self.last_diag_refresh = 0.0
        # DP43-R16：retained Toplevel——失焦绝不推断用户关闭意图，
        # 关闭只来自 X（WM_DELETE_WINDOW）/显式 hide/App shutdown

        # ---- shell：左导航 + 右内容
        self._build_shell()
        self._pages: dict[str, DashboardPage] = {}
        self._current: DashboardPage | None = None
        self._register_pages()
        # 滚轮只在页面 canvas 内滚动（§11.5）
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", self._on_wheel)
        self.bind("<Button-5>", self._on_wheel)
        self.bind("<Escape>", lambda _e: self.tooltip.hide())
        self.bind("<Button-3>", self._on_context_menu)
        self.bind("<Menu>", self._on_context_menu)
        self.bind("<Shift-F10>", self._on_context_menu)
        # DPI/宽度 breakpoint 重排：50ms debounce（§10.3）
        self._reflow_after = None
        self._last_reflow_width = -1
        self._last_reflow_dpi = self.metrics.dpi
        # Deiconify/reopen can emit redundant same-size Configure events.
        # Reflow depends on size/DPI, not window position; suppressing these
        # avoids scheduling useless 50ms work on every retained-dashboard open.
        self._last_configure_size = None
        self.bind("<Configure>", self._on_configure)
        # 工作台立即构建；其余页面首次访问时构建。
        self._show_page(PAGE_AGENTS)

    # ================================================== shell
    def _apply_window_geometry(self):
        try:
            from actions import winkeys
            _name, area = winkeys.monitor_work_area(
                self.winfo_screenwidth() // 2,
                self.winfo_screenheight() // 2)
        except Exception:
            area = None
        s = self.metrics.scale
        w, h = int(DEFAULT_WINDOW_W * s), int(DEFAULT_WINDOW_H * s)
        if area:
            work_w = area[2] - area[0]
            work_h = area[3] - area[1]
            w = min(w, max(int(work_w * s) - int(64 * s),
                           int(300 * s)))
            h = min(h, max(int(work_h * s) - int(64 * s),
                           int(240 * s)))
        self.geometry(f"{w}x{h}")
        self.minsize(int(860 * s), int(560 * s))

    def _build_shell(self):
        m = self.metrics
        # Keep the rail's physical width stable for the lifetime of the
        # retained Toplevel.  A fixed rail plus a permanent scrollbar gutter
        # prevents open/refresh/reflow from moving the content edge and makes
        # the active marker visually steady.
        nav = tk.Frame(self, bg=LIGHT.nav, width=m.px(NAV_WIDTH),
                       highlightthickness=0, bd=0)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        brand_row = tk.Frame(nav, bg=LIGHT.nav,
                             height=m.px(BRAND_HEIGHT))
        brand_row.pack(fill="x")
        brand_row.pack_propagate(False)
        self._brand_icon = None
        self._window_icons = ()
        try:
            from PIL import ImageTk
            from .icon import icon_image

            self._brand_icon = ImageTk.PhotoImage(icon_image(m.px(38)), master=self)
            self._window_icons = tuple(ImageTk.PhotoImage(icon_image(size), master=self)
                                       for size in (16, 32, 48, 64))
            self.iconphoto(False, *self._window_icons)
        except (OSError, ValueError, tk.TclError):
            # A missing/corrupt optional icon must not prevent opening settings.
            pass
        self._brand_mark = tk.Label(
            brand_row, image=self._brand_icon,
            text="" if self._brand_icon is not None else "D", bg=LIGHT.nav,
            fg=LIGHT.accent, width=0 if self._brand_icon is not None else 2,
            anchor="center", font=pick_font(self, 14, True))
        self._brand_mark.pack(side="left", padx=(m.px(14), m.px(9)), pady=m.px(12))
        brand_group = tk.Frame(brand_row, bg=LIGHT.nav)
        brand_group.pack(side="left", fill="both", expand=True, pady=11)
        tk.Label(brand_group, text="DeskPet", bg=LIGHT.nav,
                 fg="#F3FBF7", anchor="w",
                 font=pick_font(self, 13, True)).pack(anchor="w")
        tk.Label(brand_group, text="Agent workspace", bg=LIGHT.nav,
                 fg=LIGHT.nav_text, anchor="w",
                 font=pick_font(self, 8)).pack(anchor="w", pady=(1, 0))
        tk.Frame(nav, bg=LIGHT.nav_surface, height=1).pack(fill="x", padx=16,
                                                    pady=(0, 14))
        self._nav_buttons: dict[str, NavButton] = {}
        tk.Label(nav, text="工作区", bg=LIGHT.nav, fg=LIGHT.nav_text,
                 anchor="w", padx=16,
                 font=pick_font(self, 8, True)).pack(fill="x", pady=(0, 4))
        for page in (PAGE_AGENTS, PAGE_PETS, PAGE_LOOK,
                     PAGE_MONITOR, PAGE_DIAG):
            self._add_nav_item(nav, page)
        spacer = tk.Frame(nav, bg=LIGHT.nav)
        spacer.pack(fill="both", expand=True)
        status_card = tk.Frame(nav, bg=LIGHT.nav_surface, height=m.px(64))
        status_card.pack(fill="x", padx=10, pady=(0, 10))
        status_card.pack_propagate(False)
        tk.Label(status_card, text="实时监控", bg=LIGHT.nav_surface,
                 fg=LIGHT.nav_text, anchor="w",
                 font=pick_font(self, 8, True)).pack(fill="x", padx=11,
                                                     pady=(8, 0))
        self._nav_status = tk.Label(
            status_card, text="正在读取 Agent…", bg=LIGHT.nav_surface,
            fg="#F3FBF7", anchor="w", justify="left", height=2,
            padx=11, font=pick_font(self, 8))
        self._nav_status.pack(fill="x", padx=0, pady=(1, 0))
        tk.Label(nav, text="配置", bg=LIGHT.nav, fg=LIGHT.nav_text,
                 anchor="w", padx=16,
                 font=pick_font(self, 8, True)).pack(fill="x", pady=(0, 4))
        self._add_nav_item(nav, PAGE_SETTINGS)   # 固定底部
        # 右内容：页面区（滚动容器）
        self.content = self._make_scroller()
        self.content.pack(side="left", fill="both", expand=True)
        self.tooltip = TooltipController(self)

    def _make_scroller(self):
        from .widgets import ScrollableFrame
        scroller = ScrollableFrame(self)
        # 页面居中容器：横向跟 viewport（_sync_center_geometry 用 padx
        # 居中并限制最大 960 逻辑像素）；纵向高度由页面内容 propagate
        # ——不关闭 propagation，否则 _center 只配 width 时纵向 requested
        # size 可能坍塌成接近 1px（右侧正文空白的根因）。
        self._center = tk.Frame(scroller.inner, bg=LIGHT.page)
        self._center.pack(fill="x")
        # add="+"：ScrollableFrame 自己的 <Configure>（scrollregion/
        # scrollbar）是唯一 scroll owner；Dashboard 只追加几何回调，
        # 绝不覆盖已有 binding。
        scroller.inner.bind(
            "<Configure>",
            lambda e: self._sync_center_geometry(e.width),
            add="+")
        return scroller

    def _sync_center_geometry(self, viewport_width: int | None = None) -> None:
        """横向几何同步：正文宽度 = min(viewport, 960 逻辑像素)，超出
        部分左右对称留白。高度从不在此设置——由页面内容 propagate。"""
        if self._closing or not self.winfo_exists():
            return
        if viewport_width is None:
            try:
                viewport_width = self.content._canvas.winfo_width()
            except tk.TclError:
                return
        try:
            viewport_width = int(viewport_width)
        except (TypeError, ValueError):
            return
        if viewport_width <= 1:
            return
        max_content = self.metrics.px(PAGE_CONTENT_MAX_WIDTH)
        content_width = min(viewport_width, max_content)
        pad = max(0, (viewport_width - content_width) // 2)
        geometry = (viewport_width, content_width, pad)
        if geometry == self._last_center_geometry:
            return
        self._last_center_geometry = geometry
        self._center.pack_configure(padx=(pad, pad))

    def _add_nav_item(self, nav, page: str):
        btn = NavButton(nav, page, command=lambda p=page: self._show_page(p))
        btn.configure(height=self.metrics.px(NAV_ITEM_HEIGHT))
        btn.pack_propagate(False)
        btn.pack(fill="x", padx=10, pady=3)
        btn._label.configure(pady=max(2, self.metrics.px(
            NAV_ITEM_HEIGHT - 30) // 2))
        self._nav_buttons[page] = btn

    def _register_pages(self):
        for page_cls in (AgentsPage, PetsPage,
                         AppearancePage, MonitorPage, DiagnosticsPage,
                         SettingsPage):
            page = page_cls(self)
            self._pages[page.name] = page

    # ================================================== 页面生命周期（§15）
    def page_header(self, parent, title: str) -> _PageHeader:
        return _PageHeader(parent, title, self.metrics,
                           PAGE_DESCRIPTIONS.get(title, ""))

    def page_body(self, parent) -> tk.Frame:
        body = tk.Frame(parent, bg=LIGHT.page,
                        padx=self.metrics.px(PAGE_PAD_X),
                        pady=self.metrics.px(PAGE_PAD_Y))
        body.pack(fill="both", expand=True)
        return body

    def _show_page(self, page: str):
        if self._closing:
            return
        target = self._pages.get(page)
        if target is None:
            return
        if self._current is target and target.built:
            return
        if self._current is not None and self._current is not target:
            if self._current.built:
                self._current.holder.pack_forget()
                self._current.on_hide()
        self._page = page
        for name, btn in self._nav_buttons.items():
            btn.set_active(name == page)
        holder = target.ensure_built(self._center)
        holder.pack(fill="both", expand=True)
        self._current = target
        self.tooltip.hide()   # 切页关闭 tooltip（§11.2）
        target.on_show()
        width = max(0, self._center.winfo_width()
                    - 2 * self.metrics.px(PAGE_PAD_X))
        if width > 1:
            target.reflow(width)
        self._last_reflow_width = width
        self.app.ui.request(UiDirty.DASHBOARD)

    # ================================================== 刷新（§4.4 E 步）
    def refresh_current_page(self, reason: UiDirty = UiDirty.NONE):
        if self._closing or not self.winfo_exists():
            return
        if self.state() == "withdrawn":
            return
        self._refresh_nav_status()
        if self._current is not None:
            self._current.refresh(reason)

    def _refresh_nav_status(self):
        """Update the rail summary in place; never rebuild navigation widgets."""
        if not hasattr(self, "_nav_status"):
            return
        try:
            targets = self.app.monitor.get_targets()
        except Exception:
            return
        live = len(targets)
        working = sum(1 for target in targets.values()
                      if target.snapshot.status is Status.WORKING)
        attention = sum(1 for target in targets.values() if target.snapshot.status
                        in (Status.WAITING, Status.INPUT, Status.ERROR))
        signature = (live, working, attention)
        if signature == getattr(self, "_nav_status_signature", None):
            return
        self._nav_status_signature = signature
        if not live:
            text = "暂无在线 Agent"
        else:
            text = f"在线 {live} · 工作中 {working}"
            if attention:
                text += f"\n需要关注 {attention}"
        _configure_changed(self._nav_status, text=text)

    def refresh(self):
        self.refresh_current_page(UiDirty.DASHBOARD)

    # ================================================== 合并式用户动作
    def request_rescan(self):
        """Dashboard 扫描按钮：O(1) 提交，不在 Tk callback 等 UIA。"""
        accepted = self.app.monitor.rescan()
        if accepted is False:
            return False
        self.app.toast("已请求重新扫描", 3)
        self.app.ui.kick()
        return True

    def submit_action(self, key: str, work, on_done) -> bool:
        """提交一个 transient 阻塞动作；busy 时拒绝重复提交。"""
        with self._action_lock:
            if (not self._actions_accepting or self._action_result is not None
                    or (self._action_worker is not None
                        and self._action_worker.is_alive())):
                return False
            self._action_token += 1
            token = self._action_token
            worker = threading.Thread(
                target=self._run_action,
                args=(token, str(key), work, on_done),
                name="deskpet-dashboard-action", daemon=True)
            self._action_worker = worker
            try:
                worker.start()
            except Exception:
                self._action_worker = None
                return False
        self.app.ui.kick()
        return True

    def _run_action(self, token, key, work, on_done):
        try:
            payload = work()
            ok = True
        except Exception as exc:
            payload = str(exc)
            ok = False
        with self._action_lock:
            if self._actions_accepting and token == self._action_token:
                self._action_result = (token, key, on_done, ok, payload)

    def actions_pending(self) -> bool:
        with self._action_lock:
            return (self._action_result is not None
                    or (self._action_worker is not None
                        and self._action_worker.is_alive()))

    def poll_actions(self) -> bool:
        with self._action_lock:
            item, self._action_result = self._action_result, None
            if item is not None:
                self._action_worker = None
        if item is None or self._closing:
            return False
        _token, _key, on_done, ok, payload = item
        on_done(ok, payload)
        return True

    def request_action_stop(self):
        with self._action_lock:
            self._actions_accepting = False
            self._action_result = None

    def join_actions(self, timeout: float) -> bool:
        worker = self._action_worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, timeout))
            return not worker.is_alive()
        return True

    def is_open(self) -> bool:
        if self._closing or not self.winfo_exists():
            return False
        return self.state() != "withdrawn"

    def on_diagnostics_page(self) -> bool:
        return self._page == PAGE_DIAG

    # ================================================== 滚轮 / reflow
    def _on_wheel(self, event):
        if self._current is not None and self._current.built:
            self.tooltip.hide()
            delta = (120 if event.num == 4 else -120 if event.num == 5
                     else event.delta)
            self.content.wheel_scroll(delta, event.x_root, event.y_root)

    def _on_context_menu(self, event):
        if self._closing or self.app._closing or self._dialog_active:
            return "break"
        # Preserve editing/selection gestures inside text and choice inputs.
        if event.widget.winfo_class() in (
                "Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"):
            return
        self.tooltip.hide()
        if getattr(event, "num", None) == 3:
            x, y = event.x_root, event.y_root
        else:
            x, y = self.winfo_rootx() + 32, self.winfo_rooty() + 48
        self.app._menu_controller.show(self, x, y, self._build_context_menu)
        return "break"

    def _build_context_menu(self, menu):
        defer = self.app._menu_controller.deferred
        menu.add_command(label="刷新当前页", command=defer(self.refresh_current_page))
        menu.add_command(label="显示桌宠", command=defer(self.app.show_pet))
        menu.add_separator()
        menu.add_command(label="关闭仪表盘", command=defer(self.hide_dashboard))
        menu.add_command(label="退出 DeskPet",
                         command=defer(self.app.quit, allow_when_closing=True))

    def _on_configure(self, event):
        if event.widget is not self:
            return
        size = (int(event.width), int(event.height))
        if size == self._last_configure_size:
            return
        self._last_configure_size = size
        if self._reflow_after is not None:
            return
        self._reflow_after = self.after(50, self._reflow_debounced)

    def _reflow_debounced(self):
        # The geometry helper is also called directly by deterministic tests
        # and by a few DPI-change paths.  Cancel the still-pending token first
        # so a direct call cannot leave a second callback behind; otherwise a
        # later shutdown would destroy the Toplevel while that Tcl command was
        # still scheduled.
        token = self._reflow_after
        self._reflow_after = None
        if token is not None:
            try:
                self.after_cancel(token)
            except tk.TclError:
                pass
        if self._closing or not self.winfo_exists():
            return
        new_metrics = DashboardMetrics.for_window(self)
        dpi_changed = new_metrics.dpi != self._last_reflow_dpi
        if dpi_changed:
            self.metrics = new_metrics
            self._last_reflow_dpi = new_metrics.dpi
            # §8.6：DPI 变化后 960px 上限/padding 换算全部失效，先同步
            # 横向几何再重排当前页
            self._sync_center_geometry(self.content.winfo_width())
        width = max(0, self._center.winfo_width()
                    - 2 * self.metrics.px(PAGE_PAD_X))
        crossed = any((width >= breakpoint) !=
                      (self._last_reflow_width >= breakpoint)
                      for breakpoint in (SettingRow.COMPACT_ENTER,
                                        SettingRow.COMPACT_EXIT, 840, 1040))
        # §10.3：只有 DPI 变化或跨 breakpoint 才重排；像素级拖拽零工作
        if dpi_changed or crossed or self._last_reflow_width < 0:
            self._last_reflow_width = width
            if self._current is not None and self._current.built:
                self._current.reflow(width)

    # ================================================== 生命周期
    def open(self):
        """显示（刷新由 UiCoordinator 驱动；无周期 timer）。"""
        if self._closing or self.app._closing:
            return
        was_hidden = self.state() == "withdrawn"
        self.deiconify()
        self.lift()
        if not self._dialog_active:
            self.focus_set()
        if self._current is None:
            self._show_page(PAGE_AGENTS)
        elif was_hidden:
            self._current.on_show()

    def run_dialog(self, dialog, *args, **kwargs):
        """Native dialogs run nested event loops; allow only one at a time.

        A result arriving after hide/shutdown must not mutate settings or
        submit another background job.
        """
        if self._dialog_active or self._closing or self.app._closing:
            return None
        epoch = self._visibility_epoch
        self._dialog_active = True
        self.tooltip.hide()
        try:
            result = dialog(*args, **kwargs)
        finally:
            self._dialog_active = False
        if (self._closing or self.app._closing
                or epoch != self._visibility_epoch):
            return None
        return result

    def hide_dashboard(self):
        """用户关闭窗口：隐藏（0 周期唤醒；bridge 降回低档）。"""
        if self._closing:
            return
        self._visibility_epoch += 1
        if self.app._menu_controller.owner is self:
            self.app._menu_controller.dismiss()
        self.tooltip.hide()
        if self._current is not None:
            self._current.on_hide()
        self.withdraw()
        try:
            self.app.ui.kick()
        except Exception:
            pass

    def shutdown(self):
        self.request_action_stop()
        self._closing = True
        self.tooltip.hide()
        try:
            self.content.cancel_layout()
        except Exception:
            pass
        if self._reflow_after is not None:
            try:
                self.after_cancel(self._reflow_after)
            except tk.TclError:
                pass
            self._reflow_after = None
        try:
            self.destroy()
        except tk.TclError:
            pass
