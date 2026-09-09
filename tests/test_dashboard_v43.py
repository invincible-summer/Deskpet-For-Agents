"""Dashboard v4.3 架构测试（plan2 §10-§16）。

  * 7 页导航 + 设置固定底部；
  * lazy build：创建时只有概览页构建，其余首次点击才 build；
  * 只刷新当前页（隐藏页 refresh 计数 0）；
  * Agent retained rows：同 key 只 configure 不 recreate；
  * 无 bind_all("<MouseWheel>")（滚轮只在页面 canvas 内）；
  * §13.2 离散值组齐备且默认 1.00。
"""
from __future__ import annotations
import sys
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


def _make_app():
    from pet.app import PetApp
    from pet.petview import PetView
    from tests.test_ui import MemoryConfig
    with patch.object(PetApp, "_reload_skins", lambda self: None), \
         patch.object(PetView, "load_skin", lambda self, bm: None):
        return PetApp(MemoryConfig())


def _inject_agents(app, n=2):
    from agents.models import Status
    from tests.test_fleet_ui import inst, snap
    from agents.models import AgentKind
    agents = [inst(AgentKind.CODEX, i + 1, cwd=f"/w/p{i}")
              for i in range(n)]
    app.monitor.instances = {a.key: a for a in agents}
    app.monitor.snapshots = {a.key: snap(a) for a in agents}
    return agents


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


if __name__ == "__main__":
    unittest.main()
