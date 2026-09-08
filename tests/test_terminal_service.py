"""exact activation 事务测试（v4plan §18.2）：Window → Tab → Pane。

FakeBackend 提供 Window/Tab/Pane 三层拓扑；winkeys 原语按用例 mock。
核心断言：绝不 fallback 到第一个 Tab/Pane/title/MRU；fail-closed。
"""
from __future__ import annotations
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import winkeys
from agents.models import (
    ActivationCode,
    AgentInstance,
    AgentKind,
    AgentTarget,
    BindingConfidence,
    BindingOrigin,
    Snapshot,
    Status,
    TerminalBinding,
    TerminalLocation,
    WindowIdentity,
)
from agents.terminal_service import WindowsTerminalService
from agents.terminal_uia import WT_WINDOW_CLASS, PaneInfo, TerminalObserver

from test_terminal_uia import FakeBackend, NOW

ALWAYS_LIVE = lambda key: True
DEAD = lambda key: False


def make_identity(hwnd=11, pid=100, created=1.0):
    return WindowIdentity(hwnd=hwnd, pid=pid, process_created=created,
                          window_class=WT_WINDOW_CLASS)


def make_target(key="wsl:Ubuntu|codex|1|t1"):
    inst = AgentInstance(AgentKind.CODEX, 1, "wsl:Ubuntu", process_token="t1")
    snap = Snapshot(inst.key, inst.kind, inst.source, inst.pid,
                    status=Status.WORKING)
    return AgentTarget(key=inst.key, instance=inst, snapshot=snap)


