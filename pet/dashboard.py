"""V3 Dashboard：监听目标 / 详情 / 外观与设置 / 诊断 / 皮肤（plan.md §36-§40）。

不再有：会话页、新建 Codex 会话、发送任务、停止任务、批准/拒绝、
自动批准、审批记录、连接方式、绑定终端/绑定会话文件主流程。
“绑定终端”只在 terminal.confidence == AMBIGUOUS 时作为高级修复入口
出现；“绑定会话文件”彻底取消（最多显示候选 + 重新扫描）。
"""
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from agents.models import BindingConfidence
from actions import winkeys
from . import autostart, skins
from .labels import PHASE_LABELS, STATUS_LABELS, mode_text, phase_text, status_text


class Dashboard(tk.Toplevel):
    def __init__(self, app):
        self.app = app
        super().__init__(app.root)
        self.title('DeskPet · 被动监听仪表盘')
        self.geometry('1020x700')
        self.minsize(860, 600)
        self.protocol('WM_DELETE_WINDOW', self.withdraw)
        self.selected_key = ''
        self._detail_signature = None
        self._refreshing = False
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill='both', expand=True, padx=12, pady=12)
        self._tab_agents(self.nb)
        self._tab_detail(self.nb)
        self._tab_look(self.nb)
        self._tab_log(self.nb)
        self._tab_skins(self.nb)

    # ================================================== 监听目标
    def _tab_agents(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text=' 监听目标 ')
        bar = ttk.Frame(f)
        bar.pack(fill='x', padx=10, pady=8)
        ttk.Button(bar, text='自动跟随', command=lambda: self.app.monitor.reset_primary()).pack(side='left', padx=(0, 6))
        ttk.Button(bar, text='设为当前 Agent', command=self._set_selected).pack(side='left', padx=(0, 6))
        ttk.Button(bar, text='打开终端', command=self._open_terminal).pack(side='left', padx=(0, 6))
        ttk.Button(bar, text='重新扫描', command=lambda: self.app.monitor.rescan()).pack(side='left', padx=(0, 6))

        cols = ('bound', 'kind', 'project', 'env', 'mode', 'status', 'activity')
        self.tree = ttk.Treeview(f, columns=cols, show='headings', height=12)
        for cid, text, width in [('bound', '当前', 46), ('kind', 'Agent', 110),
                                 ('project', '项目', 110), ('env', '环境 / 终端', 210),
                                 ('mode', 'Mode', 90), ('status', '状态', 96),
                                 ('activity', '当前活动', 260)]:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=width, anchor='w')
        self.tree.pack(fill='both', expand=True, padx=10, pady=8)
        self.tree.bind('<Double-1>', self._set_selected)
        self.tree.bind('<<TreeviewSelect>>', self._on_select)

        opts = ttk.Frame(f)
        opts.pack(fill='x', padx=10, pady=8)
        for kind, label in [('claude', 'Claude'), ('codex', 'Codex'),
                            ('kimi', 'Kimi'), ('pi', 'pi')]:
            var = tk.BooleanVar(value=bool(self.app.config.get('monitor.agents.' + kind, True)))
            ttk.Checkbutton(opts, text=label, variable=var,
                            command=lambda k=kind, v=var: self._save('monitor.agents.' + k, v.get())).pack(side='left', padx=(0, 12))
        self._wsl_var = tk.BooleanVar(value=bool(self.app.config.get('monitor.wsl_enabled', True)))
        ttk.Checkbutton(opts, text='监听 WSL', variable=self._wsl_var,
                        command=lambda: self._save('monitor.wsl_enabled', self._wsl_var.get())).pack(side='left')
        ttk.Label(f, text='双击行 = 设为当前 Agent。DeskPet 只被动观察：Goal/Mode/阶段来自会话文件，终端审批来自 UIA 可见区域。',
                  foreground='#687970').pack(anchor='w', padx=10, pady=10)

    def _save(self, path, value):
        self.app.config.set(path, value)
        self.app.config.save()

    def _on_select(self, *_):
        sel = self.tree.selection()
        if sel:
            self.selected_key = sel[0]

    def _set_selected(self, *_):
        sel = self.tree.selection()
        if sel:
            self.app.monitor.set_primary(sel[0], manual=True)

    def _open_terminal(self):
        target = self.app.monitor.get_target(self.selected_key)
        if target is None:
            return
        binding = target.terminal
        if binding is None or not getattr(binding, 'hwnd', 0):
            self.app.toast('未能定位该 Agent 的终端窗口', 4)
            return
        if not winkeys.validate_terminal_window(binding):
            self.app.monitor.rediscover_terminal()
            self.app.toast('终端窗口已变化，正在重新识别', 4)
            return
        if not winkeys.raise_terminal(binding):
            self.app.toast('Windows 未允许切换焦点，已提醒任务栏', 4)

    # ================================================== 详情 / 高级诊断
    def _tab_detail(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text=' 详情 ')
        self.detail_title = tk.StringVar(value='在“监听目标”选择一个 Agent 查看详情')
        ttk.Label(f, textvariable=self.detail_title, font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w', padx=12, pady=10)
        self.detail_body = tk.Text(f, wrap='word', font=('Consolas', 10),
                                   bg='#fffdf8', fg='#34463f', relief='flat',
                                   height=20, state='disabled')
        self.detail_body.pack(fill='both', expand=True, padx=12, pady=6)
        bar = ttk.Frame(f)
        bar.pack(fill='x', padx=12, pady=8)
        self.repair_pane_button = ttk.Button(bar, text='高级：关联当前 Pane', command=self._repair_pane)
        self.repair_pane_button.pack(side='left', padx=(0, 8))
        ttk.Button(bar, text='重新扫描会话', command=lambda: self.app.monitor.rescan()).pack(side='left', padx=(0, 8))
        ttk.Label(f, text='“关联当前 Pane”是异常修复入口：先点击目标终端 pane 使其获得焦点，再点此按钮。绑定只在当前运行期有效。',
                  foreground='#687970').pack(anchor='w', padx=12, pady=6)

    def _repair_pane(self):
        if not self.selected_key:
            return
        if self.app.monitor.bind_focused_pane(self.selected_key):
            self.app.toast('已把当前焦点 pane 关联到该 Agent（运行期）', 4)
        else:
            messagebox.showinfo('未关联', '没有找到获得焦点的终端 Pane。\n请先点击目标终端再试。', parent=self)

    def _detail_text(self, target) -> str:
        inst, snap = target.instance, target.snapshot
        binding = target.terminal
        lines = []
        mode_label = mode_text(snap)
        if mode_label == 'Unknown' and snap.mode_raw:
            mode_label += f'（原始值：{snap.mode_raw}）'
        lines.append(f"{snap.kind.label} · {status_text(snap)}"
                     + (f" · {phase_text(snap)}" if phase_text(snap) else '')
                     + (f" · Mode: {mode_label}" if mode_label else ''))
        if snap.policy:
            lines.append(f"审批策略：{snap.policy}")
        if snap.goal:
            lines.append(f"目标：{snap.goal}")
        if snap.summary:
            lines.append(f"活动：{snap.summary}")
        if snap.waiting_detail:
            lines.append(f"等待内容：{snap.waiting_detail}（请在终端处理）")
        lines.append('')
        lines.append(f"项目目录：{inst.cwd or '?'}")
        lines.append(f"环境：{inst.environment_label}")
        parser_map = {'OK': '正常', 'PARTIAL': '部分识别', 'UNKNOWN': '未解析'}
        if snap.parser_health:
            line = f"Session parser：{parser_map.get(snap.parser_health, snap.parser_health)}"
            if snap.parser_detail:
                line += f"（{snap.parser_detail}）"
            lines.append(line)
        if binding is not None and binding.title:
            conf = {'confirmed': '已确认', 'high': '高置信', 'ambiguous': '无法唯一确定', 'none': '未绑定'}.get(
                binding.confidence.value if hasattr(binding.confidence, 'value') else str(binding.confidence), '?')
            lines.append(f"终端：{binding.title}（{conf}）")
            if binding.reason:
                detail = f"依据：{binding.reason}"
                if binding.score:
                    detail += f" · score {binding.score}"
                    if binding.runner_up_score:
                        detail += f" / 次佳 {binding.runner_up_score}"
                lines.append(detail)
            if binding.confidence == BindingConfidence.AMBIGUOUS:
                lines.append('⚠ 无法唯一确定终端 Pane：终端审批观察不会归属到该 Agent')
        if not self.app.monitor.terminal_available():
            err = self.app.monitor.terminal_startup_error()
            lines.append('终端交互状态不可读（UIA 不可用）'
                         + (f'：{err[:80]}' if err else ''))
        if snap.stale:
            lines.append('状态可能延迟（该来源进程扫描失败）')
        lines.append('')
        lines.append('—— 高级诊断 ——')
        token_src = {'proc': '/proc', 'create_time': 'create_time',
                     'fallback': 'fallback'}.get(inst.process_token_source, '')
        token_line = f"PID {inst.pid} · token {inst.process_token or '?'}"
        if token_src:
            token_line += f"（{token_src}）"
        token_line += f" · started {int(inst.started_at)}"
        lines.append(token_line)
        if inst.launcher_pids:
            lines.append(f"launcher pids：{', '.join(str(p) for p in inst.launcher_pids)}")
        if inst.tty:
            lines.append(f"TTY {inst.tty} · SID {inst.sid} · PGID {inst.pgid}")
        if inst.uid is not None:
            lines.append(f"uid {inst.uid} · user {inst.user or '?'} · HOME {inst.home or '?'}")
        if inst.wt_session:
            lines.append(f"WT_SESSION {inst.wt_session}")
        if inst.wt_profile_id:
            lines.append(f"WT_PROFILE_ID {inst.wt_profile_id}")
        if snap.session_id:
            lines.append(f"session_id {snap.session_id}")
        if snap.session_file:
            bound = '已绑定' if snap.session_bound else '未解析'
            lines.append(f"会话文件（{bound}）：{snap.session_file}")
        else:
            lines.append('会话文件：未解析（可尝试重新扫描）')
        if inst.codex_home:
            lines.append(f"CODEX_HOME {inst.codex_home}")
        if inst.claude_config_dir:
            lines.append(f"CLAUDE_CONFIG_DIR {inst.claude_config_dir}")
        if inst.kimi_code_home:
            lines.append(f"KIMI_CODE_HOME {inst.kimi_code_home}")
        if binding is not None and binding.pane_id:
            lines.append(f"UIA pane runtime {binding.pane_id}")
        if binding is not None and binding.hwnd:
            lines.append(f"HWND {binding.hwnd}")
        return '\n'.join(lines)

    # ================================================== 外观与设置
    def _tab_look(self, nb):
        outer = ttk.Frame(nb)
        nb.add(outer, text=' 外观与设置 ')
        f = ttk.Frame(outer)
        f.pack(fill='x', padx=12, pady=8)
        cfg = self.app.config
        self.look_vars = {}
        options = [('scale', '整体大小', .5, 2., 1.),
                   ('bubble.relative_width', '气泡相对宽度', .7, 1.6, 1.),
                   ('bubble.relative_height', '气泡相对高度', .8, 1.6, 1.),
                   ('bubble.relative_font', '相对字号', .75, 1.4, 1.),
                   ('speed', '动画速度', .3, 3., 1.)]
        for r, (path, label, lo, hi, default) in enumerate(options):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky='e', padx=8, pady=8)
            var = tk.DoubleVar(value=float(cfg.get(path, default)))
            self.look_vars[path] = var
            scale = ttk.Scale(f, from_=lo, to=hi, variable=var, length=320,
                              command=lambda _: self._preview_look())
            scale.grid(row=r, column=1, sticky='ew')
            scale.bind('<ButtonRelease-1>', lambda _: self._apply_look())
            ttk.Label(f, textvariable=var, width=7).grid(row=r, column=2, padx=6)
        presets = ttk.Frame(f)
        presets.grid(row=0, column=3, padx=8)
        for name, value in [('小', .75), ('标准', 1.), ('大', 1.25)]:
            ttk.Button(presets, text=name, width=5, command=lambda v=value: self._preset(v)).pack(side='left', padx=2)
        self.preview = tk.Canvas(outer, height=90, bg='#f3f4f0', highlightthickness=0)
        self.preview.pack(fill='x', padx=18, pady=6)
        self._preview_look()
        row = ttk.Frame(outer)
        row.pack(fill='x', padx=18, pady=8)
        self.font_var = tk.StringVar(value=cfg.get('bubble.font_family'))
        ttk.Label(row, text='字体').pack(side='left', padx=(0, 8))
        ttk.Combobox(row, textvariable=self.font_var, values=sorted(set(tkfont.families(self))), width=30).pack(side='left')
        self.size_var = tk.IntVar(value=int(cfg.get('bubble.font_size', 11)))
        ttk.Label(row, text='基准字号').pack(side='left', padx=8)
        ttk.Spinbox(row, from_=8, to=24, textvariable=self.size_var, width=6).pack(side='left')
        row = ttk.Frame(outer)
        row.pack(fill='x', padx=18, pady=8)
        self.bubble_on_var = tk.BooleanVar(value=cfg.get('bubble.enabled', True))
        self.anim_var = tk.BooleanVar(value=cfg.get('animated', True))
        ttk.Checkbutton(row, text='显示气泡', variable=self.bubble_on_var).pack(side='left')
        ttk.Checkbutton(row, text='播放动画', variable=self.anim_var).pack(side='left', padx=12)
        self.lock_var = tk.StringVar(value=cfg.get('force_state') or 'auto')
        ttk.Label(row, text='动画').pack(side='left')
        ttk.Combobox(row, textvariable=self.lock_var, values=['auto', 'walk', 'attack', 'die', 'special', 'sleep'],
                     state='readonly', width=10).pack(side='left', padx=8)
        terminal_var = tk.BooleanVar(value=bool(cfg.get('monitor.terminal_observer', True)))
        ttk.Checkbutton(row, text='终端交互观察（UIA，重启生效）', variable=terminal_var,
                        command=lambda: self._save('monitor.terminal_observer', terminal_var.get())).pack(side='left', padx=12)
        ttk.Button(row, text='应用外观', command=self._apply_look).pack(side='right')
        row = ttk.Frame(outer)
        row.pack(fill='x', padx=18, pady=8)
        auto = tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(row, text='开机启动', variable=auto,
                        command=lambda: autostart.set_enabled(auto.get())).pack(side='left')
        tray = tk.BooleanVar(value=cfg.get('tray_enabled', True))
        ttk.Checkbutton(row, text='托盘图标', variable=tray,
                        command=lambda: self.app.start_tray() if tray.get() else self.app.stop_tray()).pack(side='left', padx=12)
        ttk.Button(row, text='隐藏桌宠', command=self.app.hide_pet).pack(side='left')
        row = ttk.Frame(outer)
        row.pack(fill='x', padx=18, pady=8)
        root_meta = tk.BooleanVar(value=bool(cfg.get('privacy.wsl_root_metadata_fallback', False)))
        ttk.Checkbutton(row, text='允许使用 WSL root 补充 Agent 进程元数据（重启生效）',
                        variable=root_meta,
                        command=lambda: self._save('privacy.wsl_root_metadata_fallback',
                                                   root_meta.get())).pack(side='left')
        ttk.Label(outer, text='仅读取 cwd、进程启动 token、uid/HOME 和 allowlist 环境变量；默认关闭，关闭时元数据不可读的 Agent 仍会被发现（会话可能显示未解析）。',
                  foreground='#687970').pack(anchor='w', padx=18, pady=(0, 8))
        ttk.Label(outer, text='DeskPet V3 只被动监听：不启动 Agent、不配置 hooks、不发送键盘、不自动审批。',
                  foreground='#687970').pack(anchor='w', padx=18, pady=8)

    def _preset(self, value):
        self.look_vars['scale'].set(value)
        self._preview_look()
        self._apply_look()

    def _preview_look(self):
        if not hasattr(self, 'preview'):
            return
        scale = round(self.look_vars['scale'].get(), 2)
        w = round(300 * scale * self.look_vars['bubble.relative_width'].get())
        self.preview.delete('all')
        self.preview.create_text(14, 18, anchor='w',
                                 text=f'预览 · 宠物 {round(240*scale)} px · 气泡 {w} px', fill='#487f73')
        self.preview.create_text(14, 50, anchor='w', text='Codex · Plan · 编码中',
                                 fill='#34463f',
                                 font=(self.app.config.get('bubble.font_family'),
                                       max(8, round(11 * scale * self.look_vars['bubble.relative_font'].get()))))

    def _apply_look(self):
        cfg = self.app.config
        try:
            font_size = max(8, min(24, int(self.size_var.get())))
        except (ValueError, tk.TclError):
            return
        new_scale = round(self.look_vars['scale'].get(), 2)
        for path, var in self.look_vars.items():
            if path != 'scale':
                cfg.set(path, round(var.get(), 2))
        cfg.set('bubble.font_family', self.font_var.get())
        cfg.set('bubble.font_size', font_size)
        cfg.set('bubble.enabled', self.bubble_on_var.get())
        cfg.set('animated', self.anim_var.get())
        cfg.set('force_state', '' if self.lock_var.get() == 'auto' else self.lock_var.get())
        self.app.animator.set_static(not self.anim_var.get())
        self.app.animator.set_speed(cfg.get('speed'))
        if new_scale != cfg.get('scale'):
            self.app.set_scale(new_scale)
        cfg.save()
        self.app.apply_bubble_settings()

    # ================================================== 诊断
    def _tab_log(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text=' 诊断 ')
        bar = ttk.Frame(f)
        bar.pack(fill='x', padx=10, pady=8)
        ttk.Button(bar, text='清空诊断', command=self._clear_diagnostics).pack(side='left')
        self.perf_var = tk.StringVar(value='')
        ttk.Label(bar, textvariable=self.perf_var, foreground='#687970').pack(side='left', padx=12)
        self.log_text = tk.Text(f, wrap='word', bg='#fffdf8', fg='#34463f',
                                font=('Consolas', 10), state='disabled')
        self.log_text.pack(fill='both', expand=True, padx=10, pady=10)
        self._logs_signature = None

    def _clear_diagnostics(self):
        """清空诊断只清 DeskPet 自己的 log ring 与性能计数（plan §46）。"""
        monitor = self.app.monitor
        monitor._log_ring.clear()
        try:
            while True:
                monitor.log_q.get_nowait()
        except Exception:
            pass
        self._logs_signature = None
        self.refresh()

    # ================================================== 皮肤
    def _tab_skins(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text=' 皮肤 ')
        bar = ttk.Frame(f)
        bar.pack(fill='x', padx=8, pady=8)
        self.skin_var = tk.StringVar(value=str(self.app.config.get('skin', 'amiya')))
        self.skin_combo = ttk.Combobox(bar, textvariable=self.skin_var,
                                       values=sorted(skins.list_skins()),
                                       state='readonly', width=24)
        self.skin_combo.pack(side='left')
        ttk.Button(bar, text='应用皮肤', command=self._apply_skin).pack(side='left', padx=6)
        ttk.Button(bar, text='📁 导入皮肤（5 个 webm/gif）…',
                   command=self._import_skin).pack(side='left', padx=6)
        ttk.Label(f, foreground='#666', justify='left', text=(
            "皮肤规范：目录下放置五个素材文件，命名必须为\n"
            "  walk（工作中）、attack（下达指令）、die（等待批复/中断）、\n"
            "  special（完成庆祝，自动×3）、sleep（无任务睡觉）\n"
            "支持 .webm / .mp4 / .gif，黑底或绿底素材会自动抠成透明。")).pack(
            anchor='w', padx=10)

    def _apply_skin(self):
        name = self.skin_var.get()
        self.app.config.set('skin', name)
        self.app.config.save()
        self.app._switch_skin(name)

    def _import_skin(self):
        src = filedialog.askdirectory(title='选择包含 5 个素材文件的文件夹', parent=self)
        if not src:
            return
        import re
        default = re.split(r'[\\/]+', src.rstrip('/\\'))[-1] or 'myskin'
        name = default.strip() or 'myskin'
        try:
            skins.prepare_import(src, name)
        except Exception as e:
            messagebox.showerror('导入失败', str(e), parent=self)
            return
        self.app.config.set('skin', name)
        self.app.config.save()
        self.app.toast(f'正在构建皮肤 {name}（数十秒）…', 60)
        self.app._apply_skin()
        self.skin_combo.config(values=sorted(skins.list_skins()))
        self.skin_var.set(name)

    # ================================================== 刷新
    def refresh(self):
        if self._refreshing:
            return
        self._refreshing = True
        self._refresh_once()

    def _refresh_once(self):
        if not self.winfo_exists():
            return
        if self.state() == 'withdrawn':
            self.after(1000, self._refresh_once)
            return
        monitor = self.app.monitor
        targets = monitor.get_targets()
        # 主列表：PID 不在主列表（plan §36）
        for key, t in targets.items():
            inst, snap = t.instance, t.snapshot
            binding = t.terminal
            if binding is not None and binding.title:
                env = f"{inst.environment_label} · {binding.title[:24]}"
            else:
                env = inst.environment_label
            values = ('★' if monitor.is_bound(key) else '', snap.kind.label,
                      inst.project or inst.source, env,
                      mode_text(snap) or '—',
                      status_text(snap) + ('…' if snap.stale else ''),
                      (snap.summary or snap.waiting_detail or '')[:80])
            if self.tree.exists(key):
                if tuple(str(v) for v in self.tree.item(key, 'values')) != tuple(str(v) for v in values):
                    self.tree.item(key, values=values)
            else:
                self.tree.insert('', 'end', iid=key, values=values)
        for key in self.tree.get_children():
            if key not in targets:
                self.tree.delete(key)
        if self.selected_key and self.selected_key not in targets:
            self.selected_key = ''
        # 详情页
        target = targets.get(self.selected_key)
        if target is not None:
            self.detail_title.set(f"{target.snapshot.kind.label} · "
                                  f"{target.instance.project or target.instance.source}")
            content = self._detail_text(target)
            if content != self._detail_signature:
                self.detail_body.config(state='normal')
                self.detail_body.delete('1.0', 'end')
                self.detail_body.insert('1.0', content)
                self.detail_body.config(state='disabled')
                self._detail_signature = content
            show_repair = (target.terminal is not None
                           and target.terminal.confidence == BindingConfidence.AMBIGUOUS)
            self.repair_pane_button.config(state='normal' if show_repair else 'disabled')
        else:
            self.detail_title.set('在“监听目标”选择一个 Agent 查看详情')
        # 性能计数 + 日志
        stats = monitor.stats()
        perf = (f"targets={stats.get('targets', 0)}"
                f" · wsl调用={stats.get('wsl_spawn_count', 0)}"
                f" · wsl扫描={stats.get('wsl_scan_count', 0)}"
                f"（{stats.get('wsl_scan_ms', 0)}ms"
                f"/win {stats.get('windows_scan_ms', 0)}ms）"
                f" · uia事件={stats.get('events', 0)}"
                f" · 可见读取={stats.get('visible_reads', 0)}"
                f" · uia队列丢弃={stats.get('uia_queue_dropped', 0)}")
        if self.perf_var.get() != perf:
            self.perf_var.set(perf)
        logs = '\n'.join(monitor.recent_logs())
        if logs != self._logs_signature:
            self.log_text.config(state='normal')
            self.log_text.delete('1.0', 'end')
            self.log_text.insert('1.0', logs)
            self.log_text.config(state='disabled')
            self._logs_signature = logs
        self.after(400, self._refresh_once)
