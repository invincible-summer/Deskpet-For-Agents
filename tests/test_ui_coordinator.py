"""UiCoordinator / dirty-view 架构测试（v4.3 §4-§5，AC43-UI-01..09）。

合成验证（不依赖真实 Tk 定时器语义）：
  * bridge 只有 1 个 after 槽；Pet 数增长不新增 timer；
  * monitor 语义 revision 不变 → 500 bridge tick 零 reconcile；
  * 同一 event-loop batch 内多次 request 合并为一次 after_idle flush；
  * idle bridge callback p95 < 0.5ms；
  * Fleet 单 slot 变化只 dirty 对应 view；Aggregate 任一卡变化 dirty
    pet-1（真实 Tk，沿用 fleet 测试基建）；
  * 旧周期 timer（_poll_monitor/_ui_tick/_poll_build/500ms dashboard
    refresh）被移除（AC43-UI-01/02/07 源级断言）。
"""
from __future__ import annotations
import io
import sys
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pet.ui_coordinator import UiCoordinator, UiDirty


# ================================================================ fakes
class FakeTimer:
    def __init__(self, token, delay_ms, callback, idle=False):
        self.token = token
        self.delay_ms = delay_ms
        self.callback = callback
        self.idle = idle
        self.fired = 0


class FakeRoot:
    """只实现 after/after_idle/after_cancel 的 Tk root 替身。"""

    def __init__(self):
        self.timers: dict[object, FakeTimer] = {}
        self._next = 0
        self.after_calls = 0
        self.after_idle_calls = 0

    def after(self, ms, cb=None, *args):
        self._next += 1
        self.after_calls += 1
        t = FakeTimer(self._next, ms, cb)
        self.timers[self._next] = t
        return self._next

    def after_idle(self, cb=None, *args):
        self._next += 1
        self.after_idle_calls += 1
        t = FakeTimer(self._next, 0, cb, idle=True)
        self.timers[self._next] = t
        return self._next

    def after_cancel(self, token):
        self.timers.pop(token, None)

    def idle_timer_count(self) -> int:
        return sum(1 for t in self.timers.values() if t.idle)

    def run_idle(self):
        """执行全部 pending idle 回调各一次（模拟一次 event loop 空闲）。"""
        pending = [t for t in self.timers.values() if t.idle]
        for t in pending:
            self.timers.pop(t.token, None)
            t.fired += 1
            t.callback()

    def fire(self, token):
        t = self.timers.pop(token, None)
        if t is not None:
            t.fired += 1
            t.callback()


class FakeMonitor:
    """get_targets_if_changed 替身：revision 只在显式 bump 时变化。"""

    def __init__(self):
        self.revision = 0
        self.targets: dict = {}
        self.get_targets_calls = 0

    def get_targets_if_changed(self, last_revision):
        if self.revision == last_revision:
            return self.revision, None
        self.get_targets_calls += 1
        return self.revision, dict(self.targets)

    def get_targets(self):
        self.get_targets_calls += 1
        return dict(self.targets)


class FakePresentation:
    def __init__(self):
        self._revision = 0
        self.reconcile_calls = 0

    @property
    def revision(self):
        return self._revision

    def bump(self):
        self._revision += 1


class FakePetManager:
    def __init__(self, view_ids):
        self.views = {vid: object() for vid in view_ids}
        self.redraw_dirty_calls: list = []
        self.redraw_all_calls = 0

    def any_visible(self):
        return bool(self.views)

    def redraw_dirty(self, view_ids=None):
        self.redraw_dirty_calls.append(
            None if view_ids is None else set(view_ids))


class FakeBuildManager:
    def __init__(self):
        self.building_flag = False
        self.results_pending_flag = False
        self.poll_count = 0

    def building(self):
        return self.building_flag

    def results_pending(self):
        return self.results_pending_flag


