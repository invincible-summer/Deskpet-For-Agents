"""winkeys HWND 验证测试：唤起前防 HWND 复用（V3.1 审核 §16）。"""
from __future__ import annotations
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import winkeys


class _FakeUser32:
    """只实现 validate/raise 所需的 user32 表面（可注入 API 失败）。"""

    def __init__(self, is_window=True, pid=50, cls="CASCADIA_HOSTING_WINDOW_CLASS",
                 gwtpi_ret=1, gcnw_ret=None):
        self.is_window = is_window
        self.pid = pid
        self.cls = cls
        self.gwtpi_ret = gwtpi_ret      # GetWindowThreadProcessId 返回值（0=失败）
        self.gcnw_ret = gcnw_ret        # GetClassNameW 返回值（None=正常）
        self.calls: list[str] = []

    def IsWindow(self, hwnd):
        return self.is_window

    def GetWindowThreadProcessId(self, hwnd, out):
        target = getattr(out, "_obj", out)   # byref(CArgObject) 解引用
        if self.gwtpi_ret:
            target.value = self.pid
        # 失败时按 Win32 契约不写输出变量（残留值不可信）
        return self.gwtpi_ret

    def GetClassNameW(self, hwnd, buf, n):
        if self.gcnw_ret is None:
            buf.value = self.cls
            return len(self.cls)
        return self.gcnw_ret               # 失败时不写 buf

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

    def test_get_window_thread_process_id_failure_rejected(self):
        # API 失败返回 0（输出变量保持不变，即使残留期望值也不可信）
        fake = _FakeUser32(gwtpi_ret=0, pid=50)
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.validate_terminal_window(self._binding()))

    def test_zero_owner_pid_rejected(self):
        # 调用成功但报 PID=0：0 是"未完成验证"，不是"匹配"
        fake = _FakeUser32(pid=0)
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.validate_terminal_window(self._binding()))

    def test_get_class_name_failure_rejected(self):
        fake = _FakeUser32(gcnw_ret=0)
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.validate_terminal_window(self._binding()))

    def test_missing_expected_window_pid_rejected(self):
        # 绑定缺期望属主 PID → 无法完成验证
        fake = _FakeUser32()
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.validate_terminal_window(
                self._binding(pid=0)))

    def test_missing_expected_window_class_rejected(self):
        # windows-terminal 绑定必须记录窗口类；缺失即拒绝
        fake = _FakeUser32()
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.validate_terminal_window(
                self._binding(cls="")))

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
