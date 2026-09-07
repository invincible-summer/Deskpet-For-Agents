"""仪表盘：监听目标管理、外观设置、批复键位、日志、皮肤导入。"""
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from agents.models import Status

from . import autostart, skins

STATUS_LABEL = {
    Status.IDLE: "空闲", Status.WORKING: "工作中", Status.WAITING: "等待批复",
    Status.DONE: "完成", Status.UNKNOWN: "未知",
}


class Dashboard(tk.Toplevel):
    def __init__(self, app):
        self.app = app
        super().__init__(app.root)
        self.title("DeskPet 仪表盘")
        self.geometry("860x620")
        self.minsize(760, 540)
        self.configure(bg="#f5f6f8")

        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self._tab_agents(nb)
        self._tab_look(nb)
        self._tab_keys(nb)
        self._tab_log(nb)
        self._tab_skins(nb)

    # ---------- 监听 ----------
    def _tab_agents(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text=" 监听目标 ")

        cols = ("bound", "kind", "source", "pid", "status", "activity", "session")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=12)
        headers = [("bound", "主绑定", 60), ("kind", "Agent", 100),
                   ("source", "来源", 110), ("pid", "PID", 70),
                   ("status", "状态", 90), ("activity", "当前活动", 300),
                   ("session", "会话文件", 260)]
        for cid, text, width in headers:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.tree.bind("<Double-1>", self._toggle_bind)

        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=8, pady=4)
        ttk.Button(bar, text="★ 设为主绑定（或双击行）",
                   command=self._toggle_bind).pack(side="left")
        ttk.Button(bar, text="自动选择主绑定",
                   command=lambda: self.app.monitor.set_primary("", manual=False)
                   ).pack(side="left", padx=6)
        ttk.Button(bar, text="🎯 手动绑定终端窗口…",
                   command=self._pick_window).pack(side="left", padx=6)
        self._wsl_var = tk.BooleanVar(value=bool(
            self.app.config.get("monitor.wsl_enabled", True)))
        ttk.Checkbutton(bar, text="监听 WSL", variable=self._wsl_var,
                        command=self._toggle_wsl).pack(side="right")

        tip = ttk.Label(f, foreground="#666",
                        text="提示：发现 Agent 时会自动选择一个主绑定（气泡显示它的状态）；"
                             "多个 Agent 可双击行手动指定主绑定，状态“等待批复”可在气泡上一键批准/拒绝。")
        tip.pack(anchor="w", padx=8, pady=(0, 8))

    def _toggle_bind(self, *_):
        sel = self.tree.selection()
        if not sel:
            return
        self.app.monitor.set_primary(sel[0], manual=True)
        self.refresh()

    def _pick_window(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选择一个 Agent", parent=self)
            return
        self.iconify()
        self.app.bind_missing_window(sel[0])

    def _toggle_wsl(self):
        self.app.config.set("monitor.wsl_enabled", bool(self._wsl_var.get()))
        self.app.config.save()

    # ---------- 外观 ----------
    def _tab_look(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text=" 外观与气泡 ")
        cfg = self.app.config

        row = 0
        ttk.Label(f, text="字体：").grid(row=row, column=0, sticky="e", pady=6, padx=8)
        fams = sorted(set(tkfont.families(self)))
        self.font_var = tk.StringVar(value=cfg.get("bubble.font_family"))
        ttk.Combobox(f, textvariable=self.font_var, values=fams,
                     state="readonly", width=28).grid(row=row, column=1, sticky="w")

        ttk.Label(f, text="字号：").grid(row=row, column=2, sticky="e", padx=(20, 0))
        self.size_var = tk.IntVar(value=int(cfg.get("bubble.font_size", 11)))
        ttk.Spinbox(f, from_=8, to=32, textvariable=self.size_var,
                    width=5).grid(row=row, column=3, sticky="w")
        row += 1

        ttk.Label(f, text="文字颜色：").grid(row=row, column=0, sticky="e", pady=6, padx=8)
        self.color = cfg.get("bubble.font_color", "#1f2430")
        self._color_btn = tk.Button(f, bg=self.color, width=6, relief="flat",
                                    command=self._pick_color)
        self._color_btn.grid(row=row, column=1, sticky="w")
        row += 1

        ttk.Label(f, text="气泡固定宽度：").grid(row=row, column=0, sticky="e", pady=6, padx=8)
        self.width_var = tk.IntVar(value=int(cfg.get("bubble.width", 300)))
        ttk.Scale(f, from_=160, to=520, variable=self.width_var,
                  length=240).grid(row=row, column=1, sticky="w")
        ttk.Label(f, text="宠物大小：").grid(row=row, column=2, sticky="e", padx=(20, 0))
        self.scale_var = tk.DoubleVar(value=float(cfg.get("scale", 1.0)))
        ttk.Scale(f, from_=0.5, to=2.0, variable=self.scale_var,
                  length=240).grid(row=row, column=3, sticky="w")
        row += 1

        ttk.Label(f, text="播放速度：").grid(row=row, column=0, sticky="e", pady=6, padx=8)
        self.speed_var = tk.DoubleVar(value=float(cfg.get("speed", 1.0)))
        ttk.Scale(f, from_=0.3, to=3.0, variable=self.speed_var,
                  length=240).grid(row=row, column=1, sticky="w")
        self.anim_var = tk.BooleanVar(value=bool(cfg.get("animated", True)))
        ttk.Checkbutton(f, text="动态模式（取消=静态）",
                        variable=self.anim_var).grid(row=row, column=3, sticky="w")
        row += 1

        self.bubble_on_var = tk.BooleanVar(value=bool(cfg.get("bubble.enabled", True)))
        ttk.Checkbutton(f, text="显示气泡（取消=只留桌宠）",
                        variable=self.bubble_on_var).grid(
            row=row, column=1, columnspan=2, sticky="w")
        ttk.Label(f, text="锁定动画：").grid(row=row, column=2, sticky="e", padx=(20, 0))
        self.lock_var = tk.StringVar(value=str(cfg.get("force_state") or "auto"))
        ttk.Combobox(f, textvariable=self.lock_var, state="readonly", width=10,
                     values=["auto", "walk", "attack", "die", "special", "sleep"]).grid(
            row=row, column=3, sticky="w")
        row += 1

        ttk.Button(f, text="应用", command=self._apply_look).grid(
            row=row, column=1, sticky="w", pady=10)
        for cidx in range(4):
            f.columnconfigure(cidx, weight=1 if cidx in (1, 3) else 0)

        # ---- 常规 ----
        sep = ttk.Separator(f, orient="horizontal")
        sep.grid(row=row + 1, column=0, columnspan=4, sticky="ew", pady=8, padx=8)
        ttk.Label(f, text="常规：").grid(row=row + 2, column=0, sticky="e", padx=8)
        gen = ttk.Frame(f)
        gen.grid(row=row + 2, column=1, columnspan=3, sticky="w")
        self.autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(gen, text="开机自启动", variable=self.autostart_var,
                        command=self._toggle_autostart).pack(side="left")
        self.tray_var = tk.BooleanVar(value=bool(cfg.get("tray_enabled", True)))
        ttk.Checkbutton(gen, text="托盘图标（左键显示/隐藏）", variable=self.tray_var,
                        command=self._toggle_tray).pack(side="left", padx=12)
        ttk.Button(gen, text="🙈 暂时隐藏桌宠", command=self.app.hide_pet).pack(side="left")
        row += 3

        ttk.Label(f, text="批复：").grid(row=row, column=0, sticky="e", padx=8)
        gen2 = ttk.Frame(f)
        gen2.grid(row=row, column=1, columnspan=3, sticky="w")
        self.autoapr_var = tk.BooleanVar(value=bool(cfg.get("auto_approve.enabled", False)))
        ttk.Checkbutton(gen2, text="自动批复所有请求（自动发送批准键，慎用）",
                        variable=self.autoapr_var,
                        command=self._toggle_auto_approve).pack(side="left")
        ttk.Button(gen2, text="⬆ 唤起 Agent 终端（置顶）",
                   command=self.app._on_double_click).pack(side="left", padx=12)
        row += 1

        ttk.Label(f, foreground="#666", justify="left", text=(
            "提示：自动批复适合配合官方免审批模式使用——"
            "Codex 可在 ~/.codex/config.toml 设 approval_policy，"
            "Claude Code 支持 --permission-mode acceptEdits，Kimi CLI 支持 --yolo。")).grid(
            row=row, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 8))

    def _toggle_auto_approve(self):
        self.app.config.set("auto_approve.enabled", bool(self.autoapr_var.get()))
        self.app.config.save()
        self.app.toast("自动批复已" + ("开启" if self.autoapr_var.get() else "关闭"), 3)

    def _toggle_autostart(self):
        from . import autostart
        on = autostart.set_enabled(bool(self.autostart_var.get()))
        self.autostart_var.set(on)
        self.app.toast("开机自启动已" + ("开启" if on else "关闭"), 3)

    def _toggle_tray(self):
        if bool(self.tray_var.get()):
            self.app.start_tray()
        else:
            self.app.stop_tray()

    def _pick_color(self):
        from tkinter import colorchooser
        rgb, hexcolor = colorchooser.askcolor(self.color, parent=self,
                                              title="选择文字颜色")
        if hexcolor:
            self.color = hexcolor
            self._color_btn.config(bg=hexcolor)

    def _apply_look(self):
        cfg = self.app.config
        cfg.set("bubble.font_family", self.font_var.get())
        cfg.set("bubble.font_size", int(self.size_var.get()))
        cfg.set("bubble.font_color", self.color)
        cfg.set("bubble.width", int(self.width_var.get()))
        cfg.set("bubble.enabled", bool(self.bubble_on_var.get()))
        lock = self.lock_var.get()
        cfg.set("force_state", "" if lock == "auto" else lock)
        cfg.set("animated", bool(self.anim_var.get()))
        self.app.animator.set_static(not self.anim_var.get())
        cfg.set("speed", round(float(self.speed_var.get()), 2))
        self.app.animator.set_speed(float(self.speed_var.get()))
        cfg.save()
        new_scale = round(float(self.scale_var.get()), 2)
        if abs(new_scale - float(cfg.get("scale", 1.0))) > 0.01:
            self.app.set_scale(new_scale)
        # 气泡相关设置立即生效（按锚点重建窗口，桌宠不移动）
        self.app.apply_bubble_settings()
        self.app.toast("外观已更新", 2)

    # ---------- 批复键位 ----------
    def _tab_keys(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text=" 批复键位 ")
        ttk.Label(f, foreground="#666", justify="left", text=(
            "各 Agent 在终端里确认批复所用的按键（逗号分隔可发多键，如 Return,Return）。\n"
            "默认：Claude=Enter（选中默认 Yes）；Codex=y；Kimi=Enter。"
            "修改后点“保存键位”。")).pack(anchor="w", padx=10, pady=8)
        self.key_vars: dict[tuple[str, str], tk.StringVar] = {}
        grid = ttk.Frame(f)
        grid.pack(anchor="w", padx=16)
        for r, kind in enumerate(("claude", "codex", "kimi", "pi")):
            ttk.Label(grid, text={"claude": "Claude Code", "codex": "Codex",
                                  "kimi": "Kimi CLI", "pi": "pi"}[kind]).grid(
                row=r, column=0, sticky="e", padx=(0, 8), pady=4)
            for c, act in enumerate(("approve", "deny")):
                var = tk.StringVar(value=str(self.app.config.get(
                    f"keys.{kind}.{act}", "")))
                self.key_vars[(kind, act)] = var
                ttk.Entry(grid, textvariable=var, width=18).grid(
                    row=r, column=1 + c, padx=6, pady=4)
            if r == 0:
                ttk.Label(grid, text="批准").grid(row=0, column=1)
                ttk.Label(grid, text="拒绝").grid(row=0, column=2)
        ttk.Button(f, text="保存键位", command=self._save_keys).pack(
            anchor="w", padx=16, pady=10)

    def _save_keys(self):
        for (kind, act), var in self.key_vars.items():
            self.app.config.set(f"keys.{kind}.{act}", var.get().strip())
        self.app.config.save()
        self.app.toast("批复键位已保存", 2)

    # ---------- 日志 ----------
    def _tab_log(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text=" 实时日志 ")
        self.log_text = tk.Text(f, wrap="none", bg="#101418", fg="#9fd6a0",
                                insertbackground="#9fd6a0", relief="flat",
                                font=("Consolas", 10))
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)
        for line in self.app.monitor.recent_logs():
            self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        try:
            self.app.monitor.log_q.queue.clear()
        except Exception:
            pass
        self.after(700, self._pump_log)

    def _pump_log(self):
        import queue
        q = self.app.monitor.log_q
        try:
            while True:
                line = q.get_nowait()
                self.log_text.insert("end", line + "\n")
                if float(self.log_text.index("end-1c").split(".")[0]) > 400:
                    self.log_text.delete("1.0", "100.0")
                self.log_text.see("end")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(700, self._pump_log)

    # ---------- 皮肤 ----------
    def _tab_skins(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text=" 皮肤 ")
        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=8, pady=8)
        self.skin_var = tk.StringVar(value=str(self.app.config.get("skin", "amiya")))
        self.skin_combo = ttk.Combobox(bar, textvariable=self.skin_var,
                                       values=sorted(skins.list_skins()),
                                       state="readonly", width=24)
        self.skin_combo.pack(side="left")
        ttk.Button(bar, text="应用皮肤", command=self._apply_skin).pack(
            side="left", padx=6)
        ttk.Button(bar, text="📁 导入皮肤（5 个 webm/gif）…",
                   command=self._import_skin).pack(side="left", padx=6)
        ttk.Label(f, foreground="#666", justify="left", text=(
            "皮肤规范：目录下放置五个素材文件，命名必须为\n"
            "  walk（工作中）、attack（下达指令）、die（等待批复/中断）、\n"
            "  special（完成庆祝，自动×3）、sleep（无任务睡觉）\n"
            "支持 .webm / .mp4 / .gif，黑底或绿底素材会自动抠成透明。")).pack(
            anchor="w", padx=10)

    def _apply_skin(self):
        name = self.skin_var.get()
        self.app.config.set("skin", name)
        self.app.config.save()
        self.app._switch_skin(name)

    def _import_skin(self):
        src = filedialog.askdirectory(title="选择包含 5 个素材文件的文件夹",
                                      parent=self)
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
        self.app.config.set("skin", name)
        self.app.config.save()
        self.app.toast(f"正在构建皮肤 {name}（数十秒）…", 60)
        self.app._apply_skin()
        self.skin_combo.config(values=sorted(skins.list_skins()))
        self.skin_var.set(name)

    # ---------- 刷新 ----------
    def refresh(self):
        if not self.winfo_exists() or getattr(self, "_refreshing", False):
            return
        self._refreshing = True
        self._refresh_once()

    def _refresh_once(self):
        if not self.winfo_exists():
            return
        _insts, snaps = self.app.monitor.get_state()
        monitor = self.app.monitor
        rows = {}
        for key, s in snaps.items():
            rows[key] = ("★" if monitor.is_bound(key) else "", s.kind.label,
                         s.source, s.pid, STATUS_LABEL.get(s.status, s.status.value),
                         (s.bubble_text() or "")[:80], s.session_file)
        for key in list(rows):
            if self.tree.exists(key):
                self.tree.item(key, values=rows[key])
            else:
                self.tree.insert("", "end", iid=key, values=rows[key])
        for key in self.tree.get_children():
            if key not in rows:
                self.tree.delete(key)
        self.after(1200, self._refresh_once)