class FakeDashboard:
    def __init__(self):
        self.open_flag = False
        self.diag_page = False
        self.last_diag_refresh = 0.0
        self.refresh_calls = 0

    def is_open(self):
        return self.open_flag

    def on_diagnostics_page(self):
        return self.diag_page

    def refresh_current_page(self):
        self.refresh_calls += 1


# ================================================================ tests
class UiCoordinatorBridgeTests(unittest.TestCase):
    """AC43-UI-03/04/05/08/09：bridge 与 render 合并语义。"""

    def _coordinator(self, view_ids=("pet-1",)):
        root = FakeRoot()
        monitor = FakeMonitor()
        presentation = FakePresentation()
        pets = FakePetManager(view_ids)
        pets.build_manager = FakeBuildManager()
        dash = FakeDashboard()
        reconcile_calls = []

        def apply_batch(targets):
            reconcile_calls.append(1)

        ui = UiCoordinator(
            root, monitor=monitor, presentation=presentation,
            pet_manager=pets, dashboard_provider=lambda: dash,
            apply_batch=apply_batch, tray_drain=lambda: None,
            tray_enabled=lambda: False)
        return ui, root, monitor, presentation, pets, dash, reconcile_calls

    def test_single_bridge_timer_regardless_of_pet_count(self):
        """AC43-UI-03：Pet 1→8 bridge after 恒 ≤1。"""
        for n in (1, 3, 8):
            ui, root, *_ = self._coordinator(tuple(f"pet-{i}"
                                                   for i in range(1, n + 1)))
            ui.start()
            # 多次 request/kick 不产生第二个 bridge 槽
            for i in range(10):
                ui.request_view(f"pet-{(i % n) + 1}")
                ui.kick()
            bridge_tokens = [t.token for t in root.timers.values()
                             if not t.idle]
            self.assertEqual(len(bridge_tokens), 1,
                             f"pet 数 {n} 时 bridge timer 应恰为 1")
            ui.stop()

    def test_unchanged_revision_500_ticks_zero_reconcile(self):
        """AC43-UI-04：语义 revision 不变的 500 tick ≠ 500 次 reconcile。"""
        ui, root, monitor, pres, pets, dash, reconcile = self._coordinator()
        ui.start()
        # 首次 tick 收割初始 revision（targets 为空也 request MONITOR）
        bridge_token = [t.token for t in root.timers.values()
                        if not t.idle][0]
        for i in range(500):
            root.fire(bridge_token)
            # render flush 处理 dirty
            root.run_idle()
            toks = [t.token for t in root.timers.values() if not t.idle]
            self.assertEqual(len(toks), 1)
            bridge_token = toks[0]
        # 首个 tick 的初始 revision 收割只发生一次 reconcile；
        # 之后 499 个 tick revision 不变 → 不再 reconcile
        self.assertEqual(len(reconcile) + 0, 1,
                         "500 个无变化 tick 只允许首次 reconcile")
        self.assertEqual(pets.redraw_dirty_calls.count(None), 0)
        ui.stop()

    def test_revision_change_reconciles_and_redraws(self):
        ui, root, monitor, pres, pets, dash, reconcile = self._coordinator()
        ui.start()
        bridge_token = [t.token for t in root.timers.values()
                        if not t.idle][0]
        root.fire(bridge_token)
        root.run_idle()
        self.assertEqual(len(reconcile), 1)
        # 语义变化：revision bump → 下一 tick request MONITOR → flush
        monitor.revision += 1
        toks = [t.token for t in root.timers.values() if not t.idle]
        root.fire(toks[0])
        root.run_idle()
        self.assertEqual(len(reconcile), 2)

    def test_same_batch_requests_coalesce_into_one_flush(self):
        """AC43-UI-05：同批多次 request → 最多一个 after_idle。"""
        ui, root, *_ = self._coordinator()
        ui.request(UiDirty.MONITOR)
        ui.request(UiDirty.TOAST)
        ui.request_view("pet-1")
        ui.request_view("pet-2")
        self.assertEqual(root.idle_timer_count(), 1)
        ui.request_all_views()
        self.assertEqual(root.idle_timer_count(), 1)
        root.run_idle()
        self.assertEqual(root.idle_timer_count(), 0)

    def test_flush_during_flush_schedules_next_batch(self):
        """§4.4 F：flush 期间新到的 request 安排下一次 after_idle。"""
        ui, root, monitor, pres, pets, dash, reconcile = self._coordinator()

        def late_request():
            ui.request(UiDirty.TOAST)

        original = pets.redraw_dirty

        def hooked(view_ids=None):
            original(view_ids)
            late_request()

        pets.redraw_dirty = hooked
        ui.request(UiDirty.MONITOR)
        root.run_idle()
        # flush 已完成且新的 batch 已安排
        self.assertEqual(root.idle_timer_count(), 1)
        root.run_idle()

    def test_idle_bridge_tick_p95_under_half_ms(self):
        """AC43-UI-09：无变化 bridge callback synthetic p95 < 0.5ms。"""
        ui, root, monitor, pres, pets, dash, reconcile = self._coordinator(
            tuple(f"pet-{i}" for i in range(1, 9)))
        dash.open_flag = True
        dash.diag_page = True   # 最重的路径（诊断页节流检查）
        ui.start()
        # 先完成一次全量收割
        bridge_token = [t.token for t in root.timers.values()
                        if not t.idle][0]
        root.fire(bridge_token)
        root.run_idle()
        samples = []
        for _ in range(500):
            toks = [t.token for t in root.timers.values() if not t.idle]
            t0 = time.perf_counter()
            root.fire(toks[0])
            samples.append((time.perf_counter() - t0) * 1000.0)
            root.run_idle()
        samples.sort()
        p95 = samples[int(len(samples) * 0.95) - 1]
        self.assertLess(p95, 0.5, f"bridge idle p95={p95:.3f}ms")

    def test_bridge_tier_rules(self):
        """§4.3 三档：125（dashboard/worker）/ 200（pet/tray）/ 500。"""
        ui, root, monitor, pres, pets, dash, reconcile = self._coordinator()
        self.assertEqual(ui._current_interval_ms(), 200)   # 有可见 pet
        pets.views = {}   # 无 pet
        self.assertEqual(ui._current_interval_ms(), 500)   # 无 tray 无 pet
        ui.tray_enabled = lambda: True
        self.assertEqual(ui._current_interval_ms(), 200)
        dash.open_flag = True
        self.assertEqual(ui._current_interval_ms(), 125)
        dash.open_flag = False
        pets.build_manager.building_flag = True
        self.assertEqual(ui._current_interval_ms(), 125)

    def test_skin_results_only_polled_when_pending(self):
        """AC43-UI-08：build 结果由 bridge bounded poll 收割。"""
        ui, root, monitor, pres, pets, dash, reconcile = self._coordinator()
        bm = pets.build_manager
        poll_results_calls = []

        def poll_results():
            poll_results_calls.append(1)
            return []

        pets.poll_skin_builds = poll_results
        # 不 building 且无结果 → 不 poll
        ui._bridge_tick()
        self.assertEqual(len(poll_results_calls), 0)
        # 有 pending 结果（building=False）→ 也必须 poll
        bm.results_pending_flag = True
        ui._bridge_tick()
        self.assertEqual(len(poll_results_calls), 1)
        # building 中 → poll
        bm.results_pending_flag = False
        bm.building_flag = True
        ui._bridge_tick()
        self.assertEqual(len(poll_results_calls), 2)


