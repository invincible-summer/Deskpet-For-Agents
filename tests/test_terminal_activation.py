"""window-only 激活事务测试（v4.1.1 plan §19.2）。

核心断言：
  * 激活不依赖 UIA（observer=None 也要 OK——重要回归）；
  * restore + try_set_foreground 使用 exact hwnd；
  * stale WindowIdentity 只允许一次 refresh/re-resolve；仍失败 STALE_WINDOW；
  * foreground 被拒 → flash exact hwnd + FOREGROUND_DENIED，不绕过；
  * fail-closed：无绑定/Agent 已退出时不做任何窗口动作；
  * 产品代码不含 select_tab / focus_pane / SendInput（源检查）。
"""
from __future__ import annotations
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import winkeys
from agents.models import (
    ActivationCode,
    WindowBindingConfidence,
    WindowIdentity,
    TerminalWindowBinding,
)
from agents.terminal_service import WindowsTerminalService
from agents.terminal_uia import WT_WINDOW_CLASS

ALWAYS_LIVE = lambda key: True   # noqa: E731
DEAD = lambda key: False         # noqa: E731
KEY = "wsl:Ubuntu|codex|1|t1"


def make_identity(hwnd=11, pid=100, created=1.0):
    return WindowIdentity(hwnd=hwnd, pid=pid, process_created=created,
                          window_class=WT_WINDOW_CLASS)


def make_binding(ident, confidence=WindowBindingConfidence.CONFIRMED):
    return TerminalWindowBinding(window=ident, confidence=confidence,
                                 title="codex", last_seen=1.0,
                                 validated_at=1.0, reason="test")


