"""winkeys HWND 验证测试：唤起前防 HWND 复用（V3.1 审核 §16）。"""
from __future__ import annotations
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import winkeys


class _FakeUser32:
    """只实现 validate/raise 所需的 user32 表面。"""

    def __init__(self, is_window=True, pid=50, cls="CASCADIA_HOSTING_WINDOW_CLASS"):
        self.is_window = is_window
        self.pid = pid
        self.cls = cls
        self.calls: list[str] = []

    def IsWindow(self, hwnd):
        return self.is_window

    def GetWindowThreadProcessId(self, hwnd, out):
        target = getattr(out, "_obj", out)   # byref(CArgObject) 解引用
        target.value = self.pid
        return 1

    def GetClassNameW(self, hwnd, buf, n):
        buf.value = self.cls
        return len(self.cls)

    def IsIconic(self, hwnd):
        return False

    def IsWindowVisible(self, hwnd):
        return True

    def ShowWindow(self, hwnd, cmd):
        return True

    def SetForegroundWindow(self, hwnd):
        self.calls.append(f"foreground:{int(hwnd)}")
        return True

    def GetForegroundWindow(self):
        return 11 if self.calls else 0

    def FlashWindowEx(self, info):
        return True


class ValidateWindowTests(unittest.TestCase):
    def _binding(self, hwnd=11, pid=50, cls="CASCADIA_HOSTING_WINDOW_CLASS"):
        return type("B", (), {"hwnd": hwnd, "window_pid": pid,
                              "window_class": cls})()

    def test_valid_window_passes(self):
        fake = _FakeUser32()
        with patch.object(winkeys, "user32", fake):
            self.assertTrue(winkeys.validate_terminal_window(self._binding()))

    def test_dead_hwnd_rejected(self):
        fake = _FakeUser32(is_window=False)
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.validate_terminal_window(self._binding()))

    def test_pid_changed_rejected(self):
        # HWND 被复用到其他进程
        fake = _FakeUser32(pid=999)
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.validate_terminal_window(self._binding()))

    def test_class_changed_rejected(self):
        fake = _FakeUser32(cls="Notepad")
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.validate_terminal_window(self._binding()))

    def test_raise_terminal_refuses_stale_hwnd(self):
        fake = _FakeUser32(pid=999)
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.raise_terminal(self._binding()))
        self.assertEqual(fake.calls, [])   # 绝不尝试切换焦点

    def test_raise_terminal_success_path(self):
        fake = _FakeUser32()
        with patch.object(winkeys, "user32", fake):
            self.assertTrue(winkeys.raise_terminal(self._binding()))
        self.assertIn("foreground:11", fake.calls)


if __name__ == "__main__":
    unittest.main()
