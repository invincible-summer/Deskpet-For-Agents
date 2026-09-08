"""V4.1 Dashboard：左侧导航 + 六页（v4plan §13）。

  概览 / Agents / 桌宠与外观 / 监听与隐私 / 诊断 / 设置

  * ttk.Notebook 旧结构已删除；
  * 状态同时有文字/icon，不只靠颜色；
  * 技术 ID（PID/RuntimeId/HWND）只在高级诊断折叠区；
  * "打开终端"永远用 exact agent_key → Monitor.activate_target；
  * 激活/修复入口不直接 import winkeys。
"""
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from agents.models import ActivationCode, BindingConfidence, Status

from . import autostart, skins
from .labels import mode_text, phase_text, status_text
from .presentation import PresentationMode
from .theme import LIGHT, STATUS_COLOR, pick_font
from .widgets import (
    Card,
    Expander,
    HelpDot,
    NavButton,
    ScrollableFrame,
    SegmentedControl,
    StatusChip,
)

NAV_WIDTH = 184

PAGE_OVERVIEW = "概览"
PAGE_AGENTS = "Agents"
PAGE_LOOK = "桌宠与外观"
PAGE_MONITOR = "监听与隐私"
PAGE_DIAG = "诊断"
PAGE_SETTINGS = "设置"

_BINDING_LABELS = {
    BindingConfidence.CONFIRMED: "已确认",
    BindingConfidence.HIGH: "高置信",
    BindingConfidence.AMBIGUOUS: "无法唯一确定",
    BindingConfidence.NONE: "未绑定",
}


def _binding_conf_value(binding):
    value = getattr(binding, "confidence", None)
    return value.value if hasattr(value, "value") else str(value)