class WindowActivationTests(unittest.TestCase):
    def _service(self, observer=None):
        return WindowsTerminalService(observer)

    def _bind(self, service, binding):
        service._window_bindings = {KEY: binding}

    # ------------------------------------------------ A. valid window
    def test_valid_window_restores_and_foregrounds_exact_hwnd(self):
        service = self._service()
        ident = make_identity(hwnd=11, pid=100)
        self._bind(service, make_binding(ident))
        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(winkeys, "restore_window",
                          return_value=True) as restore, \
             patch.object(winkeys, "try_set_foreground",
                          return_value=True) as foreground:
            result = service.activate_cached(KEY, is_agent_live=ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.OK)
        self.assertFalse(result.repaired)
        restore.assert_called_once_with(11)
        foreground.assert_called_once_with(11)

    # ------------------------------------------------ B. UIA unavailable
    def test_uia_unavailable_still_activates_valid_binding(self):
        """observer=None（UIA 不可用）：激活仍 OK——window 激活不依赖 UIA。"""
        service = self._service(observer=None)
        self._bind(service, make_binding(make_identity(hwnd=22, pid=200)))
        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(winkeys, "restore_window", return_value=True), \
             patch.object(winkeys, "try_set_foreground", return_value=True):
            result = service.activate_cached(KEY, is_agent_live=ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.OK)

    # ------------------------------------------------ C. stale → async repair → final OK
    def test_stale_hwnd_returns_immediately_and_repair_on_monitor(self):
        """v4.3.1 DP43-R08 §16.4：cached activation 遇 stale 立即返回
        STALE_WINDOW + needs_repair；UIA refresh / re-resolve 只发生在
        repair_binding（Monitor 线程），Tk 路径零 UIA。"""
        service = self._service()
        self._bind(service, make_binding(make_identity(hwnd=11, pid=100)))
        with patch.object(winkeys, "validate_window",
                          return_value=False), \
             patch.object(service, "refresh_observed_controls",
                          return_value=True) as refresh, \
             patch.object(service, "resolve") as resolve, \
             patch.object(winkeys, "restore_window", return_value=True) as r, \
             patch.object(winkeys, "try_set_foreground",
                          return_value=True) as fg:
            result = service.activate_cached(KEY, is_agent_live=ALWAYS_LIVE)
            self.assertEqual(result.code, ActivationCode.STALE_WINDOW)
            self.assertTrue(result.needs_repair)
            refresh.assert_not_called()   # Tk 路径无 UIA refresh
            resolve.assert_not_called()   # 无 resolver 重建
            r.assert_not_called()
            fg.assert_not_called()

    def test_repair_binding_refreshes_once_and_rebuilds_binding(self):
        """repair_binding（Monitor 线程）：invalidate + force refresh +
        re-resolve；新 binding 存在 → True；最终激活前 UI 仍重校验。"""
        service = self._service()
        self._bind(service, make_binding(make_identity(hwnd=11, pid=100)))
        new_binding = make_binding(make_identity(hwnd=33, pid=300, created=9.0))

        def fake_resolve(instances, now):
            service._window_bindings[KEY] = new_binding
            return {KEY: new_binding}, {}

        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(service, "refresh_observed_controls",
                          return_value=True) as refresh, \
             patch.object(service.window_resolver,
                          "invalidate_window_cache") as invalidate, \
             patch.object(service, "resolve", side_effect=fake_resolve):
            repaired = service.repair_binding(
                KEY, instances=[], now=1.0)
        self.assertTrue(repaired)
        refresh.assert_called_once_with(force=True)
        invalidate.assert_called_once()
        # repair 后的最终激活（UI 线程）走 cached 路径成功
        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(winkeys, "restore_window", return_value=True) as r, \
             patch.object(winkeys, "try_set_foreground",
                          return_value=True) as fg:
            result = service.activate_cached(KEY, is_agent_live=ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.OK)
        r.assert_called_once_with(33)
        fg.assert_called_once_with(33)

    def test_repair_binding_false_when_no_new_binding(self):
        service = self._service()
        self._bind(service, make_binding(make_identity(hwnd=11, pid=100)))

        def fake_resolve(instances, now):
            service._window_bindings.pop(KEY, None)
            return {}, {}

        with patch.object(service, "refresh_observed_controls",
                          return_value=True), \
             patch.object(service, "resolve", side_effect=fake_resolve):
            repaired = service.repair_binding(KEY, instances=[], now=1.0)
        self.assertFalse(repaired)

    # ------------------------------------------------ D. stale after repair
    def test_window_changes_again_before_final_is_stale_window(self):
        """repair 后窗口又失效（validate False）：最终激活 STALE_WINDOW，
        一次性语义结束，不再触发新 repair。"""
        service = self._service()
        self._bind(service, make_binding(make_identity(hwnd=11, pid=100)))
        new_binding = make_binding(make_identity(hwnd=33, pid=300, created=9.0))

        def fake_resolve(instances, now):
            service._window_bindings[KEY] = new_binding
            return {KEY: new_binding}, {}

        with patch.object(service, "refresh_observed_controls",
                          return_value=True), \
             patch.object(service, "resolve", side_effect=fake_resolve):
            self.assertTrue(service.repair_binding(KEY, instances=[], now=1.0))
        # repair 完成到最终激活之间窗口再次失效
        with patch.object(winkeys, "validate_window", return_value=False), \
             patch.object(winkeys, "restore_window", return_value=True) as r, \
             patch.object(winkeys, "try_set_foreground",
                          return_value=True) as fg:
            result = service.activate_cached(KEY, is_agent_live=ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.STALE_WINDOW)
        self.assertTrue(result.needs_repair is True)   # 状态如实上报
        r.assert_not_called()
        fg.assert_not_called()

    # ------------------------------------------------ E. foreground denied
    def test_foreground_denied_flashes_exact_hwnd(self):
        service = self._service()
        ident = make_identity(hwnd=55, pid=500)
        self._bind(service, make_binding(ident))
        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(winkeys, "restore_window", return_value=True), \
             patch.object(winkeys, "try_set_foreground",
                          return_value=False), \
             patch.object(winkeys, "flash_window",
                          return_value=True) as flash:
            result = service.activate_cached(KEY, is_agent_live=ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.FOREGROUND_DENIED)
        flash.assert_called_once_with(55)

    # ------------------------------------------------ F. no binding
    def test_no_binding_returns_no_binding_without_window_action(self):
        service = self._service()
        service._window_bindings = {
            KEY: TerminalWindowBinding(
                confidence=WindowBindingConfidence.AMBIGUOUS,
                reason="multiple terminal windows")}
        with patch.object(winkeys, "restore_window", return_value=True) as r, \
             patch.object(winkeys, "try_set_foreground",
                          return_value=True) as fg:
            result = service.activate_cached(KEY, is_agent_live=ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.NO_BINDING)
        self.assertIn("multiple", result.detail)
        r.assert_not_called()
        fg.assert_not_called()

    # ------------------------------------------------ G. agent exited
    def test_agent_gone_makes_no_window_action(self):
        service = self._service()
        self._bind(service, make_binding(make_identity()))
        with patch.object(winkeys, "restore_window", return_value=True) as r, \
             patch.object(winkeys, "try_set_foreground",
                          return_value=True) as fg:
            result = service.activate_cached(KEY, is_agent_live=DEAD)
        self.assertEqual(result.code, ActivationCode.AGENT_GONE)
        r.assert_not_called()
        fg.assert_not_called()

    # ------------------------------------------------ 辅助语义（§26）
    def test_ambiguous_with_window_activates(self):
        """AMBIGUOUS + window（v3 best-positive 候选）允许唤起（§26）。"""
        service = self._service()
        self._bind(service, make_binding(make_identity(hwnd=11, pid=100),
                                         WindowBindingConfidence.AMBIGUOUS))
        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(winkeys, "restore_window", return_value=True), \
             patch.object(winkeys, "try_set_foreground", return_value=True):
            result = service.activate_cached(KEY, is_agent_live=ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.OK)

    def test_none_confidence_with_window_activates(self):
        """NONE + window（唯一窗口兜底）允许唤起（§26）。"""
        service = self._service()
        self._bind(service, make_binding(make_identity(hwnd=11, pid=100),
                                         WindowBindingConfidence.NONE))
        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(winkeys, "restore_window", return_value=True), \
             patch.object(winkeys, "try_set_foreground", return_value=True):
            result = service.activate_cached(KEY, is_agent_live=ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.OK)

    def test_drop_instance_clears_cached_bindings(self):
        service = self._service()
        self._bind(service, make_binding(make_identity()))
        service._observation_bindings = {KEY: object()}
        service.drop_instance(KEY)
        self.assertNotIn(KEY, service._window_bindings)
        self.assertNotIn(KEY, service._observation_bindings)

    def test_cached_activation_never_refreshes(self):
        """v4.3.1 DP43-R08：validate 反复失败，cached 路径也绝不 refresh
        （repair 只属于 Monitor 线程的 repair_binding，Tk 零 UIA）。"""
        service = self._service()
        self._bind(service, make_binding(make_identity()))
        with patch.object(winkeys, "validate_window", return_value=False), \
             patch.object(service, "refresh_observed_controls",
                          return_value=True) as refresh:
            for _ in range(3):
                result = service.activate_cached(
                    KEY, is_agent_live=ALWAYS_LIVE)
                self.assertEqual(result.code, ActivationCode.STALE_WINDOW)
                self.assertTrue(result.needs_repair)
        refresh.assert_not_called()


