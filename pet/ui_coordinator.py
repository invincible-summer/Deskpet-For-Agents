"""UiCoordinator（v4.3 §4.3-§4.5）：revision 驱动的单 bridge timer
+ 单 render-idle slot。

常态 Tk timer 结构（§4.5，Pet 数量增长不增加 timer）：

    1 × UiCoordinator bridge（125/200/500ms 三档）
    1 × AnimationScheduler due timer（有动画运行时才存在）
    0/1 × render after_idle（只有 dirty 时存在）
    0/1 × toast/debounce exact deadline（短期）

bridge callback 只做 O(1)/有界 queue/revision 检查（§4.3 六项），
绝不在 bridge 里 redraw；所有同一 event-loop batch 内的变化经
request() 合并成一次 after_idle `_flush_render`（§4.4 A-F 固定顺序）。

线程合同：全部方法只能在 Tk 主线程调用。tray/skin/config 的 worker
结果一律由 bridge 的 bounded poll 收割，worker 线程绝不触碰 Tk。
"""
from __future__ import annotations

import time
import logging
from enum import IntFlag


class UiDirty(IntFlag):
    NONE = 0
    MONITOR = 1          # monitor semantic revision 变化
    PRESENTATION = 2     # mode/bind/focus 等呈现事实变化
    APPEARANCE = 4       # 外观定向更新已发生，待 render
    TOAST = 8            # toast 文案/过期
    SKIN = 16            # 皮肤 build 结果
    DASHBOARD = 32       # Dashboard 当前页需要刷新


_MAX_DIRTY_VIEWS = 8   # slot 上限；溢出退化为"全部 view"


