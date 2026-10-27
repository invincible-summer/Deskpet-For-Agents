"""Current dashboard contracts: retained workspace, geometry and controller boundaries."""
from __future__ import annotations
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pet.dashboard import (
    BUBBLE_FONT_STEPS,
    BUBBLE_H_STEPS,
    BUBBLE_W_STEPS,
    SCALE_STEPS,
    SPEED_STEPS,
    PAGE_AGENTS,
    PAGE_DIAG,
    PAGE_LOOK,
    PAGE_MONITOR,
    PAGE_PETS,
    PAGE_SETTINGS,
)
from pet.ui_coordinator import UiDirty

_ALL_PAGES = (PAGE_AGENTS, PAGE_PETS, PAGE_LOOK,
              PAGE_MONITOR, PAGE_DIAG, PAGE_SETTINGS)


def _make_app():
    from pet.app import PetApp
    from pet.petview import PetView
    from tests.test_ui import MemoryConfig
    with\
         patch.object(PetView, "load_skin", lambda self, bm: None):
        app = PetApp(MemoryConfig())
        app.pet_manager.activate_skin_runtime()
        app._disarm_first_map_trigger()
        return app


def _inject_agents(app, n=2):
    from agents.models import Status
    from tests.test_fleet_ui import inst, snap
    from agents.models import AgentKind
    agents = [inst(AgentKind.CODEX, i + 1, cwd=f"/w/p{i}")
              for i in range(n)]
    app.monitor.instances = {a.key: a for a in agents}
    app.monitor.snapshots = {a.key: snap(a) for a in agents}
    return agents


def _set_logical_size(app, w: int, h: int):
    """按 DPI 换算设置 Dashboard 逻辑尺寸并驱动一次重排。"""
    dash = app.dashboard
    s = dash.metrics.scale
    dash.geometry(f"{int(w * s)}x{int(h * s)}")
    app.root.update()
    dash._reflow_debounced()
    app.root.update()
    app.root.update_idletasks()


def _screen_box(widget):
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    return (x, y, x + widget.winfo_width(), y + widget.winfo_height())


