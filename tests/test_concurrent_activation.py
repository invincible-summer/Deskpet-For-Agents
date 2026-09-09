"""并发激活测试矩阵（v4.1.3 §14/§29）。

  * Fleet body 双击 → 各自 exact agent_key（绝不走 focused_key）；
  * Fleet 气泡双击 → 各自 exact agent_key；
  * 不同 Agent 映射不同 HWND → try_set_foreground 用各自 exact hwnd；
  * 两个 Agent 同属一个 Terminal window → 都前置同一窗口且不切 Tab
    （window-level 语义，防止后续又当 bug 重引入 exact tab selection）；
  * Aggregate 气泡 race：点击瞬间 attention 变化不改变激活目标
    （visual identity == click identity，由 model.agent_key 固化）；
  * 三模式矩阵（§29）：气泡/body 均双击激活（AGGREGATE body 只互动）、
    气泡单击不激活、一次双击只一次 activation。
"""
from __future__ import annotations
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.models import (
    ActivationCode,
    ActivationResult,
    AgentInstance,
    AgentKind,
    Snapshot,
    Status,
    TerminalWindowBinding,
    WindowBindingConfidence,
    WindowIdentity,
)
from agents.terminal_service import WindowsTerminalService
from agents.terminal_uia import WT_WINDOW_CLASS

NOW = 1_000_000.0


class ConcurrentConfig:
    def __init__(self, slots):
        self.data = {
            "skin": "amiya", "scale": 1.0, "speed": 1.0, "animated": True,
            "topmost": True, "tray_enabled": False, "pet_pos": None,
            "bubble": {"enabled": True, "font_family": "Microsoft YaHei UI",
                       "font_size": 11, "height": 132, "width": 300,
                       "relative_width": 1.0, "relative_height": 1.0,
                       "relative_font": 1.0},
            "animation_cache_mb": 8,
            "presentation": {"concurrent": {
                "enabled": True, "mode": "fleet", "max_targets": 3,
                "eligible_kinds": {"codex": True, "claude": True,
                                   "kimi": True, "pi": True},
                "slots": slots}},
        }
        self.migration_notice = False
        self.saved = 0

    def get(self, path, default=None):
        node = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path, value):
        parts = path.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def save(self):
        self.saved += 1
        return type("R", (), {"ok": True, "path": "", "error": ""})()


def _slot(slot_id):
    return {"id": slot_id, "selector": None, "appearance": None,
            "placement": {"monitor": "", "u": None, "v": None,
                          "anchor": None, "manual": False}}


def inst(kind, pid, cwd="/w/p"):
    return AgentInstance(kind, pid, "wsl:Ubuntu", cwd=cwd,
                         process_token=str(pid), started_at=NOW - pid)


def snap(i, status=Status.WORKING):
    return Snapshot(i.key, i.kind, i.source, i.pid, status=status, ts=NOW)


def make_app(slots):
    from pet.app import PetApp
    from pet.petview import PetView
    from pet.presentation import PresentationMode
    cfg = ConcurrentConfig(slots)
    with patch.object(PetApp, "_reload_skins", lambda self: None), \
         patch.object(PetView, "load_skin", lambda self, bm: None):
        app = PetApp(cfg)
    # v4.3：mode 是运行期 session state（config 旧 mode=fleet 被启动
    # 策略忽略），本测试矩阵显式切到 fleet
    app.presentation.set_concurrent_mode(PresentationMode.FLEET)
    return app


def bind_window(hwnd, pid=100, created=1.0):
    return TerminalWindowBinding(
        window=WindowIdentity(hwnd=hwnd, pid=pid, process_created=created,
                              window_class=WT_WINDOW_CLASS),
        confidence=WindowBindingConfidence.CONFIRMED,
        title="wt", last_seen=NOW, validated_at=NOW,
        reason="windows-ancestor")


