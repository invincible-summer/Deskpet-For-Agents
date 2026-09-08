# -*- coding: utf-8 -*-
"""V4.1.1 回归测试：动画单位 / Dashboard Card / 气泡尾巴。

对应三个实机反馈的缺陷：
  1. AnimationCursor.advance 把毫秒当秒加进 monotonic 时间线 →
     每帧间隔 83 秒（动画几乎不动）；
  2. widgets.Card 构造时把 canvas 锁死在 1×1（内容在 canvas 窗口项内
     不贡献请求尺寸）→ 仪表盘全部卡片塌成 1px 线；以及 _on_canvas_resize
     里 delete("all") 连带删掉挂 body 的 window item → 卡片空白；
  3. 气泡尾巴尖端传成 win_h-2（窗口底）→ 尾巴贯穿整个桌宠。
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pet.animator import (
    Animation,
    AnimationCursor,
    AnimationScheduler,
    SharedAnimationCache,
)


class _FakeMeta:
    path = "unused"
    base_delay = 83
    n = 8
    width = 10
    height = 10
    loop = True


class _FakeRoot:
    """只实现 after/after_cancel 的调度根，记录延迟。"""

    def __init__(self):
        self.after_calls: list[tuple[int, object]] = []
        self._cancelled = []

    def after(self, ms, cb=None):
        self.after_calls.append((int(ms), cb))
        return f"after#{len(self.after_calls)}"

    def after_cancel(self, aid):
        self._cancelled.append(aid)


class AnimatorUnitsTests(unittest.TestCase):
    """毫秒（meta.delay_ms）必须换算成秒后才能加进 monotonic 时间线。"""

    def test_frame_delay_is_seconds(self):
        cursor = AnimationCursor("k")
        cursor.play("p", "walk", _FakeMeta())
        self.assertAlmostEqual(cursor.frame_delay(), 0.083, delta=0.002)

    def test_advance_next_due_within_frame_period(self):
        cursor = AnimationCursor("k")
        cursor.play("p", "walk", _FakeMeta())
        self.assertTrue(cursor.advance(100.0))
        # 回归：修复前 next_due = 100 + 83（秒）= 183
        self.assertGreater(cursor.next_due, 100.0)
        self.assertLessEqual(cursor.next_due, 100.5)

    def test_advance_speed_scales_delay(self):
        cursor = AnimationCursor("k")
        cursor.play("p", "walk", _FakeMeta())
        cursor.speed = 2.0
        cursor.advance(0.0)
        self.assertAlmostEqual(cursor.next_due, 0.0415, delta=0.003)

    def test_scheduler_delay_ms_not_seconds(self):
        root = _FakeRoot()
        sched = AnimationScheduler(root, SharedAnimationCache())
        cursor = AnimationCursor("k")
        cursor.play("p", "walk", _FakeMeta())
        sched.register(cursor, lambda _v: None)
        ms = root.after_calls[-1][0]
        # 回归：修复前这里会把 83 秒当作毫秒之外的换算，出现 ~83000ms
        self.assertLessEqual(ms, 500, f"scheduler armed {ms}ms")
        self.assertGreaterEqual(ms, 5)

    def test_play_and_immediate_redraw_keeps_frame_zero(self):
        """切换动画时第 0 帧立即可取（PetView._play 会立刻 redraw）。"""
        cursor = AnimationCursor("k")
        cursor.play("a", "walk", _FakeMeta())
        self.assertEqual(cursor.frame_index, 0)
        cursor.play("a", "die", _FakeMeta(), force=True)
        self.assertEqual(cursor.frame_index, 0)

    def test_static_resets_to_frame_zero(self):
        cursor = AnimationCursor("k")
        cursor.play("p", "walk", _FakeMeta())
        cursor.advance(0.0)
        cursor.static = True
        cursor.frame_index = 0
        self.assertEqual(cursor.frame_index, 0)


class CardWidgetTests(unittest.TestCase):
    """Card 必须由内容撑起高度，且 canvas 窗口项不可被删除。"""

    def setUp(self):
        try:
            import tkinter as tk
        except Exception:
            raise unittest.SkipTest("no display")
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def _card_with_content(self, padding=12):
        from pet.widgets import Card
        card = Card(self.root, padding=padding)
        card.pack(fill="x")
        inner = card.body
        import tkinter as tk
        tk.Label(inner, text="Codex · D:/work/deskpet").pack(anchor="w")
        tk.Label(inner, text="Live 2   Waiting 1").pack(anchor="w")
        import tkinter.ttk as ttk
        ttk.Button(inner, text="打开终端").pack(anchor="e")
        return card

    def test_card_grows_to_content_height(self):
        card = self._card_with_content()
        self.root.update_idletasks()
        want = card.body.winfo_reqheight() + 2 * 12
        # 请求尺寸即可证明"内容撑起卡片"（withdrawn root 下 actual 恒 1）
        self.assertGreaterEqual(card.winfo_reqheight(), want - 1)
        self.assertGreater(card.winfo_reqheight(), 30)   # 回归：修复前恒为 1
        self.root.deiconify()
        self.root.update()
        self.assertGreaterEqual(card.winfo_height(), want - 1)

    def test_card_window_item_survives_resize(self):
        card = self._card_with_content()
        self.root.update_idletasks()
        self.root.update()
        # 触发一次 canvas resize（宽度变化）
        card.configure(width=600)
        card._canvas.configure(width=600)
        self.root.update_idletasks()
        self.root.update()
        self.assertEqual(card._canvas.type(card._window), "window")
        # body 必须仍挂在 canvas 上（回归：delete("all") 曾把它删掉）
        self.assertEqual(card.body.winfo_parent(), str(card._canvas))
        self.assertGreaterEqual(card.body.winfo_reqheight(), 1)

    def test_expander_toggle_grows_card(self):
        from pet.widgets import Card, Expander
        card = Card(self.root, padding=12)
        card.pack(fill="x")
        import tkinter as tk
        exp = Expander(card.body, "高级")
        exp.pack(fill="x")
        tk.Label(exp.body, text="间隔 3.0 秒").pack(anchor="w")
        self.root.update_idletasks()
        h_closed = card.winfo_height()
        exp.toggle()
        self.root.update_idletasks()
        self.root.update()
        self.assertGreater(card.winfo_height(), h_closed)


class BubbleTailTests(unittest.TestCase):
    """尾巴尖端必须落在 pet_top（桌宠顶部），不得伸到窗口底。"""

    def setUp(self):
        try:
            import tkinter as tk
        except Exception:
            raise unittest.SkipTest("no display")
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.canvas = tk.Canvas(self.root, width=400, height=300,
                                highlightthickness=0)
        self.canvas.pack()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def _config(self):
        class Cfg:
            def get(self, path, default=None):
                return {"scale": 1.0, "bubble": {}}.get(path, default)
        return Cfg()

    def test_single_tail_apex_at_pet_top(self):
        from pet.bubble import BubbleModel, SingleAgentBubbleRenderer
        b = SingleAgentBubbleRenderer(self.canvas, self._config())
        b.model = BubbleModel(visible=True, status="Codex", text="分析",
                              footer="打开终端", agent_key="k1")
        b.layout()
        pet_top = b.h + 6          # 桌宠顶部在气泡下方 6px（gap）
        b.draw(0, 0, 200, pet_top)
        tail = b._items[0]         # 尾巴先画
        x0, y0, x1, y1 = self.canvas.bbox(tail)
        self.assertEqual(self.canvas.type(tail), "polygon")
        # bbox 含 1px outline，容差 2
        self.assertLessEqual(abs(y1 - pet_top), 2,
                             f"tail apex {y1} != pet_top {pet_top}")
        self.assertLessEqual(y1, pet_top + 2)   # 不得越过桌宠顶

    def test_single_tail_short_when_gap_small(self):
        from pet.bubble import BubbleModel, SingleAgentBubbleRenderer
        b = SingleAgentBubbleRenderer(self.canvas, self._config())
        b.model = BubbleModel(visible=True, text="x", agent_key="k1")
        b.layout()
        pet_top = b.h + 5
        b.draw(0, 0, 200, pet_top)
        x0, y0, x1, y1 = self.canvas.bbox(b._items[0])
        self.assertLessEqual(y1 - (b.h - 1), 12)   # 尾巴长度受 gap 约束

    def test_stack_renderer_removed(self):
        """V4.1.1：并发不再使用 "N Agents" 栈卡（用户反馈）。"""
        import pet.bubble as bubble
        self.assertFalse(hasattr(bubble, "AgentStackBubbleRenderer"))


class HelpDotTests(unittest.TestCase):
    """？帮助图标：悬浮提示显示/隐藏（注释不写进界面正文）。"""

    def setUp(self):
        try:
            import tkinter as tk
        except Exception:
            raise unittest.SkipTest("no display")
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_show_creates_tooltip_and_hide_destroys(self):
        from pet.widgets import HelpDot
        dot = HelpDot(self.root, "这是说明文字", bg="#F7F8F6")
        dot.pack()
        self.root.update_idletasks()
        self.root.update()
        self.assertIsNone(dot._tip)
        dot._show()
        self.assertIsNotNone(dot._tip)
        self.assertTrue(dot._tip.winfo_exists())
        # 提示窗内有完整文字
        texts = [w for w in dot._tip.winfo_children()
                 if isinstance(w, __import__("tkinter").Label)]
        self.assertEqual(texts[0].cget("text"), "这是说明文字")
        dot._hide()
        self.assertIsNone(dot._tip)

    def test_empty_text_never_shows(self):
        from pet.widgets import HelpDot
        dot = HelpDot(self.root, "")
        dot._show()
        self.assertIsNone(dot._tip)


if __name__ == "__main__":
    unittest.main()


class UiContractTests(unittest.TestCase):
    """v4.1.1 §24：普通界面不再存在 exact Tab/Pane 产品语义字符串。

    * 禁止出现：手工绑定入口文案、STALE_TAB/STALE_PANE、精确 Tab/Pane；
    * Help 文案必须出现：打开 Windows Terminal 窗口 / 不切换标签页 /
      不发送键盘输入；
    * Fleet slot 绑定 UI（Presentation 绑定）保留。
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _source(self, *parts):
        with open(os.path.join(self.ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def test_no_manual_terminal_binding_strings_in_ui(self):
        for parts in (("pet", "dashboard.py"), ("pet", "app.py"),
                      ("pet", "petview.py"), ("pet", "bubble.py")):
            src = self._source(*parts)
            for banned in ("关联当前 Terminal 位置", "STALE_TAB",
                           "STALE_PANE", "精确 Tab", "精确 Pane",
                           "Tab RuntimeId", "Pane RuntimeId"):
                self.assertNotIn(banned, src,
                                 f"{parts} 不应包含 {banned!r}")

    def test_open_terminal_help_contains_window_semantics(self):
        src = self._source("pet", "dashboard.py")
        for required in ("Windows Terminal 窗口", "不切换标签页",
                         "不发送键盘输入"):
            self.assertIn(required, src,
                          f"帮助文案缺少 {required!r}")

    def test_tray_startup_does_not_rewrite_config(self):
        """v4.1.1 §17：启动托盘是纯运行期动作，不写 config。

        只有用户显式切换（set_tray_enabled）才持久化 tray_enabled。
        """
        src = self._source("pet", "app.py")
        self.assertIn("def _start_tray_runtime", src)
        self.assertIn("def set_tray_enabled", src)

        def method_body(name):
            return src.split(f"def {name}")[1].split("\n\n    def ")[0]

        # _start_tray_runtime 不做任何持久化
        body = method_body("_start_tray_runtime")
        self.assertNotIn("set_and_commit", body)
        self.assertNotIn("config.save", body)
        # 持久化只发生在 set_tray_enabled
        set_body = method_body("set_tray_enabled")
        self.assertIn("set_and_commit", set_body)

    def test_fleet_slot_binding_ui_preserved(self):
        """Fleet slot assignment 是 Presentation 绑定，应保留（§12.2）。"""
        src = self._source("pet", "dashboard.py")
        for required in ("更换 Agent", "解除绑定"):
            self.assertIn(required, src)


class ScrollableFrameAdaptiveTests(unittest.TestCase):
    """v4.1.2 仪表盘自适应：滚动条只在内容超出时出现（按内容显隐）。"""

    def setUp(self):
        try:
            import tkinter as tk
        except Exception:
            raise unittest.SkipTest("no display")
        self.tk = tk
        self.root = tk.Tk()
        self.root.geometry("400x300+40+40")
        self.root.deiconify()
        self.root.update()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_scrollbar_hidden_when_content_fits(self):
        tk = self.tk
        from pet.widgets import ScrollableFrame
        frame = ScrollableFrame(self.root)
        frame.pack(fill="both", expand=True)
        tk.Label(frame.inner, text="short").pack()
        self.root.update_idletasks()
        self.root.update()
        # 内容不超过可视高度：滚动条隐藏（滑块不超出内容）
        self.assertFalse(frame._bar_visible)
        # 内容超出：滚动条出现
        for i in range(40):
            tk.Label(frame.inner, text=f"line {i}").pack()
        self.root.update_idletasks()
        self.root.update()
        self.assertTrue(frame._bar_visible)
        self.assertTrue(frame.winfo_ismapped())

    def test_wraplength_binding_adapts_to_width(self):
        """容器变窄 → wraplength 跟随；不低于最小可读宽度。"""
        tk = self.tk
        from pet.widgets import bind_wraplength
        holder = tk.Frame(self.root, width=240, height=30)
        holder.pack_propagate(False)
        label = tk.Label(holder, text="x" * 400, wraplength=480)
        bind_wraplength(label)   # 布局前绑定：首帧即自适应
        label.pack(fill="both", expand=True)
        holder.pack()
        self.root.update()
        self.assertLessEqual(int(label.cget("wraplength")), 240)
        # 容器变宽 → wraplength 跟随放宽
        holder.configure(width=360)
        self.root.update()
        self.assertGreater(int(label.cget("wraplength")), 240)
        # 容器极窄 → 不低于最小可读宽度
        holder.configure(width=60)
        self.root.update()
        self.assertGreaterEqual(int(label.cget("wraplength")), 160)
