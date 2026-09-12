"""Geometry and interaction regressions for dashboard controls (real Tk)."""
import tkinter as tk
import unittest
from unittest.mock import patch

from pet.widgets import (DiscreteSlider, Expander, ScrollableFrame,
                         SettingRow, TooltipController)


class DashboardPolishTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry("900x600")

    def tearDown(self):
        self.root.destroy()

    def test_font_survives_temporary_reference_and_is_reused(self):
        import gc
        from pet.theme import pick_font
        label = tk.Label(self.root, font=pick_font(self.root, 11))
        name = label.cget("font")
        gc.collect()
        self.assertIn(name, self.root.tk.call("font", "names"))
        self.assertEqual(str(pick_font(self.root, 11)), name)

    def test_wide_compact_roundtrip_keeps_controls_inside_row(self):
        row = SettingRow(self.root, "很长的设置名称，需要完整显示" * 3)
        row.pack(fill="x")
        slider = DiscreteSlider(row, (0.5, 0.75, 1, 1.25, 1.5, 1.75, 2))
        row.set_control(slider)
        row.set_side_action(tk.Button(row, text="恢复默认"))
        for width in (900, 430, 900, 430):
            self.root.geometry(f"{width}x600")
            self.root.update()
            for widget in (row._label, slider, row._side_cell):
                right = widget.winfo_rootx() + widget.winfo_width()
                self.assertLessEqual(right, row.winfo_rootx() + row.winfo_width())
            if width == 430:
                self.assertEqual(row._mode, "compact")
                for column in range(4):
                    self.assertEqual(row.grid_columnconfigure(column)["minsize"], 0)

    def test_scrollbar_toggle_preserves_viewport_width(self):
        frame = ScrollableFrame(self.root)
        frame.pack(fill="both", expand=True)
        content = tk.Frame(frame.inner, height=40)
        content.pack(fill="x")
        self.root.update()
        width = frame._canvas.winfo_width()
        for height in (1200, 40, 1200, 40):
            content.configure(height=height)
            self.root.update()
            self.assertEqual(frame._bar_visible, height > 600)
            self.assertEqual(frame._canvas.winfo_width(), width)
            self.assertIsNone(frame._layout_after)

    def test_expander_retains_body_and_grows_for_wrapped_text(self):
        expander = Expander(self.root, "更多检测信息")
        expander.pack(fill="x")
        text = tk.Label(expander.body, text="信息 " * 100, wraplength=250)
        text.pack(fill="x")
        self.root.update()
        closed_height = expander.winfo_height()
        expander.toggle()
        self.root.update()
        self.assertGreater(expander.winfo_height(), closed_height)
        self.assertTrue(text.winfo_viewable())
        expander.toggle()
        self.root.update()
        self.assertFalse(text.winfo_viewable())
        self.assertTrue(text.winfo_exists())

    def test_tooltip_does_not_pump_nested_event_loop(self):
        anchor = tk.Label(self.root, text="info")
        anchor.pack()
        self.root.update()
        tooltip = TooltipController(self.root)
        try:
            with patch.object(tk.Toplevel, "update_idletasks") as update:
                tooltip._show(anchor, "检测说明 " * 30)
            update.assert_not_called()
            self.root.update()
            self.assertGreater(tooltip._win.winfo_height(), 30)
            self.assertLessEqual(tooltip._win.winfo_width(), tooltip.MAX_W)
            tooltip.schedule(anchor, "pending")
            tooltip.hide()
            self.assertIsNone(tooltip._after)
            self.assertIsNone(tooltip._win)
        finally:
            tooltip.hide()


if __name__ == "__main__":
    unittest.main()