class Dashboard(tk.Toplevel):
    def __init__(self, app):
        self.app = app
        super().__init__(app.root)
        self.title("DeskPet · 仪表盘")
        self.geometry("1120x760")
        self.minsize(880, 620)
        self.configure(bg=LIGHT.page)
        self.protocol("WM_DELETE_WINDOW", self.withdraw)
        self.selected_key = ""
        self._page = PAGE_OVERVIEW
        self._refreshing = False

        # ---- 布局：左导航 + 右内容
        nav = tk.Frame(self, bg=LIGHT.page, width=NAV_WIDTH)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        header = tk.Label(nav, text="DeskPet V4.1", bg=LIGHT.page,
                          fg=LIGHT.text, font=pick_font(self, 12, True),
                          anchor="w", padx=16)
        header.pack(fill="x", pady=(18, 10))
        self._nav_buttons: dict[str, NavButton] = {}
        for page in (PAGE_OVERVIEW, PAGE_AGENTS, PAGE_LOOK, PAGE_MONITOR,
                     PAGE_DIAG):
            btn = NavButton(nav, page,
                            command=lambda p=page: self._show_page(p))
            btn.pack(fill="x")
            self._nav_buttons[page] = btn
        tk.Frame(nav, bg=LIGHT.page, height=16).pack()
        btn = NavButton(nav, PAGE_SETTINGS,
                        command=lambda p=PAGE_SETTINGS: self._show_page(p))
        btn.pack(fill="x", side="bottom")
        self._nav_buttons[PAGE_SETTINGS] = btn

        self.content = ScrollableFrame(self)
        self.content.pack(side="left", fill="both", expand=True)
        self._pages: dict[str, tk.Frame] = {}
        self._build_overview()
        self._build_agents()
        self._build_look()
        self._build_monitor()
        self._build_diag()
        self._build_settings()
        self._show_page(PAGE_OVERVIEW)

    # ================================================== 页面切换
    def _show_page(self, page: str):
        self._page = page
        for name, frame in self._pages.items():
            if name == page:
                frame.pack(fill="x", padx=24, pady=16)
            else:
                frame.pack_forget()
        for name, btn in self._nav_buttons.items():
            btn.set_active(name == page)

    def _new_page(self, name: str) -> tk.Frame:
        frame = tk.Frame(self.content.inner, bg=LIGHT.page)
        self._pages[name] = frame
        return frame

    # ================================================== 概览
    def _build_overview(self):
        page = self._new_page(PAGE_OVERVIEW)
        header = tk.Frame(page, bg=LIGHT.page)
        header.pack(fill="x")
        tk.Label(header, text="DeskPet V4.1", bg=LIGHT.page, fg=LIGHT.text,
                 font=pick_font(self, 14, True)).pack(side="left")
        self.ov_summary = tk.Label(header, text="", bg=LIGHT.page,
                                   fg=LIGHT.text_secondary,
                                   font=pick_font(self, 10))
        self.ov_summary.pack(side="left", padx=16, pady=(6, 0))

        stats = Card(page)
        stats.pack(fill="x", pady=(8, 0))
        self.ov_stats = tk.Label(stats.body, text="", bg=LIGHT.surface,
                                 fg=LIGHT.text, font=pick_font(self, 11),
                                 anchor="w", justify="left")
        self.ov_stats.pack(fill="x", padx=4, pady=4)

        tk.Label(page, text="Agent Cards", bg=LIGHT.page,
                 fg=LIGHT.text_secondary,
                 font=pick_font(self, 10, True),
                 anchor="w").pack(side="left", pady=(16, 4))
        HelpDot(page, "当前被动发现的全部 Agent（Monitor 层），每张卡："
                      "名称与项目目录、状态徽标、当前活动摘要；\"打开终端\""
                      "按 exact agent_key 精确唤起该 Agent 的 Windows "
                      "Terminal Tab/Pane（位置不唯一时会提示先在仪表盘修复）。",
                bg=LIGHT.page).pack(side="left", pady=(16, 4))
        self.ov_cards = tk.Frame(page, bg=LIGHT.page)
        self.ov_cards.pack(fill="x")
        self._ov_card_sig = None

    def _refresh_overview(self, targets):
        waiting = sum(1 for t in targets.values()
                      if t.snapshot.status in (Status.WAITING, Status.INPUT))
        working = sum(1 for t in targets.values()
                      if t.snapshot.status == Status.WORKING)
        ambiguous = sum(1 for t in targets.values()
                        if t.terminal and _binding_conf_value(t.terminal)
                        == "ambiguous")
        uia = "UIA ✓" if self.app.monitor.terminal_available() else "UIA ✗"
        self.ov_summary.configure(
            text=f"{len(targets)} Agents · {waiting} 等待 · {uia}")
        self.ov_stats.configure(text=(
            f"Live {len(targets)}     Waiting {waiting}     "
            f"Working {working}     Ambiguous {ambiguous}"))

        entries = []
        for key, t in sorted(targets.items()):
            snap = t.snapshot
            chip = (f"{snap.kind.label} · "
                    f"{t.instance.project or t.instance.source}")
            detail = snap.waiting_detail or snap.summary or "—"
            env = t.instance.environment_label
            if snap.stale:
                env += " · 状态可能延迟"
            entries.append((key, chip, status_text(snap), detail, env,
                            snap.status.value))
        sig = tuple(entries)
        if sig == self._ov_card_sig:
            return
        self._ov_card_sig = sig
        for child in self.ov_cards.winfo_children():
            child.destroy()
        for key, chip, status, detail, env, status_value in entries:
            card = Card(self.ov_cards, padding=12)
            card.pack(fill="x", pady=4)
            row = tk.Frame(card.body, bg=LIGHT.surface)
            row.pack(fill="x")
            tk.Label(row, text=chip, bg=LIGHT.surface, fg=LIGHT.text,
                     font=pick_font(self, 10, True)).pack(side="left")
            StatusChip(row, text=status,
                       color=STATUS_COLOR.get(status_value,
                                              LIGHT.unknown)).pack(
                side="right")
            tk.Label(card.body, text=detail, bg=LIGHT.surface,
                     fg=LIGHT.text, font=pick_font(self, 10),
                     anchor="w", justify="left", wraplength=560).pack(
                fill="x", pady=(2, 0))
            foot = tk.Frame(card.body, bg=LIGHT.surface)
            foot.pack(fill="x", pady=(4, 0))
            tk.Label(foot, text=env, bg=LIGHT.surface,
                     fg=LIGHT.text_secondary,
                     font=pick_font(self, 9)).pack(side="left")
            ttk.Button(foot, text="打开终端",
                       command=lambda k=key: self._open_terminal(k)).pack(
                side="right")
            ttk.Button(foot, text="详情",
                       command=lambda k=key: self._goto_agent(k)).pack(
                side="right", padx=6)

    # ================================================== Agents master-detail
    def _build_agents(self):
        page = self._new_page(PAGE_AGENTS)
        body = tk.Frame(page, bg=LIGHT.page)
        body.pack(fill="both", expand=True)

        left = Card(body, padding=8)
        left.pack(side="left", fill="y", padx=(0, 8), expand=False)
        self.ag_list = tk.Frame(left.body, bg=LIGHT.surface)
        self.ag_list.pack(fill="both", expand=True)
        self._ag_list_sig = None

        right = Card(body, padding=16)
        right.pack(side="left", fill="both", expand=True)
        self.ag_detail = tk.Label(right.body, text="在左侧选择一个 Agent",
                                  bg=LIGHT.surface, fg=LIGHT.text,
                                  font=pick_font(self, 11),
                                  anchor="nw", justify="left", wraplength=480)
        self.ag_detail.pack(fill="both", expand=True)

        bar = tk.Frame(right.body, bg=LIGHT.surface)
        bar.pack(fill="x", pady=(8, 0))
        self.repair_button = ttk.Button(
            bar, text="关联当前 Terminal 位置",
            command=self._repair_location)
        self.repair_button.pack(side="left")
        HelpDot(bar, "终端位置无法自动唯一确定时的修复：先在 Windows Terminal "
                     "切到该 Agent 所在的 Tab/Pane（键盘焦点留在该终端），"
                     "再点本按钮。会把当前焦点的 窗口+Tab+Pane 记为该 Agent "
                     "的位置（CONFIRMED，仅本次运行期有效；Agent 退出或窗口"
                     "变化自动失效）。").pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="重新扫描",
                   command=self.app.monitor.rescan).pack(side="left",
                                                          padx=8)
        HelpDot(bar, "清空进程/会话/终端拓扑的运行期缓存并立即重扫（不动 "
                     "Agent 数据目录，也不改任何 Agent 配置）。").pack(
            side="left")
        ttk.Button(bar, text="打开终端",
                   command=lambda: self._open_terminal(
                       self.selected_key)).pack(side="left")
        HelpDot(bar, "按 exact agent_key 唤起该 Agent 的终端：校验窗口身份 → "
                     "选中 exact Tab → 定位 Pane → 恢复并前置窗口。失败时"
                     "fail-closed 并提示原因，绝不猜测。").pack(
            side="left", padx=(6, 0))
        self.include_btn = ttk.Button(
            bar, text="加入并发", command=self._include_selected)
        self.include_btn.pack(side="left", padx=8)
        self.exclude_btn = ttk.Button(
            bar, text="移出并发", command=self._exclude_selected)
        self.exclude_btn.pack(side="left")
        HelpDot(bar, "运行期控制该 Agent 是否参与并发展示（不写配置、不影响"
                     "监听）：移出后它的桌宠/卡片消失，Monitor 仍继续观察；"
                     "重新加入即恢复。").pack(side="left", padx=(6, 0))
        tk.Label(right.body,
                 text="无法自动唯一确定终端位置时的修复：先在 Windows Terminal 切到该 "
                      "Agent 所在的 Tab/Pane，再点\"关联当前 Terminal 位置\"（本次运行期"
                      "有效，Agent 退出或窗口变化自动失效）。",
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(self, 9), wraplength=480,
                 justify="left").pack(fill="x", pady=(8, 0))
        self._detail_sig = None

    def _goto_agent(self, key: str):
        self.selected_key = key
        self._show_page(PAGE_AGENTS)

    def _refresh_agents(self, targets):
        entries = []
        for key, t in sorted(targets.items()):
            snap = t.snapshot
            entries.append((key,
                            f"{snap.kind.label} · "
                            f"{t.instance.project or t.instance.source}",
                            status_text(snap), snap.status.value))
        sig = tuple(entries)
        if sig != self._ag_list_sig:
            self._ag_list_sig = sig
            for child in self.ag_list.winfo_children():
                child.destroy()
            if not entries:
                tk.Label(self.ag_list, text="（没有发现 Agent）",
                         bg=LIGHT.surface, fg=LIGHT.text_secondary,
                         font=pick_font(self, 10), padx=8, pady=8).pack()
            for key, title, status, status_value in entries:
                row = tk.Frame(self.ag_list, bg=LIGHT.surface, cursor="hand2")
                row.pack(fill="x", pady=1)
                tk.Label(row, text=title, bg=LIGHT.surface, fg=LIGHT.text,
                         font=pick_font(self, 10)).pack(side="left", padx=8)
                StatusChip(row, text=status,
                           color=STATUS_COLOR.get(status_value,
                                                  LIGHT.unknown)).pack(
                    side="right", padx=8)
                row.bind("<Button-1>",
                         lambda _e, k=key: self._select_agent(k))
        if self.selected_key and self.selected_key not in targets:
            self.selected_key = ""
        target = targets.get(self.selected_key)
        if target is not None:
            content = self._detail_text(target)
            if content != self._detail_sig:
                self._detail_sig = content
                self.ag_detail.configure(text=content)
            binding = target.terminal
            # 只要还不是 CONFIRMED/HIGH 就允许手动关联修复（§5.10 的
            # fail-closed 逃生口），包括完全没有任何绑定证据的情况
            show_repair = (binding is None or binding.confidence not in (
                BindingConfidence.CONFIRMED, BindingConfidence.HIGH))
            self.repair_button.configure(
                state="normal" if show_repair else "disabled")
        else:
            self.ag_detail.configure(text="在左侧选择一个 Agent")
            self._detail_sig = None

    def _select_agent(self, key: str):
        self.selected_key = key
        self._refresh_agents(self.app.monitor.get_targets())

    def _include_selected(self):
        """运行期加入并发（不写 exact key 到配置，v4plan §6.2）。"""
        if self.selected_key:
            self.app.presentation.set_instance_included(
                self.selected_key, True)
            self.app._aggregate()

    def _exclude_selected(self):
        """运行期移出并发：卡片消失但 Monitor 继续监听。"""
        if self.selected_key:
            self.app.presentation.set_instance_included(
                self.selected_key, False)
            self.app._aggregate()

    def _detail_text(self, target) -> str:
        inst, snap = target.instance, target.snapshot
        binding = target.terminal
        lines = []
        mode_label = mode_text(snap)
        if mode_label == "Unknown" and snap.mode_raw:
            mode_label += f"（原始值：{snap.mode_raw}）"
        lines.append(f"{snap.kind.label} · {status_text(snap)}"
                     + (f" · {phase_text(snap)}" if phase_text(snap) else "")
                     + (f" · Mode: {mode_label}" if mode_label else ""))
        if snap.policy:
            lines.append(f"审批策略：{snap.policy}")
        if snap.goal:
            lines.append(f"目标：{snap.goal}")
        if snap.summary:
            lines.append(f"当前活动：{snap.summary}")
        if snap.waiting_detail:
            lines.append(f"等待内容：{snap.waiting_detail}（请在终端处理）")
        lines.append("")
        lines.append(f"项目目录：{inst.cwd or '?'}")
        lines.append(f"环境：{inst.environment_label}")
        parser_map = {"OK": "正常", "PARTIAL": "部分识别", "UNKNOWN": "未解析"}
        if snap.parser_health:
            line = (f"Session parser："
                    f"{parser_map.get(snap.parser_health, snap.parser_health)}")
            if snap.parser_detail:
                line += f"（{snap.parser_detail}）"
            lines.append(line)
        if binding is not None:
            conf = _BINDING_LABELS.get(binding.confidence,
                                       _binding_conf_value(binding))
            lines.append("")
            lines.append("Terminal")
            lines.append(f"Window　{conf}" + (
                f" · {binding.title[:30]}" if binding.title else ""))
            lines.append(f"Tab　　 {'已学习' if binding.tab_id else '未学习'}")
            lines.append(f"Pane　　{'已定位' if binding.pane_id else '未定位'}")
            if binding.reason:
                detail = f"依据：{binding.reason}"
                if binding.score:
                    detail += f" · score {binding.score}"
                lines.append(detail)
            if binding.confidence == BindingConfidence.AMBIGUOUS:
                lines.append("⚠ 无法唯一确定终端位置：终端审批观察不会归属到该 Agent")
        if not self.app.monitor.terminal_available():
            err = self.app.monitor.terminal_startup_error()
            lines.append("终端交互状态不可读（UIA 不可用）"
                         + (f"：{err[:80]}" if err else ""))
        if snap.stale:
            lines.append("状态可能延迟（该来源进程扫描失败）")

        # ---- 高级诊断（技术 ID 只在这里展示）
        lines.append("")
        lines.append("—— 高级诊断（技术 ID）——")
        token_src = {"proc": "/proc", "create_time": "create_time",
                     "fallback": "fallback"}.get(inst.process_token_source, "")
        token_line = f"PID {inst.pid} · token {inst.process_token or '?'}"
        if token_src:
            token_line += f"（{token_src}）"
        lines.append(token_line)
        if inst.launcher_pids:
            lines.append("launcher pids："
                         + ", ".join(str(p) for p in inst.launcher_pids))
        if inst.tty:
            lines.append(f"TTY {inst.tty} · SID {inst.sid} · PGID {inst.pgid}")
        if inst.uid is not None:
            lines.append(f"uid {inst.uid} · user {inst.user or '?'} "
                         f"· HOME {inst.home or '?'}")
        if inst.wt_session:
            lines.append(f"WT_SESSION {inst.wt_session}")
        if inst.wt_profile_id:
            lines.append(f"WT_PROFILE_ID {inst.wt_profile_id}")
        if snap.session_id:
            lines.append(f"session_id {snap.session_id}")
        if snap.session_file:
            bound = "已绑定" if snap.session_bound else "未解析"
            lines.append(f"会话文件（{bound}）：{snap.session_file}")
        if binding is not None:
            if binding.hwnd:
                lines.append(f"HWND {binding.hwnd} · window_pid "
                             f"{binding.window_pid} · created "
                             f"{binding.window_created:.0f}")
            if binding.tab_id:
                lines.append(f"Tab RuntimeId {tuple(binding.tab_id)}")
            if binding.pane_id:
                lines.append(f"Pane RuntimeId {tuple(binding.pane_id)}")
        return "\n".join(lines)

    def _repair_location(self):
        if not self.selected_key:
            return
        location = self.app.monitor.bind_focused_location(self.selected_key)
        if location is not None:
            self.app.toast("已确认该 Agent 的终端位置（本次运行期有效）", 4)
            self._refresh_agents(self.app.monitor.get_targets())
        else:
            messagebox.showinfo(
                "未关联",
                "没有找到当前焦点的 Windows Terminal 位置。\n"
                "请先在 Windows Terminal 中切到目标 Tab/Pane 再试。",
                parent=self)

    def _open_terminal(self, key: str):
        """exact key 激活（v4plan §5.9）：UI 不直接碰 winkeys。"""
        if not key:
            return
        result = self.app.monitor.activate_target(key)
        if result.code == ActivationCode.OK:
            return
        if result.code == ActivationCode.FOREGROUND_DENIED:
            self.app.toast("终端已选中，Windows 未允许抢前台（已闪烁提醒）", 4)
        elif result.code == ActivationCode.AGENT_GONE:
            self.app.toast("该 Agent 已退出", 4)
        elif result.code == ActivationCode.AMBIGUOUS:
            self.app.toast("终端位置不唯一：可用\"关联当前 Terminal 位置\"修复", 5)
        else:
            self.app.toast("未能定位该 Agent 的终端（"
                           + result.code.value + "）", 4)

    # ================================================== 桌宠与外观
    def _build_look(self):
        page = self._new_page(PAGE_LOOK)
        cfg = self.app.config

        head = Card(page)
        head.pack(fill="x")
        row = tk.Frame(head.body, bg=LIGHT.surface)
        row.pack(fill="x")
        tk.Label(row, text="并发监听显示", bg=LIGHT.surface, fg=LIGHT.text,
                 font=pick_font(self, 11, True)).pack(side="left")
        HelpDot(row, "默认关闭（单目标模式）。开启后同时展示多个 Agent："
                     "开启后仍由 Monitor 统一监听，不产生额外进程。").pack(
            side="left", padx=(6, 0))
        self.concurrent_var = tk.BooleanVar(
            value=bool(cfg.get("presentation.concurrent.enabled", False)))
        ttk.Checkbutton(row, text="（OFF / ON，手动开启）",
                        variable=self.concurrent_var,
                        command=self._toggle_concurrent).pack(side="right")

        self.concurrent_detail = tk.Frame(page, bg=LIGHT.page)
        self.concurrent_detail.pack(fill="x", pady=(8, 0))
        mode_card = Card(self.concurrent_detail)
        mode_card.pack(fill="x")
        mrow = tk.Frame(mode_card.body, bg=LIGHT.surface)
        mrow.pack(fill="x")
        tk.Label(mrow, text="展示方式", bg=LIGHT.surface,
                 fg=LIGHT.text).pack(side="left")
        HelpDot(mrow, "单宠聚合：一只桌宠显示当前最需要注意的 Agent（气泡与"
                      "单个监听一致）。多宠分离：每个 Agent 一只桌宠，自动"
                      "绑定，没绑定 Agent 的桌宠不显示。").pack(
            side="left", padx=(6, 0))
        self.mode_seg = SegmentedControl(
            mrow, ["单宠聚合", "多宠分离"],
            command=self._on_mode_segment)
        self.mode_seg.pack(side="right")
        cap_card = Card(self.concurrent_detail)
        cap_card.pack(fill="x", pady=8)
        crow = tk.Frame(cap_card.body, bg=LIGHT.surface)
        crow.pack(fill="x")
        tk.Label(crow, text="最大并发数（1..8，只是展示上限）",
                 bg=LIGHT.surface,
                 fg=LIGHT.text).pack(side="left")
        HelpDot(crow, "同时展示的桌宠/卡片上限。超过上限的 Agent 仍被正常"
                      "监听（可在 Agents 页看到）；这不是\"强制唤起 N 只"
                      "桌宠\"——没绑定 Agent 的槽位不显示桌宠。").pack(
            side="left", padx=(6, 0))
        self.max_var = tk.IntVar(
            value=int(cfg.get("presentation.concurrent.max_targets", 3)))
        ttk.Spinbox(crow, from_=1, to=8, textvariable=self.max_var,
                    width=4, command=self._save_max_targets).pack(side="right")
        kind_card = Card(self.concurrent_detail)
        kind_card.pack(fill="x")
        tk.Label(kind_card.body, text="参与并发", bg=LIGHT.surface,
                 fg=LIGHT.text).pack(anchor="w")
        tk.Label(kind_card.body,
                 text="这里控制\"并发展示\"；Agent 是否被发现由\"监听与隐私\"页控制。",
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(self, 9)).pack(anchor="w", pady=(2, 4))
        kinds_row = tk.Frame(kind_card.body, bg=LIGHT.surface)
        kinds_row.pack(fill="x")
        HelpDot(kinds_row, "勾选的 Agent 类型才参与并发展示（自动分配与"
                           "聚合卡片都只从这里取候选）。只影响显示，不影响"
                           "监听与发现。").pack(side="left")
        self.eligible_vars = {}
        for kind, label in (("codex", "Codex"), ("claude", "Claude"),
                            ("kimi", "Kimi"), ("pi", "pi")):
            var = tk.BooleanVar(value=bool(cfg.get(
                f"presentation.concurrent.eligible_kinds.{kind}", True)))
            self.eligible_vars[kind] = var
            ttk.Checkbutton(kinds_row, text=label, variable=var,
                            command=lambda k=kind: self._save_eligible(k)
                            ).pack(side="left", padx=8)

        self.fleet_frame = tk.Frame(page, bg=LIGHT.page)
        self._fleet_sig = None

        # 外观（全局默认）
        look_card = Card(page)
        look_card.pack(fill="x", pady=(16, 0))
        lrow = tk.Frame(look_card.body, bg=LIGHT.surface)
        lrow.pack(fill="x")
        tk.Label(lrow, text="外观（全局默认）", bg=LIGHT.surface,
                 fg=LIGHT.text, font=pick_font(self, 11, True)).pack(
            side="left")
        HelpDot(lrow, "对所有桌宠生效的全局外观。多宠分离模式下单个槽位的"
                      "个性化覆盖在外观菜单/后续版本提供。改完点\"应用外观\""
                      "一次性写入。").pack(side="left", padx=(6, 0))
        opts = tk.Frame(look_card.body, bg=LIGHT.surface)
        opts.pack(fill="x", pady=6)
        self.look_vars = {}
        _SLIDER_HELP = {
            "scale": "桌宠与气泡的整体缩放（0.5~2.0）。皮肤会按新尺寸"
                     "重新构建（几十秒），期间保持当前画面。",
            "bubble.relative_width": "气泡宽度的相对系数（0.7~1.6）。",
            "bubble.relative_height": "气泡高度的相对系数（0.8~1.6）。",
            "bubble.relative_font": "气泡字号相对系数（0.75~1.4）。",
            "speed": "动画播放速度倍率（0.3~3.0）：1.0 为皮肤原始节奏。",
        }
        options = [("scale", "整体大小", .5, 2., 1.),
                   ("bubble.relative_width", "气泡相对宽度", .7, 1.6, 1.),
                   ("bubble.relative_height", "气泡相对高度", .8, 1.6, 1.),
                   ("bubble.relative_font", "相对字号", .75, 1.4, 1.),
                   ("speed", "动画速度", .3, 3., 1.)]
        for r, (path, label, lo, hi, default) in enumerate(options):
            lrow2 = tk.Frame(opts, bg=LIGHT.surface)
            lrow2.grid(row=r, column=0, columnspan=3, sticky="ew")
            tk.Label(lrow2, text=label, bg=LIGHT.surface,
                     fg=LIGHT.text).pack(side="left", padx=8, pady=6)
            HelpDot(lrow2, _SLIDER_HELP.get(path, "")).pack(side="left")
            var = tk.DoubleVar(value=float(cfg.get(path, default)))
            self.look_vars[path] = var
            scale = ttk.Scale(lrow2, from_=lo, to=hi, variable=var,
                              length=280)
            scale.pack(side="left")
            scale.bind("<ButtonRelease-1>",
                       lambda _e: self._apply_look())
            tk.Label(lrow2, textvariable=var, width=7,
                     bg=LIGHT.surface).pack(side="left", padx=6)
        row2 = tk.Frame(look_card.body, bg=LIGHT.surface)
        row2.pack(fill="x", pady=6)
        self.font_var = tk.StringVar(value=cfg.get("bubble.font_family"))
        tk.Label(row2, text="字体", bg=LIGHT.surface).pack(side="left")
        HelpDot(row2, "气泡文字字体与字号（8~24）。中文建议 Microsoft "
                      "YaHei UI；等宽字体对长路径更友好。").pack(
            side="left", padx=(6, 8))
        try:
            import tkinter.font as tkfont
            families = sorted(set(tkfont.families(self)))
        except Exception:
            families = []
        ttk.Combobox(row2, textvariable=self.font_var, values=families,
                     width=26).pack(side="left", padx=8)
        self.size_var = tk.IntVar(value=int(cfg.get("bubble.font_size", 11)))
        ttk.Spinbox(row2, from_=8, to=24, textvariable=self.size_var,
                    width=5).pack(side="left", padx=4)
        self.bubble_on_var = tk.BooleanVar(value=cfg.get("bubble.enabled", True))
        ttk.Checkbutton(row2, text="显示气泡",
                        variable=self.bubble_on_var).pack(side="left",
                                                          padx=10)
        HelpDot(row2, "关闭后只保留桌宠动画，不再显示任何状态气泡。").pack(
            side="left")
        self.anim_var = tk.BooleanVar(value=cfg.get("animated", True))
        ttk.Checkbutton(row2, text="播放动画",
                        variable=self.anim_var).pack(side="left")
        HelpDot(row2, "关闭=静态模式（每段动画停在第一帧，更省电）。").pack(
            side="left")
        self.lock_var = tk.StringVar(value=cfg.get("force_state") or "auto")
        ttk.Label(row2, text="动画").pack(side="left", padx=(12, 2))
        ttk.Combobox(row2, textvariable=self.lock_var,
                     values=["auto", "walk", "attack", "die", "special",
                             "sleep"], state="readonly", width=9).pack(
            side="left")
        HelpDot(row2, "锁定某段动画用于观察：auto=按监听状态自动（工作中="
                      "walk、等待批复=die、完成=special、空闲=sleep）。"
                      "锁定只影响显示。").pack(side="left", padx=(6, 0))
        ttk.Button(row2, text="应用外观",
                   command=self._apply_look).pack(side="right")

        # 皮肤
        skin_card = Card(page)
        skin_card.pack(fill="x", pady=(16, 0))
        srow = tk.Frame(skin_card.body, bg=LIGHT.surface)
        srow.pack(fill="x")
        tk.Label(srow, text="皮肤", bg=LIGHT.surface, fg=LIGHT.text,
                 font=pick_font(self, 11, True)).pack(side="left")
        HelpDot(srow, "皮肤目录需含 walk/attack/die/special/sleep 五段素材"
                      "（.webm/.mp4/.gif），黑底或绿底自动抠透明；点\"应用"
                      "皮肤\"后按当前尺寸构建（几十秒）。").pack(
            side="left", padx=(6, 0))
        self.skin_var = tk.StringVar(
            value=str(cfg.get("skin", "amiya")))
        ttk.Combobox(srow, textvariable=self.skin_var,
                     values=sorted(skins.list_skins()),
                     state="readonly", width=22).pack(side="right")
        srow2 = tk.Frame(skin_card.body, bg=LIGHT.surface)
        srow2.pack(fill="x", pady=(6, 0))
        ttk.Button(srow2, text="应用皮肤",
                   command=self._apply_skin).pack(side="left")
        HelpDot(srow2, "切换皮肤并保存；构建完成前保持当前画面。").pack(
            side="left")
        ttk.Button(srow2, text="导入皮肤（5 个 webm/gif）…",
                   command=self._import_skin).pack(side="left", padx=8)
        HelpDot(srow2, "选择包含 5 段素材的文件夹导入为新皮肤（本地保存，"
                       "不上传）。").pack(side="left")
        tk.Label(skin_card.body,
                 text="皮肤规范：目录下放 walk / attack / die / special / sleep "
                      "五个素材（.webm/.mp4/.gif），黑底或绿底自动抠透明。",
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(self, 9)).pack(anchor="w", pady=(4, 0))

    def _toggle_concurrent(self):
        enabled = self.concurrent_var.get()
        self.app.config.set_and_commit("presentation.concurrent.enabled",
                                       enabled)
        if enabled:
            self.app.toast("并发监听已开启（默认单宠聚合）", 4)
        else:
            self.app.toast("并发监听已关闭（回到单目标）", 4)
        self.app._aggregate()
        if enabled:
            self.concurrent_detail.pack(fill="x", pady=(8, 0))
        else:
            self.concurrent_detail.pack_forget()

    def _on_mode_segment(self, index: int):
        mode = "fleet" if index == 1 else "aggregate"
        slots = list(self.app.config.get(
            "presentation.concurrent.slots") or [])
        if mode == "fleet" and len(slots) < 2:
            import copy as _copy
            for i in range(2, 4):
                slot = _copy.deepcopy(slots[0] if slots else {
                    "id": "", "selector": None, "appearance": None,
                    "placement": {"monitor": "", "u": None, "v": None,
                                  "anchor": None, "manual": False}})
                slot["id"] = f"pet-{i}"
                slot["placement"] = {"monitor": "", "u": None, "v": None,
                                     "anchor": None, "manual": False}
                slots.append(slot)
            self.app.config.set("presentation.concurrent.slots", slots)
        self.app.config.set_and_commit("presentation.concurrent.mode", mode)

    def _save_max_targets(self):
        try:
            value = max(1, min(8, int(self.max_var.get())))
        except (ValueError, tk.TclError):
            return
        self.app.config.set_and_commit(
            "presentation.concurrent.max_targets", value)

    def _save_eligible(self, kind: str):
        self.app.config.set_and_commit(
            f"presentation.concurrent.eligible_kinds.{kind}",
            bool(self.eligible_vars[kind].get()))

    def _refresh_fleet(self, state):
        if state is None or state.mode is not PresentationMode.FLEET:
            self.fleet_frame.pack_forget()
            return
        self.fleet_frame.pack(fill="x", pady=(8, 0))
        targets = self.app.monitor.get_targets()
        rows = []
        for slot_id in self.app.presentation.slot_ids():
            key = state.slot_keys.get(slot_id, "")
            target = targets.get(key) if key else None
            if target is not None:
                agent = (f"{target.snapshot.kind.label} · "
                         f"{target.instance.project or ''}")
                status = ("自动分配" if self.app.presentation.is_auto_bound(slot_id)
                          else "手动绑定")
            elif key:
                agent = "绑定的 Agent 已退出"
                status = "vacant"
            else:
                agent = "未显示（无绑定的 Agent 时不创建桌宠）"
                status = "vacant"
            rows.append((slot_id, agent, status))
        sig = tuple(rows)
        if sig == self._fleet_sig:
            return
        self._fleet_sig = sig
        for child in self.fleet_frame.winfo_children():
            child.destroy()
        tk.Label(self.fleet_frame, text="Fleet 桌宠（多宠分离）",
                 bg=LIGHT.page, fg=LIGHT.text_secondary,
                 font=pick_font(self, 10, True), anchor="w").pack(fill="x")
        for slot_id, agent, status in rows:
            card = Card(self.fleet_frame, padding=12)
            card.pack(fill="x", pady=4)
            row = tk.Frame(card.body, bg=LIGHT.surface)
            row.pack(fill="x")
            tk.Label(row, text=f"{slot_id}　{agent}", bg=LIGHT.surface,
                     fg=LIGHT.text, font=pick_font(self, 10)).pack(
                side="left")
            StatusChip(row, text=status).pack(side="right")
            actions = tk.Frame(card.body, bg=LIGHT.surface)
            actions.pack(fill="x", pady=(4, 0))
            ttk.Button(actions, text="更换 Agent",
                       command=lambda s=slot_id:
                       self.app._open_agent_picker(s)).pack(side="left")
            ttk.Button(actions, text="解除绑定",
                       command=lambda s=slot_id: self._unbind_slot(
                           s)).pack(side="left", padx=6)

    def _unbind_slot(self, slot_id: str):
        """解除绑定：slot 释放 + 该 Agent 移出并发展示（防止自动分配立即
        补位；从 Agents 页"加入并发"可再纳入）。"""
        key = self.app.presentation.slot_binding(slot_id)
        self.app.presentation.unbind_slot(slot_id)
        if key:
            self.app.presentation.set_instance_included(key, False)
        self.app._aggregate()

    def _apply_look(self):
        cfg = self.app.config
        try:
            font_size = max(8, min(24, int(self.size_var.get())))
        except (ValueError, tk.TclError):
            return
        values = {}
        for path, var in self.look_vars.items():
            values[path] = round(var.get(), 2)
        cfg.update_many(values)
        cfg.set("bubble.font_family", self.font_var.get())
        cfg.set("bubble.font_size", font_size)
        cfg.set("bubble.enabled", self.bubble_on_var.get())
        cfg.set("animated", self.anim_var.get())
        cfg.set("force_state",
                "" if self.lock_var.get() == "auto" else self.lock_var.get())
        cfg.commit()
        for view in self.app.pet_manager.views.values():
            view.set_animated(self.anim_var.get())
            view.set_speed(values.get("speed", 1.0))
            view.bubble.invalidate()
            view._win_size = None
        new_scale = values.get("scale")
        if new_scale and new_scale != cfg.get("scale"):
            cfg.set("scale", new_scale)
            self.app.set_scale(new_scale)

    def _apply_skin(self):
        self.app._switch_skin(self.skin_var.get())

    def _import_skin(self):
        src = filedialog.askdirectory(
            title="选择包含 5 个素材文件的文件夹", parent=self)
        if not src:
            return
        import re
        default = re.split(r"[\\/]+", src.rstrip("/\\"))[-1] or "myskin"
        name = default.strip() or "myskin"
        try:
            skins.prepare_import(src, name)
        except Exception as e:
            messagebox.showerror("导入失败", str(e), parent=self)
            return
        self.app.toast(f"正在构建皮肤 {name}（数十秒）…", 60)
        self.app._switch_skin(name)
        self.skin_var.set(name)

    # ================================================== 监听与隐私
    def _build_monitor(self):
        page = self._new_page(PAGE_MONITOR)
        cfg = self.app.config

        card = Card(page)
        card.pack(fill="x")
        drow = tk.Frame(card.body, bg=LIGHT.surface)
        drow.pack(fill="x")
        tk.Label(drow, text="Agent discovery（是否发现/解析）",
                 bg=LIGHT.surface,
                 fg=LIGHT.text, font=pick_font(self, 11, True)).pack(
            side="left", pady=(0, 4))
        HelpDot(drow, "勾选的 Agent 类型才会被扫描发现并解析会话状态；"
                      "取消后该类型立即停止发现（现有实例按退出清理）。只读 "
                      "Agent 自己落盘的会话文件，绝不写任何 Agent 配置。").pack(
            side="left", pady=(0, 4))
        row = tk.Frame(card.body, bg=LIGHT.surface)
        row.pack(fill="x")
        self.kind_vars = {}
        for kind, label in (("claude", "Claude"), ("codex", "Codex"),
                            ("kimi", "Kimi"), ("pi", "pi")):
            var = tk.BooleanVar(
                value=bool(cfg.get(f"monitor.agents.{kind}", True)))
            self.kind_vars[kind] = var
            ttk.Checkbutton(row, text=label, variable=var,
                            command=lambda k=kind: self._save(
                                f"monitor.agents.{k}",
                                self.kind_vars[k].get())).pack(
                side="left", padx=8)

        env = Card(page)
        env.pack(fill="x", pady=8)
        erow0 = tk.Frame(env.body, bg=LIGHT.surface)
        erow0.pack(fill="x")
        tk.Label(erow0, text="Environment", bg=LIGHT.surface,
                 fg=LIGHT.text, font=pick_font(self, 11, True)).pack(
            side="left", pady=(0, 4))
        HelpDot(erow0, "在哪些环境里寻找 Agent 进程：Windows=原生进程"
                       "（psutil 枚举）；WSL=各发行版内只读 /proc。终端观察"
                       "见右侧说明。").pack(side="left", pady=(0, 4))
        erow = tk.Frame(env.body, bg=LIGHT.surface)
        erow.pack(fill="x")
        self.windows_var = tk.BooleanVar(
            value=bool(cfg.get("monitor.windows_enabled", True)))
        ttk.Checkbutton(erow, text="Windows", variable=self.windows_var,
                        command=lambda: self._save(
                            "monitor.windows_enabled",
                            self.windows_var.get())).pack(side="left",
                                                          padx=8)
        self.wsl_var = tk.BooleanVar(
            value=bool(cfg.get("monitor.wsl_enabled", True)))
        ttk.Checkbutton(erow, text="WSL", variable=self.wsl_var,
                        command=lambda: self._save(
                            "monitor.wsl_enabled",
                            self.wsl_var.get())).pack(side="left", padx=8)
        terminal_var = tk.BooleanVar(
            value=bool(cfg.get("monitor.terminal_observer", True)))
        ttk.Checkbutton(erow, text="Terminal UIA observation（重启生效）",
                        variable=terminal_var,
                        command=lambda: self._save(
                            "monitor.terminal_observer",
                            terminal_var.get())).pack(side="left", padx=8)
        HelpDot(erow, "用系统官方 UI Automation 接口被动观察 Windows "
                      "Terminal 当前可见区域（识别\"等待审批\"等）。只读"
                      "可见文本、有频率上限，不模拟键盘、不截图、不读 "
                      "scrollback。关闭需重启 DeskPet 生效。").pack(
            side="left")

        priv = Card(page)
        priv.pack(fill="x")
        prow0 = tk.Frame(priv.body, bg=LIGHT.surface)
        prow0.pack(fill="x")
        tk.Label(prow0, text="Privacy", bg=LIGHT.surface,
                 fg=LIGHT.text, font=pick_font(self, 11, True)).pack(
            side="left", pady=(0, 4))
        HelpDot(prow0, "DeskPet 只做被动观察：读 cwd、进程启动 token、"
                       "uid/HOME 与 allowlist 内环境变量；终端文本只做"
                       "字符串匹配归类，绝不写进磁盘/日志/配置。").pack(
            side="left", pady=(0, 4))
        prow = tk.Frame(priv.body, bg=LIGHT.surface)
        prow.pack(fill="x")
        root_meta = tk.BooleanVar(
            value=bool(cfg.get("privacy.wsl_root_metadata_fallback", False)))
        ttk.Checkbutton(prow,
                        text="允许 WSL root metadata fallback（默认关闭）",
                        variable=root_meta,
                        command=lambda: self._save(
                            "privacy.wsl_root_metadata_fallback",
                            root_meta.get())).pack(side="left")
        HelpDot(prow, "WSL 里 root 用户的 /proc 元数据默认拒绝读取（安全"
                      "默认）。开启后用受控 fallback 读取 root Agent 的"
                      "元数据；关闭时这类 Agent 仍会被发现，只是详情"
                      "较少。").pack(side="left")

        adv = Card(page)
        adv.pack(fill="x", pady=8)
        expander = Expander(adv.body, "高级：扫描间隔")
        expander.pack(fill="x")
        arow = tk.Frame(expander.body, bg=LIGHT.surface)
        arow.pack(fill="x")
        HelpDot(arow, "扫描节奏微调（秒）：Windows/WSL=进程枚举间隔，文件"
                      "轮询=会话文件读取间隔。加大更省电，减小响应更快。").pack(
            side="left")
        self.interval_vars = {}
        for path, label, lo, hi in (
                ("windows_scan_sec", "Windows 扫描", 1.0, 30.0),
                ("wsl_scan_sec", "WSL 扫描", 1.0, 60.0),
                ("file_poll_sec", "文件轮询", 0.2, 5.0)):
            tk.Label(arow, text=label, bg=LIGHT.surface).pack(side="left",
                                                              padx=6)
            var = tk.DoubleVar(value=float(cfg.get(f"monitor.{path}", 3.0)))
            self.interval_vars[path] = var
            spin = ttk.Spinbox(arow, from_=lo, to=hi, increment=0.5,
                               textvariable=var, width=5,
                               command=lambda p=path: self._save(
                                   f"monitor.{p}",
                                   self.interval_vars[p].get()))
            spin.pack(side="left", padx=2)
        tk.Label(page, text="DeskPet 只被动监听：不启动 Agent、不配置 hooks、"
                            "不发送键盘、不自动审批。",
                 bg=LIGHT.page, fg=LIGHT.text_secondary,
                 font=pick_font(self, 9)).pack(anchor="w", pady=8)

    def _save(self, path, value):
        self.app.config.set_and_commit(path, value)

    # ================================================== 诊断
    def _build_diag(self):
        page = self._new_page(PAGE_DIAG)
        card = Card(page)
        card.pack(fill="x")
        tk.Label(card.body, text="健康摘要", bg=LIGHT.surface,
                 fg=LIGHT.text, font=pick_font(self, 11, True)).pack(
            anchor="w")
        self.diag_health = tk.Label(card.body, text="", bg=LIGHT.surface,
                                    fg=LIGHT.text,
                                    font=pick_font(self, 10, mono=True),
                                    anchor="nw", justify="left")
        self.diag_health.pack(fill="x", pady=(6, 0))
        perf = Card(page)
        perf.pack(fill="x", pady=8)
        tk.Label(perf.body, text="性能", bg=LIGHT.surface, fg=LIGHT.text,
                 font=pick_font(self, 11, True)).pack(anchor="w")
        self.diag_perf = tk.Label(perf.body, text="", bg=LIGHT.surface,
                                  fg=LIGHT.text,
                                  font=pick_font(self, 10, mono=True),
                                  anchor="nw", justify="left")
        self.diag_perf.pack(fill="x", pady=(6, 0))
        logs = Card(page)
        logs.pack(fill="x", pady=8)
        bar = tk.Frame(logs.body, bg=LIGHT.surface)
        bar.pack(fill="x")
        tk.Label(bar, text="日志（不含终端原文）", bg=LIGHT.surface,
                 fg=LIGHT.text, font=pick_font(self, 11, True)).pack(
            side="left")
        ttk.Button(bar, text="清空诊断",
                   command=self._clear_diagnostics).pack(side="right")
        HelpDot(bar, "清空诊断日志环形缓冲（只影响本页显示，不删任何文件）。").pack(
            side="right", padx=(0, 6))
        self.log_text = tk.Text(logs.body, wrap="word", bg=LIGHT.surface,
                                fg=LIGHT.text,
                                font=pick_font(self, 9, mono=True),
                                height=12, state="disabled",
                                relief="flat")
        self.log_text.pack(fill="both", expand=True, pady=(6, 0))
        self._logs_signature = None
        self._health_signature = None

    def _clear_diagnostics(self):
        monitor = self.app.monitor
        monitor._log_ring.clear()
        try:
            while True:
                monitor.log_q.get_nowait()
        except Exception:
            pass
        self._logs_signature = None
        self.refresh()

    def _refresh_diag(self):
        monitor = self.app.monitor
        stats = dict(monitor.stats())
        stats.update(self.app.pet_manager.stats())
        targets = monitor.get_targets()
        windows_err = getattr(monitor._probe, "windows_probe_error", "")
        lines = ["Process", f"  Windows        "
                 f"{'OK' if not windows_err else 'FAIL'}"]
        probe_snap = monitor._probe.snapshot()
        for key in sorted(k for k in probe_snap if k.startswith("wsl:")):
            sp = probe_snap.get(key)
            lines.append(f"  {key:<14} "
                         f"{'OK' if sp.authoritative else 'FAIL'}")
        lines.append("Session")
        for kind in ("codex", "claude", "kimi", "pi"):
            for t in targets.values():
                if t.snapshot.kind.value == kind:
                    lines.append(f"  {kind:<14} "
                                 f"{t.snapshot.parser_health or 'UNKNOWN'}")
                    break
        lines.append("Terminal")
        lines.append(f"  UIA            "
                     f"{'OK' if monitor.terminal_available() else '不可用'}")
        lines.append(f"  Bindings       confirmed "
                     f"{sum(1 for t in targets.values() if t.terminal and _binding_conf_value(t.terminal) == 'confirmed')}"
                     f" / high "
                     f"{sum(1 for t in targets.values() if t.terminal and _binding_conf_value(t.terminal) == 'high')}"
                     f" / ambiguous "
                     f"{sum(1 for t in targets.values() if t.terminal and _binding_conf_value(t.terminal) == 'ambiguous')}")
        text = "\n".join(lines)
        if text != self._health_signature:
            self._health_signature = text
            self.diag_health.configure(text=text)

        perf = (f"targets={stats.get('targets', 0)}"
                f" · wsl调用={stats.get('wsl_spawn_count', 0)}"
                f" · wsl扫描={stats.get('wsl_scan_count', 0)}"
                f"（{stats.get('wsl_scan_ms', 0)}ms"
                f"/win {stats.get('windows_scan_ms', 0)}ms）"
                f" · metadata={stats.get('metadata_pid_count', 0)}"
                f" · uia事件={stats.get('events', 0)}"
                f" · tab事件={stats.get('tab_events', 0)}"
                f" · 可见读取={stats.get('visible_reads', 0)}"
                f" · uia队列丢弃={stats.get('uia_queue_dropped', 0)}\n"
                f"pet_views={stats.get('pet_views', 0)}"
                f" · cache {stats.get('cache_bytes', 0) // 1024}KB"
                f"/{stats.get('cache_budget', 0) // 1024}KB"
                f"（{stats.get('cache_frames', 0)} 帧）"
                f" · skin_build_pending={stats.get('skin_build_pending', 0)}"
                f" · exit_watched={stats.get('exit_watched', 0)}")
        self.diag_perf.configure(text=perf)

        logs = "\n".join(monitor.recent_logs())
        if logs != self._logs_signature:
            self._logs_signature = logs
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("1.0", logs)
            self.log_text.configure(state="disabled")

    # ================================================== 设置
    def _build_settings(self):
        page = self._new_page(PAGE_SETTINGS)
        from .config import CONFIG_PATH

        auto = Card(page)
        auto.pack(fill="x")
        arow0 = tk.Frame(auto.body, bg=LIGHT.surface)
        arow0.pack(fill="x")
        tk.Label(arow0, text="开机启动", bg=LIGHT.surface,
                 fg=LIGHT.text, font=pick_font(self, 11, True)).pack(
            side="left")
        HelpDot(arow0, "注册表 HKCU Run 键开机启动（当前用户级）。会校验"
                       "登记的命令与当前 pythonw/程序路径一致；DeskPet "
                       "移动位置后显示\"需要修复\"，一键修复重新登记。").pack(
            side="left", padx=(6, 0))
        self.autostart_state = tk.Label(auto.body, text="", bg=LIGHT.surface,
                                        fg=LIGHT.text,
                                        font=pick_font(self, 10))
        self.autostart_state.pack(anchor="w", pady=(4, 0))
        self.autostart_detail = tk.Label(auto.body, text="", bg=LIGHT.surface,
                                         fg=LIGHT.text_secondary,
                                         font=pick_font(self, 9),
                                         wraplength=480, justify="left")
        self.autostart_detail.pack(anchor="w")
        arow = tk.Frame(auto.body, bg=LIGHT.surface)
        arow.pack(fill="x", pady=(6, 0))
        self.autostart_btn = ttk.Button(arow, text="开启",
                                        command=self._toggle_autostart)
        self.autostart_btn.pack(side="left")
        HelpDot(arow, "开启/关闭开机启动（写入当前用户注册表 Run 键，写后"
                      "回读校验）。").pack(side="left")
        self.autostart_repair_btn = ttk.Button(arow, text="修复",
                                               command=self._repair_autostart)
        HelpDot(arow, "登记路径与当前程序不一致（如 DeskPet 被移动过）时，"
                      "重新写入正确命令。").pack(side="left")

        win = Card(page)
        win.pack(fill="x", pady=8)
        tk.Label(win.body, text="窗口与托盘", bg=LIGHT.surface,
                 fg=LIGHT.text, font=pick_font(self, 11, True)).pack(
            anchor="w")
        wrow = tk.Frame(win.body, bg=LIGHT.surface)
        wrow.pack(fill="x", pady=4)
        topmost = tk.BooleanVar(value=bool(
            self.app.config.get("topmost", True)))
        ttk.Checkbutton(wrow, text="窗口置顶（立即生效并保存）",
                        variable=topmost,
                        command=lambda: self._save(
                            "topmost", topmost.get())).pack(side="left")
        HelpDot(wrow, "桌宠窗口始终保持在其他窗口之上。").pack(side="left")
        tray = tk.BooleanVar(value=bool(
            self.app.config.get("tray_enabled", True)))
        ttk.Checkbutton(wrow, text="托盘图标", variable=tray,
                        command=lambda: (self.app.start_tray()
                                         if tray.get()
                                         else self.app.stop_tray())).pack(
            side="left", padx=10)
        HelpDot(wrow, "系统托盘图标：左键显示/隐藏桌宠，右键完整菜单。"
                      "隐藏桌宠后托盘是唯一恢复入口，建议保持开启。").pack(
            side="left")
        ttk.Button(win.body, text="隐藏全部桌宠",
                   command=self.app.hide_pet).pack(anchor="w")
        HelpDot(win.body, "暂时隐藏所有桌宠（托盘图标保留，左键即可恢复；"
                          "监听不受影响）。").pack(anchor="w")

        save_card = Card(page)
        save_card.pack(fill="x")
        svrow = tk.Frame(save_card.body, bg=LIGHT.surface)
        svrow.pack(fill="x")
        tk.Label(svrow, text="配置保存状态", bg=LIGHT.surface,
                 fg=LIGHT.text, font=pick_font(self, 11, True)).pack(
            side="left")
        HelpDot(svrow, "所有设置写入 config.json（原子写入：临时文件+替换，"
                       "失败绝不静默——会在此处标红并给出重试按钮）。").pack(
            side="left", padx=(6, 0))
        self.save_state = tk.Label(save_card.body, text="", bg=LIGHT.surface,
                                   fg=LIGHT.text,
                                   font=pick_font(self, 10))
        self.save_state.pack(anchor="w", pady=(4, 0))
        self._save_error_frame = tk.Frame(save_card.body,
                                          bg=LIGHT.surface)
        self._save_retry_btn = ttk.Button(
            self._save_error_frame, text="重试保存",
            command=self._retry_save)
        HelpDot(save_card.body, "上次写入失败时点这里重试（保留全部待写入"
                                "设置）。").pack(anchor="w")
        tk.Label(save_card.body,
                 text=f"配置文件位置（只读展示）：{CONFIG_PATH}",
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(self, 9)).pack(anchor="w", pady=(6, 0))

    def _toggle_autostart(self):
        result = autostart.toggle()
        self.app.toast("开机自启动已" + ("开启" if result.enabled else "关闭")
                       + ("" if result.ok else f"（{result.reason}）"), 4)
        self._refresh_settings()

    def _repair_autostart(self):
        result = autostart.repair()
        if result.ok:
            self.app.toast("开机自启动已修复", 3)
        else:
            self.app.toast("修复失败：" + result.reason, 5)
        self._refresh_settings()

    def _retry_save(self):
        result = self.app.config.commit()
        if result.ok:
            self.app.toast("设置已写入磁盘", 3)
        self._refresh_settings()

    def _refresh_settings(self):
        from .autostart import AutostartState
        st = autostart.status()
        if st.state == AutostartState.HEALTHY:
            self.autostart_state.configure(text="已开启", fg=LIGHT.done)
            self.autostart_detail.configure(text="")
            self.autostart_repair_btn.pack_forget()
        elif st.state == AutostartState.MISSING:
            self.autostart_state.configure(text="未开启",
                                           fg=LIGHT.text_secondary)
            self.autostart_detail.configure(text="")
            self.autostart_repair_btn.pack_forget()
        elif st.state == AutostartState.STALE:
            self.autostart_state.configure(text="需要修复", fg=LIGHT.waiting)
            self.autostart_detail.configure(
                text=f"注册路径与当前 DeskPet 路径不一致：\n"
                     f"{st.registered_command}")
            self.autostart_repair_btn.pack(side="left", padx=6)
        else:
            self.autostart_state.configure(text="不可用",
                                           fg=LIGHT.text_secondary)
            self.autostart_repair_btn.pack_forget()
        self.autostart_btn.configure(
            text="关闭" if st.state == AutostartState.HEALTHY else "开启")

        result = self.app.config.last_save_result
        if result is None or result.ok:
            self.save_state.configure(text="全部设置已保存", fg=LIGHT.done)
            self._save_error_frame.pack_forget()
        else:
            self.save_state.configure(text="⚠ 最近一次设置没有写入磁盘",
                                      fg=LIGHT.error)
            for child in self._save_error_frame.winfo_children():
                if child is not self._save_retry_btn:
                    child.destroy()
            tk.Label(self._save_error_frame, text=result.error[:120],
                     bg=LIGHT.surface, fg=LIGHT.text_secondary,
                     font=pick_font(self, 9)).pack(side="left")
            self._save_retry_btn.pack(side="left", padx=6)
            self._save_error_frame.pack(anchor="w")

    # ================================================== 刷新
    def refresh(self):
        if self._refreshing:
            return
        self._refreshing = True
        self._refresh_once()

    def _refresh_once(self):
        if not self.winfo_exists():
            return
        if self.state() == "withdrawn":
            self.after(1000, self._refresh_once)
            return
        targets = self.app.monitor.get_targets()
        state = self.app._presentation_state
        try:
            self._refresh_overview(targets)
            self._refresh_agents(targets)
            self._refresh_fleet(state)
            self._refresh_diag()
            self._refresh_settings()
        except Exception:
            pass
        self.after(500, self._refresh_once)