def make_binding(ident, tab_id=None, pane_id=None,
                 confidence=BindingConfidence.CONFIRMED,
                 origin=BindingOrigin.MANUAL):
    return TerminalBinding(
        hwnd=ident.hwnd, window_pid=ident.pid,
        window_created=ident.process_created,
        window_class=ident.window_class,
        tab_id=tab_id, pane_id=pane_id,
        origin=origin, confidence=confidence, observable=True)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.observer = TerminalObserver(self.backend,
                                         cfg={"terminal_observer": True})
        self.observer._started = True
        self.observer._last_discover = time.time()
        self.service = WindowsTerminalService(self.observer)
        self.backend.available = True
        self.target = make_target()

    def _bind(self, binding):
        self.service._bindings = {self.target.key: binding}

    def _full_layout(self):
        """W11: T_a(selected, pane P_a) + T_b；W22: T_c(selected, pane P_c)。"""
        ident11 = self.backend.add_window(11, pid=100, created=1.0)
        ident22 = self.backend.add_window(22, pid=200, created=2.0)
        ta = self.backend.add_tab(11, (1,), "codex", selected=True, index=0)
        tb = self.backend.add_tab(11, (2,), "codex", selected=False, index=1)
        tc = self.backend.add_tab(22, (3,), "claude", selected=True, index=0)
        pa = self.backend.add_pane(11, (10,), "codex pane", tab_id=ta)
        pc = self.backend.add_pane(22, (30,), "claude pane", tab_id=tc)
        self.observer.refresh_panes(force=True)
        return ident11, ident22, ta, tb, tc, pa, pc

    # ------------------------------------------------ 1/2：窗口/Tab 拓扑
    def test_two_windows_multiple_tabs_tab_id_selects_exact(self):
        ident11, _i22, ta, tb, tc, pa, pc = self._full_layout()
        # Tab B 的 pane：未被观察过（B 未选中过），但注册在其名下
        pb = self.backend.add_pane(11, (11,), "codex pane B", tab_id=tb)
        self._bind(make_binding(ident11, tab_id=tb, pane_id=pb))
        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(winkeys, "restore_window", return_value=True), \
             patch.object(winkeys, "try_set_foreground", return_value=True):
            result = self.service.activate(self.target, ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.OK)
        # 选中的是 exact Tab B（不是当前 selected 的 Tab A）
        self.assertEqual(self.backend.select_calls, [tb])
        self.assertEqual(self.backend.selected_tabs[11], tb)
        # focus 到 Tab B 的 pane，而不是 Tab A 的
        self.assertEqual(self.backend.focus_calls, [pb])

    def test_same_title_tabs_distinguished_by_runtime_id(self):
        ident11 = self.backend.add_window(11, pid=100)
        t1 = self.backend.add_tab(11, (1,), "codex", selected=True, index=0)
        t2 = self.backend.add_tab(11, (2,), "codex", selected=False, index=1)
        p1 = self.backend.add_pane(11, (10,), "codex", tab_id=t1)
        p2 = self.backend.add_pane(11, (11,), "codex", tab_id=t2)
        self.observer.refresh_panes(force=True)
        self._bind(make_binding(ident11, tab_id=t2, pane_id=p2))
        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(winkeys, "restore_window", return_value=True), \
             patch.object(winkeys, "try_set_foreground", return_value=True):
            result = self.service.activate(self.target, ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.OK)
        self.assertEqual(self.backend.select_calls, [t2])   # 不是 t1
        self.assertEqual(self.backend.focus_calls, [p2])

    def test_tab_reorder_keeps_exact_binding(self):
        """用户拖动 Tab：index 变化、RuntimeId 不变 → 仍选 exact Tab。"""
        ident11 = self.backend.add_window(11, pid=100)
        t1 = self.backend.add_tab(11, (1,), "one", selected=True, index=0)
        t2 = self.backend.add_tab(11, (2,), "two", selected=False, index=1)
        p2 = self.backend.add_pane(11, (20,), "two pane", tab_id=t2)
        self.observer.refresh_panes(force=True)
        # 拖动：t2 的 index_hint 变化（RuntimeId 不变）
        from dataclasses import replace
        self.backend.tabs[t2] = replace(self.backend.tabs[t2], index_hint=0)
        self._bind(make_binding(ident11, tab_id=t2, pane_id=p2))
        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(winkeys, "restore_window", return_value=True), \
             patch.object(winkeys, "try_set_foreground", return_value=True):
            result = self.service.activate(self.target, ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.OK)
        self.assertEqual(self.backend.select_calls, [t2])

    # ------------------------------------------------ 4/5：Tab 失效
    def test_target_tab_closed_returns_stale_tab(self):
        ident11, _i22, ta, tb, tc, pa, pc = self._full_layout()
        self._bind(make_binding(ident11, tab_id=tb, pane_id=None))
        self.backend.tabs.pop(tb)   # Tab B 被关闭
        self.observer.refresh_panes(force=True)
        with patch.object(winkeys, "validate_window", return_value=True):
            result = self.service.activate(self.target, ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.STALE_TAB)
        self.assertEqual(self.backend.select_calls, [])   # 绝不猜第一个 Tab

    def test_tab_tearout_invalidates_old_window_binding(self):
        """Tab 拖出成新窗口：旧 tab_id 在原 HWND 下消失 → STALE_TAB。"""
        ident11, _i22, ta, tb, tc, pa, pc = self._full_layout()
        self._bind(make_binding(ident11, tab_id=tb, pane_id=None))
        # tear-out：tb 从 W11 移除，出现在新 W33
        self.backend.tabs.pop(tb)
        ident33 = self.backend.add_window(33, pid=300, created=3.0)
        self.backend.tabs[(33, tb[1])] = type(
            "T", (), {"tab_id": (33, tb[1]), "hwnd": 33,
                      "window_pid": 300, "title": "codex",
                      "index_hint": 0, "selected": True,
                      "last_seen": NOW})()
        self.observer.refresh_panes(force=True)
        with patch.object(winkeys, "validate_window", return_value=True):
            result = self.service.activate(self.target, ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.STALE_TAB)
        self.assertEqual(self.backend.select_calls, [])

    # ------------------------------------------------ 6/7/8：pane revalidation
    def test_stable_pane_runtime_id_exact_focus(self):
        ident11 = self.backend.add_window(11, pid=100)
        t1 = self.backend.add_tab(11, (1,), "one", selected=True)
        p1 = self.backend.add_pane(11, (10,), "x", tab_id=t1)
        self.observer.refresh_panes(force=True)
        self._bind(make_binding(ident11, tab_id=t1, pane_id=p1))
        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(winkeys, "restore_window", return_value=True), \
             patch.object(winkeys, "try_set_foreground", return_value=True):
            result = self.service.activate(self.target, ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.OK)
        self.assertEqual(self.backend.focus_calls, [p1])

    def test_sole_pane_rebind_when_runtime_id_changed(self):
        """inactive→active detach/reattach 后 RuntimeId 可能变化：
        唯一 pane → safe rebind（v4plan §5.6）。"""
        ident11 = self.backend.add_window(11, pid=100)
        t1 = self.backend.add_tab(11, (1,), "one", selected=True)
        p_old = self.backend.add_pane(11, (10,), "x", tab_id=t1)
        self.observer.refresh_panes(force=True)
        # RuntimeId 换代：旧 pane 消失，同 tab 出现新 pane
        self.backend.panes.pop(p_old)
        p_new = self.backend.add_pane(11, (99,), "x", tab_id=t1)
        self.observer.refresh_panes(force=True)
        self._bind(make_binding(ident11, tab_id=t1, pane_id=p_old))
        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(winkeys, "restore_window", return_value=True), \
             patch.object(winkeys, "try_set_foreground", return_value=True):
            result = self.service.activate(self.target, ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.OK)
        self.assertEqual(self.backend.focus_calls, [p_new])
        # 绑定已被 rebind（运行期）
        self.assertEqual(self.service._bindings[self.target.key].pane_id, p_new)

    def test_multi_pane_runtime_change_is_stale_pane(self):
        """RuntimeId 变化 + 多个 pane → STALE_PANE，绝不选第一个。"""
        ident11 = self.backend.add_window(11, pid=100)
        t1 = self.backend.add_tab(11, (1,), "one", selected=True)
        p_old = self.backend.add_pane(11, (10,), "x", tab_id=t1)
        self.observer.refresh_panes(force=True)
        self.backend.panes.pop(p_old)
        p_a = self.backend.add_pane(11, (91,), "left", tab_id=t1)
        p_b = self.backend.add_pane(11, (92,), "right", tab_id=t1)
        self.observer.refresh_panes(force=True)
        self._bind(make_binding(ident11, tab_id=t1, pane_id=p_old))
        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(winkeys, "restore_window", return_value=True), \
             patch.object(winkeys, "try_set_foreground", return_value=True):
            result = self.service.activate(self.target, ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.STALE_PANE)
        self.assertEqual(self.backend.focus_calls, [])   # 不猜

    # ------------------------------------------------ 9/10：窗口复用
    def test_hwnd_pid_mismatch_is_stale_window(self):
        ident = self.backend.add_window(11, pid=100, created=1.0)
        self.observer.refresh_panes(force=True)
        self._bind(make_binding(ident))
        # HWND 复用到别的进程：validate 失败
        with patch.object(winkeys, "validate_window", return_value=False):
            result = self.service.activate(self.target, ALWAYS_LIVE)
        # refresh 后 binding 仍在（mock validate 恒 False）→ STALE_WINDOW
        self.assertEqual(result.code, ActivationCode.STALE_WINDOW)

    def test_hwnd_pid_reuse_with_new_create_time_is_stale_window(self):
        """HWND+PID 都被复用但 create_time 不同：validate_window 拒绝。"""
        ident11 = self.backend.add_window(11, pid=100, created=1.0)
        t1 = self.backend.add_tab(11, (1,), "one", selected=True)
        p1 = self.backend.add_pane(11, (10,), "x", tab_id=t1)
        self.observer.refresh_panes(force=True)
        self._bind(make_binding(ident11, tab_id=t1, pane_id=p1))
        # 真实 validate_window 语义：pid 一致但 create_time 不同 → False
        fake = type("U", (), {"IsWindow": staticmethod(lambda h: True)})()
        ident_new = make_identity(11, pid=100, created=99.0)
        with patch.object(winkeys, "validate_window",
                          return_value=False):
            result = self.service.activate(self.target, ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.STALE_WINDOW)
        self.assertEqual(self.backend.select_calls, [])

    # ------------------------------------------------ 11：Select pattern 不可用
    def test_selection_pattern_unavailable_is_uia_unavailable(self):
        ident11 = self.backend.add_window(11, pid=100)
        t1 = self.backend.add_tab(11, (1,), "one", selected=True)
        p1 = self.backend.add_pane(11, (10,), "x", tab_id=t1)
        self.observer.refresh_panes(force=True)
        self._bind(make_binding(ident11, tab_id=t1, pane_id=p1))
        self.backend.select_fails = True
        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(winkeys, "restore_window", return_value=True), \
             patch.object(winkeys, "try_set_foreground", return_value=True):
            result = self.service.activate(self.target, ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.UIA_UNAVAILABLE)
        self.assertEqual(self.backend.focus_calls, [])   # 无 keyboard fallback

    # ------------------------------------------------ 12：foreground 被拒
    def test_foreground_denied_selects_tab_and_flashes(self):
        ident11 = self.backend.add_window(11, pid=100)
        t1 = self.backend.add_tab(11, (1,), "one", selected=False)
        t2 = self.backend.add_tab(11, (2,), "two", selected=True)
        p1 = self.backend.add_pane(11, (10,), "x", tab_id=t1)
        p2 = self.backend.add_pane(11, (20,), "x", tab_id=t2)
        self.observer.refresh_panes(force=True)
        self._bind(make_binding(ident11, tab_id=t1, pane_id=p1))
        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(winkeys, "restore_window", return_value=True), \
             patch.object(winkeys, "try_set_foreground", return_value=False), \
             patch.object(winkeys, "flash_window", return_value=True) as flash:
            result = self.service.activate(self.target, ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.FOREGROUND_DENIED)
        self.assertEqual(self.backend.select_calls, [t1])   # Tab 仍选对
        self.assertTrue(flash.called)
        self.assertEqual(self.backend.focus_calls, [])   # 前台未成→不 SetFocus

    # ------------------------------------------------ 13：手工绑定
    def test_bind_focused_location_confirms(self):
        ident = make_identity(11, pid=100, created=1.0)
        tab_id = (11, (5,))
        pane_id = (11, (50,))
        location = TerminalLocation(window=ident, tab_id=tab_id,
                                    pane_id=pane_id)
        self.backend.focused_location = lambda: location
        got = self.service.bind_focused_location(self.target.key)
        self.assertIsNotNone(got)
        self.assertEqual(got.tab_id, tab_id)
        self.assertEqual(self.service.resolver.manual_location(self.target.key),
                         location)

    def test_bind_focused_location_none_fails_closed(self):
        self.backend.focused_location = lambda: None
        self.assertIsNone(self.service.bind_focused_location(self.target.key))

    # ------------------------------------------------ 14：退出竞态
    def test_agent_exit_during_activation_returns_agent_gone(self):
        ident11 = self.backend.add_window(11, pid=100)
        t1 = self.backend.add_tab(11, (1,), "one", selected=True)
        p1 = self.backend.add_pane(11, (10,), "x", tab_id=t1)
        self.observer.refresh_panes(force=True)
        self._bind(make_binding(ident11, tab_id=t1, pane_id=p1))
        result = self.service.activate(self.target, DEAD)
        self.assertEqual(result.code, ActivationCode.AGENT_GONE)
        self.assertEqual(self.backend.select_calls, [])

    def test_no_binding_returns_no_binding(self):
        self._bind(None)
        self.service._bindings = {}
        result = self.service.activate(self.target, ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.NO_BINDING)

    def test_ambiguous_binding_not_activated(self):
        ident11 = self.backend.add_window(11, pid=100)
        self.observer.refresh_panes(force=True)
        self._bind(make_binding(ident11, confidence=BindingConfidence.AMBIGUOUS))
        result = self.service.activate(self.target, ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.AMBIGUOUS)
        self.assertEqual(self.backend.select_calls, [])

    def test_drop_instance_clears_manual_and_cached_binding(self):
        ident = make_identity(11, pid=100, created=1.0)
        self.service.resolver.set_manual_location(
            self.target.key,
            TerminalLocation(window=ident, tab_id=(11, (5,)),
                             pane_id=(11, (50,))))
        self.service._bindings = {self.target.key: make_binding(ident)}
        self.service.drop_instance(self.target.key)
        self.assertIsNone(self.service.resolver.manual_location(self.target.key))
        self.assertNotIn(self.target.key, self.service._bindings)


if __name__ == "__main__":
    unittest.main()