class DirtyViewProtocolTests(unittest.TestCase):
    """AC43-UI-06：单 slot 变化只 dirty 对应 view（真实 Tk + PetView）。"""

    def _fleet_app(self, slots):
        from test_fleet_ui import FleetConfig, _slot
        from pet.app import PetApp
        from pet.petview import PetView
        from pet.presentation import PresentationMode
        cfg = FleetConfig([_slot(s) for s in slots])
        with patch.object(PetApp, "_reload_skins", lambda self: None), \
             patch.object(PetView, "load_skin", lambda self, bm: None):
            app = PetApp(cfg)
            app.pet_manager.activate_skin_runtime()
            app._disarm_first_map_trigger()
        app.presentation.set_concurrent_mode(PresentationMode.FLEET)
        return app

    def test_fleet_single_slot_change_dirties_only_that_view(self):
        from test_fleet_ui import inst, snap
        from agents.models import AgentKind, Status
        app = self._fleet_app(["pet-1", "pet-2"])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2, cwd="/w/other")
            app.monitor.instances = {a.key: a, b.key: b}
            app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
            app._aggregate()
            app.pet_manager.redraw_dirty()   # 清空初始 dirty
            # 只改 pet-2 的 Agent 状态
            app.monitor.snapshots[b.key] = snap(b, Status.ERROR)
            app._aggregate()
            dirty = {vid for vid, v in app.pet_manager.views.items()
                     if v.visual_dirty}
            self.assertEqual(dirty, {"pet-2"})
        finally:
            app.quit()

    def test_aggregate_card_change_dirties_pet1(self):
        from test_fleet_ui import FleetConfig, _slot, inst, snap
        from agents.models import AgentKind, Status
        from pet.app import PetApp
        from pet.petview import PetView
        from pet.presentation import PresentationMode
        cfg = FleetConfig([_slot("pet-1")])
        with patch.object(PetApp, "_reload_skins", lambda self: None), \
             patch.object(PetView, "load_skin", lambda self, bm: None):
            app = PetApp(cfg)
            app.pet_manager.activate_skin_runtime()
            app._disarm_first_map_trigger()
        try:
            app.presentation.set_concurrent_enabled(True)
            app.presentation.set_concurrent_mode(
                PresentationMode.AGGREGATE)
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2, cwd="/w/other")
            app.monitor.instances = {a.key: a, b.key: b}
            app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
            app._aggregate()
            view = app.pet_manager.views["pet-1"]
            self.assertTrue(view.stack_bubbles)   # 聚合叠层已建立
            app.pet_manager.redraw_dirty()
            # 任一 displayed card（叠卡）变化 → dirty pet-1
            app.monitor.snapshots[b.key] = snap(b, Status.WAITING)
            app._aggregate()
            self.assertTrue(view.visual_dirty)
        finally:
            app.quit()


