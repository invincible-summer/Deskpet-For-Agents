"""Dashboard v4.3 架构测试（plan2 §10-§16）。

  * 7 页导航 + 设置固定底部；
  * lazy build：创建时只有概览页构建，其余首次点击才 build；
  * 只刷新当前页（隐藏页 refresh 计数 0）；
  * Agent retained rows：同 key 只 configure 不 recreate；
  * 无 bind_all("<MouseWheel>")（滚轮只在页面 canvas 内）；
  * §13.2 离散值组齐备且默认 1.00。

v4.3.1 交互收口新增（plan §18）：七页真实 geometry gate（compact/wide
双尺寸）、scrollregion/滚动条合同、滚轮 bounds、hide/reopen 几何健康。
该组直接抓"built=True 但 height≈1px 右侧空白"的旧盲点。
"""
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
    PAGE_OVERVIEW,
    PAGE_PETS,
    PAGE_SETTINGS,
)
from pet.ui_coordinator import UiDirty

_ALL_PAGES = (PAGE_OVERVIEW, PAGE_AGENTS, PAGE_PETS, PAGE_LOOK,
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

    def test_drag_snaps_to_nearest_index(self):
        slider, calls = self._slider()
        # 拖到两个合法值之间（index 1.4）→ 立即 snap 到 1
        slider._on_drag(1.4)
        self.assertAlmostEqual(slider.value(), 0.75)
        self.assertEqual(calls, [0.75])
        slider._on_drag(1.6)
        self.assertAlmostEqual(slider.value(), 1.0)
        self.assertEqual(calls, [0.75, 1.0])

    def test_keyboard_steps_invoke_immediately(self):
        slider, calls = self._slider()
        slider.set_external(1.0)
        self.assertEqual(calls, [])   # 外部同步不触发
        slider._step(1)
        self.assertAlmostEqual(slider.value(), 1.25)
        self.assertEqual(calls[-1], 1.25)
        slider._step(-1)
        slider._step(-1)
        self.assertAlmostEqual(slider.value(), 0.75)
        # Home/End
        slider._set_index(0, invoke=True)
        self.assertAlmostEqual(slider.value(), 0.5)
        slider._set_index(len(slider._values) - 1, invoke=True)
        self.assertAlmostEqual(slider.value(), 1.5)

    def test_off_step_config_shown_as_custom_not_rewritten(self):
        slider, calls = self._slider()
        slider.set_external(1.37)   # 非 step 值：不触发 command
        self.assertEqual(calls, [])
        self.assertAlmostEqual(slider.value(), 1.25)   # thumb 最近 step
        self.assertIn("自定义", slider._value_label["text"])
        # 用户第一次移动 → 进入合法离散值
        slider._step(1)
        self.assertAlmostEqual(slider.value(), 1.5)
        self.assertEqual(calls, [1.5])


class DashboardV43Tests(unittest.TestCase):
    def test_nav_pages_and_lazy_build(self):
        app = _make_app()
        try:
            app.open_dashboard()
            app.root.update()
            dash = app.dashboard
            names = list(dash._pages)
            self.assertEqual(names, [PAGE_OVERVIEW, PAGE_AGENTS, PAGE_PETS,
                                     PAGE_LOOK, PAGE_MONITOR, PAGE_DIAG,
                                     PAGE_SETTINGS])
            # 创建时只有默认页（概览）构建
            built = [n for n, p in dash._pages.items() if p.built]
            self.assertEqual(built, [PAGE_OVERVIEW])
            for name in (PAGE_AGENTS, PAGE_LOOK, PAGE_MONITOR, PAGE_DIAG,
                         PAGE_SETTINGS):
                dash._show_page(name)
                app.root.update()
                self.assertTrue(dash._pages[name].built, name)
        finally:
            app.quit()

    def test_only_current_page_refreshed(self):
        app = _make_app()
        try:
            _inject_agents(app)
            app._aggregate()
            app.open_dashboard()
            app.root.update()
            dash = app.dashboard
            counts: dict[str, int] = {}
            originals = {}
            for name, page in dash._pages.items():
                originals[name] = page.refresh

                def make_refresh(_orig, _name):
                    def _refresh(reason):
                        counts[_name] = counts.get(_name, 0) + 1
                    return _refresh

                page.refresh = make_refresh(originals[name], name)
            # 概览是当前页：只有它被刷新
            dash.refresh_current_page()
            dash.refresh_current_page()
            self.assertEqual(counts, {PAGE_OVERVIEW: 2})
            dash._show_page(PAGE_DIAG)
            dash.refresh_current_page()
            self.assertEqual(counts[PAGE_OVERVIEW], 2)
            self.assertEqual(counts.get(PAGE_DIAG), 1)
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
            overview = dash._pages[PAGE_OVERVIEW]
            overview.refresh(0)
            first_keys = set(overview._rows)
            row_ids = {k: overview._rows[k]["frame"].winfo_id()
                       for k in overview._rows}
            # 状态变化 → 只 configure（widget 身份不变）
            from tests.test_fleet_ui import snap as _snap
            from agents.models import Status
            app.monitor.snapshots[agents[0].key] = _snap(
                agents[0], Status.ERROR)
            overview.refresh(0)
            self.assertEqual(set(overview._rows), first_keys)
            for k, widget_id in row_ids.items():
                self.assertEqual(overview._rows[k]["frame"].winfo_id(),
                                 widget_id, "同 key 行不得 recreate")
            # Agent 消失 → 行删除
            app.monitor.instances.pop(agents[1].key)
            app.monitor.snapshots.pop(agents[1].key)
            overview.refresh(0)
            self.assertNotIn(agents[1].key, overview._rows)
        finally:
            app.quit()

    def test_no_global_mousewheel_binding(self):
        def code_lines(path):
            text = Path(path).read_text(encoding="utf-8")
            out = []
            for line in text.splitlines():
                stripped = line.strip()
                if (stripped.startswith("#") or stripped.startswith('"""')
                        or stripped.startswith("'''")):
                    continue
                out.append(line.split("#")[0])
            return "\n".join(out)

        self.assertNotIn(".bind_all(", code_lines("pet/widgets.py"))
        self.assertNotIn(".bind_all(", code_lines("pet/dashboard.py"))

    def test_discrete_step_sets(self):
        for steps in (SCALE_STEPS, SPEED_STEPS, BUBBLE_W_STEPS,
                      BUBBLE_H_STEPS, BUBBLE_FONT_STEPS):
            self.assertIn(1.0, steps)
            self.assertEqual(steps, tuple(sorted(steps)))
            self.assertEqual(len(steps), 7)


class DashboardGeometryTests(unittest.TestCase):
    """v4.3.1 plan §18：七页真实 geometry gate（compact/wide）、
    scrollregion/滚动条、滚轮 bounds、hide/reopen 几何健康。"""

    def setUp(self):
        self.app = _make_app()
        self.app.open_dashboard()
        self.app.root.update()

    def tearDown(self):
        self.app.quit()

    def test_seven_pages_geometry_wide_and_compact(self):
        """§18.3/§18.4：全部七页（不是抽两页）× wide(1120x720) 与
        compact(860x560) 都通过 geometry gate。"""
        for logical_w, logical_h in ((1120, 720), (860, 560)):
            _set_logical_size(self.app, logical_w, logical_h)
            for page_name in _ALL_PAGES:
                assert_page_has_visible_geometry(
                    self, self.app, page_name)
                holder = self.app.dashboard._pages[page_name].holder
                self.assertGreater(holder.winfo_height(), 100,
                                   f"{page_name}@{logical_w} holder 高度")

    def test_long_content_shows_scrollbar(self):
        """§18.5：长内容（多 Agent）→ 滚动条出现且 scrollregion 高于
        canvas 可视高度。"""
        _inject_agents(self.app, 20)
        self.app._aggregate()
        _set_logical_size(self.app, 860, 560)
        self.app.dashboard._show_page(PAGE_OVERVIEW)
        self.app.dashboard.refresh_current_page()   # 生产刷新路径
        self.app.root.update()
        self.app.root.update_idletasks()
        canvas = self.app.dashboard.content._canvas
        self.assertGreater(len(self.app.dashboard._pages[PAGE_OVERVIEW]._rows),
                           0, "fixture Agent 行必须真实进入页面")
        self.assertTrue(self.app.dashboard.content._bar_visible,
                        "长内容必须显示滚动条")
        region = [int(v) for v in str(canvas["scrollregion"]).split()]
        self.assertGreater(region[3] - region[1],
                           canvas.winfo_height(),
                           "scrollregion 高度必须超过可视高度")

    def test_short_page_hides_scrollbar(self):
        _set_logical_size(self.app, 1120, 720)
        self.app.dashboard._show_page(PAGE_SETTINGS)
        self.app.root.update()
        self.app.root.update_idletasks()
        self.app.dashboard._reflow_debounced()
        self.app.root.update()
        self.app.root.update_idletasks()
        self.assertFalse(self.app.dashboard.content._bar_visible,
                         "短内容页不得显示滚动条")

    def test_wheel_scroll_bounds(self):
        """§18.6：nav 点不滚；canvas 内点滚（长内容）；短内容不滚。"""
        _inject_agents(self.app, 20)
        self.app._aggregate()
        _set_logical_size(self.app, 860, 560)
        dash = self.app.dashboard
        dash._show_page(PAGE_OVERVIEW)
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

    def test_hide_reopen_100_cycles_keeps_geometry(self):
        """§18.7：100 轮 hide/reopen——Toplevel 身份不变、当前页保留、
        重开后几何健康、无 reflow timer 残留。"""
        dash = self.app.dashboard
        dash._show_page(PAGE_PETS)
        first = dash
        for _ in range(100):
            dash.hide_dashboard()
            dash.open()
            self.app.root.update()
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


class ControllerWiringTests(unittest.TestCase):
    """v4.3.1 DP43-R11/R02/R03：Dashboard 控件必须接回 controller/
    saver/monitor（无死控件），保存全部异步。"""

    def _page(self, name):
        app = _make_app()
        app.open_dashboard()
        app.root.update()
        dash = app.dashboard
        dash._show_page(name)
        app.root.update()
        return app, dash._pages[name]

    def test_font_selection_calls_set_global_exactly_once(self):
        # DP43-R11：用户选字体 → set_global("bubble.font_family") 恰一次
        app, page = self._page(PAGE_LOOK)
        try:
            with patch.object(app.appearance, "set_global") as sg:
                page.font_var.set("SimHei")
                page.font_combo.event_generate("<<ComboboxSelected>>")
                app.root.update()
            sg.assert_called_once_with("bubble.font_family", "SimHei")
        finally:
            app.quit()

    def test_font_sync_from_config_no_save_no_apply(self):
        # DP43-R11：程序化 sync（refresh 路径）不触发 set_global/save
        app, page = self._page(PAGE_LOOK)
        try:
            with patch.object(app.appearance, "set_global") as sg, \
                 patch.object(app.config_saver, "request_save") as rs:
                page._sync_from_config(initial=True)
                app.root.update()
            sg.assert_not_called()
            rs.assert_not_called()
        finally:
            app.quit()

    def test_font_change_updates_config_and_marks_view_dirty(self):
        # DP43-R11：真实 controller 路径——Config 更新 + 异步保存请求 +
        # 受影响 view 进 render 队列（bubble invalidate）
        app, page = self._page(PAGE_LOOK)
        try:
            with patch.object(app.config_saver, "request_save"):
                app.appearance.set_global("bubble.font_family", "SimHei")
            self.assertEqual(app.config.get("bubble.font_family"),
                             "SimHei")
            # render flush 尚未执行（无 update）：dirty set 仍含 pet-1
            if not app.ui._dirty_all_views:
                self.assertIn("pet-1", app.ui._dirty_views)
        finally:
            app.quit()

    def test_privacy_toggle_hits_monitor_runtime(self):
        # DP43-R03：隐私开关 runtime 撤权/授权立即下发 Monitor
        app, page = self._page(PAGE_MONITOR)
        try:
            calls = []
            with patch.object(
                    app.monitor, "set_wsl_root_metadata_fallback",
                    side_effect=lambda f: calls.append(f)):
                page.root_meta_var.set(False)
                page.root_meta_check.invoke()   # toggle → True + command
                app.root.update()
            self.assertEqual(calls, [True])
            self.assertTrue(app.config.get(
                "privacy.wsl_root_metadata_fallback"))
        finally:
            app.quit()

    def test_retry_save_is_async(self):
        # DP43-R02：重试保存走 saver（immediate+force），绝不同步写盘
        app, page = self._page(PAGE_SETTINGS)
        try:
            with patch.object(app.config_saver, "request_save") as rs, \
                 patch.object(app.config, "save") as save:
                page._retry_save()
            rs.assert_called_once_with(immediate=True, force=True)
            save.assert_not_called()   # 不在 Tk 同步写盘
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

    def test_same_appearance_value_is_zero_work(self):
        current = self.app.config.get("scale")
        with patch.object(self.app.config, "set", return_value=False) as set_, \
                patch.object(self.app.pet_manager,
                          "apply_appearance_change") as apply, \
                patch.object(self.app.config_saver, "request_save") as save, \
                patch.object(self.app.ui, "request") as render:
            self.app.appearance.set_global("scale", current)
        set_.assert_called_once_with("scale", current)
        apply.assert_not_called()
        save.assert_not_called()
        render.assert_not_called()

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

    def test_autostart_status_failure_does_not_retry_loop(self):
        self.dash._show_page(PAGE_SETTINGS)
        self.app.root.update()
        page = self.dash._pages[PAGE_SETTINGS]
        page._autostart_status_done(False, "registry unavailable")
        self.assertIsNotNone(page._autostart_status)
        with patch.object(self.dash, "submit_action") as submit:
            page.refresh(UiDirty.DASHBOARD)
        submit.assert_not_called()

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

    def test_action_thread_start_failure_is_recoverable(self):
        with patch.object(threading.Thread, "start",
                          side_effect=RuntimeError("no thread")):
            self.assertFalse(self.dash.submit_action(
                "failed", lambda: None, lambda *_: None))
        self.assertFalse(self.dash.actions_pending())


if __name__ == "__main__":
    unittest.main()
