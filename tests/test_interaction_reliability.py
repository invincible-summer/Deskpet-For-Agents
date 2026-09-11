"""Regression coverage for nested menu loops, exit starvation and UI recovery.

Native menu tracking is simulated; callbacks deliberately reenter the real Tk
interpreter, without leaving interactive menus blocking the test process.
"""
import tkinter as tk
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from test_dp43_ui_lifecycle import make_app
import test_tray_native as tray_tests
from test_ui_coordinator import FakeRoot
from pet.context_menu import TkContextMenuController
from pet.ui_coordinator import UiCoordinator


class MenuReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.ctrl = TkContextMenuController(self.root)
        self.addCleanup(self.root.destroy)
        self.addCleanup(self.ctrl.shutdown)

    def test_native_loop_never_schedules_waiting_idle_and_selects_once(self):
        calls = []
        commands = []

        def build(menu):
            commands.append(self.ctrl.deferred(calls.append, "selected"))
            menu.add_command(label="go", command=commands[-1])

        def track(menu, *_args):
            menu.invoke(0)
            menu.invoke(0)
            # Real Windows tracking services idle callbacks while still
            # posted. An idle that requeues itself here hangs update_idletasks.
            self.assertEqual(self.root.tk.call("after", "info"), "")
            self.root.update_idletasks()
            self.assertEqual(calls, [])
            self.assertTrue(menu.winfo_exists())

        with patch.object(tk.Menu, "tk_popup", track):
            self.ctrl.show("pet-1", 10, 10, build)
        self.root.update_idletasks()
        commands[0]()  # stale native callback, after destruction
        self.root.update_idletasks()
        self.assertEqual(calls, ["selected"])
        self.assertFalse(self.ctrl.active)

    def test_posted_native_command_survives_popup_return(self):
        calls = []
        def track(menu, *_args):
            # Model Windows posted WM_COMMAND: selection is dispatched only
            # after tk_popup returns, unlike synchronous menu.invoke probes.
            self.root.after(0, lambda: menu.invoke(0))
        with patch.object(tk.Menu, "tk_popup", track):
            self.ctrl.show("pet", 0, 0, lambda menu: menu.add_command(
                label="dashboard", command=self.ctrl.deferred(calls.append, "opened")))
        self.assertTrue(self.ctrl.active, "command registry must survive native return")
        self.root.update()
        self.assertEqual(calls, ["opened"])
        self.assertFalse(self.ctrl.active)
        self.assertEqual(self.root.tk.call("after", "info"), "")

    def test_new_popup_cannot_cancel_an_already_selected_exit(self):
        quit_command = Mock()
        with patch.object(tk.Menu, "tk_popup", lambda *args: None):
            def build(menu):
                self.ctrl.deferred(quit_command)()
            self.ctrl.show("first", 0, 0, build)
            self.ctrl.show("next", 0, 0, Mock())
        self.root.update_idletasks()
        quit_command.assert_called_once()

    def test_nested_context_request_does_not_destroy_tracking_menu(self):
        nested_build = Mock()

        def track(menu, *_args):
            self.ctrl.show("pet-2", 20, 20, nested_build)
            self.assertTrue(menu.winfo_exists())
            self.assertEqual(self.ctrl.owner, "pet-1")

        with patch.object(tk.Menu, "tk_popup", track):
            self.ctrl.show("pet-1", 10, 10, lambda m: m.add_command(label="x"))
        nested_build.assert_not_called()
        self.root.update()
        self.assertFalse(self.ctrl.active)

    def test_shutdown_cancels_selected_action_and_all_callbacks(self):
        command = Mock()

        def build(menu):
            menu.add_command(label="x", command=self.ctrl.deferred(command))
            menu.invoke(0)

        with patch.object(tk.Menu, "tk_popup", lambda *a: None):
            self.ctrl.show("pet", 0, 0, build)
        self.ctrl.shutdown()
        self.root.update_idletasks()
        command.assert_not_called()
        self.assertEqual(self.root.tk.call("after", "info"), "")

    def test_exit_supersedes_selection_but_waits_for_native_return(self):
        selected, quit_command = Mock(), Mock()

        def track(menu, *_args):
            self.ctrl.deferred(selected)()
            self.ctrl.close_then(quit_command)
            self.root.update_idletasks()
            quit_command.assert_not_called()
            self.assertTrue(menu.winfo_exists())

        with patch.object(tk.Menu, "tk_popup", track):
            self.ctrl.show("pet", 0, 0, lambda m: m.add_command(label="x"))
        self.root.update_idletasks()
        selected.assert_not_called()
        quit_command.assert_called_once()