class AsyncRepairQueueTests(unittest.TestCase):
    """v4.3.1 DP43-R08 §16.5/§16.6：Monitor repair 队列（coalesced、
    bounded、Monitor 线程执行、结果 bounded 收割）。"""

    KEY = "wsl:Ubuntu|codex|1|t1"

    def _monitor(self):
        import queue as queue_mod
        import threading as threading_mod
        from agents.monitor import Monitor
        monitor = Monitor.__new__(Monitor)
        monitor.lock = threading_mod.Lock()
        monitor.instances = {}
        monitor.log_q = queue_mod.Queue(maxsize=200)
        monitor._log_ring = []
        monitor._repair_lock = threading_mod.Lock()
        monitor._repair_requests = {}
        monitor._repair_next_id = 1
        monitor._repair_results = queue_mod.Queue(maxsize=16)

        class _Svc:
            def __init__(self):
                self.repair_calls = []

            def repair_binding(self, agent_key, *, instances, now):
                self.repair_calls.append((agent_key, now))
                return True

        monitor._terminal_service = _Svc()
        return monitor

    def test_repair_requests_coalesce_per_agent(self):
        monitor = self._monitor()
        rid1 = monitor.request_activation_repair(self.KEY)
        rid2 = monitor.request_activation_repair(self.KEY)
        rid3 = monitor.request_activation_repair(self.KEY)
        self.assertEqual(rid1, rid2)
        self.assertEqual(rid2, rid3)
        self.assertEqual(len(monitor._repair_requests), 1)

    def test_repair_executes_once_on_monitor_thread(self):
        monitor = self._monitor()
        monitor.instances = {self.KEY: object()}   # Agent 仍 live
        monitor.request_activation_repair(self.KEY)
        monitor.request_activation_repair(self.KEY)   # coalesce
        monitor._process_repair_requests(now=100.0)
        self.assertEqual(
            monitor._terminal_service.repair_calls, [(self.KEY, 100.0)])
        results = monitor.drain_activation_repairs()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].repaired)
        # 队列已空：一次性语义（同一 user request 只 repair 一次）
        self.assertEqual(monitor.drain_activation_repairs(), [])

    def test_agent_exit_during_repair_reports_not_repaired(self):
        monitor = self._monitor()
        monitor.instances = {}   # Agent 已退出
        monitor.request_activation_repair(self.KEY)
        monitor._process_repair_requests(now=100.0)
        self.assertEqual(monitor._terminal_service.repair_calls, [])
        results = monitor.drain_activation_repairs()
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].repaired)

    def test_expired_request_is_dropped(self):
        monitor = self._monitor()
        monitor.instances = {}
        monitor.request_activation_repair(self.KEY)
        monitor._repair_requests[self.KEY] = type(
            monitor._repair_requests[self.KEY])(
            1, self.KEY, expires_at=50.0)   # 已过期
        monitor._process_repair_requests(now=100.0)
        self.assertEqual(monitor._terminal_service.repair_calls, [])
        self.assertEqual(monitor.drain_activation_repairs(), [])

    def test_repair_result_queue_bounded(self):
        from agents.monitor import ActivationRepairRequest, Monitor
        import queue as queue_mod
        monitor = self._monitor()
        req = ActivationRepairRequest(1, self.KEY, 1e18)
        for i in range(40):   # 远超 16：最旧被挤掉，不抛异常
            monitor._publish_repair_result(req, True)
        self.assertLessEqual(monitor._repair_results.qsize(), 16)
        # bridge 单次收割 <= 4
        self.assertLessEqual(len(monitor.drain_activation_repairs()), 4)



