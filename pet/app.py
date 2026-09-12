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
import logging
import os
import queue
import random
import time
import tkinter as tk

from agents.models import ActivationCode, AgentSurface

from . import autostart, skins
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

# v4.3.1 DP43-R09 §17.9：单次 bridge drain 的托盘事件上限
TRAY_DRAIN_MAX = 8

# v4.3.1 DP43-R17 §8.2：全局退出预算（秒）——所有子系统 join 只能
# 使用该 deadline 的剩余量，不允许局部 timeout 累加
SHUTDOWN_BUDGET_SEC = 3.0


class PetApp:
    def __init__(self, config, *, startup_baseline: float | None = None,
                 config_loaded_at: float | None = None):
        self.config = config
        # DP43-R18 §9.9：startup 里程碑（纯内存、每个事件只写一次
        # perf_counter；无 timer）。main.py 提供 process 基准与
        # config_loaded；其余按发生顺序记录。
        self._startup_metrics: dict[str, float] = {}
        self._mark_startup("process_start", startup_baseline)
        self._mark_startup("config_loaded", config_loaded_at)
        self.root = tk.Tk()
        self._mark_startup("tk_created")
        self.root.withdraw()   # controller root：不是宠物窗口（v4plan §8.1）
        self.root.configure(bg="#101011")

        from agents.monitor import Monitor
        self.monitor = Monitor(config)
        self.presentation = PresentationController(config)
        self.pet_manager = PetViewManager(self.root, config,
                                          self.presentation)
        # v4.3 §7.4：外观唯一运行期修改接口（save 策略由注入回调决定）
        from .appearance import AppearanceController
        # v4.3 §8.2：非阻塞配置保存（650ms debounce + 单 transient
        # worker；bridge 按 pending() 收割）
        from .config_save import ConfigSaveCoordinator

        def _on_config_saved(result):
            # 绝不静默吞保存失败（v4plan §9.4）
            if result is not None and getattr(result, "ok", True) is False:
                self.toast(f"配置保存失败：{getattr(result, 'error', '')}",
                           5)
            self.ui.request(UiDirty.DASHBOARD)

        self.config_saver = ConfigSaveCoordinator(
            self.root, config, on_result=_on_config_saved)

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
            config, self.pet_manager,
            request_save=self.config_saver.request_save,
            request_render=_appearance_render)
        # DP43-R14：单一 Tk Pet context menu owner（deferred 语义发布）
        from .context_menu import TkContextMenuController
        self._menu_controller = TkContextMenuController(
            self.root, is_closing=lambda: self._closing)
        self.pet_manager.set_hooks(
            on_activate=self.activate_agent,
            on_context_menu=self._show_pet_menu,
            on_interact=self.interact,
            on_moved=self._on_pet_moved,
            on_double_vacant=self._on_vacant_double_click,
        )
        # DP43-R18 §9.7：first-map 触发后台 runtime 的防御性 one-shot
        # after fallback（Map 回调触发后取消；两路径只保留一个实现）。
        # 必须在 _arm_first_map_trigger 之前初始化（arm 会写入 token）。
        self._first_map_after = None
        self._first_map_bound = None
        self._background_started = False
        # 隐式 pet-1 立即创建（single/aggregate 模式也用它）。
        # DP43-R18 §9.1：首个可见首帧先于 Monitor scan / UIA boot /
        # Tray heavy load / Skin catalog maintenance——此处只创建窗口
        # 与 bootstrap 占位，后台 runtime 由 first-map 回调启动。
        self.pet_manager.note_startup_event = self._mark_startup
        first_view = self.pet_manager.ensure_view("pet-1")
        self._mark_startup("first_pet_created")
        self._arm_first_map_trigger(first_view)

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
            activation_repair_drain=self._drain_activation_repairs,
        )
        self.ui.config_saver = self.config_saver
        # v4.3 §9：异步皮肤导入结果经 build lane → poll_results 收割
        self._import_switch_name = ""
        self.pet_manager.build_manager.on_import_result = \
            self._on_skin_import_result
        # DP43-R19 §9.7：bootstrap 结果经现有 bridge 的 poll_results 收割
        self.pet_manager.build_manager.on_bootstrap_result = \
            self._on_skin_bootstrap_result
        self.pet_manager.set_render_requester(self.ui.request_view)

        self._janitor_after = None
        self._reassert_after = None
        # v4.3 §4.5：toast 过期用"最近 expiry 的单次 deadline"
        self._toast_after = None
        self._toast_deadline = None
        # v4.3：进程内最多一个 agent picker（死弹窗防护）
        self._agent_picker = None
        # Dashboard lazy import（§9.8）：约千行级完整设置 UI，普通启动
        # 不加载；open_dashboard 首次实际需要时局部 import
        self.dashboard = None
        self.tray = None
        # v4.3.1 DP43-R15 §6.7：FAILED generation 的有界 retry 门——同一
        # desired session 内不自动重建；用户重新切换 intent / hide 恢复
        # 入口出现 / 显式重试才清。
        self._tray_generation_failed = False
        self._tray_failure_reason = ""
        self._presentation_state: PresentationState | None = None

        self._toast: tuple[str, float] | None = None
        # Agent 级反馈（key -> (text, expire)）：只改对应 Agent 自己的
        # 那张卡，其他卡片不收起、不变化（v4.1.4）
        self._agent_toasts: dict[str, tuple[str, float]] = {}
        # 全局 toast 曾覆盖过主卡模型（过期时需要用 targets 重建一次）
        self._toast_applied = False
        self._closing = False

        if getattr(config, "migration_notice", False):
            self.toast(f"{APP_LABEL}：被动监听 · 终端窗口唤起 · "
                       "启动默认并行监听 + 单宠聚合", 8)

    # ================= startup（DP43-R18/R19 §9） =================
    def _mark_startup(self, name: str, value: float | None = None):
        """里程碑只写一次（无 timer；诊断/验收读取）。"""
        if name in self._startup_metrics:
            return
        self._startup_metrics[name] = (
            value if value is not None else time.perf_counter())

    def startup_metrics(self) -> dict[str, float]:
        return dict(self._startup_metrics)

    def _arm_first_map_trigger(self, view: PetView):
        """§9.7 推荐路径：initial Pet 的 one-shot <Map> 回调启动后台
        runtime（直接表达"窗口已映射"）；附一个防御性 one-shot after
        fallback（Map 先到则取消）。"""
        root = view.window.root
        self._first_map_bound = root

        def _on_map(_ev=None):
            self._disarm_first_map_trigger()
            self._on_first_pet_mapped()

        root.bind("<Map>", _on_map)
        self._first_map_after = self.root.after(
            3000, self._on_first_pet_mapped)

    def _disarm_first_map_trigger(self):
        token = self._first_map_after
        self._first_map_after = None
        if token is not None:
            try:
                self.root.after_cancel(token)
            except tk.TclError:
                pass
        bound = getattr(self, "_first_map_bound", None)
        if bound is not None:
            try:
                bound.unbind("<Map>")
            except tk.TclError:
                pass
            self._first_map_bound = None

    def _on_first_pet_mapped(self):
        self._mark_startup("first_pet_mapped")
        self._start_background_runtime()

    def _start_background_runtime(self):
        """§9.7：幂等执行——Monitor / skin bootstrap / tray / janitor
        全部在首个可见首帧之后才启动。"""
        if self._background_started or self._closing:
            return
        self._background_started = True
        self._mark_startup("background_runtime_started")
        self.monitor.start()
        keys = tuple(view.desired_build_key()
                     for view in self.pet_manager.views.values())
        self.pet_manager.build_manager.request_bootstrap(keys)
        self._reconcile_tray_runtime()
        # v4.3.1 DP43-R04：janitor 定期做 ready index reconciliation
        self._janitor_after = self.root.after(600_000, self._janitor)

    def _on_skin_bootstrap_result(self, payload: dict, error: str):
        """bootstrap 结果（bridge 的 poll_results 内回调，UI 线程）。

        ready index 已由 poll_results 合入；这里激活 skin runtime
        （miss 的 view 才在此刻产生 BUILD），再排队 maintenance
        （低优先级，不阻首屏）。"""
        self._mark_startup("skin_bootstrap_finished")
        if error:
            self.toast("皮肤启动检查失败，使用内置兜底", 5)
        self.pet_manager.activate_skin_runtime()
        self.pet_manager.build_manager.request_maintenance()
        self.ui.kick()   # skin lane 活跃 → bridge 升 125ms 档收割 BUILD

    # ================= 交互入口 =================
    def interact(self):
        self.toast(random.choice(INTERACT_LINES), 4)

    def activate_agent(self, key: str):
        """唯一激活入口：UI 只携带 exact agent_key（v4.1.3 §20/§21）。

        只恢复并前置该 Agent 所在的 Windows Terminal 顶层窗口（候选
        由 v3-compatible resolver 给出，confidence 不拦用户显式唤起）；
        DeskPet 不切换 Terminal 标签页、不发送键盘输入。

        v4.3.1 DP43-R08：cached activation 遇到 stale binding 立即
        返回（Tk 不做 UIA refresh）；修复经 Monitor 线程一次异步
        repair，结果由 bridge 收割后做最后一次激活（一次性语义，
        不循环）。
        """
        if self._closing:
            return
        result = self.monitor.activate_target(key)
        desktop = self._target_is_desktop(key)
        if result.code == ActivationCode.OK:
            if desktop:
                # plan2 §10：Desktop 激活 capability = APP_ONLY，文案不
                # 承诺会话级跳转
                self.agent_toast(key, "已唤起该 Agent 的应用窗口（桌面会话不支持定位到具体对话）", 3)
            else:
                self.agent_toast(key, "已打开该 Agent 的终端窗口", 2)
        elif result.code == ActivationCode.FOREGROUND_DENIED:
            self.agent_toast(key, "Windows 未允许将该窗口置于前台，已闪烁任务栏提醒", 4)
        elif result.code == ActivationCode.AGENT_GONE:
            # 该 Agent 的卡片会随本轮 reconcile 消失，无卡可挂 → 主卡提示
            self.toast("该 Agent 已退出", 4)
        elif result.code == ActivationCode.NO_BINDING:
            self.agent_toast(key, "未能定位该 Agent 的终端窗口", 4)
        elif result.code == ActivationCode.STALE_WINDOW:
            self.monitor.request_activation_repair(key)
            self.ui.kick()   # 立即进入下一 bridge tick 收割 repair 结果
            self.agent_toast(key, "原终端窗口已失效，正在重新识别…", 4)
        else:
            self.agent_toast(key, "无法打开该 Agent 的终端窗口", 4)

    def _target_is_desktop(self, key: str) -> bool:
        try:
            target = self.monitor.get_target(key)
        except Exception:
            return False
        return (target is not None
                and getattr(target.instance, "surface", None)
                is AgentSurface.DESKTOP)

    def _drain_activation_repairs(self):
        """bridge 每 tick 调用（bounded，<=4 条；DP43-R08 §16.6）。

        repaired → UI 线程做最后一次 activate_cached（最终动作前
        仍重新核验 is_agent_live + WindowIdentity，fail-closed）；
        这一次激活不再触发新 repair（一次性语义）。
        """
        for res in self.monitor.drain_activation_repairs():
            if self._closing:
                break
            if not res.repaired:
                self.agent_toast(
                    res.agent_key,
                    "原终端窗口已失效，重新识别后仍无法安全打开", 4)
                continue
            if not self.monitor.is_live_key(res.agent_key):
                self.toast("该 Agent 已退出", 4)
                continue
            result = self.monitor.activate_target(res.agent_key)
            if result.code == ActivationCode.OK:
                self.agent_toast(key=res.agent_key,
                                 text="已打开该 Agent 的终端窗口（已自动重新识别）",
                                 sec=2)
            elif result.code == ActivationCode.AGENT_GONE:
                self.toast("该 Agent 已退出", 4)
            elif result.code == ActivationCode.NO_BINDING:
                self.agent_toast(res.agent_key, "未能定位该 Agent 的终端窗口", 4)
            elif result.code == ActivationCode.STALE_WINDOW:
                # repair 后窗口又变（或仍失效）：一次性语义，到此为止
                self.agent_toast(
                    res.agent_key,
                    "原终端窗口已失效，重新识别后仍无法安全打开", 4)
            elif result.code == ActivationCode.FOREGROUND_DENIED:
                self.agent_toast(res.agent_key,
                                 "Windows 未允许将终端置于前台，已闪烁任务栏提醒", 4)

    def _on_vacant_double_click(self, view: PetView):
        """空 slot 双击 → Agent picker（fleet 绑定入口，v4plan §8.2）。"""
        self._open_agent_picker(view.view_id)

    def _open_agent_picker(self, slot_id: str):
        """空 slot 双击/菜单 → Agent picker（fleet 绑定入口，v4plan §8.2）。

        v4.3：进程内最多一个 picker（重复打开先销毁旧的，杜绝死弹窗
        残留）；Escape/取消/选择后确定性销毁；quit() 一并销毁。
        """
        if self._closing:
            return
        self._menu_controller.dismiss()
        self._destroy_agent_picker()
        targets = self.monitor.get_targets()
        if not targets:
            self.toast("当前没有发现任何 Agent", 4)
            return
        picker = tk.Toplevel(self.root)
        picker.title("选择要绑定的 Agent")
        picker.geometry("420x300")
        # The controller root is withdrawn; a transient of it can be hidden
        # by the window manager. Use a visible owner when one exists.
        if self.dashboard is not None and self.dashboard.is_open():
            picker.transient(self.dashboard)
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
        def _close(_evt=None):
            self._destroy_agent_picker()

        def _bind(_evt=None):
            sel = listbox.curselection()
            if not sel:
                return
            key = items[sel[0]][0]
            if not self.monitor.is_live_key(key):
                self.toast("该 Agent 已退出，请重新选择", 4)
                _close()
                return
            taken = {v.agent_key for v in self.pet_manager.views.values()
                     if v.view_id != slot_id}
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
        # DP43-R15：Agent 集合只在语义 revision 变化时刷新 tray 菜单
        # snapshot（O(#agents)，非每 tick）
        self._update_tray_snapshot(targets)

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
            self.config.set("presentation.concurrent.slots", slots)
        else:
            self.config.set("pet_pos", [view.anchor[0], view.anchor[1]])
        # v4.3 §8.2：拖动结束一次 debounce 保存（不再 Tk 线程写盘）
        self.config_saver.request_save()
        view.invalidate_dpi()
        # v4.3：无周期 tick 兜底，拖动结束/DPI 变化后显式标 dirty
        view.mark_dirty(layout=True)

    def set_scale(self, scale: float):
        # v4.3 §7.4：经 AppearanceController（clamp + 定向 apply +
        # 350ms 最终尺寸 build debounce + 保存策略回调）
        self.appearance.set_global("scale", scale)

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
        # 隐私开关：内存立即生效，磁盘经 debounce 保存器（§8.2）
        self.config.set("monitor.terminal_observer", bool(flag))
        self.config_saver.request_save()
        if flag:
            self.toast("终端观察将在重启 DeskPet 后启用", 5)
        else:
            self.toast("终端观察将在重启 DeskPet 后停用", 5)

    def _rebuild_skin(self):
        # v4.3.1 DP43-R06：重建走 skin lane 的 staging 事务（force
        # build → 完整后原子发布），Tk 线程不再 rmtree live cache。
        for view in self.pet_manager.views.values():
            view.request_skin_rebuild(self.pet_manager.build_manager)
        self.ui.kick()

    # ================= 皮肤导入（v4.3 §9 异步） =================
    def begin_skin_import(self, src_dir: str, name: str) -> None:
        """异步导入入口：Tk 线程只排队 job + 立即非模态反馈。"""
        self._import_switch_name = name
        self.toast(f"正在导入皮肤 {name}…", 8)
        self.pet_manager.build_manager.submit_import(src_dir, name)
        self.ui.kick()   # worker 活跃 → bridge 升 125ms 档收割结果

    def _on_skin_import_result(self, ok: bool, name: str,
                               error: str) -> None:
        """导入结果（bridge 的 poll_results 内回调，UI 线程）。"""
        if ok:
            self.toast(f"皮肤 {name} 导入完成，正在构建…", 5)
            if self._import_switch_name == name:
                self._import_switch_name = ""
                self._switch_skin(name)
        else:
            self.toast(f"皮肤导入失败：{error or name}", 6)
        self.ui.request(UiDirty.SKIN)   # 仪表盘外观页刷新皮肤列表

    # ================= 显示/隐藏/托盘/自启 =================
    def hide_pet(self):
        if self._closing:
            return
        self._menu_controller.dismiss()
        self._destroy_agent_picker()
        was_desired = self._tray_desired()
        self.pet_manager.hide_all()
        if not was_desired:
            # hide 恢复入口 intent 从 false→true：清除 FAILED session 门
            self._tray_generation_failed = False
        self._reconcile_tray_runtime()   # 隐藏后必须留托盘入口恢复（不写配置）
        self.toast("桌宠已隐藏，点击托盘图标恢复", 4)
        self._update_tray_snapshot()

    def show_pet(self):
        if self._closing:
            return
        self.pet_manager.show_all()
        self._reconcile_tray_runtime()
        self._update_tray_snapshot()

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
            self._reconcile_tray_runtime()   # user_hidden 解除 → desired 可能变 false
            self._update_tray_snapshot()
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

    # ================= 托盘（DP43-R15 §6.7 reconcile） =================
    def _tray_desired(self) -> bool:
        """desired = tray_enabled OR user_hidden。

        用户显式"隐藏全部桌宠"时，Tray 是现有设计声明的恢复入口：即
        使用户配置默认不显示 Tray，也不能在 Pet 已隐藏的 session 中把
        最后入口删掉。
        """
        return (bool(self.config.get("tray_enabled", True))
                or bool(self.pet_manager.user_hidden))

    def _reconcile_tray_runtime(self):
        """desired/current 状态矩阵（plan §6.7）。

        * self.tray 持有当前 generation 直到终态（STOPPING 期间只改
          desired，不替换 owner——同一时刻最多一个 live TrayIcon）；
        * harvest 并入 UiCoordinator bridge 的 tray drain hook（本方法
          由 bridge 每 tick 调用），无 100ms restart timer；
        * FAILED 在同一 desired session 内不自动重建（有界 retry）。
        """
        if os.name != "nt" or self._closing:
            return
        from .tray import TrayIcon, TrayState
        desired = self._tray_desired()
        tray = self.tray
        if tray is not None:
            quit_requested = getattr(tray, "quit_requested", None)
            if quit_requested is not None and quit_requested.is_set():
                self.quit()
                return  # never reap an unconsumed exit intent
        if tray is None:
            if desired and not self._tray_generation_failed:
                self._spawn_tray_generation()
            return
        state = tray.status()
        if state in (TrayState.STARTING, TrayState.READY):
            if not desired:
                tray.request_stop()
            elif state is TrayState.READY:
                tray.show_icon()
            return
        if state is TrayState.STOPPING:
            return   # 等旧 generation 终态；下一次 bridge tick 再收割
        # terminal：STOPPED / FAILED → reap
        if state is TrayState.FAILED:
            if not self._tray_generation_failed:
                self._tray_generation_failed = True
                self._tray_failure_reason = tray.last_error()
        self.tray = None
        if desired and not self._tray_generation_failed:
            self._spawn_tray_generation()

    def _spawn_tray_generation(self):
        from .tray import TrayIcon
        self.tray = TrayIcon("DeskPet - 左键显示桌宠，右键菜单")
        self._update_tray_snapshot()
        self.tray.start()

    def _update_tray_snapshot(self, targets: dict | None = None):
        """用最新呈现事实构建 immutable 菜单模型换入 worker（O(#agents)）。"""
        tray = self.tray
        if tray is None:
            return
        from .tray import TrayAgentItem, TrayMenuSnapshot
        if targets is None:
            targets = self.monitor.get_targets()
        items = []
        for key, t in sorted(targets.items()):
            s = t.snapshot
            items.append(TrayAgentItem(
                key=key,
                label=f"{s.kind.label} · "
                      f"{t.instance.project or t.instance.source} · "
                      f"{status_text(s)}"))
        tray.update_menu_snapshot(TrayMenuSnapshot(
            pet_visible=self.pet_visible, agents=tuple(items)))

    def set_tray_enabled(self, enabled: bool):
        """用户显式切换托盘：运行期启停 + 一次性持久化（§17）。

        显式重新开启 = 受控 retry：清除 FAILED session 门。
        """
        if enabled:
            self._tray_generation_failed = False
        self.config.set("tray_enabled", enabled)
        self.config_saver.request_save()
        self._reconcile_tray_runtime()
        self.ui.kick()   # bridge 档位可能变化（200↔500ms）

    def toggle_autostart(self) -> bool:
        result = autostart.toggle()
        return result.enabled

    def _poll_tray_events(self):
        """bridge 每 tick：先 reconcile（收割 terminal generation），再
        有界收割语义事件（DP43-R09 §17.9：<= TRAY_DRAIN_MAX）。

        native menu 已在 tray worker 内确定性结束（TrackPopupMenuEx
        返回 → DestroyMenu → NIM_SETFOCUS → 事件入队），Tk 收到时
        原生交互早已完成。
        """
        self._reconcile_tray_runtime()
        tray = self.tray
        if tray is None:
            return
        quit_requested = getattr(tray, "quit_requested", None)
        if quit_requested is not None and quit_requested.is_set():
            self.quit()
            return
        for _ in range(TRAY_DRAIN_MAX):
            if self._closing:
                break
            try:
                ev = tray.events.get_nowait()
            except queue.Empty:
                break
            command = getattr(ev, "command", "")
            if command == "restore":
                # 左键/键盘激活只显示/恢复，绝不隐藏可见桌宠（§15）
                self.restore_pet_from_tray()
            elif command == "toggle_visible":
                self.toggle_visible()
            elif command == "dashboard":
                self.open_dashboard()
            elif command == "rescan":
                self.monitor.rescan()
            elif command == "activate":
                self.activate_agent(getattr(ev, "agent_key", "") or "")
            elif command == "quit":
                self.quit()
                return

    # ================= ephemeral 菜单生命周期 =================
    # DP43-R20：_destroy_menu/_active_menu/_dismiss_active_menu 已删除——
    # Pet 菜单销毁由 TkContextMenuController._destroy 单一拥有；Tray
    # 菜单为原生 HMENU（worker 内销毁）。

    def _agents_submenu(self, menu):
        """Agents 子菜单：每项捕获 exact key（§8.4/§8.5）；command
        一律 deferred。

        托盘 Agent 项只激活对应 Terminal 窗口，不改 presentation 的
        focused 状态。
        """
        d = self._menu_controller.deferred
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
                command=d(self.activate_agent, key))
        menu.add_cascade(label="Agents", menu=m)

    # ================= 右键菜单（每只桌宠；DP43-R14） =================
    def _show_pet_menu(self, view: PetView, x_root: int, y_root: int):
        """Pet context request 入口：单一 controller 拥有 popup。"""
        self._menu_controller.show(
            view, x_root, y_root,
            lambda menu: self._build_pet_menu(menu, view))

    def _build_pet_menu(self, menu: tk.Menu, view: PetView | None):
        """构建桌宠右键菜单（所有 command 一律 deferred 发布）。"""
        state = self._presentation_state
        mode = state.mode if state is not None else PresentationMode.SINGLE
        if mode is PresentationMode.FLEET and view is not None:
            self._fleet_menu(menu, view)
            return
        d = self._menu_controller.deferred
        menu.add_command(label="🤚 摸摸头（互动）", command=d(self.interact))
        self._agents_submenu(menu)
        menu.add_command(label="📊 打开仪表盘", command=d(self.open_dashboard))
        menu.add_command(
            label="🙈 暂时隐藏桌宠（托盘可恢复）", command=d(self.hide_pet))
        menu.add_separator()
        self._appearance_menu(menu)
        self._system_menu(menu)
        menu.add_separator()
        menu.add_command(label="🔄 重建当前皮肤缓存", command=d(self._rebuild_skin))
        menu.add_command(label="❌ 退出",
                         command=d(self.quit, allow_when_closing=True))

    def _fleet_menu(self, menu: tk.Menu, view: PetView):
        """Fleet 每只 Pet 的菜单（v4plan §14；builder 显式 view）。"""
        d = self._menu_controller.deferred
        target = self.monitor.get_target(view.agent_key) if view.agent_key else None
        if target is not None:
            s = target.snapshot
            menu.add_command(
                label=f"{s.kind.label} · {target.instance.project or ''}")
            menu.add_command(
                label="打开此 Agent 终端",
                command=d(self.activate_agent, view.agent_key))
            menu.add_command(label="更换 Agent",
                             command=d(self._open_agent_picker,
                                       view.view_id))
            menu.add_command(label="解除绑定",
                             command=d(self._unbind_view, view))
        else:
            menu.add_command(label="（未绑定 Agent）", state="disabled")
            menu.add_command(label="绑定 Agent",
                             command=d(self._open_agent_picker,
                                       view.view_id))
        menu.add_separator()
        menu.add_command(label="隐藏此桌宠", command=d(self._hide_view, view))
        menu.add_command(label="仪表盘", command=d(self.open_dashboard))
        menu.add_separator()
        self._appearance_menu(menu)
        self._system_menu(menu)
        menu.add_separator()
        menu.add_command(label="❌ 退出",
                         command=d(self.quit, allow_when_closing=True))

    def _hide_view(self, view: PetView):
        if self._closing:
            return
        self._menu_controller.dismiss()
        view.hide()
        if not self.pet_manager.any_visible():
            # Hiding the last fleet pet needs the same tray restore path as
            # hiding the whole fleet, even with tray_enabled=False.
            self.hide_pet()
        else:
            self._update_tray_snapshot()

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
        d = self._menu_controller.deferred
        m_look = tk.Menu(menu, tearoff=0)
        bubble_on = tk.BooleanVar(value=bool(self.config.get("bubble.enabled", True)))
        m_look.add_checkbutton(label="显示气泡（取消=只留桌宠）", variable=bubble_on,
                               command=d(lambda: self._toggle_bubble(bubble_on.get())))
        animated = tk.BooleanVar(value=bool(self.config.get("animated", True)))
        m_look.add_checkbutton(label="动态模式（取消=静态）", variable=animated,
                               command=d(lambda: self._set_animated(animated.get())))
        m_lock = tk.Menu(m_look, tearoff=0)
        m_lock.add_radiobutton(label="自动（按监听状态）", value="",
                               command=d(self._set_force_state, ""))
        for st_name, label in (("walk", "walk 工作中"), ("attack", "attack 下达指令"),
                               ("die", "die 等待批复"), ("special", "special 完成"),
                               ("sleep", "sleep 睡觉")):
            m_lock.add_radiobutton(label=label, value=st_name,
                                   command=d(self._set_force_state, st_name))
        m_look.add_cascade(label="锁定动画", menu=m_lock)
        m_speed = tk.Menu(m_look, tearoff=0)
        for sp in (0.5, 0.75, 1.0, 1.5, 2.0, 3.0):
            m_speed.add_radiobutton(label=f"{sp:g}x", value=sp,
                                    command=d(self._set_speed, sp))
        m_look.add_cascade(label="播放速度", menu=m_speed)
        m_scale = tk.Menu(m_look, tearoff=0)
        for sc in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            m_scale.add_radiobutton(label=f"{sc:g}x", value=sc,
                                    command=d(self.set_scale, sc))
        m_look.add_cascade(label="大小", menu=m_scale)
        topmost = tk.BooleanVar(value=bool(self.config.get("topmost", True)))
        m_look.add_checkbutton(label="窗口置顶", variable=topmost,
                               command=d(lambda: self._set_topmost(topmost.get())))
        m_skins = tk.Menu(m_look, tearoff=0)
        for name, mf in skins.list_skins().items():
            label = mf.get("title") or name
            m_skins.add_radiobutton(label=f"{label} ({name})", value=name,
                                    command=d(self._switch_skin, name))
        m_look.add_cascade(label="皮肤", menu=m_skins)
        menu.add_cascade(label="🎨 外观", menu=m_look)

    def _set_topmost(self, flag: bool):
        self.config.set("topmost", bool(flag))
        self.config_saver.request_save()
        for view in self.pet_manager.views.values():
            view.window.set_topmost(flag)

    def _system_menu(self, menu: tk.Menu):
        d = self._menu_controller.deferred
        m_sys = tk.Menu(menu, tearoff=0)
        from .autostart import status as autostart_status
        st = autostart_status()
        label = {"healthy": "开机自启动 ✔", "missing": "开机自启动",
                 "stale": "开机自启动（需要修复）"}.get(st.state, "开机自启动")
        m_sys.add_checkbutton(label=label,
                              command=d(self._toggle_autostart))
        tray_on = tk.BooleanVar(value=bool(self.config.get("tray_enabled", True)))
        m_sys.add_checkbutton(label="托盘图标", variable=tray_on,
                              command=d(lambda: self._toggle_tray(tray_on.get())))
        terminal_on = tk.BooleanVar(value=bool(self.config.get("monitor.terminal_observer", True)))
        m_sys.add_checkbutton(label="终端交互观察（UIA）", variable=terminal_on,
                              command=d(lambda: self._toggle_terminal_observer(terminal_on.get())))
        menu.add_cascade(label="⚙ 设置", menu=m_sys)

    def _toggle_autostart(self):
        on = self.toggle_autostart()
        self.toast("开机自启动已" + ("开启" if on else "关闭"), 3)

    def _toggle_tray(self, flag: bool):
        self.set_tray_enabled(flag)

    # ================= 仪表盘 =================
    def open_dashboard(self):
        """打开仪表盘：不改桌宠逻辑 hidden 状态，只对原本可见的桌宠做
       一次 no-activate Z-order 重声明（v4.1.3 §18）。

        DP43-R18 §9.8：Dashboard 是约千行级完整设置 UI——首次实际
        需要时才局部 import（普通启动不加载其页面类）。"""
        if self._closing:
            return
        self._menu_controller.dismiss()
        if self.dashboard is None or not tk.Toplevel.winfo_exists(self.dashboard):
            from .dashboard import Dashboard
            self.dashboard = Dashboard(self)
        self.dashboard.open()
        self.ui.kick()   # Dashboard 可见 → bridge 立即升到 125ms 档
        self.ui.request(UiDirty.DASHBOARD)
        self._schedule_reassert()

    # ================= 生命周期 =================
    def run(self):
        # DP43-R18 §9.7：首帧优先——ui.start 只安排 initial render +
        # bridge；initial Pet bootstrap 占位已存在（__init__ ensure_view）；
        # Monitor/UIA/Tray/skin lane 由 first-map <Map> 回调启动。
        # 绝不 root.update()/nested update 强制首帧。
        self.ui.start()
        self.root.mainloop()
    def _janitor(self):
        """定时清理入口（v4.3.1 DP43-R06：Tk 线程只做 O(1) 调度）。

        日志/事件队列 trim 在 Tk（小集合）；一切 filesystem mutation
        （converter temp、.deskpet-* 遗留、缺失皮肤 cache、超额尺寸、
        ready index reconciliation）移交 skin lane 的 maintenance job。
        """
        if self._closing:
            return
        try:
            self.monitor.trim()
            self.pet_manager.build_manager.request_maintenance()
        except Exception:
            pass
        self._janitor_after = self.root.after(600_000, self._janitor)

    def request_quit(self):
        """唯一退出实现（DP43-R17 §8）：hide-first + 单一绝对 deadline。

        固定顺序：
          A. UI 封口（快）——菜单/picker/dashboard 确定性结束，全部
             visible Toplevel withdraw，bridge/after 取消；
          B. 只发 stop signal（不 join）——monitor/skin lane/tray/
             config writer；
          C. 全局 deadline 回收——deadline = monotonic()+3.0s，所有
             join 只用剩余量，到期不再等待；
          D. Tk 最终清理——PhotoImage/动画缓存释放，root.destroy。
        第二次调用直接 no-op（幂等）。
        """
        if self._closing:
            return
        if self._menu_controller.posting:
            # A tray quit can arrive inside Tk's native menu loop. Unwind it
            # before destroying Tcl or waiting for workers.
            self._menu_controller.close_then(self.request_quit)
            return
        self._closing = True
        deadline = time.monotonic() + SHUTDOWN_BUDGET_SEC

        def clean(fn, *args):
            try:
                return fn(*args)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Shutdown step failed: %s", getattr(fn, "__name__", fn))

        def remaining():
            return max(0.0, deadline - time.monotonic())

        try:
            # Each owner is independent: one broken widget/worker must never
            # prevent the other owners from receiving their stop signals.
            clean(self._menu_controller.shutdown)
            clean(self._destroy_agent_picker)
            clean(self._disarm_first_map_trigger)
            if self.dashboard is not None:
                clean(self.dashboard.shutdown)
            clean(self.pet_manager.hide_all_for_shutdown)
            clean(self.ui.stop)
            for attr in ("_janitor_after", "_reassert_after", "_toast_after"):
                callback = getattr(self, attr, None)
                setattr(self, attr, None)
                if callback is not None:
                    clean(self.root.after_cancel, callback)
            clean(self.monitor.request_stop)
            clean(self.pet_manager.request_stop)
            if self.tray is not None:
                clean(self.tray.request_stop)
            # Give the final config snapshot a chance to save while other
            # workers stop, even if one of them consumes the entire deadline.
            if getattr(self.config, "dirty", False):
                clean(lambda: self.config_saver.request_save(immediate=True))
            clean(self.config_saver.begin_shutdown)
            clean(self.monitor.join_for_shutdown, remaining())
            clean(self.pet_manager.join_for_shutdown, remaining())
            if self.dashboard is not None:
                clean(self.dashboard.join_actions, remaining())
            if self.tray is not None:
                clean(self.tray.join_for_shutdown, remaining())
                self.tray = None
            clean(self.config_saver.flush_for_shutdown, remaining())
        finally:
            clean(self.pet_manager.finalize_tk_resources)
            clean(self.root.quit)
            clean(self.root.destroy)
        gc.collect()

    def quit(self):
        """极薄 alias（DP43-R17 §8.1：兼容已有测试/外部入口）。"""
        self.request_quit()
