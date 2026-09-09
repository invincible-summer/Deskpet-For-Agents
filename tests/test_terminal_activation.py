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
            result = service.activate(KEY, is_agent_live=ALWAYS_LIVE)
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
            result = service.activate(KEY, is_agent_live=ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.OK)

    # ------------------------------------------------ C. stale → refresh once → OK
    def test_stale_hwnd_refresh_once_then_ok(self):
        service = self._service()
        self._bind(service, make_binding(make_identity(hwnd=11, pid=100)))
        new_binding = make_binding(make_identity(hwnd=33, pid=300, created=9.0))
        calls = {"validate": 0}

        def fake_validate(identity):
            calls["validate"] += 1
            if calls["validate"] == 1:
                # 第一次 stale：模拟重解析拿到新绑定
                service._window_bindings[KEY] = new_binding
                return False
            return True

        with patch.object(winkeys, "validate_window",
                          side_effect=fake_validate), \
             patch.object(service, "refresh_observed_controls",
                          return_value=True) as refresh, \
             patch.object(winkeys, "restore_window", return_value=True), \
             patch.object(winkeys, "try_set_foreground", return_value=True):
            result = service.activate(KEY, is_agent_live=ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.OK)
        self.assertTrue(result.repaired)
        refresh.assert_called_once_with(force=True)

    # ------------------------------------------------ D. stale after retry
    def test_stale_after_refresh_returns_stale_window(self):
        service = self._service()
        self._bind(service, make_binding(make_identity(hwnd=11, pid=100)))
        with patch.object(winkeys, "validate_window", return_value=False), \
             patch.object(service, "refresh_observed_controls",
                          return_value=True), \
             patch.object(winkeys, "restore_window", return_value=True) as r, \
             patch.object(winkeys, "try_set_foreground",
                          return_value=True) as fg:
            result = service.activate(KEY, is_agent_live=ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.STALE_WINDOW)
        self.assertTrue(result.repaired)
        r.assert_not_called()   # 窗口身份无法证明：绝不动作
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
            result = service.activate(KEY, is_agent_live=ALWAYS_LIVE)
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
            result = service.activate(KEY, is_agent_live=ALWAYS_LIVE)
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
            result = service.activate(KEY, is_agent_live=DEAD)
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
            result = service.activate(KEY, is_agent_live=ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.OK)

    def test_none_confidence_with_window_activates(self):
        """NONE + window（唯一窗口兜底）允许唤起（§26）。"""
        service = self._service()
        self._bind(service, make_binding(make_identity(hwnd=11, pid=100),
                                         WindowBindingConfidence.NONE))
        with patch.object(winkeys, "validate_window", return_value=True), \
             patch.object(winkeys, "restore_window", return_value=True), \
             patch.object(winkeys, "try_set_foreground", return_value=True):
            result = service.activate(KEY, is_agent_live=ALWAYS_LIVE)
        self.assertEqual(result.code, ActivationCode.OK)

    def test_drop_instance_clears_cached_bindings(self):
        service = self._service()
        self._bind(service, make_binding(make_identity()))
        service._observation_bindings = {KEY: object()}
        service.drop_instance(KEY)
        self.assertNotIn(KEY, service._window_bindings)
        self.assertNotIn(KEY, service._observation_bindings)

    def test_refresh_runs_at_most_once(self):
        """validate 反复失败也只 refresh 一次（不循环重试）。"""
        service = self._service()
        self._bind(service, make_binding(make_identity()))
        with patch.object(winkeys, "validate_window", return_value=False), \
             patch.object(service, "refresh_observed_controls",
                          return_value=True) as refresh:
            service.activate(KEY, is_agent_live=ALWAYS_LIVE)
        refresh.assert_called_once()


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