class ForegroundRecorder:
    """记录 restore/foreground/flash 调用（mock 三件套）。"""

    def __init__(self):
        self.foreground = []
        self.restores = []
        self.flashes = []

    def patches(self):
        return [
            patch("actions.winkeys.validate_window", return_value=True),
            patch("actions.winkeys.restore_window",
                  side_effect=self.restores.append),
            patch("actions.winkeys.try_set_foreground",
                  side_effect=self._foreground),
            patch("actions.winkeys.flash_window",
                  side_effect=self.flashes.append),
        ]

    def _foreground(self, hwnd):
        self.foreground.append(hwnd)
        return True


class FleetBodyActivationTests(unittest.TestCase):
    def test_each_pet_double_click_activates_its_own_agent(self):
        app = make_app([_slot("pet-1"), _slot("pet-2")])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2)
            app.monitor.instances = {a.key: a, b.key: b}
            app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
            app._aggregate()
            view1 = app.pet_manager.views["pet-1"]
            view2 = app.pet_manager.views["pet-2"]
            activated = []
            view1._on_activate = activated.append
            view2._on_activate = activated.append
            view1._on_body_double()
            view2._on_body_double()
            # pet-1/pet-2 各自 exact key（自动分配按注意力排序）
            keys = {view1.agent_key, view2.agent_key}
            self.assertEqual(activated[0], view1.agent_key)
            self.assertEqual(activated[1], view2.agent_key)
            self.assertEqual(keys, {a.key, b.key})
        finally:
            app.quit()

    def test_activation_never_consults_focused_key(self):
        """focused_key 指向 B 时，双击 pet-1 仍激活 pet-1 的 Agent。"""
        app = make_app([_slot("pet-1"), _slot("pet-2")])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2)
            app.monitor.instances = {a.key: a, b.key: b}
            app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
            app._aggregate()
            app.presentation.set_focus(b.key)   # 用户选择 B
            view1 = app.pet_manager.views["pet-1"]
            if view1.agent_key == b.key:
                self.skipTest("pet-1 绑定的就是 focused Agent")
            activated = []
            view1._on_activate = activated.append
            view1._on_body_double()
            self.assertEqual(activated, [view1.agent_key])
        finally:
            app.quit()


