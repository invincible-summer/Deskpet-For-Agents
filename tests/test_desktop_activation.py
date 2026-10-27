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
    ActivationResult,
    WindowIdentity,
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
    """All lifecycle cases share one fixture; hidden content is never raw shown."""

    def setUp(self):
        self.svc = DesktopWindowService()
        self.inst = _desktop_instance()
        self.patches = [
            patch("agents.desktop_window.winkeys"),
            patch("psutil.Process"),
            patch("agents.desktop_window.start_desktop_reopen")]
        self.wk, self.proc, self.reopen = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)
        self.addCleanup(self.svc.close)
        self.proc.return_value.create_time.return_value = 111.0
        self.proc.return_value.exe.return_value = "C:/app/ZCode.exe"
        self.wk.enum_windows.return_value = [
            (70, 900, "", "Chrome_WidgetWin_1")]
        self.wk.is_app_window.return_value = True
        self.identity = WindowIdentity(70, 900, 111.0, "Chrome_WidgetWin_1")
        self.wk.window_identity.return_value = self.identity
        self.wk.validate_window.return_value = True
        self.wk.window_visible.return_value = True
        self.wk.window_ready.return_value = True
        self.wk.try_set_foreground.return_value = True
        self.reopen.return_value.poll.return_value = None

    def test_identity_and_foreground_contracts(self):
        for mode, expected in [
            ("missing", ActivationCode.NO_BINDING),
            ("gone", ActivationCode.AGENT_GONE),
            ("generation", ActivationCode.AGENT_GONE),
            ("stale", ActivationCode.STALE_WINDOW),
            ("denied", ActivationCode.FOREGROUND_DENIED),
            ("ok", ActivationCode.OK),
        ]:
            with self.subTest(mode=mode):
                self.proc.return_value.create_time.return_value = (
                    999.0 if mode == "generation" else 111.0)
                self.proc.side_effect = OSError("gone") if mode == "gone" else None
                self.wk.validate_window.return_value = mode != "stale"
                self.wk.try_set_foreground.return_value = mode != "denied"
                inst = (_desktop_instance(host_pid=0)
                        if mode == "missing" else self.inst)
                self.assertIs(self.svc.activate_host(inst).code, expected)
        self.reopen.assert_not_called()

    def test_hidden_tray_uses_app_show_once_then_original_window(self):
        for kind in (AgentKind.CODEX, AgentKind.ZCODE, AgentKind.DSH):
            with self.subTest(kind=kind):
                inst = _desktop_instance(kind=kind)
                self.proc.return_value.exe.return_value = {
                    AgentKind.CODEX: "C:/app/ChatGPT.exe",
                    AgentKind.ZCODE: "C:/app/ZCode.exe",
                    AgentKind.DSH: "C:/app/DeepSeek Harness.exe"}[kind]
                self.wk.window_visible.return_value = False
                self.reopen.return_value.poll.return_value = None
                self.reopen.reset_mock()
                self.wk.restore_window.reset_mock()
                self.wk.try_set_foreground.reset_mock()
                for _ in range(3):
                    self.assertIs(self.svc.activate_host(inst).code,
                                  ActivationCode.RESTORE_PENDING)
                self.reopen.assert_called_once_with(kind, 900, "111.000")
                self.wk.restore_window.assert_not_called()
                self.wk.try_set_foreground.assert_not_called()
                # The application owns showing Aura content. A visible HWND
                # alone cannot finish the job while its bootstrap is running.
                self.reopen.return_value.poll.return_value = 0
                self.wk.window_ready.return_value = True
                self.assertIs(self.svc.activate_host(inst).code, ActivationCode.OK)

    def test_taskbar_restore_uses_no_reopen_and_waits_for_readiness(self):
        self.wk.window_ready.return_value = False
        self.assertIs(self.svc.activate_host(self.inst).code,
                      ActivationCode.RESTORE_PENDING)
        self.wk.restore_window.assert_called_once_with(70, allow_hidden=False)
        self.reopen.assert_not_called()
        self.wk.try_set_foreground.assert_not_called()
        self.wk.window_ready.return_value = True
        self.assertIs(self.svc.activate_host(self.inst).code, ActivationCode.OK)

    def test_unsupported_entry_fails_without_native_show(self):
        self.wk.window_visible.return_value = False
        self.reopen.return_value = None
        self.assertIs(self.svc.activate_host(self.inst).code, ActivationCode.NO_BINDING)
        self.wk.restore_window.assert_not_called()
        self.wk.try_set_foreground.assert_not_called()

    def test_helpers_disabled_ime_and_foreign_instances_are_excluded(self):
        self.wk.enum_windows.return_value = [
            (30, 900, "helper", "Chrome_WidgetWin_0"),
            (40, 900, "IME", "IME"),
            (50, 900, "disabled", "Chrome_WidgetWin_1"),
            (60, 800, "foreign", "Chrome_WidgetWin_1"),
            (70, 900, "", "Chrome_WidgetWin_1")]
        self.wk.is_app_window.side_effect = lambda hwnd: hwnd != 50
        with patch("agents.desktop_window._pid_belongs_to_host",
                   side_effect=lambda pid, host: pid == host):
            self.assertIs(self.svc.activate_host(self.inst).code, ActivationCode.OK)
        self.wk.window_identity.assert_called_once_with(70)

    def test_stale_window_host_exit_bootstrap_failure_and_deadline_clean_up(self):
        for failure, expected in [
            ("stale", ActivationCode.STALE_WINDOW),
            ("gone", ActivationCode.AGENT_GONE),
            ("exit", ActivationCode.NO_BINDING),
            ("timeout", ActivationCode.NO_BINDING),
        ]:
            with self.subTest(failure=failure):
                self.wk.window_visible.return_value = False
                self.wk.validate_window.return_value = True
                self.proc.side_effect = None
                self.reopen.return_value.poll.return_value = None
                self.reopen.return_value.cancel.reset_mock()
                self.svc.activate_host(self.inst)
                if failure == "stale":
                    self.wk.validate_window.return_value = False
                elif failure == "gone":
                    self.proc.side_effect = OSError("gone")
                elif failure == "exit":
                    self.reopen.return_value.poll.return_value = 1
                else:
                    for job in self.svc._pending.values():
                        job.deadline = 0.0
                self.assertIs(self.svc.activate_host(self.inst).code, expected)
                self.assertFalse(self.svc._pending)
                self.reopen.return_value.cancel.assert_called_once()
                self.wk.try_set_foreground.assert_not_called()

    def test_shutdown_reaps_only_own_bootstrap_without_waiting(self):
        self.wk.window_visible.return_value = False
        self.svc.activate_host(self.inst)
        self.svc.close()
        self.reopen.return_value.cancel.assert_called_once()
        self.reopen.return_value.wait.assert_not_called()
        self.assertFalse(self.svc._pending)