class BridgeReliabilityTests(unittest.TestCase):
    def test_failed_tick_rearms_single_bridge(self):
        root = FakeRoot()
        ui = UiCoordinator(root)
        ui.start()
        token = ui._bridge_after
        with patch.object(ui, "_bridge_tick", side_effect=RuntimeError("fault")):
            with self.assertRaises(RuntimeError):
                root.fire(token)
        self.assertIsNotNone(ui._bridge_after)
        self.assertNotEqual(ui._bridge_after, token)
        self.assertEqual(sum(not t.idle for t in root.timers.values()), 1)
        root.fire(ui._bridge_after)
        self.assertEqual(ui.bridge_count, 2)
        ui.stop()
        self.assertFalse(root.timers)

    def test_persistently_broken_monitor_cannot_starve_tray(self):
        monitor = Mock()
        monitor.get_targets_if_changed.side_effect = RuntimeError("broken source")
        drain = Mock()
        ui = UiCoordinator(FakeRoot(), monitor=monitor, tray_drain=drain)
        with self.assertLogs("pet.ui_coordinator", level="ERROR"):
            for _ in range(3):
                ui._bridge_tick()
        self.assertEqual(drain.call_count, 3)

    def test_tray_exit_stops_the_rest_of_bridge(self):
        repair = Mock()
        ui = UiCoordinator(FakeRoot(), activation_repair_drain=repair)
        ui.tray_drain = ui.stop
        ui._bridge_tick()
        repair.assert_not_called()


class TrayReliabilityTests(unittest.TestCase):
    def test_reentrant_native_menu_ignored_and_null_message_posted(self):
        import pet.tray as mod
        helper = tray_tests.TrayNativeMenuTests()
        icon = helper._icon()
        log, contexts = helper._menu_env(icon, 0)
        with ExitStack() as stack:
            for ctx in contexts:
                stack.enter_context(ctx)
            post = stack.enter_context(patch.object(mod.user32, "PostMessageW"))
            track = stack.enter_context(patch.object(
                mod.user32, "TrackPopupMenuEx",
                side_effect=lambda *args: (icon._open_native_menu(), 0)[1]))
            icon._open_native_menu()
        track.assert_called_once()
        self.assertFalse(icon._menu_active)
        post.assert_called_once_with(icon._hwnd, 0, 0, 0)
        self.assertEqual(sum(item[0] == "CreatePopupMenu" for item in log), 2)

    def test_failed_submenu_construction_releases_unattached_handle(self):
        import pet.tray as mod
        helper = tray_tests.TrayNativeMenuTests()
        icon = helper._icon()
        log, contexts = helper._menu_env(icon, 0)
        with ExitStack() as stack:
            for ctx in contexts:
                stack.enter_context(ctx)
            stack.enter_context(patch.object(mod.user32, "PostMessageW"))
            stack.enter_context(patch.object(
                mod.user32, "AppendMenuW", side_effect=[True, False]))
            icon._open_native_menu()
        self.assertEqual([row[1] for row in log if row[0] == "DestroyMenu"],
                         [0xAAA2, 0xAAA1])
        self.assertEqual(icon.menu_open_failures(), 1)
        self.assertFalse(icon._menu_active)

