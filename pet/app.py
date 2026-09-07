"""PetApp：状态机聚合 + 气泡调度 + 右键菜单 + 批复动作 + 托盘/隐藏/自启。

位置模型：只记“锚点”（桌宠底部中心的屏幕坐标），窗口尺寸/位置全部由锚点
反推；拖动时更新锚点。气泡高度变化、换肤、缩放都不会让桌宠漂移。
"""
import queue
import time
import tkinter as tk

from actions import approver
from agents.models import Status
from agents.monitor import Monitor

from . import autostart, skins
from .animator import Animator
from .bubble import BubbleModel, BubbleRenderer
from .config import KEY_LABEL
from .dashboard import Dashboard
from .petwindow import MAGIC, PetWindow
from .tray import TrayIcon

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
        self.monitor = Monitor(config)
        self.dashboard: Dashboard | None = None
        self.tray: TrayIcon | None = None

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

        self.animator.bind_tick(self._redraw)
        self.win.on_menu = self._build_menu
        self.win.on_click_button = self._on_bubble_button
        self.win.on_interact = self._on_double_click
        self.win.on_moved = self._on_moved

        if self.anchor is None:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            self.anchor = (sw - 300, sh - 240)

        self._apply_skin()
        if bool(self.config.get("tray_enabled", True)):
            self.start_tray()

    # ================= 皮肤 =================
    def _gif_height(self) -> int:
        scale = float(self.config.get("scale", 1.0))
        return max(96, min(480, int(round(240 * scale))))

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
        self._build_q = skins.start_build(skin, height, fps, log=None)

    def _poll_build(self):
        if self._closing:
            return
        if self._build_q is not None:
            try:
                kind, payload = self._build_q.get_nowait()
            except queue.Empty:
                pass
            else:
                self._build_q = None
                if kind == "ok":
                    self._skin_ready(payload)
                    self.toast("皮肤就绪！", 2)
                else:
                    self.toast(f"皮肤构建失败：{payload}", 10)
        self.root.after(300, self._poll_build)

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
        self.config.set("scale", round(scale, 2))
        self.config.save()
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
        c.delete("all")
        self.bubble.clear_items()
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

        win_w = max(pw, bw) + MARGIN * 2
        win_h = (bh + GAP if model.visible else 0) + ph + MARGIN
        self._ensure_window(win_w, win_h)

        cw = win_w
        # 2) 宠物（底部居中）与气泡（顶部居中）
        img = self.animator.frame_image()
        if img is not None:
            c.create_image((cw - pw) // 2, win_h - ph, image=img, anchor="nw")
        if model.visible:
            self.bubble.draw((cw - bw) // 2, 0, cw // 2, win_h - ph - GAP)

    def _bubble_model(self) -> BubbleModel:
        m = BubbleModel()
        now = time.time()

        if self._toast and now < self._toast[1]:
            m.visible = True
            m.text = self._toast[0]
            return m
        if self._toast and now >= self._toast[1]:
            self._toast = None

        # 气泡总开关关闭（右键可暂时只留桌宠）
        if not bool(self.config.get("bubble.enabled", True)):
            self._waiting_key = None
            return m

        # 没有存活的 Agent：气泡彻底隐藏，不再显示“没有运行中”占位
        s = self.monitor.primary_snapshot()
        if s is None:
            self._waiting_key = None
            return m

        if s.status == Status.WAITING:
            self._waiting_key = s.key
            m.visible = True
            est = "" if s.exact_waiting else "（推测）"
            m.text = f"⛔等待批复{est}：{s.approval.summary if s.approval else (s.bubble_text() or '')}"
            key = self.config.get(f"keys.{s.kind.value}.approve", "")
            deny = self.config.get(f"keys.{s.kind.value}.deny", "")
            m.approve_label = f"批准({KEY_LABEL.get(key, key)})"
            m.deny_label = f"拒绝({KEY_LABEL.get(deny, deny)})"
            return m

        self._waiting_key = None
        # 固定显示当前任务：优先任务标题（稳定），其次最新活动摘要；不轮播、不加名称前缀
        m.text = s.title if s.title else (s.bubble_text() or "待命中")
        if s.mode and s.status == Status.WORKING:
            m.text = f"{s.mode}｜{m.text}"
        m.visible = True
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
        self.root.after(400, self._poll_monitor)

    def _auto_approve_pass(self):
        """自动批复：对所有等待批复的 Agent 发送批准键（带冷却与焦点还原）。"""
        if not bool(self.config.get("auto_approve.enabled", False)):
            return
        now = time.time()
        _insts, snaps = self.monitor.get_state()
        for s in snaps.values():
            if s.status != Status.WAITING:
                continue
            if now - self._auto_apr_seen.get(s.key, 0) < 4.0:
                continue
            self._auto_apr_seen[s.key] = now
            saved = self.config.get("window_instances", {}).get(s.key)
            ok, msg = approver.send_approval(self.config, s, "approve", saved)
            self._auto_apr_seen[s.key] = time.time()
            if ok:
                self.toast(f"🤖 已自动批准：{s.kind.label}", 4)
                self._set_state("attack", repeat=1)
            else:
                self.toast(f"⚠ 自动批复失败：{msg}", 6)

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
        """双击桌宠：唤起当前 Agent 的终端窗口到最上层；无 Agent 时摸摸头。"""
        if self._raise_current_terminal():
            return
        self.interact()

    def _pick_focus_snapshot(self):
        """优先等待批复，其次工作中，再次任意实例。"""
        _insts, snaps = self.monitor.get_state()
        if not snaps:
            return None
        for wanted in (Status.WAITING, Status.WORKING):
            for s in snaps.values():
                if s.status == wanted:
                    return s
        return next(iter(snaps.values()))

    def _raise_current_terminal(self) -> bool:
        s = self._pick_focus_snapshot()
        if not s:
            self.toast("当前没有监听中的 Agent", 3)
            return False
        saved = self.config.get("window_instances", {}).get(s.key)
        ok, msg = approver.raise_terminal(self.config, s, saved)
        self.toast(("⬆ " if ok else "⚠ ") + msg, 4)
        return ok

    def _on_bubble_button(self, tag: str):
        if tag not in ("approve", "deny") or not self._waiting_key:
            return
        _insts, snaps = self.monitor.get_state()
        snap = snaps.get(self._waiting_key)
        if not snap:
            self.toast("该请求已失效", 3)
            return
        saved = self.config.get("window_instances", {}).get(snap.key)
        ok, msg = approver.send_approval(self.config, snap, tag, saved)
        self.toast(("⚔ " if ok else "⚠ ") + msg, 6)
        if ok:
            self._set_state("attack", repeat=1)

    def bind_missing_window(self, key: str):
        self.toast("请在 3 秒内点击目标终端窗口…", 4)

        def capture():
            title = approver.winkeys.foreground_window_title()
            if title:
                m = dict(self.config.get("window_instances", {}))
                m[key] = title
                self.config.set("window_instances", m)
                self.config.save()
                self.toast(f"已绑定窗口：{title[:40]}", 5)
            else:
                self.toast("未捕获到窗口", 3)

        self.root.after(3000, capture)

    # ================= 显示/隐藏/托盘/自启 =================
    def hide_pet(self):
        self.win.hide()
        self.start_tray()   # 隐藏后必须留托盘入口恢复
        self.toast("桌宠已隐藏，点击托盘图标恢复", 4)

    def show_pet(self):
        self.win.show()

    def toggle_visible(self):
        if self.win.visible:
            self.hide_pet()
        else:
            self.show_pet()

    def start_tray(self):
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
            menu.tk_popup(pt.x, pt.y)
        finally:
            menu.grab_release()

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
        auto_apr = tk.BooleanVar(value=bool(self.config.get("auto_approve.enabled", False)))
        m_sys.add_checkbutton(label="自动批复所有请求（慎用）", variable=auto_apr,
                              command=lambda: self._toggle_auto_approve(auto_apr.get()))
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
        self.config.set("auto_approve.enabled", bool(flag))
        self.config.save()
        self.toast("自动批复已" + ("开启（对所有等待批复自动发送批准键）" if flag else "关闭"), 5)

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
        self.root.after(250, self._ui_tick)
        self.root.after(600_000, self._janitor)
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
        self.root.after(600_000, self._janitor)

    def _ui_tick(self):
        """UI 心跳：皮肤未就绪/静态模式下动画循环不走，气泡与提示也要能刷新。"""
        if self._closing:
            return
        try:
            self._redraw()
        except Exception:
            import traceback
            traceback.print_exc()
        self.root.after(250, self._ui_tick)

    def quit(self):
        self._closing = True
        try:
            self.monitor.stop()
            self.animator.stop()
            if self.tray:
                self.tray.stop()
            self.config.save()
        finally:
            self.root.destroy()