class FleetBubbleActivationTests(unittest.TestCase):
    def test_bubble_hit_carries_view_agent_key(self):
        app = make_app([_slot("pet-1"), _slot("pet-2")])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2, cwd="/w/q")
            app.monitor.instances = {a.key: a, b.key: b}
            app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
            app._aggregate()
            app.root.update()
            for view in app.pet_manager.views.values():
                view.redraw()
                # 气泡模型 key == view key（visual identity == click identity）
                self.assertEqual(view.bubble.model.agent_key, view.agent_key)
                self.assertTrue(view.bubble._hit_boxes)
                box = view.bubble._hit_boxes[0][0]
                tag = view._hit((box[0] + box[2]) // 2,
                                (box[1] + box[3]) // 2)
                self.assertEqual(tag, ("activate", view.agent_key))
        finally:
            app.quit()


class WindowIndependenceTests(unittest.TestCase):
    """§20.3：不同 Agent 不同 HWND → 各自前置；绝不串窗口。"""

    def _prepare(self, app, a, b, hwnd_a, hwnd_b):
        service = WindowsTerminalService(None)
        service._window_bindings = {
            a.key: bind_window(hwnd_a), b.key: bind_window(hwnd_b)}
        service._last_instances = [a, b]
        app.monitor._terminal_service = service
        app.monitor.instances = {a.key: a, b.key: b}
        app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
        app._aggregate()
        return service

    def test_pet_click_foregrounds_its_own_hwnd(self):
        app = make_app([_slot("pet-1"), _slot("pet-2")])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2, cwd="/w/q")
            self._prepare(app, a, b, hwnd_a=101, hwnd_b=202)
            rec = ForegroundRecorder()
            with rec.patches()[0], rec.patches()[1], rec.patches()[2], \
                 rec.patches()[3]:
                for view in app.pet_manager.views.values():
                    app.activate_agent(view.agent_key)
            hwnd_by_key = {a.key: 101, b.key: 202}
            # 每个 Agent 的 key 恰好前置自己的 hwnd
            self.assertEqual(len(rec.foreground), 2)
            self.assertNotEqual(rec.foreground[0], rec.foreground[1])
            self.assertEqual(sorted(rec.foreground), [101, 202])
            # 反向验证：再次点击 pet-1 只动 101
            view1 = next(v for v in app.pet_manager.views.values()
                         if hwnd_by_key[v.agent_key] == 101)
            rec2 = ForegroundRecorder()
            with rec2.patches()[0], rec2.patches()[1], rec2.patches()[2], \
                 rec2.patches()[3]:
                app.activate_agent(view1.agent_key)
            self.assertEqual(rec2.foreground, [101])
        finally:
            app.quit()

    def test_same_window_semantics_documented(self):
        """§20.4：两个 Agent 同属一个 WT 窗口 → 都前置同一窗口。

        这是 window-level 语义的正确行为：DeskPet 不承诺自动切换到
        各自的 Tab/Pane。该断言显式记录语义，防止后续开发把"点击
        两个 Pet 都前置同一窗口"当成 bug 重引入 exact tab selection。
        """
        app = make_app([_slot("pet-1"), _slot("pet-2")])
        try:
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2, cwd="/w/q")
            self._prepare(app, a, b, hwnd_a=101, hwnd_b=101)   # 同一窗口
            rec = ForegroundRecorder()
            with rec.patches()[0], rec.patches()[1], rec.patches()[2], \
                 rec.patches()[3]:
                for view in app.pet_manager.views.values():
                    app.activate_agent(view.agent_key)
            self.assertEqual(rec.foreground, [101, 101])
        finally:
            app.quit()


