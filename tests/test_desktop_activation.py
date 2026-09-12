"""v4.4 Desktop 激活 + UI labels 测试（plan2 §10/§11/§15，Phase 5）。

激活链全部 mock Win32 原语与 psutil，只验证安全决策逻辑；
不打开真实窗口、不发送任何输入。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from agents.desktop_window import DesktopWindowService
from agents.models import (
    ActivationCode,
    AgentInstance,
    AgentKind,
    AgentSurface,
    DesktopHost,
)
from pet.labels import environment_label


def _desktop_instance(kind=AgentKind.ZCODE, host_pid=900,
                      token="111.000"):
    return AgentInstance(
        kind=kind, pid=0, source="windows",
        surface=AgentSurface.DESKTOP,
        host_pid=host_pid, host_process_token=token,
        host_key=f"windows|{kind.value}|desktop-host|{host_pid}|{token}",
        logical_session_id="s-1")


class DesktopWindowServiceTests(unittest.TestCase):
    def setUp(self):
        self.svc = DesktopWindowService()

    def test_missing_host_identity_fails_closed(self):
        inst = AgentInstance(kind=AgentKind.ZCODE, pid=0, source="windows",
                             surface=AgentSurface.DESKTOP)
        result = self.svc.activate_host(inst)
        self.assertIs(result.code, ActivationCode.NO_BINDING)

    def test_host_process_gone_is_agent_gone(self):
        inst = _desktop_instance()
        with patch("agents.desktop_window.winkeys") as wk, \
             patch("psutil.Process", side_effect=Exception("gone")):
            result = self.svc.activate_host(inst)
        self.assertIs(result.code, ActivationCode.AGENT_GONE)
        wk.enum_windows.assert_not_called()   # 身份未证明前绝不枚举窗口

    def test_host_incarnation_mismatch_is_agent_gone(self):
        inst = _desktop_instance(token="111.000")
        with patch("agents.desktop_window.winkeys") as wk, \
             patch("psutil.Process") as proc:
            proc.return_value.create_time.return_value = 999.999
            result = self.svc.activate_host(inst)
        self.assertIs(result.code, ActivationCode.AGENT_GONE)
        wk.enum_windows.assert_not_called()

    def test_no_matching_window_is_no_binding(self):
        inst = _desktop_instance()
        with patch("agents.desktop_window.winkeys") as wk, \
             patch("psutil.Process") as proc:
            proc.return_value.create_time.return_value = 111.000
            wk.enum_windows.return_value = [(50, 1234, "其他", "Other")]
            proc.return_value.exe.return_value = "C:\\other\\app.exe"
            result = self.svc.activate_host(inst)
        self.assertIs(result.code, ActivationCode.NO_BINDING)

    def test_foreign_pid_with_matching_exe_accepted(self):
        # 窗口属主是同 app 的 Electron 子进程（exe 匹配）→ 允许
        inst = _desktop_instance(kind=AgentKind.CODEX)

        class FakeProc:
            def __init__(self, pid):
                self._pid = pid

            def create_time(self):
                return 111.000

            def exe(self):
                return ("C:/app/ChatGPT.exe" if self._pid == 300
                        else "C:/app/other.exe")

        with patch("agents.desktop_window.winkeys") as wk,              patch("psutil.Process", side_effect=FakeProc):
            wk.enum_windows.return_value = [
                (70, 300, "ChatGPT", "Chrome_WidgetWin_1")]
            wk.window_identity.return_value = type("I", (), {
                "hwnd": 70, "pid": 300,
                "process_created": 111.0,
                "window_class": "Chrome_WidgetWin_1"})
            wk.validate_window.return_value = True
            wk.try_set_foreground.return_value = True
            result = self.svc.activate_host(inst)
        self.assertIs(result.code, ActivationCode.OK)
        wk.try_set_foreground.assert_called_once_with(70)

    def test_validation_failure_is_stale_window(self):
        inst = _desktop_instance()
        with patch("agents.desktop_window.winkeys") as wk, \
             patch("psutil.Process") as proc:
            proc.return_value.create_time.return_value = 111.000
            wk.enum_windows.return_value = [(70, 900, "ZCode",
                                             "Chrome_WidgetWin_1")]
            wk.window_identity.return_value = type("I", (), {
                "hwnd": 70, "pid": 900, "process_created": 111.0,
                "window_class": "Chrome_WidgetWin_1"})
            wk.validate_window.return_value = False
            result = self.svc.activate_host(inst)
        self.assertIs(result.code, ActivationCode.STALE_WINDOW)
        wk.try_set_foreground.assert_not_called()

    def test_foreground_denied_flashes_instead(self):
        inst = _desktop_instance()
        with patch("agents.desktop_window.winkeys") as wk, \
             patch("psutil.Process") as proc:
            proc.return_value.create_time.return_value = 111.000
            wk.enum_windows.return_value = [(70, 900, "ZCode",
                                             "Chrome_WidgetWin_1")]
            wk.window_identity.return_value = type("I", (), {
                "hwnd": 70, "pid": 900, "process_created": 111.0,
                "window_class": "Chrome_WidgetWin_1"})
            wk.validate_window.return_value = True
            wk.try_set_foreground.return_value = False
            result = self.svc.activate_host(inst)
        self.assertIs(result.code, ActivationCode.FOREGROUND_DENIED)
        wk.flash_window.assert_called_once_with(70)

    def test_real_winkeys_surface_has_no_injection_path(self):
        # 安全边界：desktop activation 复用的原语模块绝不提供输入注入/
        # 剪贴板调用面（plan2 §19）
        import actions.winkeys as real_winkeys
        for banned in ("SendInput", "keybd_event", "mouse_event",
                       "OpenClipboard", "SetClipboardData",
                       "send_input", "set_clipboard"):
            self.assertFalse(hasattr(real_winkeys, banned), banned)

    def test_tray_hidden_main_window_restored_for_both_agents(self):
        for kind in (AgentKind.CODEX, AgentKind.ZCODE):
            with self.subTest(kind=kind), \
                 patch('agents.desktop_window.winkeys') as wk, \
                 patch('psutil.Process') as proc:
                proc.return_value.create_time.return_value = 111.0
                wk.enum_windows.side_effect = [[], [
                    (60, 900, 'helper', 'Chrome_WidgetWin_1'),
                    (70, 900, 'app', 'Chrome_WidgetWin_1'),
                    (80, 900, 'IME', 'IME')]]
                wk.is_app_window.side_effect = lambda hwnd: hwnd == 70
                wk.window_identity.return_value = type('I', (), {'hwnd':70})
                wk.validate_window.return_value = True
                wk.try_set_foreground.return_value = True
                result = self.svc.activate_host(_desktop_instance(kind=kind))
                self.assertIs(result.code, ActivationCode.OK)
                wk.enum_windows.assert_called_with(include_hidden=True)
                wk.restore_window.assert_called_once_with(70)
                wk.try_set_foreground.assert_called_once_with(70)

    def test_restore_hidden_and_minimized_use_distinct_commands(self):
        from actions import winkeys
        for iconic, visible, expected in [(True, True, 9), (False, False, 5),
                                           (False, True, None)]:
            with self.subTest(iconic=iconic, visible=visible), \
                 patch.object(winkeys, 'user32') as u:
                u.IsWindow.return_value = True
                u.IsIconic.return_value = iconic
                u.IsWindowVisible.return_value = visible
                winkeys.restore_window(70)
                if expected is None:
                    u.ShowWindowAsync.assert_not_called()
                else:
                    u.ShowWindowAsync.assert_called_once_with(70, expected)


class MonitorSurfaceDispatchTests(unittest.TestCase):
    def test_pet_double_click_routes_exact_desktop_key(self):
        from agents.monitor import Monitor
        from agents.models import ActivationResult
        from pet.petview import PetView
        from tests.test_monitoring import MemoryConfig
        for kind in (AgentKind.CODEX, AgentKind.ZCODE):
            with self.subTest(kind=kind):
                monitor = Monitor(MemoryConfig())
                inst = _desktop_instance(kind=kind)
                monitor.instances = {inst.key: inst}
                view = object.__new__(PetView)
                view.body_activates = True
                view.agent_key = inst.key
                view._on_activate = monitor.activate_target
                with patch.object(monitor._desktop_window_service, 'activate_host',
                                  return_value=ActivationResult(ActivationCode.OK)) as activate:
                    view._on_body_double()
                    activate.assert_called_once_with(inst)
                    activate.reset_mock()
                    view._on_hit_tag(('activate', inst.key))
                    activate.assert_called_once_with(inst)

    def test_desktop_target_routed_to_desktop_service(self):
        from agents.monitor import Monitor
        from agents.terminal_service import WindowsTerminalService
        from tests.test_monitoring import MemoryConfig
        monitor = Monitor(MemoryConfig())
        monitor._terminal_service = WindowsTerminalService(None)
        inst = _desktop_instance()
        monitor.instances = {inst.key: inst}
        calls = {"desktop": 0, "terminal": 0}

        def fake_desktop(instance):
            calls["desktop"] += 1
            from agents.models import ActivationResult
            return ActivationResult(ActivationCode.OK)

        def fake_terminal(*args, **kwargs):
            calls["terminal"] += 1
            from agents.models import ActivationResult
            return ActivationResult(ActivationCode.OK)

        with patch.object(monitor._desktop_window_service, "activate_host",
                          side_effect=fake_desktop), \
             patch.object(monitor._terminal_service, "activate_cached",
                          side_effect=fake_terminal):
            monitor.activate_target(inst.key)
        self.assertEqual(calls, {"desktop": 1, "terminal": 0})

    def test_terminal_target_never_reaches_desktop_service(self):
        from agents.monitor import Monitor
        from agents.terminal_service import WindowsTerminalService
        from tests.test_monitoring import MemoryConfig
        monitor = Monitor(MemoryConfig())
        monitor._terminal_service = WindowsTerminalService(None)
        inst = AgentInstance(AgentKind.CODEX, 5, "windows",
                             process_token="5")
        monitor.instances = {inst.key: inst}
        with patch.object(monitor._desktop_window_service,
                          "activate_host") as dsvc, \
             patch.object(monitor._terminal_service, "activate_cached",
                          return_value=None):
            monitor.activate_target(inst.key)
        dsvc.assert_not_called()

    def test_live_key_mismatch_is_agent_gone(self):
        from agents.monitor import Monitor
        from agents.terminal_service import WindowsTerminalService
        from tests.test_monitoring import MemoryConfig
        monitor = Monitor(MemoryConfig())
        monitor._terminal_service = WindowsTerminalService(None)
        result = monitor.activate_target("windows|zcode|desktop|1|x")
        self.assertIs(result.code, ActivationCode.AGENT_GONE)


class ConfigAndLabelTests(unittest.TestCase):
    def test_zcode_kind_normalized_for_old_config(self):
        from pet.config import CONFIG_VERSION, DEFAULTS, normalize
        loaded = {
            "config_version": 5,
            "monitor": {"agents": {"codex": True, "claude": True,
                                    "kimi": True, "pi": True}},
            "presentation": {"concurrent": {
                "enabled": True, "mode": "aggregate", "max_targets": 3,
                "eligible_kinds": {"codex": True, "claude": True,
                                    "kimi": True, "pi": True},
                "slots": []}},
        }
        out = normalize(loaded)
        self.assertEqual(out["config_version"], CONFIG_VERSION)   # 仍为 5
        self.assertTrue(out["monitor"]["agents"]["zcode"])
        self.assertTrue(
            out["presentation"]["concurrent"]["eligible_kinds"]["zcode"])
        self.assertTrue(DEFAULTS["monitor"]["agents"]["zcode"])

    def test_zcode_kind_can_be_disabled(self):
        from pet.config import normalize
        loaded = {
            "config_version": 5,
            "monitor": {"agents": {"codex": True, "claude": True,
                                    "kimi": True, "pi": True,
                                    "zcode": False}},
            "presentation": {"concurrent": {
                "enabled": True, "mode": "aggregate", "max_targets": 3,
                "eligible_kinds": {}, "slots": []}},
        }
        out = normalize(loaded)
        self.assertFalse(out["monitor"]["agents"]["zcode"])

    def test_environment_labels(self):
        win = AgentInstance(AgentKind.CODEX, 1, "windows", process_token="1")
        self.assertEqual(environment_label(win), "Codex")
        wsl = AgentInstance(AgentKind.CODEX, 2, "wsl:Ubuntu",
                            process_token="2")
        self.assertEqual(environment_label(wsl), "Codex · WSL Ubuntu")
        desktop = _desktop_instance()
        self.assertEqual(environment_label(desktop), "ZCode · Desktop")
        codex_desktop = _desktop_instance(kind=AgentKind.CODEX)
        self.assertEqual(environment_label(codex_desktop), "Codex · Desktop")


if __name__ == "__main__":
    unittest.main()