class UiCoordinator:
    """一个 bridge after + 一个 render after_idle；多余请求天然合并。"""

    def __init__(self, root, *, monitor=None, presentation=None,
                 pet_manager=None, dashboard_provider=None,
                 tray_drain=None, apply_batch=None, apply_toasts=None,
                 config_saver=None, tray_enabled=None,
                 activation_repair_drain=None):
        self.root = root
        self.monitor = monitor
        self.presentation = presentation
        self.pet_manager = pet_manager
        self.dashboard_provider = dashboard_provider
        self.tray_drain = tray_drain
        self.apply_batch = apply_batch
        self.apply_toasts = apply_toasts
        self.config_saver = config_saver
        self.tray_enabled = tray_enabled
        # v4.3.1 DP43-R08 §16.6：bounded repair 结果收割钩子（app 提供；
        # 内部有界（<=4），repaired 时 UI 线程做最后一次 activate_cached）
        self.activation_repair_drain = activation_repair_drain

        # 内部状态（§4.3）：只允许这些持久回调槽
        self._bridge_after = None
        self._render_after_idle = None
        self._dirty_flags = UiDirty.NONE
        self._dirty_views: set[str] = set()
        self._dirty_all_views = False
        self._last_monitor_revision = -1
        self._last_presentation_revision = -1
        self._cached_targets: dict | None = None
        self._stopping = False

        # production 轻量统计（§19.1）：只存最近值/计数
        self.bridge_count = 0
        self.bridge_last_ms = 0.0
        self.render_count = 0
        self.render_last_ms = 0.0
        self.render_dirty_views_last = 0

    # ------------------------------------------------------------ 请求入口
    def request(self, flags: UiDirty, view_ids: set[str] | None = None):
        """合并式 dirty 声明：只在尚无 render after_idle 时安排一次。"""
        self._dirty_flags |= flags
        if view_ids:
            for view_id in view_ids:
                self.request_view(view_id)
        self._ensure_render_scheduled()

    def request_view(self, view_id: str):
        """把单个 view 加入 bounded dirty set（≤8，溢出退化为全部）。"""
        if not self._dirty_all_views:
            self._dirty_views.add(view_id)
            if len(self._dirty_views) > _MAX_DIRTY_VIEWS:
                self._dirty_views = set()
                self._dirty_all_views = True
        self._ensure_render_scheduled()

    def request_all_views(self):
        self._dirty_all_views = True
        self._dirty_views = set()
        self._ensure_render_scheduled()

    def _ensure_render_scheduled(self):
        if self._render_after_idle is None and not self._stopping:
            self._render_after_idle = self.root.after_idle(self._flush_render)

    # ------------------------------------------------------------ 生命周期
    def start(self):
        """启动 bridge 并安排初始渲染（首帧不空等一个 bridge 间隔）。"""
        self._stopping = False
        self.request(UiDirty.MONITOR | UiDirty.PRESENTATION)
        self._arm_bridge()

    def stop(self):
        self._stopping = True
        for attr in ("_bridge_after", "_render_after_idle"):
            token = getattr(self, attr)
            setattr(self, attr, None)
            if token is not None:
                try:
                    self.root.after_cancel(token)
                except Exception:
                    pass

    def kick(self):
        """交互后立即用最新档位重排 bridge（如 Dashboard 刚打开）。"""
        if self._stopping:
            return
        if self._bridge_after is not None:
            try:
                self.root.after_cancel(self._bridge_after)
            except Exception:
                pass
            self._bridge_after = None
        self._arm_bridge()

    # ------------------------------------------------------------ bridge
    def _arm_bridge(self):
        if self._bridge_after is not None or self._stopping:
            return
        self._bridge_after = self.root.after(
            self._current_interval_ms(), self._bridge)

    def _current_interval_ms(self) -> int:
        # §4.3 固定三档：125（Dashboard 可见或 worker 活跃）/
        # 200（有可见 Pet 或 tray）/ 500（全部隐藏且无 worker）
        if self._dashboard_is_open():
            return 125
        if self._worker_active():
            return 125
        if (self.pet_manager is not None and self.pet_manager.any_visible()):
            return 200
        if self.tray_enabled is not None and self.tray_enabled():
            return 200
        return 500

    def _dashboard_is_open(self) -> bool:
        if self.dashboard_provider is None:
            return False
        try:
            dash = self.dashboard_provider()
        except Exception:
            return False
        return dash is not None and dash.is_open()

    def _worker_active(self) -> bool:
        if (self.pet_manager is not None
                and self.pet_manager.build_manager.building()):
            return True
        if (self.config_saver is not None
                and self.config_saver.pending()):
            return True
        if self.dashboard_provider is not None:
            try:
                dash = self.dashboard_provider()
                if dash is not None and dash.actions_pending():
                    return True
            except Exception:
                pass
        return False

    def _bridge(self):
        self._bridge_after = None
        if self._stopping:
            return
        t0 = time.perf_counter()
        try:
            self._bridge_tick()
        finally:
            self.bridge_count += 1
            self.bridge_last_ms = (time.perf_counter() - t0) * 1000.0
            # A failed consumer must not permanently disable every UI input.
            self._arm_bridge()

    def _bridge_tick(self):
        """只做 §4.3 的六项 O(1)/有界检查；有变化才 request()。"""
        # 1) monitor 语义 revision（不变 → 零 reconcile/复制）
        if self.monitor is not None:
            try:
                revision, targets = self.monitor.get_targets_if_changed(
                    self._last_monitor_revision)
                if targets is not None:
                    self._last_monitor_revision = revision
                    self._cached_targets = targets
                    self.request(UiDirty.MONITOR)
            except Exception:
                logging.getLogger(__name__).exception("Monitor UI snapshot failed")
                # Keep serving tray/quit even when this source stays broken.
        # 1b) presentation 语义 revision：mode/bind/focus/include 等
        #     运行期变化（任何入口改了呈现事实，这里统一收割，无需
        #     各 mutation 点自行 request）
        if self.presentation is not None:
            revision = self.presentation.revision
            if revision != self._last_presentation_revision:
                self._last_presentation_revision = revision
                self.request(UiDirty.PRESENTATION)
        # 2) tray 事件队列（bounded drain，事件由 app 消费）
        if self.tray_drain is not None:
            try:
                self.tray_drain()
            except Exception:
                pass
        if self._stopping:
            return  # tray quit may have destroyed the interpreter
        # 2b) stale terminal binding 的异步 repair 结果（DP43-R08 §16.6；
        #     bounded O(1)/bounded-drain，<=4 条/tick）
        if self.activation_repair_drain is not None:
            try:
                self.activation_repair_drain()
            except Exception:
                pass
        # 3) skin build 结果：仅在 building()/有结果待收割时 poll
        if self.pet_manager is not None:
            build_manager = self.pet_manager.build_manager
            if build_manager.building() or build_manager.results_pending():
                try:
                    results = self.pet_manager.poll_skin_builds()
                except Exception:
                    results = []
                if results:
                    self.request(UiDirty.SKIN)
        # 4) config save worker：仅在 pending 时 poll（Phase 5 接入）
        if (self.config_saver is not None
                and self.config_saver.pending()):
            try:
                self.config_saver.poll()
            except Exception:
                pass
        # 4b) Dashboard 外部动作结果：同一 bridge 有界收割；worker
        #     永不直接触碰 Tk。
        if self.dashboard_provider is not None:
            try:
                dash = self.dashboard_provider()
                if dash is not None and dash.actions_pending():
                    if dash.poll_actions():
                        self.request(UiDirty.DASHBOARD)
            except Exception:
                pass
        # 5) Dashboard 诊断页 ≥1000ms 自动刷新（非诊断页不周期刷）
        if self._dashboard_is_open():
            dash = self.dashboard_provider()
            if dash.on_diagnostics_page():
                now = time.monotonic()
                if now - dash.last_diag_refresh >= 1.0:
                    self.request(UiDirty.DASHBOARD)

    # ------------------------------------------------------------ render
    def _flush_render(self):
        self._render_after_idle = None
        if self._stopping:
            return
        t0 = time.perf_counter()
        # 取走当前 batch；flush 期间新到的 request 写入新的空集合并
        # 自行安排下一次 after_idle（§4.4 F）
        flags = self._dirty_flags
        view_ids = None if self._dirty_all_views else set(self._dirty_views)
        self._dirty_flags = UiDirty.NONE
        self._dirty_views = set()
        self._dirty_all_views = False
        try:
            self._render_batch(flags, view_ids)
        finally:
            self.render_count += 1
            self.render_last_ms = (time.perf_counter() - t0) * 1000.0

    def _render_batch(self, flags: UiDirty, view_ids):
        # A. 呈现事实 → reconcile + sync + 动画决策（revision 未变时
        #    MONITOR 不会被置位，故语义不变 → 零 reconcile）
        if flags & (UiDirty.MONITOR | UiDirty.PRESENTATION):
            targets = self._cached_targets
            if targets is None and self.monitor is not None:
                targets = self.monitor.get_targets()
                self._cached_targets = targets
            if self.apply_batch is not None and targets is not None:
                self.apply_batch(targets)
        # B. APPEARANCE/SKIN：已由 AppearanceController/PetViewManager
        #    定向更新并 mark dirty，此处无需动作
        # C. toast 模型变化
        if flags & UiDirty.TOAST and self.apply_toasts is not None:
            self.apply_toasts()
        # D. 只重画 dirty 的 view（无兜底 redraw_all，§4.4）
        if self.pet_manager is not None:
            self.pet_manager.redraw_dirty(view_ids)
            self.render_dirty_views_last = (
                -1 if view_ids is None else len(view_ids))
        # E. Dashboard 只刷新当前页
        if self._dashboard_is_open():
            if flags & (UiDirty.DASHBOARD | UiDirty.MONITOR
                        | UiDirty.PRESENTATION | UiDirty.APPEARANCE
                        | UiDirty.SKIN):
                dash = self.dashboard_provider()
                try:
                    dash.refresh_current_page(flags)
                except Exception:
                    pass
        # F. batch 已在开头取走清零；flush 期间新到的请求已自行安排
        #    下一次 after_idle