class OldTimerRemovalTests(unittest.TestCase):
    """AC43-UI-01/02/07：旧周期 timer 源级移除断言。"""

    def test_petapp_periodic_loops_removed(self):
        from pet.app import PetApp
        for name in ("_poll_monitor", "_ui_tick", "_poll_build"):
            self.assertFalse(
                hasattr(PetApp, name),
                f"PetApp.{name} 应被 UiCoordinator 替代（AC43-UI-01）")

    def test_dashboard_500ms_refresh_removed(self):
        from pet.dashboard import Dashboard
        for name in ("_refresh_tick", "_refresh_once", "start_refresh",
                     "stop_refresh"):
            self.assertFalse(
                hasattr(Dashboard, name),
                f"Dashboard.{name} 应删除（AC43-UI-02）")
        self.assertTrue(hasattr(Dashboard, "refresh_current_page"))

    def test_no_periodic_callback_calls_redraw_all(self):
        """AC43-UI-07：周期回调（coordinator/scheduler）不调全量重绘。"""
        import inspect
        import re

        def code_only(source: str) -> str:
            stripped = []
            for line in source.splitlines():
                code = line.split("#", 1)[0]
                if not code.strip().startswith(('"', "'")):
                    stripped.append(code)
            return "\n".join(stripped)

        from pet import ui_coordinator
        self.assertNotIn("redraw_all",
                         code_only(inspect.getsource(ui_coordinator)))
        from pet import animator
        self.assertNotIn("redraw_all",
                         code_only(inspect.getsource(animator)))

    def test_render_after_idle_cleared_on_stop(self):
        root = FakeRoot()
        ui = UiCoordinator(root)
        ui.start()
        self.assertEqual(len(root.timers), 2)   # bridge + 初始 render idle
        ui.stop()
        self.assertEqual(len(root.timers), 0)