class AggregateBubbleRaceTests(unittest.TestCase):
    """§20.5：Aggregate 气泡使用绘制时固化的 exact key。"""

    def test_click_uses_drawn_key_not_new_attention(self):
        app = make_app([_slot("pet-1")])
        try:
            # aggregate 模式（v4.3：运行期切换）
            from pet.presentation import PresentationMode
            app.presentation.set_concurrent_enabled(True)
            app.presentation.set_concurrent_mode(PresentationMode.AGGREGATE)
            a = inst(AgentKind.CODEX, 1)
            b = inst(AgentKind.CLAUDE, 2, cwd="/w/q")
            app.monitor.instances = {a.key: a, b.key: b}
            app.monitor.snapshots = {a.key: snap(a), b.key: snap(b)}
            app._aggregate()      # 绘制：attention（无 focused）= 先启动的 a
            app.ui._flush_render()   # v4.3：重画经 render flush（§4.4 D）
            view = app.pet_manager.views["pet-1"]
            drawn_key = view.bubble.model.agent_key
            other = b if drawn_key == a.key else a
            self.assertTrue(view.bubble._hit_boxes)
            box = view.bubble._hit_boxes[0][0]
            # 绘制之后、点击之前 attention 变化（另一个 Agent 变 WAITING）；
            # 不触发重绘——点击必须使用绘制时固化的 key，不能现场重算。
            app.monitor.snapshots[other.key] = snap(other, Status.WAITING)
            activated = []
            view._on_activate = activated.append
            tag = view._hit((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
            view._on_hit_tag(tag)
            self.assertEqual(tag, ("activate", drawn_key))
            self.assertEqual(activated, [drawn_key])
        finally:
            app.quit()


class _Ev:
    """极简点击事件（x/y 像素坐标）。"""
    def __init__(self, x, y):
        self.x = x
        self.y = y


class InteractionModeMatrixTests(unittest.TestCase):
    """§29 完整矩阵：

    | 模式       | 气泡单击 | 气泡双击       | body 双击       |
    |-----------|---------|---------------|----------------|
    | SINGLE    | 无动作   | exact 激活     | exact 激活     |
    | AGGREGATE | 无动作   | 绘制 key 激活 | 只互动          |
    | FLEET     | 无动作   | own exact 激活 | own exact 激活 |
    """

    def _make(self, enabled, mode, slots=None, n_agents=1):
        from pet.presentation import PresentationMode
        app = make_app(slots or [_slot("pet-1")])
        # v4.3：enabled/mode 是运行期 session state
        app.presentation.set_concurrent_enabled(bool(enabled))
        if enabled:
            app.presentation.set_concurrent_mode(
                PresentationMode.FLEET if mode == "fleet"
                else PresentationMode.AGGREGATE)
        agents = [inst(AgentKind.CODEX, i + 1, cwd=f"/w/p{i}")
                  for i in range(n_agents)]
        app.monitor.instances = {a.key: a for a in agents}
        app.monitor.snapshots = {a.key: snap(a) for a in agents}
        app._aggregate()
        return app

    def _bubble_center(self, view):
        view.redraw()
        self.assertTrue(view.bubble._hit_boxes,
                        "气泡已绘制且携带激活区")
        x0, y0, x1, y1 = view.bubble._hit_boxes[0][0]
        return (x0 + x1) // 2, (y0 + y1) // 2

    def test_single_bubble_single_press_no_activation_no_drag(self):
        """§29：气泡单击不激活（等待双击），也不启动拖动。"""
        app = self._make(enabled=False, mode="aggregate", n_agents=1)
        try:
            view = app.pet_manager.views["pet-1"]
            activated = []
            view._on_activate = activated.append
            cx, cy = self._bubble_center(view)
            ret = view.window._on_press(_Ev(cx, cy))
            self.assertEqual(ret, "break")
            self.assertEqual(activated, [])
            self.assertIsNone(view.window._drag_off)
        finally:
            app.quit()

    def test_single_bubble_double_activates_exact_agent(self):
        app = self._make(enabled=False, mode="aggregate", n_agents=1)
        try:
            view = app.pet_manager.views["pet-1"]
            self.assertTrue(view.agent_key)
            activated = []
            view._on_activate = activated.append
            cx, cy = self._bubble_center(view)
            ret = view.window._on_double(_Ev(cx, cy))
            self.assertEqual(ret, "break")
            self.assertEqual(activated, [view.agent_key])
        finally:
            app.quit()

    def test_single_body_double_activates_exact_agent(self):
        app = self._make(enabled=False, mode="aggregate", n_agents=1)
        try:
            view = app.pet_manager.views["pet-1"]
            self.assertTrue(view.body_activates)   # SINGLE：body 双击激活
            activated = []
            view._on_activate = activated.append
            view._on_body_double()
            self.assertEqual(activated, [view.agent_key])
        finally:
            app.quit()

    def test_aggregate_body_double_interacts_only(self):
        """§29：AGGREGATE 下 body 双击只互动，绝不激活。"""
        app = self._make(enabled=True, mode="aggregate", n_agents=2)
        try:
            view = app.pet_manager.views["pet-1"]
            self.assertFalse(view.body_activates)
            activated = []
            interacted = []
            view._on_activate = activated.append
            view._on_interact_cb = lambda: interacted.append(1)
            view._on_body_double()
            self.assertEqual(interacted, [1])
            self.assertEqual(activated, [])
        finally:
            app.quit()

    def test_aggregate_bubble_double_uses_drawn_key(self):
        app = self._make(enabled=True, mode="aggregate", n_agents=2)
        try:
            view = app.pet_manager.views["pet-1"]
            activated = []
            view._on_activate = activated.append
            cx, cy = self._bubble_center(view)
            view.window._on_double(_Ev(cx, cy))
            self.assertEqual(activated, [view.bubble.model.agent_key])
        finally:
            app.quit()

    def test_one_double_click_single_activation(self):
        """§29：一次双击只产生一次 activation callback。"""
        app = self._make(enabled=False, mode="aggregate", n_agents=1)
        try:
            view = app.pet_manager.views["pet-1"]
            activated = []
            view._on_activate = activated.append
            cx, cy = self._bubble_center(view)
            # 双击 = press/press/double 序列；press 不激活，double 激活一次
            view.window._on_press(_Ev(cx, cy))
            view.window._on_press(_Ev(cx, cy))
            view.window._on_double(_Ev(cx, cy))
            self.assertEqual(activated, [view.agent_key])
        finally:
            app.quit()

    def test_fleet_bubble_double_own_exact_agent(self):
        app = self._make(enabled=True, mode="fleet",
                         slots=[_slot("pet-1"), _slot("pet-2")], n_agents=2)
        try:
            for view in app.pet_manager.views.values():
                self.assertTrue(view.agent_key)
                activated = []
                view._on_activate = activated.append
                cx, cy = self._bubble_center(view)
                view.window._on_double(_Ev(cx, cy))
                self.assertEqual(activated, [view.agent_key])
        finally:
            app.quit()


class AggregateStackTests(unittest.TestCase):
    """v4.1.4 聚合叠层：同一桌宠把每张 Agent 卡片叠成一摞。

    * 主卡（focused/attention 优先）最下、带指向桌宠的尾巴；
     上方卡片无尾巴、小间隔、不粘连；
    * 每张卡携带自己的 agent_key——双击对应气泡唤起对应终端窗口；
    * 单卡/SINGLE/FLEET 无叠层；终端唤起反馈只改对应 Agent 自己那张
      卡（其他卡不变、叠层不收起）；应用级提示只占主卡、叠层保持显示；
    * Agent 退出后叠层收缩，不留残留渲染器。
    """

    def _app(self, n=3, mode="aggregate"):
        from pet.presentation import PresentationMode
        app = make_app([_slot("pet-1")])
        # v4.3：运行期 session state 切换
        app.presentation.set_concurrent_enabled(True)
        app.presentation.set_concurrent_mode(
            PresentationMode.FLEET if mode == "fleet"
            else PresentationMode.AGGREGATE)
        agents = [inst(AgentKind.CODEX, i + 1, cwd=f"/w/p{i}")
                  for i in range(n)]
        app.monitor.instances = {a.key: a for a in agents}
        app.monitor.snapshots = {a.key: snap(a) for a in agents}
        app._aggregate()
        return app, agents

    @staticmethod
    def _band(renderer):
        """该渲染器当前命中盒的 y 区间。"""
        x0, y0, x1, y1 = renderer._hit_boxes[0][0]
        return y0, y1

    def test_stack_shows_all_cards_attention_first_at_bottom(self):
        app, agents = self._app(3)
        try:
            view = app.pet_manager.views["pet-1"]
            # 主卡 = attention 序第一（启动序），其余按序叠上
            self.assertEqual(view.bubble.model.agent_key, agents[0].key)
            self.assertEqual([b.model.agent_key for b in view.stack_bubbles],
                             [agents[1].key, agents[2].key])
            for model in ([view.bubble.model]
                          + [b.model for b in view.stack_bubbles]):
                self.assertTrue(model.visible)
            self.assertFalse(view.body_activates)   # aggregate body 只互动
        finally:
            app.quit()

    def test_double_click_each_bubble_activates_its_agent(self):
        app, agents = self._app(3)
        try:
            view = app.pet_manager.views["pet-1"]
            view.redraw()
            activated = []
            view._on_activate = activated.append
            renderers = [view.bubble] + view.stack_bubbles
            expected = [agents[0].key, agents[1].key, agents[2].key]
            for renderer, key in zip(renderers, expected):
                box = renderer._hit_boxes[0][0]
                cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
                tag = view._hit(cx, cy)
                self.assertEqual(tag, ("activate", key))
                view.window._on_double(_Ev(cx, cy))
            self.assertEqual(activated, expected)   # 各自 exact 唤起
        finally:
            app.quit()

    def test_only_bottom_bubble_has_tail(self):
        """主卡带指向桌宠的倒三角（3 点 polygon）；叠卡无尾巴。"""
        app, agents = self._app(3)
        try:
            view = app.pet_manager.views["pet-1"]
            view.redraw()
            canvas = view.window.canvas

            def has_triangle(renderer) -> bool:
                for item in renderer._items:
                    try:
                        coords = canvas.coords(item)
                    except Exception:
                        continue
                    if len(coords) == 6:   # 3 顶点 polygon = 尾巴
                        return True
                return False

            self.assertTrue(has_triangle(view.bubble))
            for renderer in view.stack_bubbles:
                self.assertFalse(has_triangle(renderer))
                # 每张叠卡都是完整卡片：圆角矩形 + 标题/正文/脚注文字
                texts = [i for i in renderer._items
                         if canvas.type(i) == "text"]
                rects = [i for i in renderer._items
                         if canvas.type(i) == "polygon"
                         and len(canvas.coords(i)) > 6]
                self.assertGreaterEqual(len(texts), 3)
                self.assertEqual(len(rects), 1)
        finally:
            app.quit()

    def test_stack_cards_separated_by_small_gap(self):
        """卡片间有小间隔（不粘连、也不留太宽）。"""
        app, agents = self._app(3)
        try:
            view = app.pet_manager.views["pet-1"]
            view.redraw()
            # 自上而下：stack[1] → stack[0] → 主卡（离桌宠最近）。
            # 相邻间隔 = 下卡顶边 − 上卡底边。
            _, top_y1 = self._band(view.stack_bubbles[1])
            mid_y0, mid_y1 = self._band(view.stack_bubbles[0])
            primary_y0, _ = self._band(view.bubble)
            gap_upper = mid_y0 - top_y1
            gap_lower = primary_y0 - mid_y1
            for gap in (gap_upper, gap_lower):
                self.assertGreater(gap, 0)          # 不粘连
                self.assertLessEqual(gap, 30)       # 一点点间隔
            self.assertEqual(gap_upper, gap_lower)  # 间隔一致
            # 上面的卡永远在下面的卡上方
            self.assertLess(top_y1, mid_y0)
            self.assertLess(mid_y1, primary_y0)
        finally:
            app.quit()

    def test_agent_feedback_only_changes_that_card(self):
        """v4.1.4：终端唤起反馈只改对应 Agent 自己那张卡——其他卡内容
        不变、叠层不收起、被反馈卡身份/可双击性不变。"""
        app, agents = self._app(3)
        try:
            view = app.pet_manager.views["pet-1"]
            before = {
                agents[0].key: view.bubble.model.text,
                agents[1].key: view.stack_bubbles[0].model.text,
                agents[2].key: view.stack_bubbles[1].model.text,
            }
            app.agent_toast(agents[1].key, "已打开该 Agent 的终端窗口", 5)
            app._apply_toasts()
            app.ui._flush_render()   # v4.3：卡片绘制经 render flush
            # 三张卡都在，没有任何收起
            self.assertTrue(view.bubble.model.visible)
            self.assertEqual(len(view.stack_bubbles), 2)
            self.assertTrue(all(b.model.visible for b in view.stack_bubbles))
            # 主卡与最上卡内容不变
            self.assertEqual(view.bubble.model.text, before[agents[0].key])
            self.assertEqual(view.stack_bubbles[1].model.text,
                             before[agents[2].key])
            # 只有被反馈的卡正文换成反馈文案，agent_key 原样（仍可双击）
            card = view.stack_bubbles[0]
            self.assertEqual(card.model.text, "已打开该 Agent 的终端窗口")
            self.assertEqual(card.model.agent_key, agents[1].key)
            box = card._hit_boxes[0][0]
            cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
            self.assertEqual(view._hit(cx, cy), ("activate", agents[1].key))
            # 反馈过期后下一轮 aggregate 恢复正常内容
            app._agent_toasts.clear()
            app._aggregate()
            self.assertEqual(card.model.text, before[agents[1].key])
        finally:
            app.quit()

    def test_activate_agent_feedback_scoped_to_its_card(self):
        """activate_agent 的结果反馈走 Agent 级通道（端到端）。"""
        app, agents = self._app(3)
        try:
            view = app.pet_manager.views["pet-1"]
            primary_text = view.bubble.model.text
            app.monitor.activate_target = lambda key: ActivationResult(
                ActivationCode.OK)
            app.activate_agent(agents[1].key)
            self.assertIn(agents[1].key, app._agent_toasts)
            self.assertNotIn(agents[0].key, app._agent_toasts)
            app._aggregate()
            app._apply_toasts()   # v4.3：toast 套用在 render flush C 步
            card = view.stack_bubbles[0]
            self.assertEqual(card.model.text, "已打开该 Agent 的终端窗口")
            self.assertEqual(view.bubble.model.text, primary_text)
            self.assertEqual(len(view.stack_bubbles), 2)
            self.assertTrue(all(b.model.visible for b in view.stack_bubbles))
        finally:
            app.quit()

    def test_global_toast_keeps_stack_visible(self):
        """应用级提示只占主卡；叠层卡片保持显示、内容不被改写。"""
        app, agents = self._app(3)
        try:
            view = app.pet_manager.views["pet-1"]
            app.toast("测试提示", 5)
            app._apply_toasts()
            for renderer in view.stack_bubbles:
                self.assertTrue(renderer.model.visible)
                self.assertNotEqual(renderer.model.text, "测试提示")
            self.assertEqual(view.bubble.model.text, "测试提示")
            # 提示过期后下一轮 aggregate 恢复主卡内容
            app._toast = None
            app._aggregate()
            self.assertNotEqual(view.bubble.model.text, "测试提示")
        finally:
            app.quit()

    def test_feedback_without_card_falls_back_to_primary(self):
        """Agent 级反馈找不到卡片（Agent 刚退出）：升级为主卡提示，
        双击绝无静默失败；叠层同样不收起。"""
        app, agents = self._app(3)
        try:
            view = app.pet_manager.views["pet-1"]
            app.agent_toast("gone-key", "该 Agent 已退出", 5)
            app._apply_toasts()
            self.assertEqual(view.bubble.model.text, "该 Agent 已退出")
            self.assertEqual(len(view.stack_bubbles), 2)
            self.assertTrue(all(b.model.visible for b in view.stack_bubbles))
        finally:
            app.quit()

    def test_agent_exit_shrinks_stack(self):
        app, agents = self._app(3)
        try:
            view = app.pet_manager.views["pet-1"]
            self.assertEqual(len(view.stack_bubbles), 2)
            app.monitor.instances.pop(agents[2].key)
            app.monitor.snapshots.pop(agents[2].key)
            app._aggregate()
            self.assertEqual(len(view.stack_bubbles), 1)
            self.assertEqual(view.stack_bubbles[0].model.agent_key,
                             agents[1].key)
        finally:
            app.quit()

    def test_single_card_and_fleet_no_stack(self):
        app, agents = self._app(1)
        try:
            self.assertEqual(app.pet_manager.views["pet-1"].stack_bubbles,
                             [])
        finally:
            app.quit()
        app, agents = self._app(2, mode="fleet")
        try:
            app.config.data["presentation"]["concurrent"]["slots"] = [
                _slot("pet-1"), _slot("pet-2")]
            app._aggregate()
            for view in app.pet_manager.views.values():
                self.assertEqual(view.stack_bubbles, [])
        finally:
            app.quit()


if __name__ == "__main__":
    unittest.main()