class AppReliabilityTests(unittest.TestCase):
    def test_bad_cleanup_cannot_skip_other_stop_signals(self):
        app = make_app()
        with patch.object(app.monitor, "request_stop", side_effect=RuntimeError("fault")), \
                patch.object(app.pet_manager, "request_stop", wraps=app.pet_manager.request_stop) as stop, \
                patch.object(app.config_saver, "begin_shutdown", wraps=app.config_saver.begin_shutdown) as save, \
                self.assertLogs("pet.app", level="ERROR"):
            app.quit()
        stop.assert_called_once()
        save.assert_called_once()
        self.assertTrue(app._closing)
        app.quit()

    def test_tray_quit_is_terminal_for_the_drain_batch(self):
        from pet.tray import TrayEvent, TrayIcon
        app = make_app()
        icon = TrayIcon()
        # Raw queue intentionally bypasses the priority quit latch.
        icon.events.put(TrayEvent("quit"))
        icon.events.put(TrayEvent("dashboard"))
        app.tray = icon
        with patch.object(app, "_reconcile_tray_runtime"), \
                patch.object(app, "open_dashboard") as dashboard:
            app._poll_tray_events()
        dashboard.assert_not_called()
        self.assertTrue(app._closing)

    def test_quit_in_native_tk_loop_waits_for_stack_unwind(self):
        app = make_app()
        def track(menu, *_args):
            app.quit()
            self.assertFalse(app._closing)
            self.assertTrue(menu.winfo_exists())
            self.assertTrue(app.root.winfo_exists())
        with patch.object(tk.Menu, "tk_popup", track):
            app._menu_controller.show("pet", 0, 0, lambda m: m.add_command(label="x"))
        app.root.update_idletasks()
        self.assertTrue(app._closing)

    def test_stopped_tray_generation_cannot_lose_exit_intent(self):
        from pet.tray import TrayIcon, TrayEvent, TrayState
        app = make_app()
        icon = TrayIcon()
        icon._state = TrayState.STOPPED
        icon._enqueue(TrayEvent("quit"))
        app.tray = icon
        app._reconcile_tray_runtime()
        self.assertTrue(app._closing)

    def test_hiding_last_fleet_pet_keeps_restore_entry(self):
        app = make_app()
        try:
            with patch.object(app, "_reconcile_tray_runtime") as reconcile:
                app._hide_view(app.pet_manager.views["pet-1"])
            self.assertTrue(app._tray_desired())
            self.assertTrue(app.pet_manager.user_hidden)
            reconcile.assert_called_once()
            with patch.object(app, "_reconcile_tray_runtime"):
                app.show_pet()
            self.assertTrue(app.pet_manager.any_visible())
        finally:
            app.quit()

    def test_new_tray_has_snapshot_before_start(self):
        from pet.tray import TrayIcon
        app = make_app()
        try:
            def start(icon):
                self.assertFalse(icon._menu_snapshot.pet_visible)
            app.pet_manager.hide_all()
            with patch.object(TrayIcon, "start", start):
                app._spawn_tray_generation()
        finally:
            app.quit()

    def test_empty_picker_request_clears_previous_window(self):
        app = make_app()
        try:
            old = app._agent_picker = tk.Toplevel(app.root)
            app._open_agent_picker("pet-1")
            self.assertIsNone(app._agent_picker)
            self.assertFalse(old.winfo_exists())
        finally:
            app.quit()

    def test_dashboard_dialog_reentry_and_late_result(self):
        app = make_app()
        try:
            app.open_dashboard()
            dash = app.dashboard
            nested = Mock()
            def dialog():
                self.assertIsNone(dash.run_dialog(nested))
                dash.hide_dashboard()
                dash.open()
                return "late folder"
            self.assertIsNone(dash.run_dialog(dialog))
            nested.assert_not_called()
            self.assertFalse(dash._dialog_active)
            self.assertEqual(dash.run_dialog(lambda: "new folder"), "new folder")
            with self.assertRaises(RuntimeError):
                dash.run_dialog(Mock(side_effect=RuntimeError("dialog failed")))
            self.assertFalse(dash._dialog_active)
        finally:
            app.quit()


