"""PetApp：状态机聚合 + 气泡调度 + 右键菜单 + 批复动作 + 托盘/隐藏/自启。

位置模型：只记“锚点”（桌宠底部中心的屏幕坐标），窗口尺寸/位置全部由锚点
反推；拖动时更新锚点。气泡高度变化、换肤、缩放都不会让桌宠漂移。
"""
import os
import queue
import time
import tkinter as tk

from actions import approver
from agents.models import Status
from agents.managed import ManagedManager
from agents.summarize import shorten
from agents.monitor import Monitor

from . import autostart, skins
from .animator import Animator
from .bubble import BubbleModel, BubbleRenderer

from .dashboard import Dashboard
from .petwindow import MAGIC, PetWindow


MARGIN = 8
GAP = 4        # 气泡与宠物间距（尾巴）
INTERACT_LINES = [
    "摸摸头～今天也要加油哦",
    "收到！马上冲！",
    "咚咚咚——有人吗～",
    "别戳啦，正在盯着 Agent 呢",
    "我的剑已饥渴难耐了！",
]


class PetApp:
    def __init__(self, config):
        self.config = config
        self.root = tk.Tk()
        self.root.configure(bg=MAGIC)
        self.win = PetWindow(self.root, config)
        self.animator = Animator(self.root)
        self.bubble = BubbleRenderer(self.win.canvas, config)
        self.win.bind_hit(self.bubble.hit_button)
        self.managed = ManagedManager(config)
        self.monitor = Monitor(config)
        self.monitor.managed = self.managed
        self.animator.cache_bytes = int(config.get("animation_cache_mb", 48)) * 1024 * 1024
        self._pet_item = None
        self._pet_image = None
        self._waiting_ref = None
        self._display_key = None
        self._skin_after = None
        self._poll_build_after = None
        self._poll_monitor_after = None
        self._ui_after = None
        self._janitor_after = None
        self.dashboard: Dashboard | None = None
        self.tray: TrayIcon | None = None
        self._active_menu = None

        self.state = "sleep"
        self._waiting_key: str | None = None
        self._toast: tuple[str, float] | None = None
        self._special_until = 0.0
        self._done_seen: dict[str, float] = {}
        self._closing = False
        self._auto_apr_seen: dict[str, float] = {}   # 自动批复冷却

        # 锚点 = 桌宠底部中心（屏幕坐标）
        pos = self.config.get("pet_pos")
        self.anchor: tuple[int, int] | None = tuple(pos) if pos and len(pos) == 2 else None
        self._win_size: tuple[int, int] | None = None
        self._build_q: "queue.Queue" | None = None
        self._build_target = None

        self.animator.bind_tick(self._redraw)
        self.win.on_menu = self._build_menu
        self.win.on_click_button = self._on_bubble_button
        self.win.on_interact = self._on_double_click
        self.win.on_moved = self._on_moved

        if self.anchor is None:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            self.anchor = (sw - 300, sh - 240)

        self._apply_skin()
        if getattr(config, "migration_notice", False):
            self.toast("旧自动按键审批已停用，请在会话页设置协议审批", 8)
        if bool(self.config.get("tray_enabled", True)):
            self.start_tray()

    # ================= 皮肤 =================
    def _gif_height(self) -> int:
        scale = float(self.config.get("scale", 1.0))
        dpi = self.root.winfo_fpixels("1i") / 96
        return max(96, min(960, int(round(240 * scale * dpi))))

    def _apply_skin(self):
        """加载皮肤；目标尺寸缓存缺失时先用现成尺寸顶上，同时后台重建。"""
        skin = self.config.get("skin", "amiya")
        height = self._gif_height()
        fps = int(self.config.get("convert.fps", 12))
        paths = skins.built_gifs(skin, height)
        if paths:
            self._skin_ready(paths)
            return
        # 就近显示，避免桌宠“消失”
        alt = skins.built_gifs_any(skin)
        if alt:
            self._skin_ready(alt)
            self.toast(f"正在生成 {height}px 尺寸（旧尺寸暂时顶替）…", 60)
        else:
            self.toast(f"正在构建皮肤 {skin}（首次需数十秒）…", 60)
        if self._build_q is not None:
            return  # one converter at a time; stale completion starts latest requested size
        self._build_target = (skin, height)
        self._build_q = skins.start_build(skin, height, fps, log=None)

    def _poll_build(self):
        self._poll_build_after = None
        if self._closing:
            return
        if self._build_q is not None:
            try:
                kind, payload = self._build_q.get_nowait()
            except queue.Empty:
                pass
            else:
                self._build_q = None
                if self._build_target != (self.config.get("skin", "amiya"), self._gif_height()):
                    self._apply_skin()
                elif kind == "ok":
                    self._skin_ready(payload)
                    self.toast("皮肤就绪！", 2)
                else:
                    self.toast(f"皮肤构建失败：{payload}", 10)
        self._poll_build_after = self.root.after(300, self._poll_build)

    def _skin_ready(self, paths: dict[str, str]):
        """热替换皮肤：先切换到新动画，再清理旧缓存（桌宠不消失）。"""
        self.animator.load_pool(paths)
        self.animator.set_speed(float(self.config.get("speed", 1.0)))
        state = self.state if self.state in paths else "sleep"
        self.state = state
        # force：即使同名也要切到新尺寸的动画对象
        self.animator.play(state, force=True)
        self.animator.set_static(not bool(self.config.get("animated", True)))
        self.animator.prune()

    def set_scale(self, scale: float):
        self.config.set("scale", round(max(.5, min(2., scale)), 2))
        self.config.save()
        self.apply_bubble_settings()
        if self._skin_after:
            self.root.after_cancel(self._skin_after)
        self._skin_after = self.root.after(300, self._commit_scale)

    def _commit_scale(self):
        self._skin_after = None
        self._apply_skin()

    def _switch_skin(self, name: str):
        self.config.set("skin", name)
        self.config.save()
        self._apply_skin()

    # ================= 几何/绘制 =================
    def _ensure_window(self, w: int, h: int):
        if self._win_size == (w, h):
            return
        self._win_size = (w, h)
        self.win.apply_geometry(w, h, self.anchor[0] - w // 2, self.anchor[1] - h)

    def _on_moved(self):
        """拖动结束：由当前窗口位置更新锚点并持久化。"""
        w, h = self._win_size or (self.win.canvas.winfo_width(),
                                  self.win.canvas.winfo_height())
        self.anchor = self.win.anchor_from_window(w, h)
        self.config.set("pet_pos", [self.anchor[0], self.anchor[1]])
        self.config.save()

    def _redraw(self):
        if not self.win.visible or self.win.dragging:
            return  # 拖动中冻结重绘：避免气泡高度变化把窗口从鼠标下拽走
        c = self.win.canvas

        pw, ph = self.animator.frame_size()

        # 1) 布局气泡（先算，再改窗口，再绘制）；宠物没就绪时也显示气泡提示
        model = self._bubble_model()
        self.bubble.model = model
        bw, bh = self.bubble.layout()
        if pw <= 0:                       # 皮肤未就绪：仅显示气泡
            win_w = bw + MARGIN * 2
            win_h = bh + MARGIN * 2
            self._ensure_window(win_w, win_h)
            self.bubble.draw((win_w - bw) // 2, MARGIN, win_w // 2, win_h - 2)
            return

        win_w = max(pw, bw if model.visible else 0) + MARGIN * 2
        gap = max(2, round(GAP * float(self.config.get("scale", 1)) * self.root.winfo_fpixels("1i") / 96))
        win_h = (bh + gap if model.visible else 0) + ph + MARGIN
        self._ensure_window(win_w, win_h)

        cw = win_w
        # 2) 宠物（底部居中）与气泡（顶部居中）
        img = self.animator.frame_image()
        if img is not None:
            if self._pet_item is None:
                self._pet_item = c.create_image((cw-pw)//2, win_h-ph, image=img, anchor="nw")
            else:
                c.coords(self._pet_item, (cw-pw)//2, win_h-ph)
                if self._pet_image is not img:
                    c.itemconfigure(self._pet_item, image=img)
            self._pet_image = img
        self.bubble.draw((cw-bw)//2, 0, cw//2, win_h-ph-2)

    def _bubble_model(self) -> BubbleModel:
        m = BubbleModel()
        self._waiting_ref = None
        snap = self.monitor.primary_snapshot()
        self._display_key = snap.key if snap else None
        if not bool(self.config.get("bubble.enabled", True)):
            return m
        if self._toast and time.time() < self._toast[1]:
            m.visible, m.status, m.text = True, "DeskPet", shorten(self._toast[0], 160)
            return m
        self._toast = None
        if snap is None:
            return m
        labels = {"idle":"待命", "working":"处理中", "waiting":"等待审批",
                  "input":"等待你的回复", "done":"已完成", "error":"需要留意", "unknown":"状态暂不可读"}
        phases = {"plan":"计划中", "planning":"计划中", "thinking":"思考中", "reading":"阅读中",
                  "coding":"编码中", "executing":"执行中", "testing":"测试中",
                  # 受控会话可能直接转发本地化阶段名；统一映射后避免
                  # 气泡退回笼统的“处理中”。
                  "计划":"计划中", "计划中":"计划中", "思考":"思考中", "思考中":"思考中",
                  "读取":"阅读中", "阅读":"阅读中", "阅读中":"阅读中",
                  "编码":"编码中", "编码中":"编码中", "执行":"执行中", "执行中":"执行中",
                  "测试":"测试中", "测试中":"测试中", "回答":"回答中", "回答中":"回答中",
                  "修改":"编码中", "写入":"编码中"}
        phase = getattr(snap.phase, "value", snap.phase)
        label = phases.get(phase, labels.get(snap.status.value, "处理中")) if snap.status == Status.WORKING else labels.get(snap.status.value, "处理中")
        m.visible = True
        m.status = f"{snap.kind.label} · {label}"
        m.text = shorten(snap.summary or snap.last_line or "等待新的任务", 160)
        m.footer = ("目标 · " + shorten(snap.goal, 60)) if snap.goal else ("查看会话" if snap.connection == "managed" else "打开终端 · 只读监听")
        if snap.status == Status.DONE:
            m.accent = "#487f73"
        elif snap.status.value in ("waiting", "input", "error"):
            m.accent = "#a06b38"
        ap = snap.approval
        if snap.connection == "managed" and ap and ap.exact and ap.state in ("pending", "submitting"):
            m.text = shorten(ap.summary, 160)
            m.submitting = ap.state == "submitting"
            if snap.can_approve:
                m.approve_label = "正在提交" if m.submitting else "批准本次"
                m.deny_label = "拒绝"
                m.request_id = ap.request_id
                self._waiting_ref = (snap.key, ap.request_id)
            else:
                m.footer = "打开会话回复"
        return m

    def toast(self, text: str, sec: float = 3.0):
        self._toast = (text, time.time() + sec)

    def apply_bubble_settings(self):
        """气泡/字体设置变化后：失效缓存并按锚点重建窗口。"""
        self.bubble.invalidate()
        self._win_size = None
        self._redraw()

    # ================= 状态机 =================
    def _poll_monitor(self):
        self._poll_monitor_after = None
        if self._closing:
            return
        try:
            self._aggregate()
        except Exception:
            pass
        try:
            self._auto_approve_pass()
        except Exception:
            pass
        try:
            self._poll_tray_events()
        except Exception:
            pass
        self._poll_monitor_after = self.root.after(400, self._poll_monitor)

    def _auto_approve_pass(self):
        # Protocol manager owns request identity, deduplication and per-session policy.
        pass

    def _aggregate(self):
        now = time.time()
        _insts, snaps = self.monitor.get_state()

        # 锁定动画：固定展示五状态之一（仪表盘/菜单可设）
        locked = str(self.config.get("force_state") or "")
        if locked in ("walk", "attack", "die", "special", "sleep"):
            if locked != self.state or self.animator.current is None:
                self.state = locked
                self.animator.play(locked, repeat=3 if locked == "special" else 0,
                                   force=True)
            return

        bound = [s for s in snaps.values() if self.monitor.is_bound(s.key)]

        if now < self._special_until:
            return

        waiting = [s for s in bound if s.status == Status.WAITING]
        done_now = [s for s in bound if s.status == Status.DONE]
        working = [s for s in bound if s.status == Status.WORKING]

        new_target = None
        for s in done_now:
            if now - self._done_seen.get(s.key, 0) > 12 and now - s.ts < 10:
                self._done_seen[s.key] = now
                new_target = "special"
                break
        if new_target is None:
            if waiting:
                new_target = "die"
            elif working:
                new_target = "walk"
            else:
                new_target = "sleep"

        if new_target == "special":
            self._special_until = now + 12
            self._set_state("special", repeat=3)
        elif new_target != self.state or self.animator.current is None:
            # 状态切换，或单次动画（die/attack）播完后重播以维持状态指示
            self._set_state(new_target, force=self.animator.current is None)

    def _set_state(self, state: str, repeat: int = 0, force: bool = False):
        self.state = state
        self.animator.play(state, repeat=repeat, force=force)

    # ================= 交互 =================
    def interact(self):
        import random
        self.toast(random.choice(INTERACT_LINES), 4)

    def _on_double_click(self):
        if self.monitor.primary_snapshot() is None:
            self.interact()
        else:
            self._raise_current_terminal()

    def _pick_focus_snapshot(self):
        return self.monitor.primary_snapshot()

    def _raise_current_terminal(self) -> bool:
        snap = self._pick_focus_snapshot()
        if snap is None:
            return False
        if snap.connection == "managed":
            self.open_dashboard()
            self.dashboard.open_session(snap.key)
            return True
        saved = self.config.get("window_instances", {}).get(snap.key)
        ok, msg = approver.raise_terminal(self.config, snap, saved)
        self.toast(msg, 4)
        if not ok and not approver.winkeys.find_terminal_window(snap.key, snap.pid, snap.source, [], saved):
            self.bind_missing_window(snap.key)
        return ok

    def _on_bubble_button(self, tag: str):
        if tag == "details":
            self._raise_current_terminal()
            return
        if tag not in ("approve", "deny") or not self._waiting_ref:
            return
        key, request_id = self._waiting_ref
        ok, msg = self.managed.approve(key, request_id, "accept" if tag == "approve" else "decline")
        if not ok:
            self.toast(msg, 5)
        elif tag == "approve":
            self._set_state("attack", repeat=1)
        self._redraw()

    def bind_missing_window(self, key: str):
        self.open_dashboard()
        self.dashboard.pick_terminal(key)

    def set_connection_mode(self, mode):
        if mode not in ("hybrid", "readonly"):
            return
        self.config.set("connection_mode", mode)
        self.config.save()
        self.managed.set_mode(mode)

    # ================= 显示/隐藏/托盘/自启 =================
    def hide_pet(self):
        self.animator.set_paused(True)
        self.win.hide()
        self.start_tray()   # 隐藏后必须留托盘入口恢复
        self.toast("桌宠已隐藏，点击托盘图标恢复", 4)

    def show_pet(self):
        self.win.show()
        self.animator.set_paused(False)
        self._redraw()

    def toggle_visible(self):
        if self.win.visible:
            self.hide_pet()
        else:
            self.show_pet()

    def start_tray(self):
        if os.name != "nt":
            return
        from .tray import TrayIcon
        if self.tray is None:
            self.tray = TrayIcon("DeskPet - 左键显示/隐藏，右键菜单")
            self.tray.start()
        else:
            self.tray.show_icon()
        self.config.set("tray_enabled", True)
        self.config.save()

    def stop_tray(self):
        if self.tray:
            self.tray.stop()
            self.tray = None
        self.config.set("tray_enabled", False)
        self.config.save()

    def toggle_autostart(self) -> bool:
        new = not autostart.is_enabled()
        return autostart.set_enabled(new)

    def _poll_tray_events(self):
        if not self.tray:
            return
        try:
            while True:
                ev = self.tray.events.get_nowait()
                if ev == "left":
                    self.toggle_visible()
                elif ev == "right":
                    self._tray_menu()
                elif ev == "error":
                    self.tray = None
        except queue.Empty:
            pass

    def _tray_menu(self):
        menu = tk.Menu(self.root, tearoff=0)
        self._active_menu = menu
        menu.add_command(
            label="显示桌宠" if not self.win.visible else "隐藏桌宠",
            command=self.toggle_visible)
        menu.add_command(label="打开仪表盘", command=self.open_dashboard)
        menu.add_separator()
        menu.add_command(label="退出", command=self.quit)
        try:
            import ctypes
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            # ``tk_popup`` enters a nested menu loop.  Posting the menu keeps
            # tray events and shutdown callbacks responsive, while ``quit``
            # below unposts and destroys the active menu when the app exits.
            menu.post(pt.x, pt.y)
        except (OSError, tk.TclError):
            try:
                menu.destroy()
            except tk.TclError:
                pass
            self._active_menu = None

    # ================= 右键菜单 =================
    def _build_menu(self, menu: tk.Menu):
        menu.add_command(label="⬆ 唤起 Agent 终端（置顶）", command=self._on_double_click)
        menu.add_command(label="🤚 摸摸头（互动）", command=self.interact)
        menu.add_command(label="📊 打开仪表盘", command=self.open_dashboard)
        menu.add_command(
            label="🙈 暂时隐藏桌宠（托盘可恢复）", command=self.hide_pet)
        menu.add_separator()

        _insts, snaps = self.monitor.get_state()
        m_targets = tk.Menu(menu, tearoff=0)
        m_targets.add_radiobutton(
            label="自动选择主绑定", value="",
            command=lambda: self.monitor.set_primary("", manual=False))
        for key, s in sorted(snaps.items()):
            label = f"{s.kind.label} · {s.source} · pid{s.pid} · {s.status.value}"
            m_targets.add_radiobutton(
                label=("★ " if self.monitor.is_bound(key) else "") + label,
                value=key,
                command=lambda k=key: self.monitor.set_primary(k, manual=True))
        for key, s in sorted(snaps.items()):
            m_targets.add_command(
                label=f"🎯 绑定窗口… {s.kind.label} pid{s.pid}",
                command=lambda k=key: self.bind_missing_window(k))
        menu.add_cascade(label="🎧 监听目标", menu=m_targets)

        m_look = tk.Menu(menu, tearoff=0)
        bubble_on = tk.BooleanVar(value=bool(self.config.get("bubble.enabled", True)))
        m_look.add_checkbutton(label="显示气泡（取消=只留桌宠）", variable=bubble_on,
                               command=lambda: self._toggle_bubble(bubble_on.get()))
        animated = tk.BooleanVar(value=bool(self.config.get("animated", True)))
        m_look.add_checkbutton(label="动态模式（取消=静态）", variable=animated,
                               command=lambda: self._set_animated(animated.get()))
        m_lock = tk.Menu(m_look, tearoff=0)
        m_lock.add_radiobutton(label="自动（按监听状态）", value="",
                               command=lambda: self._set_force_state(""))
        for st_name, label in (("walk", "walk 工作中"), ("attack", "attack 下达指令"),
                               ("die", "die 等待批复"), ("special", "special 完成"),
                               ("sleep", "sleep 睡觉")):
            m_lock.add_radiobutton(label=label, value=st_name,
                                   command=lambda v=st_name: self._set_force_state(v))
        m_look.add_cascade(label="锁定动画", menu=m_lock)
        m_speed = tk.Menu(m_look, tearoff=0)
        for sp in (0.5, 0.75, 1.0, 1.5, 2.0, 3.0):
            m_speed.add_radiobutton(label=f"{sp:g}x", value=sp,
                                    command=lambda v=sp: self._set_speed(v))
        m_look.add_cascade(label="播放速度", menu=m_speed)
        m_scale = tk.Menu(m_look, tearoff=0)
        for sc in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            m_scale.add_radiobutton(label=f"{sc:g}x", value=sc,
                                    command=lambda v=sc: self.set_scale(v))
        m_look.add_cascade(label="大小", menu=m_scale)
        topmost = tk.BooleanVar(value=bool(self.config.get("topmost", True)))
        m_look.add_checkbutton(label="窗口置顶", variable=topmost,
                               command=lambda: self.win.set_topmost(topmost.get()))
        m_skins = tk.Menu(m_look, tearoff=0)
        for name, mf in skins.list_skins().items():
            label = mf.get("title") or name
            m_skins.add_radiobutton(label=f"{label} ({name})", value=name,
                                    command=lambda n=name: self._switch_skin(n))
        m_look.add_cascade(label="皮肤", menu=m_skins)
        menu.add_cascade(label="🎨 外观", menu=m_look)

        m_sys = tk.Menu(menu, tearoff=0)
        m_sys.add_command(label="当前会话审批设置…", command=self.open_dashboard)
        mode_menu = tk.Menu(m_sys, tearoff=0)
        for mode, label in (("hybrid", "兼容监听＋可控新会话"), ("readonly", "仅监听现有终端")):
            mode_menu.add_radiobutton(label=label, value=mode,
                                      command=lambda v=mode: self.set_connection_mode(v))
        m_sys.add_cascade(label="接入方式", menu=mode_menu)
        autostart_on = tk.BooleanVar(value=autostart.is_enabled())
        m_sys.add_checkbutton(label="开机自启动", variable=autostart_on,
                              command=lambda: self._toggle_autostart())
        tray_on = tk.BooleanVar(value=bool(self.config.get("tray_enabled", True)))
        m_sys.add_checkbutton(label="托盘图标", variable=tray_on,
                              command=lambda: self._toggle_tray(tray_on.get()))
        menu.add_cascade(label="⚙ 设置", menu=m_sys)

        menu.add_separator()
        menu.add_command(label="🔄 重建当前皮肤缓存", command=self._rebuild_skin)
        menu.add_command(label="❌ 退出", command=self.quit)

    def _set_animated(self, flag: bool):
        self.config.set("animated", bool(flag))
        self.config.save()
        self.animator.set_static(not flag)

    def _set_speed(self, v: float):
        self.config.set("speed", v)
        self.config.save()
        self.animator.set_speed(v)

    def _toggle_bubble(self, flag: bool):
        self.config.set("bubble.enabled", bool(flag))
        self.config.save()
        self.apply_bubble_settings()

    def _set_force_state(self, v: str):
        self.config.set("force_state", v)
        self.config.save()
        self.toast("锁定动画：" + (v if v else "自动"), 3)

    def _toggle_auto_approve(self, flag: bool):
        snap = self.monitor.primary_snapshot()
        if snap and snap.connection == "managed":
            self.managed.set_auto(snap.key, bool(flag))

    def _toggle_autostart(self):
        on = self.toggle_autostart()
        self.toast("开机自启动已" + ("开启" if on else "关闭"), 3)

    def _toggle_tray(self, flag: bool):
        if flag:
            self.start_tray()
        else:
            self.stop_tray()

    def _rebuild_skin(self):
        import os
        from .config import CACHE_DIR
        d = skins.cache_dir(self.config.get("skin", "amiya"), self._gif_height())
        if os.path.isdir(d):
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        self._apply_skin()

    # ================= 仪表盘 =================
    def open_dashboard(self):
        if self.dashboard is None or not tk.Toplevel.winfo_exists(self.dashboard):
            self.dashboard = Dashboard(self)
        self.dashboard.deiconify()
        self.dashboard.lift()
        self.dashboard.refresh()

    # ================= 生命周期 =================
    def run(self):
        self.monitor.start()
        self._poll_monitor()
        self._poll_build()
        self._ui_after = self.root.after(250, self._ui_tick)
        self._janitor_after = self.root.after(600_000, self._janitor)
        self.root.mainloop()

    def _janitor(self):
        """定时清理：日志/事件队列、转换临时目录、不再存在的皮肤缓存、过多的高度缓存。"""
        if self._closing:
            return
        try:
            import glob
            import shutil
            import tempfile
            self.monitor.trim()
            now = time.time()
            tmp = tempfile.gettempdir()
            for d in glob.glob(os.path.join(tmp, "deskpet_conv_*")):
                try:
                    if now - os.path.getmtime(d) > 3600:
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass
            # 清理不存在皮肤的缓存；每个皮肤最多保留 2 个尺寸
            by_skin: dict[str, list[tuple[float, str]]] = {}
            if os.path.isdir(skins.CACHE_DIR):
                for name in os.listdir(skins.CACHE_DIR):
                    path = os.path.join(skins.CACHE_DIR, name)
                    skin = name.split("@")[0]
                    if skin not in skins.list_skins():
                        shutil.rmtree(path, ignore_errors=True)
                        continue
                    try:
                        by_skin.setdefault(skin, []).append(
                            (os.path.getmtime(path), path))
                    except OSError:
                        pass
            for items in by_skin.values():
                items.sort(reverse=True)
                for _mt, path in items[2:]:
                    shutil.rmtree(path, ignore_errors=True)
            self._log("清理完成") if hasattr(self, "_log") else None
        except Exception:
            pass
        self._janitor_after = self.root.after(600_000, self._janitor)

    def _ui_tick(self):
        """UI 心跳：皮肤未就绪/静态模式下动画循环不走，气泡与提示也要能刷新。"""
        self._ui_after = None
        if self._closing:
            return
        try:
            self._redraw()
        except Exception:
            import traceback
            traceback.print_exc()
        self._ui_after = self.root.after(250, self._ui_tick)

    def quit(self):
        self._closing = True
        try:
            self.managed.stop()
            self.monitor.stop()
            self.animator.stop()
            if self.tray:
                self.tray.stop()
            self.config.save()
        finally:
            # Explicitly leave any nested Tk menu/event loop before tearing
            # down widgets; this also makes tray-driven test exits reliable.
            if self._active_menu is not None:
                try:
                    self._active_menu.unpost()
                    self._active_menu.destroy()
                except tk.TclError:
                    pass
                self._active_menu = None
            for attr in ("_poll_build_after", "_poll_monitor_after", "_ui_after", "_janitor_after", "_skin_after"):
                callback = getattr(self, attr, None)
                if callback is not None:
                    try:
                        self.root.after_cancel(callback)
                    except tk.TclError:
                        pass
                    setattr(self, attr, None)
            try:
                self.root.quit()
            finally:
                self.root.destroy()
