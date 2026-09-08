"""Autostart V4.1 测试（v4plan §18.5）：missing/stale/healthy、
写入后重读验证、>260 字符显式失败、repair。"""
from __future__ import annotations
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pet import autostart
from pet.autostart import (
    AutostartState,
    MAX_COMMAND_LEN,
    expected_command,
)


class _FakeWinreg:
    """winreg 模块假体：value 存内存，可注入写失败。"""

    HKEY_CURRENT_USER = 0x80000001
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1

    def __init__(self, initial=None):
        self.values = dict(initial or {})
        self.write_error = None

    def OpenKey(self, root, path, reserved=0, access=KEY_READ):
        if self.write_error and access & self.KEY_SET_VALUE:
            raise OSError(self.write_error)
        return _FakeKey(self)

    # 模块级函数形态：winreg.QueryValueEx(key, name) 等
    @staticmethod
    def QueryValueEx(key, name):
        if name not in key.reg.values:
            raise FileNotFoundError(name)
        return key.reg.values[name], 1

    @staticmethod
    def SetValueEx(key, name, reserved, dtype, value):
        key.reg.values[name] = value

    @staticmethod
    def DeleteValue(key, name):
        if name not in key.reg.values:
            raise FileNotFoundError(name)
        key.reg.values.pop(name, None)


class _FakeKey:
    def __init__(self, reg):
        self.reg = reg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class AutostartTests(unittest.TestCase):
    def test_expected_command_shape(self):
        cmd = expected_command()
        self.assertIn('"', cmd)
        self.assertIn("main.py", cmd)
        self.assertLessEqual(len(cmd), MAX_COMMAND_LEN)

    def test_missing_state(self):
        fake = _FakeWinreg()
        with patch.object(autostart, "winreg", fake):
            st = autostart.status()
        self.assertEqual(st.state, AutostartState.MISSING)
        self.assertFalse(st.registered)
        self.assertFalse(st.healthy)

    def test_healthy_state_and_verify_after_write(self):
        fake = _FakeWinreg()
        with patch.object(autostart, "winreg", fake):
            result = autostart.set_enabled(True)
            self.assertTrue(result.ok, result.reason)
            self.assertTrue(result.enabled)
            st = autostart.status()
        self.assertEqual(st.state, AutostartState.HEALTHY)
        self.assertEqual(st.registered_command, st.expected_command)

    def test_stale_command_detected(self):
        fake = _FakeWinreg({"DeskPet": '"C:\\old\\pythonw.exe" "C:\\old\\main.py"'})
        with patch.object(autostart, "winreg", fake):
            st = autostart.status()
        self.assertEqual(st.state, AutostartState.STALE)
        self.assertTrue(st.registered)
        self.assertFalse(st.healthy)
        self.assertEqual(st.reason, "stale-command")

    def test_repair_rewrites_to_expected(self):
        fake = _FakeWinreg({"DeskPet": '"C:\\old\\pythonw.exe" "C:\\old\\main.py"'})
        with patch.object(autostart, "winreg", fake):
            result = autostart.repair()
            self.assertTrue(result.ok, result.reason)
            st = autostart.status()
        self.assertEqual(st.state, AutostartState.HEALTHY)

    def test_disable_removes_value(self):
        fake = _FakeWinreg({"DeskPet": expected_command()})
        with patch.object(autostart, "winreg", fake):
            result = autostart.set_enabled(False)
            self.assertTrue(result.ok)
            self.assertFalse(result.enabled)
            st = autostart.status()
        self.assertEqual(st.state, AutostartState.MISSING)

    def test_write_failure_reported_not_swallowed(self):
        fake = _FakeWinreg({"DeskPet": expected_command()})
        fake.write_error = "access denied"
        with patch.object(autostart, "winreg", fake):
            result = autostart.set_enabled(True)
        self.assertFalse(result.ok)
        self.assertIn("access denied", result.reason)

    def test_overlong_command_fails_explicitly(self):
        with patch.object(autostart, "expected_command",
                          return_value='"' + "x" * 400 + '"'):
            fake = _FakeWinreg()
            with patch.object(autostart, "winreg", fake):
                result = autostart.set_enabled(True)
        self.assertFalse(result.ok)
        self.assertIn("260", result.reason)

    def test_interpreter_missing_fails_validation(self):
        fake_cmd = '"Z:\\definitely\\missing\\pythonw.exe" "Z:\\missing\\main.py"'
        with patch.object(autostart, "expected_command",
                          return_value=fake_cmd):
            fake = _FakeWinreg()
            with patch.object(autostart, "winreg", fake):
                result = autostart.set_enabled(True)
                st = autostart.status()
        self.assertFalse(result.ok)
        self.assertIn("missing", result.reason)

    def test_toggle_repairs_stale(self):
        fake = _FakeWinreg({"DeskPet": '"C:\\old\\pythonw.exe" "C:\\old\\main.py"'})
        with patch.object(autostart, "winreg", fake):
            result = autostart.toggle()
            self.assertTrue(result.ok)
            self.assertEqual(autostart.status().state,
                             AutostartState.HEALTHY)


if __name__ == "__main__":
    unittest.main()
