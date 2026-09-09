"""PetApp：V4.1 应用控制器 + 气泡调度 + 右键菜单 + 托盘/隐藏/自启。

V4.1（v4plan §15）：root 是隐藏的 controller Tk root，不是宠物。
宠物 = PetViewManager 管理的 N 个 Toplevel PetView（Fleet）或 1 个
（single/aggregate）。所有 Pet 共享：
  * 一份 SharedAnimationCache（进程级 48 MB 预算）
  * 一个 AnimationScheduler（一个 root.after）
  * 一个 SkinBuildManager（同 skin+height+fps 只 build 一次）
  * 一个 Monitor / TerminalService / PresentationController

UI 激活 Terminal 只允许 Monitor.activate_target(exact agent_key)。
Aggregate 模式下桌宠 body 单击/双击只做互动，绝不激活 Terminal。
"""
import gc
import glob
import os
import queue
import random
import shutil
import tempfile
import time
import tkinter as tk

from agents.models import ActivationCode, Status

from . import autostart, skins
from .dashboard import Dashboard
from .labels import status_text
from .petview import PetView, PetViewManager
from .presentation import PresentationController, PresentationMode, PresentationState
from .ui_coordinator import UiDirty
from .version import APP_LABEL

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
        self.root.withdraw()   # controller root：不是宠物窗口（v4plan §8.1）
        self.root.configure(bg="#101011")

        from agents.monitor import Monitor
        self.monitor = Monitor(config)
        self.presentation = PresentationController(config)
        self.pet_manager = PetViewManager(self.root, config,
                                          self.presentation)
        # v4.3 §7.4：外观唯一运行期修改接口（save 策略由注入回调决定）
        from .appearance import AppearanceController

        def _appearance_render(paths: set[str]):
            # force_state/bubble.* 影响 reconcile 决策或模型可见性，
            # 定向 apply 之外还需一次 PRESENTATION 级 flush
            needs_reconcile = any(
                p == "force_state" or p.startswith("bubble.")
                for p in paths)
            self.ui.request(
                UiDirty.APPEARANCE
                | (UiDirty.PRESENTATION if needs_reconcile
                   else UiDirty.NONE))

        self.appearance = AppearanceController(
            config, self.pet_manager, request_render=_appearance_render)
        self.pet_manager.set_hooks(
            on_activate=self.activate_agent,
            on_menu=self._build_menu,
            on_interact=self._on_interact,
            on_moved=self._on_pet_moved,
            on_double_vacant=self._on_vacant_double_click,
        )
        # 隐式 pet-1 立即创建（single/aggregate 模式也用它）
        self.pet_manager.ensure_view("pet-1")

        # v4.3 §4.3：唯一 bridge timer + render-idle slot（替代
        # _poll_monitor/_ui_tick/_poll_build 三个周期 timer）
        from .ui_coordinator import UiCoordinator
        self.ui = UiCoordinator(
            self.root,
            monitor=self.monitor,
            presentation=self.presentation,
            pet_manager=self.pet_manager,
            dashboard_provider=lambda: self.dashboard,
            tray_drain=self._poll_tray_events,
            apply_batch=self._aggregate,
            apply_toasts=self._apply_toasts,
            tray_enabled=lambda: bool(self.config.get("tray_enabled", True)),
        )
        self.pet_manager.set_render_requester(self.ui.request_view)

        self._janitor_after = None
        self._skin_after = None
        self._reassert_after = None
        # v4.3 §4.5：toast 过期用"最近 expiry 的单次 deadline"
        self._toast_after = None
        self._toast_deadline = None
        # v4.3：进程内最多一个 agent picker（死弹窗防护）
        self._agent_picker = None
        self.dashboard: Dashboard | None = None
        self.tray = None
        self._active_menu = None
        self._presentation_state: PresentationState | None = None

        self._toast: tuple[str, float] | None = None
        # Agent 级反馈（key -> (text, expire)）：只改对应 Agent 自己的
        # 那张卡，其他卡片不收起、不变化（v4.1.4）
        self._agent_toasts: dict[str, tuple[str, float]] = {}
        # 全局 toast 曾覆盖过主卡模型（过期时需要用 targets 重建一次）
        self._toast_applied = False
        self._closing = False

        if getattr(config, "migration_notice", False):
            self.toast(f"{APP_LABEL}：被动监听 · 终端窗口唤起 · 并发需手动开启", 8)
        if bool(self.config.get("tray_enabled", True)):
            self._start_tray_runtime()

    # ================= 交互入口 =================
    def interact(self):
        self.toast(random.choice(INTERACT_LINES), 4)

    def _on_interact(self):
        self.interact()

    def activate_agent(self, key: str):
        """唯一激活入口：UI 只携带 exact agent_key（v4.1.3 §20/§21）。

        只恢复并前置该 Agent 所在的 Windows Terminal 顶层窗口（候选
        由 v3-compatible resolver 给出，confidence 不拦用户显式唤起）；
        DeskPet 不切换 Terminal 标签页、不发送键盘输入。
        """
        result = self.monitor.activate_target(key)
        if result.code == ActivationCode.OK:
            self.agent_toast(key, "已打开该 Agent 的终端窗口"
                             + ("（已自动重新识别）" if result.repaired else ""), 2)
        elif result.code == ActivationCode.FOREGROUND_DENIED:
            self.agent_toast(key, "Windows 未允许将终端置于前台，已闪烁任务栏提醒", 4)
        elif result.code == ActivationCode.AGENT_GONE:
            # 该 Agent 的卡片会随本轮 reconcile 消失，无卡可挂 → 主卡提示
            self.toast("该 Agent 已退出", 4)
        elif result.code == ActivationCode.NO_BINDING:
            self.agent_toast(key, "未能定位该 Agent 的终端窗口", 4)
        elif result.code == ActivationCode.STALE_WINDOW:
            self.agent_toast(key, "原终端窗口已失效，重新识别后仍无法安全打开", 4)
        else:
            self.agent_toast(key, "无法打开该 Agent 的终端窗口", 4)

    def _focus_and_activate(self, key: str):
        """Dashboard"查看并设为当前"类操作：设焦点 + 激活（§8.5）。

        Fleet pet body/bubble、Tray Agent、Dashboard"打开终端"只走
        activate_agent(key)，不偷偷改变 presentation 的 focused 状态。
        """
        self.presentation.set_focus(key)
        self.activate_agent(key)

    def _on_vacant_double_click(self, view: PetView):
        """空 slot 双击 → Agent picker（fleet 绑定入口，v4plan §8.2）。"""
        self._open_agent_picker(view.view_id)

    def _open_agent_picker(self, slot_id: str):
        """空 slot 双击/菜单 → Agent picker（fleet 绑定入口，v4plan §8.2）。

        v4.3：进程内最多一个 picker（重复打开先销毁旧的，杜绝死弹窗
        残留）；Escape/取消/选择后确定性销毁；quit() 一并销毁。
        """
        targets = self.monitor.get_targets()
        if not targets:
            self.toast("当前没有发现任何 Agent", 4)
            return
        self._destroy_agent_picker()
        picker = tk.Toplevel(self.root)
        picker.title("选择要绑定的 Agent")
        picker.geometry("420x300")
        picker.transient(self.root)
        self._agent_picker = picker
        tk.Label(picker, text=f"绑定到 {slot_id}（只影响本次运行期）",
                 font=("Microsoft YaHei UI", 10)).pack(anchor="w", padx=12,
                                                       pady=(10, 4))
        listbox = tk.Listbox(picker, font=("Microsoft YaHei UI", 10))
        listbox.pack(fill="both", expand=True, padx=12, pady=6)
        items = []
        for key, t in sorted(targets.items()):
            s = t.snapshot
            items.append((key, f"{s.kind.label} · "
                          f"{t.instance.project or t.instance.source} · "
                          f"{status_text(s)}"))
            listbox.insert("end", items[-1][1])
        taken = {v.agent_key for v in self.pet_manager.views.values()}

        def _close(_evt=None):
            self._destroy_agent_picker()

        def _bind(_evt=None):
            sel = listbox.curselection()
            if not sel:
                return
            key = items[sel[0]][0]
            if key in taken:
                self.toast("该 Agent 已由其他桌宠展示", 4)
                return
            if self.presentation.bind_slot(slot_id, key):
                self.toast("已绑定（本次运行期有效）", 3)
                self.ui.request(UiDirty.PRESENTATION)
                _close()
            else:
                self.toast("绑定失败：该 Agent 已被占用", 4)

        tk.Button(picker, text="绑定", command=_bind).pack(
            side="left", expand=True, fill="x", padx=(12, 0), pady=(0, 10))
        tk.Button(picker, text="取消", command=_close).pack(
            side="left", expand=True, fill="x", padx=(0, 12), pady=(0, 10))
        listbox.bind("<Double-Button-1>", _bind)
        listbox.bind("<Return>", _bind)
        picker.bind("<Escape>", _close)
        picker.protocol("WM_DELETE_WINDOW", _close)
        listbox.focus_set()

    def _destroy_agent_picker(self):
        """确定性关闭 agent picker（幂等；quit 与重复打开时调用）。"""
        picker = self._agent_picker
        self._agent_picker = None
        if picker is not None:
            try:
                picker.destroy()
            except tk.TclError:
                pass

    def toast(self, text: str, sec: float = 3.0):
        """应用级提示：只占主卡；叠层卡片保持显示（v4.1.4 不折叠叠层）。"""
        self._toast = (text, time.time() + sec)
        self._schedule_toast_deadline()
        self.ui.request(UiDirty.TOAST)

    def agent_toast(self, key: str, text: str, sec: float = 3.0):
        """Agent 级反馈：只改该 Agent 自己那张卡的正文（agent_key/status/
        配色不变，卡片仍可双击），其他卡片不收起、不变化（v4.1.4）。"""
        if not key:
            self.toast(text, sec)
            return
        self._agent_toasts[key] = (text, time.time() + sec)
        self._schedule_toast_deadline()
        self.ui.request(UiDirty.TOAST)

    def _toast_text(self) -> str:
        if self._toast and time.time() < self._toast[1]:
            return self._toast[0]
        self._toast = None
        return ""

    def _prune_agent_toasts(self):
        now = time.time()
        for key in [k for k, (_, expire) in self._agent_toasts.items()
                    if now >= expire]:
            del self._agent_toasts[key]

    # ================= 主循环（v4.3 §4：UiCoordinator 驱动） =================
    def _aggregate(self, targets: dict | None = None):
        """MONITOR/PRESENTATION dirty 时的呈现批次（v4plan §6）。

        reconcile 呈现事实 + 同步 views + 推进动画；重画由 render
        flush 的 redraw_dirty 完成（§4.4 A→D），此处不做任何 redraw。
        """
        now = time.time()
        if targets is None:
            targets = self.monitor.get_targets()
        state = self.presentation.reconcile(targets, now)
        self._presentation_state = state
        self.pet_manager.sync(state, targets, now)
        force_state = str(self.config.get("force_state") or "")
        self.pet_manager.apply_animation(state, targets, now, force_state)

    def _apply_toasts(self):
        """提示绘制规则（v4.1.4，v4.3 §4.4 C 步）：

        - Agent 级反馈只覆盖对应 Agent 自己那张卡的正文（身份与可双击
          性不变），其他卡片不收起、不变化；过期恢复原正文；
        - 应用级提示只占主卡，叠层卡片保持显示（不折叠为一摞）；
        - Agent 级反馈找不到对应卡片（该 Agent 刚退出等）：最新一条
          升级为主卡提示，反馈不会静默丢失。

        只改模型 + mark dirty；重画由同批 render flush 的 redraw_dirty
        完成。无变化不 mark（idle 零工作）。
        """
        self._prune_agent_toasts()
        shown: set[str] = set()
        for view in self.pet_manager.views.values():
            for renderer in [view.bubble, *view.stack_bubbles]:
                m = renderer.model
                if not (m.visible and m.agent_key):
                    continue
                shown.add(m.agent_key)
                entry = self._agent_toasts.get(m.agent_key)
                if entry:
                    if not getattr(m, "toast_applied", False):
                        m.pre_toast_text = m.text   # 首次覆盖前保存原正文
                        m.toast_applied = True
                        view.mark_dirty()
                    if m.text != entry[0]:
                        m.text = entry[0]
                        view.mark_dirty()
                elif getattr(m, "toast_applied", False):
                    m.toast_applied = False
                    m.text = getattr(m, "pre_toast_text", m.text)
                    m.pre_toast_text = None
                    view.mark_dirty()
        text = self._toast_text()
        if not text:
            orphans = [k for k in self._agent_toasts if k not in shown]
            if orphans:
                latest = max(orphans,
                             key=lambda k: self._agent_toasts[k][1])
                text = self._agent_toasts[latest][0]
        if not text:
            if self._toast_applied:
                # 全局 toast 刚过期：主卡曾被覆盖 → 用当前 targets 重建
                # 一次模型（sync 内的 change 检测会 mark dirty 需要重画
                # 的 view），再补套尚存的 agent 级 overlay
                self._toast_applied = False
                self._aggregate()
                self._apply_toasts()
            return
        self._toast_applied = True
        for view in self.pet_manager.views.values():
            m = view.bubble.model
            if not (m.visible and m.status == "DeskPet"
                    and m.text == text):
                view.mark_dirty()
            m.visible = True
            m.status = "DeskPet"
            m.text = text
            m.footer = ""
            m.agent_key = ""
            m.accent = "#487f73"

    def _toast_expired(self):
        self._toast_after = None
        self._toast_deadline = None
        if self._closing:
            return
        self.ui.request(UiDirty.TOAST)
        self._schedule_toast_deadline()   # 可能还有更晚的 agent toast

    def _schedule_toast_deadline(self):
        """v4.3 §4.5：toast 过期用最近 expiry 的单次 deadline（非周期）。"""
        if self._closing:
            return
        expiries = []
        if self._toast:
            expiries.append(self._toast[1])
        expiries.extend(e for _, e in self._agent_toasts.values())
        if not expiries:
            return
        deadline = min(expiries)
        if (self._toast_after is not None
                and self._toast_deadline is not None
                and self._toast_deadline <= deadline + 0.05):
            return   # 已有相同/更早的 deadline
        if self._toast_after is not None:
            try:
                self.root.after_cancel(self._toast_after)
            except Exception:
                pass
        self._toast_deadline = deadline
        delay_ms = max(16, int((deadline - time.time()) * 1000) + 16)
        self._toast_after = self.root.after(delay_ms, self._toast_expired)

    # ================= 几何/外观 =================
    def _on_pet_moved(self, view: PetView):
        """拖动结束：一次 placement 换算 + 一次 commit（不每 move 写盘）。"""
        view.anchor = view.anchor_from_window()
        state = self._presentation_state
        if state is not None and state.mode is PresentationMode.FLEET:
            placement = view.window.placement_from_anchor(*view.anchor)
            slots = list(self.config.get(
                "presentation.concurrent.slots", []) or [])
            for slot in slots:
                if isinstance(slot, dict) and slot.get("id") == view.view_id:
                    slot["placement"] = placement
                    break
            self.config.save()
        else:
            self.config.set("pet_pos", [view.anchor[0], view.anchor[1]])
            self.config.save()
        view.invalidate_dpi()
        # v4.3：无周期 tick 兜底，拖动结束/DPI 变化后显式标 dirty
        view.mark_dirty(layout=True)

    def set_scale(self, scale: float):
        # v4.3 §7.4：经 AppearanceController（clamp + 定向 apply +
        # 350ms 最终尺寸 build debounce + 保存策略回调）
        self.appearance.set_global("scale", scale)

    def _reload_skins(self):
        self._skin_after = None
        for view in self.pet_manager.views.values():
            view.load_skin(self.pet_manager.build_manager)

    def _switch_skin(self, name: str):
        self.appearance.set_global("skin", name)

    def _set_animated(self, flag: bool):
        self.appearance.set_global("animated", bool(flag))

    def _set_speed(self, v: float):
        self.appearance.set_global("speed", v)

    def _toggle_bubble(self, flag: bool):
        self.appearance.set_global("bubble.enabled", bool(flag))

    def _set_force_state(self, v: str):
        self.appearance.set_global("force_state", v)
        self.toast("锁定动画：" + (v if v else "自动"), 3)

    def _toggle_terminal_observer(self, flag: bool):
        self.config.set("monitor.terminal_observer", bool(flag))
        self.config.save()
        if flag:
            self.toast("终端观察将在重启 DeskPet 后启用", 5)
        else:
            self.toast("终端观察将在重启 DeskPet 后停用", 5)

    def _rebuild_skin(self):
        from .config import CACHE_DIR
        d = skins.cache_dir(self.config.get("skin", skins.BUILTIN_SKIN), 240)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        self._reload_skins()

    # ================= 显示/隐藏/托盘/自启 =================
    def hide_pet(self):
        self.pet_manager.hide_all()
        self._start_tray_runtime()   # 隐藏后必须留托盘入口恢复（不写配置）
        self.toast("桌宠已隐藏，点击托盘图标恢复", 4)

    def show_pet(self):
        self.pet_manager.show_all()

    @property
    def pet_visible(self) -> bool:
        return self.pet_manager.any_visible()

    def toggle_visible(self):
        if self.pet_visible:
            self.hide_pet()
        else:
            self.show_pet()

    def restore_pet_from_tray(self):
        """托盘左键：幂等显示/恢复（v4.1.3 §15）。

        绝不隐藏已经可见的桌宠——可见时只重新声明 Z-order；全部隐藏
        时才 show_all。显式隐藏仍走右键菜单的 toggle_visible。
        """
        if self.pet_manager.any_visible():
            self.pet_manager.reassert_visible_windows()
        else:
            self.pet_manager.show_all()
            self._schedule_reassert()

    def _schedule_reassert(self):
        """idle 时刻做一次 Z-order 重声明；after id 全程可取消。"""
        if self._closing:
            return
        if self._reassert_after is not None:
            try:
                self.root.after_cancel(self._reassert_after)
            except tk.TclError:
                pass
        self._reassert_after = self.root.after_idle(self._reassert_now)

    def _reassert_now(self):
        self._reassert_after = None
        if self._closing:
            return
        self.pet_manager.reassert_visible_windows()

    def _start_tray_runtime(self):
        """启动/显示托盘图标（纯运行期，不写配置——v4.1.1 §17）。"""
        if os.name != "nt":
            return
        from .tray import TrayIcon
        if self.tray is None:
            self.tray = TrayIcon("DeskPet - 左键显示桌宠，右键菜单")
            self.tray.start()
        else:
            self.tray.show_icon()

    def _stop_tray_runtime(self):
        if self.tray:
            self.tray.stop()
            self.tray = None

    def set_tray_enabled(self, enabled: bool):
        """用户显式切换托盘：运行期启停 + 一次性持久化（§17）。"""
        if enabled:
            self._start_tray_runtime()
        else:
            self._stop_tray_runtime()
        self.config.set_and_commit("tray_enabled", enabled)
        self.ui.kick()   # bridge 档位可能变化（200↔500ms）

    def toggle_autostart(self) -> bool:
        result = autostart.toggle()
        return result.enabled

    def _poll_tray_events(self):
        if not self.tray:
            return
        try:
            while True:
                ev = self.tray.events.get_nowait()
                if ev == "left":
                    # 左键只显示/恢复，绝不隐藏可见桌宠（§15）
                    self.restore_pet_from_tray()
                elif ev == "right":
                    self._tray_menu()
                elif ev == "error":
                    self.tray = None
        except queue.Empty:
            pass

    # ================= ephemeral 菜单生命周期（v4.2.3 §9） =================
    def _destroy_menu(self, menu):
        """幂等销毁一个 popup menu；任何阶段失败都不抛 TclError。"""
        if menu is None:
            return
        try:
            menu.grab_release()
        except tk.TclError:
            pass
        try:
            menu.unpost()
        except tk.TclError:
            pass
        try:
            menu.destroy()
        except tk.TclError:
            pass

    def _dismiss_active_menu(self):
        """销毁当前 tray popup（进程内最多一个）；幂等。"""
        menu = self._active_menu
        self._active_menu = None
        self._destroy_menu(menu)

    def _tray_menu(self):
        # v4.2.3 §9：先确定性销毁旧 popup（最多一个 tray menu），
        # tk_popup + finally 销毁，不长期持有 _active_menu，不加
        # click-away polling/focus watcher。
        # v4.3：弹出前对托盘隐藏窗口做 Win32 前台准备——没有前台状态
        # 的 TrackPopupMenu 点击菜单外不收起、模态循环不退出（经典
        # tray-menu 缺陷，用户实测卡死需手动点击）。
        self._dismiss_active_menu()
        menu = tk.Menu(self.root, tearoff=0)
        self._active_menu = menu
        menu.add_command(
            label="显示桌宠" if not self.pet_visible else "隐藏桌宠",
            command=self.toggle_visible)
        self._agents_submenu(menu)
        menu.add_command(label="仪表盘", command=self.open_dashboard)
        menu.add_command(label="重新扫描", command=self.monitor.rescan)
        menu.add_separator()
        menu.add_command(label="退出", command=self.quit)
        from actions import winkeys
        hwnd = self.tray.menu_hwnd if self.tray is not None else None
        try:
            import ctypes
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            if hwnd:
                winkeys.prepare_menu_popup(hwnd)
            menu.tk_popup(pt.x, pt.y)
        except (OSError, tk.TclError):
            pass
        finally:
            if hwnd:
                winkeys.finish_menu_popup(hwnd)
            # quit() 等 command 可能已销毁并清空 _active_menu（幂等）；
            # 只有仍为当前对象时才清属性，销毁本身无条件执行。
            if self._active_menu is menu:
                self._active_menu = None
            self._destroy_menu(menu)

    def _agents_submenu(self, menu):
        """Agents 子菜单：每项捕获 exact key（§8.4/§8.5）。

        托盘 Agent 项只激活对应 Terminal 窗口，不改 presentation 的
        focused 状态。
        """
        targets = self.monitor.get_targets()
        m = tk.Menu(menu, tearoff=0)
        if not targets:
            m.add_command(label="（当前没有发现 Agent）", state="disabled")
        for key, t in sorted(targets.items()):
            s = t.snapshot
            m.add_command(
                label=f"{s.kind.label} · "
                      f"{t.instance.project or t.instance.source} · "
                      f"{status_text(s)}",
                command=lambda k=key: self.activate_agent(k))
        menu.add_cascade(label="Agents", menu=m)

    # ================= 右键菜单（每只桌宠） =================
    def _build_menu(self, menu: tk.Menu):
        state = self._presentation_state
        mode = state.mode if state is not None else PresentationMode.SINGLE
        view = getattr(self, "_menu_view", None)
        if mode is PresentationMode.FLEET and view is not None:
            self._fleet_menu(menu, view)
            return
        menu.add_command(label="🤚 摸摸头（互动）", command=self.interact)
        self._agents_submenu(menu)
        menu.add_command(label="📊 打开仪表盘", command=self.open_dashboard)
        menu.add_command(
            label="🙈 暂时隐藏桌宠（托盘可恢复）", command=self.hide_pet)
        menu.add_separator()
        self._appearance_menu(menu)
        self._system_menu(menu)
        menu.add_separator()
        menu.add_command(label="🔄 重建当前皮肤缓存", command=self._rebuild_skin)
        menu.add_command(label="❌ 退出", command=self.quit)

    def _fleet_menu(self, menu: tk.Menu, view: PetView):
        """Fleet 每只 Pet 的菜单（v4plan §14）。"""
        target = self.monitor.get_target(view.agent_key) if view.agent_key else None
        if target is not None:
            s = target.snapshot
            menu.add_command(
                label=f"{s.kind.label} · {target.instance.project or ''}")
            menu.add_command(
                label="打开此 Agent 终端",
                command=lambda k=view.agent_key: self.activate_agent(k))
            menu.add_command(label="更换 Agent",
                             command=lambda v=view: self._open_agent_picker(
                                 v.view_id))
            menu.add_command(label="解除绑定",
                             command=lambda v=view: self._unbind_view(v))
        else:
            menu.add_command(label="（未绑定 Agent）", state="disabled")
            menu.add_command(label="绑定 Agent",
                             command=lambda v=view: self._open_agent_picker(
                                 v.view_id))
        menu.add_separator()
        menu.add_command(label="隐藏此桌宠", command=lambda v=view: v.hide())
        menu.add_command(label="仪表盘", command=self.open_dashboard)
        menu.add_separator()
        self._appearance_menu(menu)
        self._system_menu(menu)
        menu.add_separator()
        menu.add_command(label="❌ 退出", command=self.quit)

    def _unbind_view(self, view: PetView):
        """解除绑定：slot 释放 + 该 Agent 移出并发展示（否则下一轮自动
        分配会立刻补位，桌宠不会消失）。加入并发入口可再纳入。"""
        key = view.agent_key
        self.presentation.unbind_slot(view.view_id)
        if key:
            self.presentation.set_instance_included(key, False)
        view.set_agent("")
        self._aggregate()
        self.toast("已解除绑定并移出并发展示", 3)

    def _appearance_menu(self, menu: tk.Menu):
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
                               command=lambda: self._set_topmost(topmost.get()))
        m_skins = tk.Menu(m_look, tearoff=0)
        for name, mf in skins.list_skins().items():
            label = mf.get("title") or name
            m_skins.add_radiobutton(label=f"{label} ({name})", value=name,
                                    command=lambda n=name: self._switch_skin(n))
        m_look.add_cascade(label="皮肤", menu=m_skins)
        menu.add_cascade(label="🎨 外观", menu=m_look)

    def _set_topmost(self, flag: bool):
        self.config.set("topmost", bool(flag))
        self.config.save()
        for view in self.pet_manager.views.values():
            view.window.set_topmost(flag)

    def _system_menu(self, menu: tk.Menu):
        m_sys = tk.Menu(menu, tearoff=0)
        from .autostart import status as autostart_status
        st = autostart_status()
        label = {"healthy": "开机自启动 ✔", "missing": "开机自启动",
                 "stale": "开机自启动（需要修复）"}.get(st.state, "开机自启动")
        m_sys.add_checkbutton(label=label,
                              command=self._toggle_autostart)
        tray_on = tk.BooleanVar(value=bool(self.config.get("tray_enabled", True)))
        m_sys.add_checkbutton(label="托盘图标", variable=tray_on,
                              command=lambda: self._toggle_tray(tray_on.get()))
        terminal_on = tk.BooleanVar(value=bool(self.config.get("monitor.terminal_observer", True)))
        m_sys.add_checkbutton(label="终端交互观察（UIA）", variable=terminal_on,
                              command=lambda: self._toggle_terminal_observer(terminal_on.get()))
        menu.add_cascade(label="⚙ 设置", menu=m_sys)

    def _toggle_autostart(self):
        on = self.toggle_autostart()
        self.toast("开机自启动已" + ("开启" if on else "关闭"), 3)

    def _toggle_tray(self, flag: bool):
        self.set_tray_enabled(flag)

    # ================= 仪表盘 =================
    def open_dashboard(self):
        """打开仪表盘：不改桌宠逻辑 hidden 状态，只对原本可见的桌宠做
       一次 no-activate Z-order 重声明（v4.1.3 §18）。"""
        if self.dashboard is None or not tk.Toplevel.winfo_exists(self.dashboard):
            self.dashboard = Dashboard(self)
        self.dashboard.open()
        self.ui.kick()   # Dashboard 可见 → bridge 立即升到 125ms 档
        self.ui.request(UiDirty.DASHBOARD)
        self._schedule_reassert()

    # ================= 生命周期 =================
    def run(self):
        self.monitor.start()
        # v4.3 §4.5：唯一 bridge timer（125/200/500ms 三档）+ 按需
        # render after_idle；不再有 _poll_monitor/_ui_tick/_poll_build
        self.ui.start()
        self._janitor_after = self.root.after(600_000, self._janitor)
        self.root.mainloop()

    def _janitor(self):
        """定时清理：日志/事件队列、转换临时目录、皮肤缓存、共享帧缓存。"""
        if self._closing:
            return
        try:
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
        except Exception:
            pass
        self._janitor_after = self.root.after(600_000, self._janitor)

    def quit(self):
        self._closing = True
        try:
            self.ui.stop()
            self.monitor.stop()
            self.pet_manager.stop()
            if self.tray:
                self.tray.stop()
            if self.dashboard is not None:
                try:
                    self.dashboard.shutdown()
                except tk.TclError:
                    pass
            self.config.save()
        finally:
            self._dismiss_active_menu()   # v4.2.3 §9：幂等，不 double-destroy
            self._destroy_agent_picker()  # v4.3：不留死弹窗
            for attr in ("_janitor_after", "_skin_after", "_reassert_after",
                         "_toast_after"):
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
        # 主线程立即回收残余引用环（v4.2.1）：PhotoImage 已在
        # pet_manager.stop() 里释放，这里兜底保证之后任何工作线程
        # 触发 GC 都不会再碰到 Tcl 对象
        gc.collect()