# ================================================================ DP43-R02
class BridgeTierWithSaverTests(unittest.TestCase):
    """saver active => 125ms；save finished => 空闲回 200/500ms。"""

    class FakeSaver:
        def __init__(self):
            self.pending_flag = False
            self.poll_calls = 0

        def pending(self):
            return self.pending_flag

        def poll(self):
            self.poll_calls += 1

    def _tier_coordinator(self, visible=True, tray=False):
        root = FakeRoot()
        saver = self.FakeSaver()

        class _VisibleManager(FakePetManager):
            def any_visible(self):
                return visible

        pets = _VisibleManager(("pet-1",) if visible else ())
        pets.build_manager = FakeBuildManager()
        ui = UiCoordinator(
            root, monitor=FakeMonitor(), presentation=FakePresentation(),
            pet_manager=pets, dashboard_provider=lambda: FakeDashboard(),
            tray_drain=lambda: None, tray_enabled=lambda: tray,
            config_saver=saver)
        return ui, root, saver

    def test_saver_active_uses_125ms(self):
        ui, root, saver = self._tier_coordinator()
        ui.start()
        saver.pending_flag = True
        ui.kick()
        bridge = [t for t in root.timers.values() if not t.idle]
        self.assertEqual(len(bridge), 1)
        self.assertEqual(bridge[0].delay_ms, 125)

    def test_idle_bridge_returns_to_200(self):
        # DP43-R02 问题 A 回归：save 完成（pending False）后 bridge
        # 回 200ms（有可见 pet），不再永久卡 125ms
        ui, root, saver = self._tier_coordinator(visible=True)
        ui.start()
        saver.pending_flag = True
        ui.kick()
        bridge = [t for t in root.timers.values() if not t.idle][0]
        self.assertEqual(bridge.delay_ms, 125)
        saver.pending_flag = False   # harvest 完成
        root.fire(bridge.token)      # 下一轮 arm 用新档位
        bridge = [t for t in root.timers.values() if not t.idle][0]
        self.assertEqual(bridge.delay_ms, 200)

    def test_hidden_idle_bridge_returns_to_500(self):
        # 全部隐藏 + 无 tray + 无 worker → 500ms
        ui, root, saver = self._tier_coordinator(visible=False, tray=False)
        ui.start()
        saver.pending_flag = True
        ui.kick()
        bridge = [t for t in root.timers.values() if not t.idle][0]
        self.assertEqual(bridge.delay_ms, 125)
        saver.pending_flag = False
        root.fire(bridge.token)
        bridge = [t for t in root.timers.values() if not t.idle][0]
        self.assertEqual(bridge.delay_ms, 500)

    def test_hidden_with_tray_returns_to_200(self):
        ui, root, saver = self._tier_coordinator(visible=False, tray=True)
        ui.start()
        saver.pending_flag = False
        bridge = [t for t in root.timers.values() if not t.idle][0]
        self.assertEqual(bridge.delay_ms, 200)

    def test_bridge_polls_saver_only_when_pending(self):
        ui, root, saver = self._tier_coordinator()
        ui.start()
        bridge = [t for t in root.timers.values() if not t.idle][0]
        root.fire(bridge.token)
        self.assertEqual(saver.poll_calls, 0)   # 不 pending 不 poll
        saver.pending_flag = True
        bridge = [t for t in root.timers.values() if not t.idle][0]
        root.fire(bridge.token)
        self.assertEqual(saver.poll_calls, 1)


if __name__ == "__main__":
    unittest.main()