class ForbiddenApiRegressionTests(unittest.TestCase):
    """§19.2-H：产品代码不得包含 tab/pane 激活与输入注入路径。"""

    def _source(self, name):
        root = Path(__file__).resolve().parents[1]
        return (root / name).read_text(encoding="utf-8")

    def test_terminal_service_has_no_tab_pane_or_input_injection(self):
        src = self._source("agents/terminal_service.py")
        for banned in ("select_tab", "focus_pane", "SendInput",
                       "keybd_event", "PostMessage"):
            self.assertNotIn(banned, src,
                             f"terminal_service.py 不应包含 {banned}")

    def test_terminal_uia_has_no_tab_selection(self):
        src = self._source("agents/terminal_uia.py")
        for banned in ("select_tab", "selected_tab", "focus_pane",
                       "SelectionItemPattern"):
            self.assertNotIn(banned, src,
                             f"terminal_uia.py 不应包含 {banned}")

    def test_winkeys_has_no_key_injection(self):
        """v4plan §5.7：winkeys 绝不提供输入注入面。

        v4.3 修订：PostMessage 的字面量禁令收窄为"只允许
        finish_menu_popup 的 WM_NULL 收尾"——那是 Microsoft 文档化的
        托盘菜单配套（发给自家窗口的空消息，不携带任何输入）；
        键盘/鼠标注入 API 与输入消息常量仍然全禁。
        """
        src = self._source("actions/winkeys.py")
        for banned in ("SendInput", "keybd_event", "SendKeys",
                       "mouse_event", "WM_KEYDOWN", "WM_CHAR",
                       "WM_LBUTTONDOWN", "WM_MOUSEMOVE"):
            self.assertNotIn(banned, src,
                             f"winkeys.py 不应包含 {banned}")
        # PostMessageW 只允许出现在 finish_menu_popup 内（WM_NULL）
        import re
        for m in re.finditer(r"PostMessage", src):
            after = src[m.start():m.start() + 400]
            self.assertIn("finish_menu_popup", src[max(0, m.start() - 400):m.start()]
                          or after,
                          "PostMessage 只允许作为 finish_menu_popup 的 WM_NULL 收尾")
            self.assertIn("WM_NULL", after)

    def test_monitor_has_no_manual_binding_api(self):
        src = self._source("agents/monitor.py")
        for banned in ("bind_focused_location", "set_manual_location",
                       "manual_location"):
            self.assertNotIn(banned, src,
                             f"monitor.py 不应包含 {banned}")

    def test_no_confidence_gate_in_activation_path(self):
        """§26：激活链禁止重新引入 Window confidence gate——wakeability
        只由 binding.window 决定（observation binding 自己的置信度
        fail-closed 检查不受此限制）。"""
        service_src = self._source("agents/terminal_service.py")
        self.assertNotIn(".confidence", service_src,
                         "terminal_service.py 不应读取任何 binding.confidence")
        monitor_src = self._source("agents/monitor.py")
        for banned in ("terminal_window.confidence",
                       "window_binding.confidence",
                       "WindowBindingConfidence"):
            self.assertNotIn(banned, monitor_src,
                             f"monitor.py 激活链不应包含 {banned}")


if __name__ == "__main__":
    unittest.main()
