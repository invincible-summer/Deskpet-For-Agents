"""Normal app reopen contracts; no real process or desktop is touched."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import psutil

from actions import desktop_launch
from agents.models import AgentKind


class DesktopLaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "resources").mkdir()
        (self.root / "resources" / "app.asar").touch()
        self.executable = self.root / "ZCode.exe"
        self.executable.touch()
        self.environment = {"APPDATA": "host-profile", "CODEX_HOME": "host-codex"}
        self.process = self._snapshot()
        self.processes = self.enterContext(patch.object(
            desktop_launch.psutil, "Process", return_value=self.process))
        self.launch = self.enterContext(patch.object(
            desktop_launch.subprocess, "Popen"))
        self.enterContext(patch.object(desktop_launch, "_WINDOWS", True))
        self.enterContext(patch.object(desktop_launch.subprocess,
                                       "CREATE_NO_WINDOW", 0x08000000,
                                       create=True))

    def _snapshot(self, *, executable=None, arguments=None, created=111.0):
        executable = str(executable or self.executable)
        return SimpleNamespace(
            create_time=Mock(return_value=created),
            exe=Mock(return_value=executable),
            cmdline=Mock(return_value=[executable] if arguments is None else arguments),
            environ=Mock(return_value=self.environment),
            terminate=Mock(),
        )

    def _start(self, kind=AgentKind.ZCODE, pid=900, token="111.000"):
        return desktop_launch.start_desktop_reopen(kind, pid, token)

    def test_supported_installed_apps_reopen_once_without_waiting(self):
        for kind, name in ((AgentKind.CODEX, "ChatGPT.exe"),
                           (AgentKind.CODEX, "Codex.exe"),
                           (AgentKind.ZCODE, "ZCode.exe"),
                           (AgentKind.DSH, "DeepSeek Harness.exe")):
            with self.subTest(kind=kind, name=name):
                executable = self.root / name
                executable.touch()
                self.processes.return_value = self._snapshot(executable=executable)
                self.launch.reset_mock()
                self.assertIsInstance(self._start(kind), desktop_launch.DesktopReopen)
                args, options = self.launch.call_args
                self.assertEqual(args, ([os.path.normcase(os.path.realpath(executable))],))
                self.assertEqual(options["cwd"], os.path.normcase(str(self.root)))
                self.assertEqual(options["env"], self.environment)
                self.assertFalse(options["shell"])
                self.assertTrue(options["close_fds"])
                self.assertEqual(options["creationflags"], 0x08000000)
                for stream in ("stdin", "stdout", "stderr"):
                    self.assertEqual(options[stream], desktop_launch.subprocess.DEVNULL)
                self.launch.assert_called_once()
                self.launch.return_value.wait.assert_not_called()
                self.launch.return_value.communicate.assert_not_called()

    def test_host_environment_preserved_without_inheriting_parent_profile(self):
        self.environment["electron_run_as_node"] = "1"
        with patch.dict(os.environ, {"APPDATA": "wrong-profile",
                                     "ELECTRON_RUN_AS_NODE": "1"}):
            self.assertIsNotNone(self._start())
        child_environment = self.launch.call_args.kwargs["env"]
        self.assertEqual(child_environment, {"APPDATA": "host-profile",
                                             "CODEX_HOME": "host-codex"})
        self.assertIn("electron_run_as_node", self.environment)

    def test_invalid_identity_provider_or_platform_never_launches(self):
        cases = ((AgentKind.OPENCODE, 900, "111.000"),
                 (AgentKind.CLAUDE, 900, "111.000"),
                 (AgentKind.ZCODE, 0, "111.000"),
                 (AgentKind.ZCODE, True, "111.000"),
                 (AgentKind.ZCODE, 900, ""),
                 (AgentKind.ZCODE, 900, "112.000"))
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertIsNone(self._start(*arguments))
        with patch.object(desktop_launch, "_WINDOWS", False):
            self.assertIsNone(self._start())
        self.launch.assert_not_called()

    def test_custom_arguments_helpers_and_mismatched_executable_rejected(self):
        executable = str(self.executable)
        cases = ([], [executable, "--type=renderer"],
                 [executable, "--user-data-dir=other"],
                 [executable, "--profile", "other"],
                 [executable, "--updated"], [executable, "app.cjs"],
                 [str(self.root / "other.exe")], ["ZCode.exe"])
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.processes.return_value = self._snapshot(arguments=arguments)
                self.assertIsNone(self._start())
        self.launch.assert_not_called()

    def test_uninstalled_cli_or_wrong_executable_rejected(self):
        (self.root / "resources" / "app.asar").unlink()
        self.assertIsNone(self._start())
        self.processes.return_value = self._snapshot(executable=self.root / "node.exe")
        self.assertIsNone(self._start())
        self.processes.return_value = self._snapshot(executable="ZCode.exe")
        self.assertIsNone(self._start())
        self.launch.assert_not_called()

    def test_unsupported_environment_or_inaccessible_environment_rejected(self):
        for key in desktop_launch._UNSUPPORTED_ENV:
            with self.subTest(key=key):
                self.environment[key.lower()] = "custom"
                self.assertIsNone(self._start())
                del self.environment[key.lower()]
        self.process.environ.side_effect = psutil.AccessDenied(900)
        self.assertIsNone(self._start())
        self.launch.assert_not_called()

    def test_codex_custom_profile_environment_is_rejected(self):
        executable = self.root / "ChatGPT.exe"
        executable.touch()
        self.processes.return_value = self._snapshot(executable=executable)
        # This is a custom Codex profile selector, even with a default argv.
        # Our supported capability is the ordinary installed-app profile only.
        for key, value in (("CODEX_ELECTRON_USER_DATA_PATH", "custom-profile"),
                           ("BUILD_FLAVOR", "agent"),
                           ("CODEX_ELECTRON_AGENT_RUN_ID", "agent-run")):
            with self.subTest(key=key):
                self.environment[key] = value
                self.assertIsNone(self._start(AgentKind.CODEX))
                del self.environment[key]
        self.launch.assert_not_called()

    def test_pid_or_executable_change_during_validation_never_launches(self):
        for replacement in (self._snapshot(created=112.0),
                            self._snapshot(executable=self.root / "other.exe"),
                            self._snapshot(arguments=[str(self.executable), "--type=utility"])):
            with self.subTest(replacement=replacement):
                self.processes.side_effect = [self.process, self.process, replacement]
                self.assertIsNone(self._start())
        self.launch.assert_not_called()

    def test_launch_failure_returns_none_without_retry(self):
        self.launch.side_effect = OSError("launch denied")
        self.assertIsNone(self._start())
        self.launch.assert_called_once()

    def test_owned_handle_polls_and_cancels_only_its_launcher_without_waiting(self):
        child = self.launch.return_value
        child.poll.return_value = None
        handle = self._start()
        self.assertIsNone(handle.poll())
        handle.cancel()
        handle.cancel()
        child.terminate.assert_called_once_with()
        self.processes.return_value.terminate.assert_not_called()
        child.wait.assert_not_called()
        child.communicate.assert_not_called()
        child.poll.return_value = 0
        self.assertEqual(handle.poll(), 0)

    def test_owned_handle_does_not_cancel_an_already_exited_launcher(self):
        child = self.launch.return_value
        child.poll.return_value = 0
        handle = self._start()
        handle.cancel()
        child.terminate.assert_not_called()
        child.wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