class MonitorSurfaceDispatchTests(unittest.TestCase):
    def _monitor(self):
        from agents.monitor import Monitor
        from tests.test_monitoring import MemoryConfig
        monitor = Monitor(MemoryConfig())
        inst = _desktop_instance()
        monitor.instances = {inst.key: inst}
        return monitor, inst

    def test_all_click_surfaces_enqueue_once_and_worker_finishes(self):
        from pet.petview import PetView
        monitor, inst = self._monitor()
        view = object.__new__(PetView)
        view.body_activates = True
        view.agent_key = inst.key
        view._on_activate = monitor.activate_target
        with patch.object(monitor._desktop_window_service, "activate_host",
                          return_value=ActivationResult(ActivationCode.OK)) as activate:
            view._on_body_double()
            view._on_hit_tag(("activate", inst.key))
            activate.assert_not_called()
            self.assertEqual(len(monitor._repair_requests), 1)
            self.assertTrue(monitor._wake.is_set())
            monitor._process_repair_requests(0.0)
            activate.assert_called_once_with(inst)
        res, = monitor.drain_activation_repairs()
        self.assertEqual(res.agent_key, inst.key)
        self.assertIs(res.activation_result.code, ActivationCode.OK)

    def test_two_sessions_share_host_coalesce_until_final_result(self):
        monitor, inst = self._monitor()
        other = _desktop_instance()
        other.logical_session_id = "s-2"
        monitor.instances[other.key] = other
        with patch.object(monitor._desktop_window_service, "activate_host",
                          side_effect=[ActivationResult(ActivationCode.RESTORE_PENDING),
                                       ActivationResult(ActivationCode.OK)]):
            monitor.activate_target(inst.key)
            monitor.activate_target(other.key)
            self.assertEqual(len(monitor._repair_requests), 1)
            monitor._process_repair_requests(0.0)
            self.assertFalse(monitor.drain_activation_repairs())
            monitor.activate_target(other.key)
            self.assertEqual(len(monitor._repair_requests), 1)
            monitor._process_repair_requests(0.0)
        self.assertEqual(len(monitor.drain_activation_repairs()), 1)
        self.assertFalse(monitor._repair_requests)

    def test_terminal_and_gone_dispatch(self):
        monitor, inst = self._monitor()
        terminal = AgentInstance(AgentKind.CODEX, 5, "windows", process_token="5")
        monitor.instances[terminal.key] = terminal
        with patch.object(monitor._desktop_window_service, "activate_host") as desktop, \
             patch.object(monitor._terminal_service, "activate_cached",
                          return_value=ActivationResult(ActivationCode.OK)) as term:
            self.assertIs(monitor.activate_target(terminal.key).code, ActivationCode.OK)
            self.assertIs(monitor.activate_target("gone").code, ActivationCode.AGENT_GONE)
        term.assert_called_once()
        desktop.assert_not_called()

    def test_queue_bound_and_expired_desktop_cleanup(self):
        monitor, inst = self._monitor()
        for i in range(40):
            monitor.request_activation_repair(str(i))
        self.assertEqual(len(monitor._repair_requests), 16)
        monitor._repair_requests.clear()
        monitor.activate_target(inst.key)
        with patch.object(monitor._desktop_window_service, "cancel") as cancel:
            monitor._process_repair_requests(1e30)
        cancel.assert_called_once_with(inst)
        self.assertIs(monitor.drain_activation_repairs()[0].activation_result.code,
                      ActivationCode.NO_BINDING)


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
