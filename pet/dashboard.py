"""A lightweight session workspace, shared settings and bounded diagnostics."""
import json
import os
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from agents.models import Status
from actions import winkeys
from . import autostart, skins
from .bubble import metrics

STATUS_LABEL = {Status.IDLE: '待命', Status.WORKING: '工作中', Status.WAITING: '等待审批',
                Status.INPUT: '等待回复', Status.DONE: '完成', Status.ERROR: '错误',
                Status.UNKNOWN: '未知'}
MODES={'兼容监听＋可控新会话':'hybrid','仅监听现有终端':'readonly'}


class Dashboard(tk.Toplevel):
    def __init__(self,app):
        self.app=app
        super().__init__(app.root)
        self.title('DeskPet · 会话与设置')
        self.geometry('980x720'); self.minsize(800,620)
        self.protocol('WM_DELETE_WINDOW',self.withdraw)
        self.selected_key=''
        self._history_signature=None
        self._pending_signature=None
        self._refreshing=False
        self.nb=ttk.Notebook(self); self.nb.pack(fill='both',expand=True,padx=12,pady=12)
        self._tab_agents(self.nb)
        self._tab_session(self.nb)
        self._tab_look(self.nb)
        self._tab_log(self.nb)
        self._tab_skins(self.nb)

    def _mode_control(self,parent):
        row=ttk.Frame(parent); row.pack(fill='x',padx=10,pady=8)
        ttk.Label(row,text='接入方式').pack(side='left',padx=(0,10))
        var=tk.StringVar(value=next((k for k,v in MODES.items() if v==self.app.config.get('connection_mode','hybrid')),next(iter(MODES))))
        combo=ttk.Combobox(row,textvariable=var,values=list(MODES),state='readonly',width=28)
        combo.pack(side='left'); combo.bind('<<ComboboxSelected>>',lambda _:self.app.set_connection_mode(MODES[var.get()]))
        if not hasattr(self,'mode_vars'): self.mode_vars=[]
        self.mode_vars.append(var)
        return row

    def _tab_agents(self,nb):
        f=ttk.Frame(nb); nb.add(f,text=' 监听目标 ')
        bar=self._mode_control(f)
        self.new_button=ttk.Button(bar,text='＋ 新建 Codex 会话',command=self._new_dialog)
        self.new_button.pack(side='right')
        cols=('bound','kind','source','status','activity','session')
        self.tree=ttk.Treeview(f,columns=cols,show='headings',height=12)
        for cid,text,width in [('bound','主绑定',55),('kind','Agent / 接入',125),('source','来源',100),
                               ('status','状态',100),('activity','当前活动',270),('session','会话',160)]:
            self.tree.heading(cid,text=text);self.tree.column(cid,width=width,anchor='w')
        self.tree.pack(fill='both',expand=True,padx=10,pady=8)
        self.tree.bind('<Double-1>',self._toggle_bind)
        bar=ttk.Frame(f);bar.pack(fill='x',padx=10,pady=8)
        for label,cmd in [('设为气泡会话',self._toggle_bind),('查看详情',self._open_selected),
                          ('绑定终端',self._pick_window),('绑定会话文件',self._pick_session_file),
                          ('恢复自动选择',lambda:self.app.monitor.set_primary('',manual=False))]:
            ttk.Button(bar,text=label,command=cmd).pack(side='left',padx=(0,6))
        opts=ttk.Frame(f);opts.pack(fill='x',padx=10,pady=8)
        for kind,label in [('claude','Claude'),('codex','Codex'),('kimi','Kimi'),('pi','pi')]:
            var=tk.BooleanVar(value=bool(self.app.config.get('monitor.agents.'+kind,True)))
            ttk.Checkbutton(opts,text=label,variable=var,command=lambda k=kind,v=var:self._save('monitor.agents.'+k,v.get())).pack(side='left',padx=(0,12))
        self._wsl_var=tk.BooleanVar(value=bool(self.app.config.get('monitor.wsl_enabled',True)))
        ttk.Checkbutton(opts,text='监听 WSL',variable=self._wsl_var,command=lambda:self._save('monitor.wsl_enabled',self._wsl_var.get())).pack(side='left')
        ttk.Label(f,text='双击行固定气泡目标。外部会话只读；可控 Codex 会话支持真实请求审批。',foreground='#687970').pack(anchor='w',padx=10,pady=10)

    def _save(self,path,value):
        self.app.config.set(path,value); self.app.config.save()

    def _toggle_bind(self,*_):
        sel=self.tree.selection()
        if sel:self.app.monitor.set_primary(sel[0],manual=True)

    def _open_selected(self):
        sel=self.tree.selection()
        if sel:self.open_session(sel[0])

    def _pick_window(self):
        sel=self.tree.selection()
        if sel:self.pick_terminal(sel[0])

    def _pick_session_file(self):
        sel=self.tree.selection()
        if not sel:return
        snap=self.app.monitor.get_state()[1].get(sel[0])
        if not snap or snap.connection=='managed':return
        path=filedialog.askopenfilename(parent=self,title='选择该 Agent 的会话 JSONL 文件',filetypes=[('JSONL','*.jsonl')])
        if path:
            bindings=dict(self.app.config.get('monitor.session_bindings',{}));bindings[sel[0]]=path
            self._save('monitor.session_bindings',bindings)

    def pick_terminal(self,key):
        snap=self.app.monitor.get_state()[1].get(key)
        if snap and snap.connection=='managed':
            self.open_session(key);return
        dialog=tk.Toplevel(self);dialog.title('绑定终端窗口');dialog.geometry('720x340')
        ttk.Label(dialog,text='选择与当前会话对应的终端。多标签终端只定位到宿主窗口。').pack(anchor='w',padx=12,pady=12)
        tree=ttk.Treeview(dialog,columns=('title','pid'),show='headings',height=8)
        tree.heading('title',text='窗口标题');tree.heading('pid',text='PID');tree.column('title',width=560)
        tree.pack(fill='both',expand=True,padx=12)
        for hwnd,pid,title,_ in winkeys.terminal_candidates():tree.insert('','end',iid=str(hwnd),values=(title,pid))
        def bind():
            selected=tree.selection()
            if not selected:return
            identity=winkeys.window_identity(int(selected[0]))
            if not identity:
                messagebox.showinfo('窗口已关闭','请重新选择终端窗口',parent=dialog);return
            mapping=dict(self.app.config.get('window_instances',{}));mapping[key]=identity
            self._save('window_instances',mapping);dialog.destroy();self.app.toast('终端绑定已更新',3)
        ttk.Button(dialog,text='绑定选中窗口',command=bind).pack(pady=12)

    def _new_dialog(self):
        if self.app.config.get('connection_mode')!='hybrid':return
        dialog=tk.Toplevel(self);dialog.title('新建 Codex 会话');dialog.resizable(False,False)
        source=tk.StringVar(value='windows');cwd=tk.StringVar(value=os.getcwd())
        for row,label in enumerate(('运行位置','工作目录')):ttk.Label(dialog,text=label).grid(row=row,column=0,padx=12,pady=12)
        ttk.Combobox(dialog,textvariable=source,values=['windows','wsl:Ubuntu'],width=42).grid(row=0,column=1,padx=12)
        ttk.Entry(dialog,textvariable=cwd,width=45).grid(row=1,column=1,padx=12)
        ttk.Label(dialog,text='WSL 填写 wsl:发行版名称，工作目录使用对应 Linux 路径。\n使用现有 Codex 登录与模型设置；新会话默认手动审批。',foreground='#687970').grid(row=2,column=0,columnspan=2,padx=12,pady=8)
        def create():
            if not cwd.get().strip():return
            try:key=self.app.managed.create(cwd.get().strip(),source.get().strip())
            except (ValueError,RuntimeError) as exc:
                messagebox.showerror('无法创建',str(exc),parent=dialog);return
            dialog.destroy();self.open_session(key)
        ttk.Button(dialog,text='创建会话',command=create).grid(row=3,column=1,sticky='e',padx=12,pady=12)

    def _tab_session(self,nb):
        self.session_tab=ttk.Frame(nb);nb.add(self.session_tab,text=' 会话 ')
        bar=ttk.Frame(self.session_tab);bar.pack(fill='x',padx=10,pady=8)
        self.session_title=tk.StringVar(value='选择一个会话查看详情')
        ttk.Label(bar,textvariable=self.session_title).pack(side='left')
        self.auto_var=tk.BooleanVar(value=False)
        self.auto_button=ttk.Checkbutton(bar,text='此会话自动批准真实请求',variable=self.auto_var,command=self._set_auto)
        self.auto_button.pack(side='right')
        self.session_info=tk.StringVar()
        ttk.Label(self.session_tab,textvariable=self.session_info,foreground='#687970',wraplength=880).pack(anchor='w',padx=10)
        self.history=tk.Text(self.session_tab,wrap='word',font=('Microsoft YaHei UI',10),relief='flat',bg='#fffdf8',height=12,state='disabled')
        self.history.pack(fill='both',expand=True,padx=10,pady=8)
        self.pending_frame=ttk.LabelFrame(self.session_tab,text='待处理请求')
        self.pending_frame.pack(fill='x',padx=10,pady=4)
        self.input=tk.Text(self.session_tab,height=3,wrap='word',font=('Microsoft YaHei UI',10))
        self.input.pack(fill='x',padx=10,pady=6)
        self.input.bind('<Control-Return>',lambda _:self._send())
        bar=ttk.Frame(self.session_tab);bar.pack(fill='x',padx=10,pady=8)
        self.send_button=ttk.Button(bar,text='发送任务  Ctrl+Enter',command=self._send);self.send_button.pack(side='right')
        self.stop_button=ttk.Button(bar,text='停止当前任务',command=lambda:self.app.managed.interrupt(self.selected_key));self.stop_button.pack(side='right',padx=8)
        ttk.Button(bar,text='作为气泡会话',command=lambda:self.app.monitor.set_primary(self.selected_key,manual=True)).pack(side='left')
        ttk.Button(bar,text='查看审批记录',command=self._show_audit).pack(side='left',padx=8)

    def open_session(self,key):
        self.selected_key=key;self._history_signature=self._pending_signature=None
        self.nb.select(self.session_tab);self.deiconify();self.lift();self._refresh_session()

    def _set_auto(self):
        try:self.app.managed.set_auto(self.selected_key,bool(self.auto_var.get()))
        except (ValueError,RuntimeError) as exc:self.app.toast(str(exc),4)
        self._refresh_session()

    def _send(self):
        text=self.input.get('1.0','end-1c').strip()
        if not text:return 'break'
        try:
            result=self.app.managed.send(self.selected_key,text)
            if isinstance(result,tuple) and not result[0]:raise ValueError(result[1])
            self.input.delete('1.0','end')
        except (ValueError,RuntimeError) as exc:self.app.toast(str(exc),5)
        return 'break'

    def _decision(self,key,rid,decision):
        ok,msg=self.app.managed.approve(key,rid,decision)
        if not ok:self.app.toast(msg,4)
        self._refresh_session()

    def _answer_dialog(self,key,req):
        dialog=tk.Toplevel(self);dialog.title('回复 Agent');dialog.geometry('650x440')
        params=req.get('params',{});questions=params.get('questions',[]);fields={}
        body=ttk.Frame(dialog);body.pack(fill='both',expand=True,padx=12,pady=12)
        if questions:
            for q in questions:
                ttk.Label(body,text=q.get('question',q.get('header','问题')),wraplength=600).pack(anchor='w',pady=4)
                var=tk.StringVar();fields[q['id']]=var
                options=q.get('options') or []
                if options:
                    ttk.Combobox(body,textvariable=var,values=[o.get('label','') for o in options],width=70).pack(fill='x')
                else:ttk.Entry(body,textvariable=var).pack(fill='x')
        else:
            ttk.Label(body,text=str(params.get('message',req.get('summary','请输入结构化回复'))),wraplength=600).pack(anchor='w')
            ttk.Label(body,text='表单字段（JSON 对象）；取消不会授权请求。',foreground='#687970').pack(anchor='w',pady=6)
            editor=tk.Text(body,height=9);editor.pack(fill='both',expand=True)
            editor.insert('1.0',json.dumps(params.get('requestedSchema',{}),ensure_ascii=False,indent=2))
        def submit():
            try:
                answers={qid:{'answers':[var.get()]} for qid,var in fields.items()} if questions else json.loads(editor.get('1.0','end'))
                if questions and any(not v.get().strip() for v in fields.values()):raise ValueError('请回答所有问题')
                result=self.app.managed.answer(key,req['request_id'],answers)
                if isinstance(result,tuple) and not result[0]:raise ValueError(result[1])
            except (ValueError,RuntimeError) as exc:
                messagebox.showerror('未提交',str(exc),parent=dialog);return
            dialog.destroy()
        ttk.Button(dialog,text='提交回复',command=submit).pack(pady=10)
        if req.get('method') == 'mcpServer/elicitation/request':
            def cancel():
                ok, msg = self.app.managed.cancel(key, req['request_id'])
                if not ok:
                    messagebox.showerror('未取消', msg, parent=dialog)
                    return
                dialog.destroy()
            ttk.Button(dialog, text='取消请求', command=cancel).pack(pady=(0, 10))

    def _show_audit(self):
        details=self.app.managed.details(self.selected_key) or {}
        win=tk.Toplevel(self);win.title('此会话审批记录');win.geometry('720x380')
        text=tk.Text(win,wrap='word');text.pack(fill='both',expand=True)
        text.insert('1.0',json.dumps(details.get('audit',[]),ensure_ascii=False,indent=2));text.config(state='disabled')

    def _refresh_session(self):
        if not self.selected_key:return
        snap=self.app.monitor.get_state()[1].get(self.selected_key)
        details=self.app.managed.details(self.selected_key) or {}
        managed=bool(details) or bool(snap and snap.connection=='managed')
        title=(snap.kind.label if snap else 'Codex')+' · '+('可控会话' if managed else '只读监听')
        self.session_title.set(title)
        self.session_info.set(str(details.get('error') or details.get('cwd') or (snap.cwd if snap else '正在连接…')))
        for widget in (self.send_button,self.stop_button):widget.config(state='normal' if managed else 'disabled')
        self.input.config(state='normal' if managed else 'disabled')
        self.auto_button.config(state='normal' if managed and self.app.config.get('connection_mode')=='hybrid' else 'disabled')
        self.auto_var.set(bool(details.get('auto',False)))
        history=details.get('history',[])
        if managed:
            content='\n\n'.join(f"{item.get('role','')}\n{item.get('text','')}" for item in history)[-160000:]
        elif snap:
            content=f"状态：{STATUS_LABEL.get(snap.status,snap.status.value)}\n目标：{snap.goal}\n\n{snap.summary or snap.last_line}\n\n会话文件：{snap.session_file}\n\n此会话只读，请在对应终端继续操作。"
        else:content='会话已退出或正在连接。'
        if content!=self._history_signature:
            bottom=self.history.yview()[1]>=.98
            self.history.config(state='normal');self.history.delete('1.0','end');self.history.insert('1.0',content);self.history.config(state='disabled')
            if bottom:self.history.see('end')
            self._history_signature=content
        pending=details.get('pending',[])
        signature=repr(pending)
        if signature!=self._pending_signature:
            for child in self.pending_frame.winfo_children():child.destroy()
            if not pending:ttk.Label(self.pending_frame,text='暂无待处理请求',foreground='#687970').pack(anchor='w',padx=8,pady=6)
            for req in pending[:3]:
                row=ttk.Frame(self.pending_frame);row.pack(fill='x',padx=8,pady=5)
                ttk.Label(row,text=req.get('summary','等待回复')[:180],wraplength=640).pack(side='left',fill='x',expand=True)
                rid=req['request_id'];key=self.selected_key;method=req.get('method','');state='normal' if req.get('state','pending')=='pending' else 'disabled'
                if method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval','item/permissions/requestApproval'):
                    ttk.Button(row,text='批准本次',state=state,command=lambda k=key,r=rid:self._decision(k,r,'accept')).pack(side='right',padx=4)
                    ttk.Button(row,text='拒绝',state=state,command=lambda k=key,r=rid:self._decision(k,r,'decline')).pack(side='right')
                else:
                    ttk.Button(row,text='回复…',state=state,command=lambda k=key,r=req:self._answer_dialog(k,r)).pack(side='right')
            if len(pending)>3:ttk.Label(self.pending_frame,text=f'另有 {len(pending)-3} 个请求，处理后依次显示').pack(anchor='w',padx=8)
            self._pending_signature=signature

    def _tab_look(self,nb):
        outer=ttk.Frame(nb);nb.add(outer,text=' 外观与设置 ')
        self._mode_control(outer)
        f=ttk.Frame(outer);f.pack(fill='x',padx=12,pady=8)
        cfg=self.app.config;self.look_vars={}
        options=[('scale','整体大小',.5,2.,1.),('bubble.relative_width','气泡相对宽度',.7,1.6,1.),
                 ('bubble.relative_height','气泡相对高度',.8,1.6,1.),('bubble.relative_font','相对字号',.75,1.4,1.),
                 ('speed','动画速度',.3,3.,1.)]
        for r,(path,label,lo,hi,default) in enumerate(options):
            ttk.Label(f,text=label).grid(row=r,column=0,sticky='e',padx=8,pady=8)
            var=tk.DoubleVar(value=float(cfg.get(path,default)));self.look_vars[path]=var
            scale=ttk.Scale(f,from_=lo,to=hi,variable=var,length=320,command=lambda _:self._preview_look())
            scale.grid(row=r,column=1,sticky='ew')
            scale.bind('<ButtonRelease-1>',lambda _:self._apply_look())
            ttk.Label(f,textvariable=var,width=7).grid(row=r,column=2,padx=6)
        presets=ttk.Frame(f);presets.grid(row=0,column=3,padx=8)
        for name,value in [('小',.75),('标准',1.),('大',1.25)]:
            ttk.Button(presets,text=name,width=5,command=lambda v=value:self._preset(v)).pack(side='left',padx=2)
        self.preview=tk.Canvas(outer,height=90,bg='#f3f4f0',highlightthickness=0);self.preview.pack(fill='x',padx=18,pady=6)
        self._preview_look()
        row=ttk.Frame(outer);row.pack(fill='x',padx=18,pady=8)
        self.font_var=tk.StringVar(value=cfg.get('bubble.font_family'))
        ttk.Label(row,text='字体').pack(side='left',padx=(0,8))
        ttk.Combobox(row,textvariable=self.font_var,values=sorted(set(tkfont.families(self))),width=30).pack(side='left')
        self.size_var=tk.IntVar(value=int(cfg.get('bubble.font_size',11)))
        ttk.Label(row,text='基准字号').pack(side='left',padx=8)
        ttk.Spinbox(row,from_=8,to=24,textvariable=self.size_var,width=6).pack(side='left')
        row=ttk.Frame(outer);row.pack(fill='x',padx=18,pady=8)
        self.bubble_on_var=tk.BooleanVar(value=cfg.get('bubble.enabled',True));self.anim_var=tk.BooleanVar(value=cfg.get('animated',True))
        ttk.Checkbutton(row,text='显示气泡',variable=self.bubble_on_var).pack(side='left')
        ttk.Checkbutton(row,text='播放动画',variable=self.anim_var).pack(side='left',padx=12)
        self.lock_var=tk.StringVar(value=cfg.get('force_state') or 'auto')
        ttk.Label(row,text='动画').pack(side='left');ttk.Combobox(row,textvariable=self.lock_var,values=['auto','walk','attack','die','special','sleep'],state='readonly',width=10).pack(side='left',padx=8)
        ttk.Button(row,text='应用外观',command=self._apply_look).pack(side='right')
        row=ttk.Frame(outer);row.pack(fill='x',padx=18,pady=8)
        auto=tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(row,text='开机启动',variable=auto,command=lambda:autostart.set_enabled(auto.get())).pack(side='left')
        tray=tk.BooleanVar(value=cfg.get('tray_enabled',True))
        ttk.Checkbutton(row,text='托盘图标',variable=tray,command=lambda:self.app.start_tray() if tray.get() else self.app.stop_tray()).pack(side='left',padx=12)
        ttk.Button(row,text='隐藏桌宠',command=self.app.hide_pet).pack(side='left')
        ttk.Label(outer,text='审批按会话设置：在“会话”页选择手动或自动。新会话和重新连接默认手动。',foreground='#687970').pack(anchor='w',padx=18,pady=8)

    def _preset(self,value):
        self.look_vars['scale'].set(value);self._preview_look();self._apply_look()

    def _preview_look(self):
        if not hasattr(self,'preview'):return
        scale=round(self.look_vars['scale'].get(),2)
        w=round(300*scale*self.look_vars['bubble.relative_width'].get());h=round(132*scale*self.look_vars['bubble.relative_height'].get())
        self.preview.delete('all')
        self.preview.create_text(14,18,anchor='w',text=f'预览 · 宠物 {round(240*scale)} px · 气泡 {w} × {h} px',fill='#487f73')
        self.preview.create_text(14,50,anchor='w',text='思考中 · 正在整理气泡布局的改进方案',fill='#34463f',font=(self.app.config.get('bubble.font_family'),max(8,round(11*scale*self.look_vars['bubble.relative_font'].get()))))

    def _apply_look(self):
        cfg=self.app.config
        try:
            font_size=max(8,min(24,int(self.size_var.get())))
        except (ValueError,tk.TclError):return
        new_scale=round(self.look_vars['scale'].get(),2)
        for path,var in self.look_vars.items():
            if path!='scale':cfg.set(path,round(var.get(),2))
        cfg.set('bubble.font_family',self.font_var.get());cfg.set('bubble.font_size',font_size)
        cfg.set('bubble.enabled',self.bubble_on_var.get());cfg.set('animated',self.anim_var.get())
        cfg.set('force_state','' if self.lock_var.get()=='auto' else self.lock_var.get())
        self.app.animator.set_static(not self.anim_var.get());self.app.animator.set_speed(cfg.get('speed'))
        if new_scale!=cfg.get('scale'):self.app.set_scale(new_scale)
        cfg.save();self.app.apply_bubble_settings()

    def _tab_log(self,nb):
        f=ttk.Frame(nb);nb.add(f,text=' 诊断 ')
        self.log_text=tk.Text(f,wrap='word',bg='#fffdf8',fg='#34463f',font=('Consolas',10),state='disabled')
        self.log_text.pack(fill='both',expand=True,padx=10,pady=10)
        self._logs_signature=None

    def refresh(self):
        if self._refreshing:return
        self._refreshing=True;self._refresh_once()

    def _refresh_once(self):
        if not self.winfo_exists():return
        if self.state()=='withdrawn':
            self.after(1000,self._refresh_once);return
        mode=self.app.config.get('connection_mode','hybrid')
        if mode not in MODES.values():
            mode = 'hybrid'
        label=next(k for k,v in MODES.items() if v==mode)
        for var in self.mode_vars:
            if var.get()!=label:var.set(label)
        self.new_button.config(state='normal' if mode=='hybrid' else 'disabled')
        _,snaps=self.app.monitor.get_state();monitor=self.app.monitor
        for key,s in snaps.items():
            values=('★' if monitor.is_bound(key) else '',s.kind.label+(' · 可控' if s.connection=='managed' else ''),s.source,
                    STATUS_LABEL.get(s.status,s.status.value),(s.summary or s.last_line)[:80],s.session_id or s.session_file)
            if self.tree.exists(key):
                if tuple(str(v) for v in self.tree.item(key,'values'))!=tuple(str(v) for v in values):self.tree.item(key,values=values)
            else:self.tree.insert('','end',iid=key,values=values)
        for key in self.tree.get_children():
            if key not in snaps:self.tree.delete(key)
        if self.nb.select()==str(self.session_tab):self._refresh_session()
        logs='\n'.join(self.app.monitor.recent_logs())
        if logs!=self._logs_signature:
            self.log_text.config(state='normal');self.log_text.delete('1.0','end');self.log_text.insert('1.0',logs);self.log_text.config(state='disabled');self._logs_signature=logs
        self.after(400,self._refresh_once)

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