class DashboardEntryTests(unittest.TestCase):
    def _invoke_context(self, app, label):
        from types import SimpleNamespace
        dash = app.dashboard
        event = SimpleNamespace(widget=dash, num=3, x_root=40, y_root=50)
        def track(menu, *_args):
            self.assertIs(app._menu_controller.owner, dash)
            for i in range(menu.index("end") + 1):
                if menu.type(i) == "command" and menu.entrycget(i, "label") == label:
                    menu.invoke(i)
                    return
            self.fail(f"Missing dashboard menu action: {label}")
        with patch.object(tk.Menu, "tk_popup", track):
            self.assertEqual(dash._on_context_menu(event), "break")
        self.assertFalse(app._menu_controller.active)
        app.root.update_idletasks()

    def test_repeated_open_close_reuses_dashboard_and_retains_page(self):
        from pet.dashboard import PAGE_PETS
        app = make_app()
        try:
            app.open_dashboard()
            dash = app.dashboard
            dash._show_page(PAGE_PETS)
            for _ in range(20):
                self._invoke_context(app, "关闭仪表盘")
                self.assertEqual(dash.state(), "withdrawn")
                self.assertFalse(app._closing)
                app.open_dashboard()
                self.assertIs(app.dashboard, dash)
                self.assertEqual(dash.state(), "normal")
                self.assertEqual(dash._page, PAGE_PETS)
            app.root.tk.call(dash.protocol("WM_DELETE_WINDOW"))
            self.assertEqual(dash.state(), "withdrawn")
            app.open_dashboard()
            self.assertIs(app.dashboard, dash)
        finally:
            app.quit()

    def test_dashboard_exit_menu_closes_application(self):
        app = make_app()
        app.open_dashboard()
        with patch.object(app.root, "destroy", wraps=app.root.destroy) as destroy:
            self._invoke_context(app, "退出 DeskPet")
            self.assertTrue(app._closing)
            destroy.assert_called_once()
            app.open_dashboard()  # late activation after exit cannot revive UI
            app.quit()
            destroy.assert_called_once()

    def test_dashboard_context_preserves_text_controls(self):
        from types import SimpleNamespace
        from tkinter import ttk
        app = make_app()
        try:
            app.open_dashboard()
            dash = app.dashboard
            entry = ttk.Entry(dash)
            with patch.object(app._menu_controller, "show") as show:
                dash._on_context_menu(SimpleNamespace(widget=entry, num=3))
            show.assert_not_called()
            entry.destroy()
        finally:
            app.quit()

    def test_dashboard_keyboard_menu_uses_same_owner(self):
        from types import SimpleNamespace
        app = make_app()
        try:
            app.open_dashboard()
            dash = app.dashboard
            with patch.object(app._menu_controller, "show") as show:
                dash._on_context_menu(SimpleNamespace(widget=dash, num="??"))
            self.assertIs(show.call_args.args[0], dash)
            self.assertEqual(show.call_args.args[3], dash._build_context_menu)
        finally:
            app.quit()


class ConverterReliabilityTests(unittest.TestCase):
    def _run_child(self, *, cancel_during_spawn=False, fail_assignment=False,
                   cancel_after_registration=False):
        import ctypes
        import subprocess
        import sys
        from pet.skins import ConverterJob
        conv = ConverterJob()
        processes = []
        gate_writes = []
        if cancel_after_registration:
            import threading
            class CancelOnRegistration:
                def __init__(self):
                    self.lock = threading.Lock()
                    self.fired = False
                def __enter__(self):
                    self.lock.acquire()
                def __exit__(self, *_args):
                    registered = conv._proc is not None and not self.fired
                    self.lock.release()
                    if registered:
                        self.fired = True
                        conv.cancel()
            conv._lock = CancelOnRegistration()
        popen = subprocess.Popen
        def spawn(_cmd, **kwargs):
            proc = popen([sys.executable, "-c",
                          "import sys; sys.exit(0 if sys.stdin.buffer.read(1) == b'g' else 2)"],
                         **kwargs)
            processes.append(proc)
            gate = Mock(wraps=proc.stdin)
            proc.stdin = gate
            gate_writes.append(gate.write)
            if cancel_during_spawn:
                conv.cancel()
            return proc
        with ExitStack() as stack:
            stack.enter_context(patch("subprocess.Popen", side_effect=spawn))
            if fail_assignment:
                stack.enter_context(patch.object(
                    ctypes.windll.kernel32, "AssignProcessToJobObject", return_value=0))
            ok = conv.run("unused", "unused", 100, 10)
        self.assertEqual(len(processes), 1)
        proc = processes[0]
        self.assertIsNotNone(proc.poll(), "owned converter must be reaped")
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stderr.closed)
        self.assertIsNone(conv._proc)
        self.assertIsNone(conv._hjob)
        if cancel_during_spawn or cancel_after_registration or fail_assignment:
            gate_writes[0].assert_not_called()
        return ok

    def test_gate_pipe_can_be_closed_before_communicate(self):
        self.assertTrue(self._run_child())

    def test_cancel_between_spawn_and_registration_reaps_child(self):
        self.assertFalse(self._run_child(cancel_during_spawn=True))

    def test_cancel_after_registration_never_releases_unassigned_gate(self):
        self.assertFalse(self._run_child(cancel_after_registration=True))

    def test_job_assignment_failure_never_releases_gate(self):
        self.assertFalse(self._run_child(fail_assignment=True))


if __name__ == "__main__":
    unittest.main()