def _boxes_intersect(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def assert_page_has_visible_geometry(testcase, app, page_name):
    """§18.1/§18.2 geometry gate：切页 → update → update_idletasks →
    读实际几何。避免 pixel-perfect，只抓空白/坍塌/未映射。"""
    dash = app.dashboard
    dash._show_page(page_name)
    app.root.update()
    app.root.update_idletasks()
    # 全量套件高负载下 WM 的 MapNotify 可能晚于一次 update；在有限窗口内
    # 等待映射完成。门槛不变：必须 viewable 且几何健康。
    deadline = time.monotonic() + 2.0
    while not dash.winfo_viewable() and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    testcase.assertTrue(dash.winfo_viewable(), f"{page_name} 窗口不可见")
    canvas = dash.content._canvas
    testcase.assertGreater(canvas.winfo_width(), 200,
                           f"{page_name} canvas 宽度坍塌")
    testcase.assertGreater(canvas.winfo_height(), 200,
                           f"{page_name} canvas 高度坍塌")
    center = dash._center
    testcase.assertGreater(center.winfo_width(), 200,
                           f"{page_name} 正文容器宽度坍塌")
    testcase.assertGreater(center.winfo_height(), 100,
                           f"{page_name} 正文容器高度坍塌（右侧空白根因）")
    page = dash._pages[page_name]
    holder = page.holder
    testcase.assertIsNotNone(holder)
    testcase.assertTrue(holder.winfo_ismapped(), f"{page_name} holder 未映射")
    testcase.assertGreater(holder.winfo_height(), 100,
                           f"{page_name} holder 高度坍塌")
    testcase.assertGreater(holder.winfo_reqheight(), 0)
    bbox = canvas.bbox("all")
    testcase.assertIsNotNone(bbox, f"{page_name} scrollregion 内容为空")
    region = canvas["scrollregion"] or ""
    parts = [int(v) for v in str(region).split()]
    testcase.assertEqual(len(parts), 4)
    testcase.assertEqual((parts[2] - parts[0], parts[3] - parts[1]),
                         (bbox[2] - bbox[0], bbox[3] - bbox[1]),
                         f"{page_name} scrollregion 必须等于内容 bbox")
    # 至少一个 meaningful 子控件与 viewport 有交集（不是只 built 不显示）
    viewport = _screen_box(canvas)
    meaningful = []
    stack = [holder]
    while stack and not meaningful:
        widget = stack.pop()
        for child in widget.winfo_children():
            if child.winfo_ismapped() and child.winfo_width() > 5 \
                    and child.winfo_height() > 5:
                meaningful.append(child)
            stack.append(child)
    testcase.assertTrue(meaningful, f"{page_name} 无可见子控件")
    testcase.assertTrue(any(_boxes_intersect(_screen_box(w), viewport)
                            for w in meaningful),
                        f"{page_name} 子控件与 viewport 无交集")


class DiscreteSliderTests(unittest.TestCase):
    """§13.1/§13.2：snap/键盘/off-step 自定义值/即时 command。"""

    def setUp(self):
        import tkinter as tk
        try:
            self.root = tk.Tk()
        except Exception:
            raise unittest.SkipTest("no display")
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def _slider(self, values=None):
        from pet.widgets import DiscreteSlider
        calls = []
        slider = DiscreteSlider(self.root, values or (0.5, 0.75, 1.0,
                                                      1.25, 1.5),
                                command=calls.append)
        slider.pack()
        self.root.update()
        return slider, calls

    def test_discrete_input_and_config_sync(self):
        for steps in (SCALE_STEPS, SPEED_STEPS, BUBBLE_W_STEPS,
                      BUBBLE_H_STEPS, BUBBLE_FONT_STEPS):
            self.assertIn(1.0, steps)
            self.assertEqual(steps, tuple(sorted(steps)))
        slider, calls = self._slider()
        slider._on_drag(1.4)
        slider._on_drag(1.6)
        self.assertEqual(calls, [0.75, 1.0])
        calls.clear()
        slider.set_external(1.37)
        self.assertEqual(calls, [])
        self.assertIn("自定义", slider._value_label["text"])
        slider._step(1)
        self.assertEqual(slider.value(), 1.5)
        self.assertEqual(calls, [1.5])
        slider._set_index(0, invoke=True)
        self.assertEqual(slider.value(), 0.5)


class DashboardContractTests(unittest.TestCase):
    def test_nav_pages_and_lazy_build(self):
        app = _make_app()
        try:
            app.open_dashboard()
            app.root.update()
            dash = app.dashboard
            names = list(dash._pages)
            self.assertEqual(names, [PAGE_AGENTS, PAGE_PETS,
                                     PAGE_LOOK, PAGE_MONITOR, PAGE_DIAG,
                                     PAGE_SETTINGS])
            # Only the workspace is built before navigating to settings.
            built = [n for n, p in dash._pages.items() if p.built]
            self.assertEqual(built, [PAGE_AGENTS])
            self.assertEqual(app.root.bind_all("<MouseWheel>"), "")
            counts = {}
            for name, page in dash._pages.items():
                page.refresh = lambda reason, n=name: counts.__setitem__(
                    n, counts.get(n, 0) + 1)
            dash.refresh_current_page()
            self.assertEqual(counts, {PAGE_AGENTS: 1})
            for name in (PAGE_PETS, PAGE_LOOK, PAGE_MONITOR, PAGE_DIAG,
                         PAGE_SETTINGS):
                dash._show_page(name)
                app.root.update()
                self.assertTrue(dash._pages[name].built, name)
        finally:
            app.quit()


    def test_agent_rows_retained_configure_only(self):
        app = _make_app()
        try:
            agents = _inject_agents(app, 2)
            app._aggregate()
            app.open_dashboard()
            app.root.update()
            dash = app.dashboard
            page = dash._pages[PAGE_AGENTS]
            page.refresh(0)
            first_keys = set(page._rows)
            row_ids = {k: page._rows[k]["frame"].winfo_id()
                       for k in page._rows}
            # 状态变化 → 只 configure（widget 身份不变）
            from tests.test_fleet_ui import snap as _snap
            from agents.models import Status
            app.monitor.snapshots[agents[0].key] = _snap(
                agents[0], Status.ERROR)
            page.refresh(0)
            self.assertEqual(set(page._rows), first_keys)
            for k, widget_id in row_ids.items():
                self.assertEqual(page._rows[k]["frame"].winfo_id(),
                                 widget_id, "同 key 行不得 recreate")
            page._filters.select(1)
            page.refresh(0)
            self.assertEqual(page._row_order, (agents[0].key,))
            self.assertEqual(page._detail_key, agents[0].key)
            self.assertEqual(set(page._rows), first_keys)
            page._filters.select(0)
            page.refresh(0)
            # Agent 消失 → 行删除
            app.monitor.instances.pop(agents[1].key)
            app.monitor.snapshots.pop(agents[1].key)
            page.refresh(0)
            self.assertNotIn(agents[1].key, page._rows)
            app.monitor.instances.clear()
            app.monitor.snapshots.clear()
            page.refresh(0)
            self.assertEqual(page._detail_key, "")
            self.assertTrue(page._activate_btn.instate(["disabled"]))
            self.assertEqual(page._fields["目标"]["text"], "—")
        finally:
            app.quit()


class DashboardGeometryTests(unittest.TestCase):
    """All current pages use one viewport and stable interaction metrics."""

    def setUp(self):
        self.app = _make_app()
        self.app.open_dashboard()
        self.app.root.update()

    def tearDown(self):
        self.app.quit()

    def test_all_pages_geometry_wide_and_compact(self):
        _inject_agents(self.app, 2)
        for logical_w, logical_h in ((1120, 720), (860, 560)):
            _set_logical_size(self.app, logical_w, logical_h)
            for page_name in _ALL_PAGES:
                assert_page_has_visible_geometry(
                    self, self.app, page_name)
                holder = self.app.dashboard._pages[page_name].holder
                self.assertGreater(holder.winfo_height(), 100,
                                   f"{page_name}@{logical_w} holder 高度")
                self.app.dashboard.refresh_current_page()
                self.app.root.update_idletasks()
                widgets = []
                pending = [holder, *self.app.dashboard._nav_buttons.values()]
                while pending:
                    widget = pending.pop()
                    pending.extend(widget.winfo_children())
                    if widget.winfo_ismapped():
                        widgets.append(widget)
                before = {widget: _screen_box(widget) for widget in widgets}
                for widget in widgets:
                    if widget.winfo_class() in ("TButton", "TCheckbutton", "TCombobox"):
                        for event in ("<Enter>", "<Leave>", "<FocusIn>", "<FocusOut>"):
                            widget.event_generate(event)
                            self.app.root.update_idletasks()
                for button in self.app.dashboard._nav_buttons.values():
                    button._hover(True)
                    button._hover(False)
                self.app.root.update_idletasks()
                self.assertEqual(before, {widget: _screen_box(widget)
                                          for widget in widgets},
                                 f"hover/focus changed {page_name} layout")
                status = self.app.dashboard._nav_status
                self.assertLessEqual(status.winfo_rooty() + status.winfo_height(),
                                     status.master.winfo_rooty()
                                     + status.master.winfo_height())


    def test_wheel_scroll_bounds(self):
        """§18.6：nav 点不滚；canvas 内点滚（长内容）；短内容不滚。"""
        _inject_agents(self.app, 20)
        self.app._aggregate()
        _set_logical_size(self.app, 860, 560)
        dash = self.app.dashboard
        dash._show_page(PAGE_AGENTS)
        dash.refresh_current_page()   # 生产刷新路径：fixture 行进入页面
        self.app.root.update()
        self.app.root.update_idletasks()
        canvas = dash.content._canvas
        self.assertTrue(dash.content._bar_visible)
        cx = canvas.winfo_rootx()
        cy = canvas.winfo_rooty()
        before = canvas.yview()
        # 指针在 canvas 左侧（nav 区域）→ 不滚
        self.assertFalse(dash.content.wheel_scroll(120, cx - 40, cy + 40))
        self.assertEqual(canvas.yview(), before)
        # 指针在 canvas 内 → 滚动
        self.assertTrue(dash.content.wheel_scroll(
            -120, cx + canvas.winfo_width() // 2,
            cy + canvas.winfo_height() // 2))
        self.assertNotEqual(canvas.yview(), before)
        # 短内容（无滚动条）→ False
        _set_logical_size(self.app, 1120, 720)
        dash._show_page(PAGE_SETTINGS)
        self.app.root.update()
        self.app.root.update_idletasks()
        dash._reflow_debounced()
        self.app.root.update()
        self.app.root.update_idletasks()
        self.assertFalse(dash.content._bar_visible)
        self.assertFalse(dash.content.wheel_scroll(
            -120, canvas.winfo_rootx() + 40, canvas.winfo_rooty() + 40))

    def test_hide_reopen_retains_page_and_geometry(self):
        """Reopening retains the page and drains pending layout callbacks."""
        dash = self.app.dashboard
        dash._show_page(PAGE_PETS)
        first = dash
        for _ in range(8):
            dash.hide_dashboard()
            dash.open()
            self.app.root.update()
        dash._reflow_debounced()
        self.app.root.update_idletasks()
        self.assertIs(self.app.dashboard, first)
        self.assertEqual(dash._page, PAGE_PETS)
        self.assertTrue(dash.is_open())
        self.assertIsNone(dash._reflow_after, "无周期 reflow timer")
        assert_page_has_visible_geometry(self, self.app, PAGE_PETS)


class DashboardActionSemanticsTests(unittest.TestCase):
    """v4.3.1 plan §10/§11/§19：Agents 行真实可点击区域 + "打开终端"
    统一语义（不改 focused 状态）。"""

    def setUp(self):
        self.app = _make_app()
        _inject_agents(self.app, 2)
        self.app._aggregate()
        self.app.open_dashboard()
        self.app.root.update()
        self.dash = self.app.dashboard
        self.dash._show_page(PAGE_AGENTS)
        self.dash.refresh_current_page()   # 生产刷新路径：行真实进入页面
        self.app.root.update()

    def tearDown(self):
        self.app.quit()

    def test_open_terminal_uses_activate_agent_without_focus_mutation(self):
        # §11/§19.1：exact key、恰好一次、focused_key 不变、
        # 不调用 presentation.set_focus
        page = self.dash._pages[PAGE_AGENTS]
        key = sorted(self.app.monitor.instances)[0]
        self.app.presentation.set_focus(
            sorted(self.app.monitor.instances)[1])
        focused_before = self.app.presentation.focused_key
        page._detail_key = key
        with patch.object(self.app, "activate_agent") as act_mock, \
                patch.object(self.app.presentation, "set_focus") as sf:
            page._activate_selected()
        act_mock.assert_called_once_with(key)
        sf.assert_not_called()
        self.assertEqual(self.app.presentation.focused_key, focused_before,
                         "打开终端不得偷偷改变 focused 状态")

    def test_row_click_areas_all_select_exact_agent(self):
        """§10.2/§19.2：frame / title / chip 三处 synthetic <Button-1>
        event 都选中 exact key——chip 是独立 child widget，必须显式
        绑定才可点（真实鼠标点击由 interactive 验收覆盖）。"""
        page = self.dash._pages[PAGE_AGENTS]
        keys = sorted(page._rows)
        self.assertTrue(keys)
        for key in keys:
            row = page._rows[key]
            for widget_name in ("frame", "title", "chip"):
                widget = row[widget_name]
                self.assertTrue(widget.bind("<Button-1>"),
                                f"{widget_name} 缺少 <Button-1> 绑定")
                page._detail_key = ""
                widget.event_generate("<Button-1>")   # synthetic routing
                self.app.root.update()
                self.assertEqual(page._detail_key, key,
                                 f"{widget_name} 点击必须选中 exact key")


class DataRootSettingsTests(unittest.TestCase):
    def test_monitor_page_data_roots_build_save_and_apply(self):
        import os
        import tempfile
        from agents import paths as agent_paths
        app = _make_app()
        try:
            from pet.dashboard import PAGE_MONITOR
            app.open_dashboard()
            dash = app.dashboard
            dash._show_page(PAGE_MONITOR)
            app.root.update()
            page = dash._pages[PAGE_MONITOR]
            self.assertTrue(page.built)
            vars_ = page.data_root_vars
            self.assertEqual(set(vars_), {"codex", "claude", "kimi", "pi",
                                          "zcode", "opencode", "dsh"})
            self.assertTrue(all(v.get() == "" for v in vars_.values()))
            custom = os.path.join(tempfile.gettempdir(), "codex-root")
            vars_["codex"].set(custom)
            page.data_root_savers["codex"]()
            self.assertEqual(
                app.config.get("monitor.data_roots.codex"), custom)
            self.assertEqual(agent_paths.data_root_overrides().get("codex"),
                             custom)
            self.assertEqual(agent_paths.codex_home_root(), custom)
            # 相对路径被拒绝，保持原值
            vars_["codex"].set("relative/path")
            page.data_root_savers["codex"]()
            self.assertEqual(
                app.config.get("monitor.data_roots.codex"), custom)
            # 清空 = 恢复默认
            vars_["codex"].set("")
            page.data_root_savers["codex"]()
            self.assertEqual(app.config.get("monitor.data_roots.codex"), "")
            self.assertNotIn("codex", agent_paths.data_root_overrides())
        finally:
            agent_paths.set_data_root_overrides({})
            app.root.destroy()


class ControllerWiringTests(unittest.TestCase):
    def test_settings_delegate_to_controllers_without_sync_writes(self):
        app = _make_app()
        try:
            app.open_dashboard()
            dash = app.dashboard
            dash._show_page(PAGE_LOOK)
            app.root.update()
            page = dash._pages[PAGE_LOOK]
            with patch.object(app.appearance, "set_global") as change, \
                    patch.object(app.config_saver, "request_save") as save:
                page._sync_from_config(initial=True)
                change.assert_not_called()
                save.assert_not_called()
                page.font_var.set("SimHei")
                page.font_combo.event_generate("<<ComboboxSelected>>")
                app.root.update()
                change.assert_called_once_with("bubble.font_family", "SimHei")
            with patch.object(app.config_saver, "request_save"):
                app.appearance.set_global("bubble.font_family", "SimHei")
            self.assertEqual(app.config.get("bubble.font_family"), "SimHei")
            self.assertTrue(app.ui._dirty_all_views or "pet-1" in app.ui._dirty_views)
            dash._show_page(PAGE_MONITOR)
            app.root.update()
            page = dash._pages[PAGE_MONITOR]
            with patch.object(app.monitor, "set_wsl_root_metadata_fallback") as runtime:
                page.root_meta_var.set(False)
                page.root_meta_check.invoke()
                runtime.assert_called_once_with(True)
            dash._show_page(PAGE_SETTINGS)
            app.root.update()
            page = dash._pages[PAGE_SETTINGS]
            with patch.object(app.config_saver, "request_save") as save, \
                    patch.object(app.config, "save") as write:
                page._retry_save()
                save.assert_called_once_with(immediate=True, force=True)
                write.assert_not_called()
            page._autostart_status_done(False, "registry unavailable")
            with patch.object(dash, "submit_action") as submit:
                page.refresh(UiDirty.DASHBOARD)
                submit.assert_not_called()
        finally:
            app.quit()


class DashboardInteractionStabilityTests(unittest.TestCase):
    """v4.3.1：按钮、刷新与 Configure 必须在一个 idle batch 收敛。"""

    def setUp(self):
        self.app = _make_app()
        _inject_agents(self.app, 2)
        self.app._aggregate()
        self.app.open_dashboard()
        self.app.root.update()
        self.dash = self.app.dashboard

    def tearDown(self):
        self.app.quit()

    def test_active_nav_is_idempotent(self):
        current = self.dash._page
        with patch.object(self.app.ui, "request") as request, \
                patch.object(self.dash._current, "reflow") as reflow:
            self.dash._show_page(current)
        request.assert_not_called()
        reflow.assert_not_called()

    def test_presentation_action_uses_only_coalesced_render_path(self):
        self.dash._show_page(PAGE_AGENTS)
        self.app.root.update()
        page = self.dash._pages[PAGE_AGENTS]
        page._detail_key = sorted(self.app.monitor.instances)[0]
        with patch.object(self.app, "_aggregate") as aggregate, \
                patch.object(page, "refresh") as refresh, \
                patch.object(self.app.ui, "request",
                             wraps=self.app.ui.request) as request:
            page._toggle_include()
        aggregate.assert_not_called()
        refresh.assert_not_called()
        self.assertEqual(request.call_count, 1)

    def test_duplicate_rescan_is_zero_ui_work(self):
        with patch.object(self.app.monitor, "rescan", return_value=False), \
                patch.object(self.app, "toast") as toast, \
                patch.object(self.app.ui, "kick") as kick:
            self.assertFalse(self.dash.request_rescan())
        toast.assert_not_called()
        kick.assert_not_called()

    def test_same_geometry_does_not_reconfigure_center(self):
        width = max(400, self.dash.content._canvas.winfo_width())
        self.dash._sync_center_geometry(width)
        with patch.object(self.dash._center, "pack_configure") as configure:
            for _ in range(100):
                self.dash._sync_center_geometry(width)
        configure.assert_not_called()

    def test_configure_storm_coalesces_and_drains(self):
        scroller = self.dash.content
        event = type("Event", (), {"width": 800})()
        for _ in range(100):
            scroller._on_canvas_configure(event)
            scroller._on_inner_configure(event)
        self.assertIsNotNone(scroller._layout_after)
        self.app.root.update_idletasks()
        self.assertIsNone(scroller._layout_after)


    def test_same_batch_refreshes_current_page_once(self):
        page = self.dash._current
        with patch.object(page, "refresh") as refresh:
            self.app.ui.request(UiDirty.DASHBOARD)
            self.app.ui.request(UiDirty.DASHBOARD)
            self.app.root.update_idletasks()
        refresh.assert_called_once()

    def test_rapid_appearance_changes_coalesce_save_render_build(self):
        """20 次快速外观变化：最多一个保存请求、一个 render idle、
        一次最终尺寸 build；Dashboard 页面身份保持不重建。"""
        from tests.test_ui import MemoryConfig
        from pet.petview import PetView
        self.dash._show_page(PAGE_LOOK)
        self.app.root.update()
        self.app.root.update_idletasks()
        holder_id = id(self.dash._pages[PAGE_LOOK].holder)
        saves = []
        loads = []
        saver = self.app.config_saver
        with patch.object(MemoryConfig, "save",
                          side_effect=lambda *a, **k: saves.append(1)), \
                patch.object(PetView, "load_skin", autospec=True,
                             side_effect=lambda view, bm:
                                 loads.append(view.view_id)):
            render_before = self.app.ui.render_count
            steps = SCALE_STEPS
            for i in range(20):
                self.app.appearance.set_global("scale", steps[i % len(steps)])
            self.app.root.update_idletasks()
            render_delta = self.app.ui.render_count - render_before
            # 20 次 request_save 合并为恰一个 debounce 排程
            self.assertIsNotNone(saver._timer)
            # 最终尺寸 build：单一 debounce token，flush 每个 view 恰一次
            self.assertIsNotNone(self.app.pet_manager._build_debounce_after)
            self.app.root.after_cancel(self.app.pet_manager._build_debounce_after)
            self.app.pet_manager._flush_deferred_build()
            self.assertTrue(saver.pending())
            saver.request_save(immediate=True, force=True)
        self.assertEqual(render_delta, 1)
        self.assertEqual(len(saves), 1)          # 20 次请求 → 一次保存
        self.assertEqual(len(loads), len(self.app.pet_manager.views))
        self.assertEqual(self.app.config.get("scale"),
                         steps[(20 - 1) % len(steps)])
        self.assertFalse(saver.pending())
        self.assertEqual(id(self.dash._pages[PAGE_LOOK].holder), holder_id)

    def test_external_action_is_single_worker_and_tk_harvested(self):
        with patch.object(threading.Thread, "start",
                          side_effect=RuntimeError("no thread")):
            self.assertFalse(self.dash.submit_action(
                "failed", lambda: None, lambda *_: None))
        self.assertFalse(self.dash.actions_pending())
        caller = threading.get_ident()
        entered = threading.Event()
        release = threading.Event()
        worker_ids = []
        callbacks = []

        def work():
            worker_ids.append(threading.get_ident())
            entered.set()
            release.wait(2.0)
            return "done"

        def done(ok, payload):
            callbacks.append((threading.get_ident(), ok, payload))

        self.assertTrue(self.dash.submit_action("one", work, done))
        self.assertTrue(entered.wait(1.0))
        self.assertFalse(self.dash.submit_action("duplicate", work, done))
        release.set()
        deadline = time.time() + 2.0
        while self.dash.actions_pending() and time.time() < deadline:
            self.app.ui._bridge_tick()
            self.app.root.update_idletasks()
            time.sleep(0.01)
        self.assertEqual(worker_ids, [worker_ids[0]])
        self.assertNotEqual(worker_ids[0], caller)
        self.assertEqual(callbacks, [(caller, True, "done")])
        self.assertFalse(self.dash.actions_pending())


    def test_shutdown_discards_late_action_result(self):
        entered = threading.Event()
        release = threading.Event()
        callbacks = []

        def work():
            entered.set()
            release.wait(2.0)
            return "late"

        self.assertTrue(self.dash.submit_action(
            "late", work, lambda *result: callbacks.append(result)))
        self.assertTrue(entered.wait(1.0))
        self.dash.request_action_stop()
        release.set()
        self.assertTrue(self.dash.join_actions(1.0))
        self.assertFalse(self.dash.poll_actions())
        self.assertEqual(callbacks, [])


if __name__ == "__main__":
    unittest.main()
