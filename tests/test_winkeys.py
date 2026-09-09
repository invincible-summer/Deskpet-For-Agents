"""winkeys 窗口原语测试：WindowIdentity 验证 + foreground 语义（v4plan §5.7）。"""
from __future__ import annotations
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import winkeys
from agents.models import WindowIdentity


class _FakeUser32:
    """只实现验证/唤起/闪烁所需 user32 表面（可注入 API 失败）。"""

    def __init__(self, is_window=True, pid=50, cls="CASCADIA_HOSTING_WINDOW_CLASS",
                 gwtpi_ret=1, gcnw_ret=None, iconic=False, fg_after=11):
        self.is_window = is_window
        self.pid = pid
        self.cls = cls
        self.gwtpi_ret = gwtpi_ret      # GetWindowThreadProcessId 返回值（0=失败）
        self.gcnw_ret = gcnw_ret        # GetClassNameW 返回值（None=正常）
        self.iconic = iconic
        self.fg_after = fg_after        # SetForegroundWindow 后的前台窗口
        self.calls: list[str] = []
        self.swp_calls: list[tuple] = []

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
        return self.iconic

    def IsWindowVisible(self, hwnd):
        return True

    def ShowWindow(self, hwnd, cmd):
        self.calls.append('restore')
        return True

    def SetForegroundWindow(self, hwnd):
        self.calls.append(f"foreground:{int(hwnd)}")
        return True

    def GetForegroundWindow(self):
        return self.fg_after if self.calls else 0

    def FlashWindowEx(self, info):
        self.calls.append('flash')
        return True

    def SetWindowPos(self, hwnd, insert_after, x, y, cx, cy, flags):
        self.swp_calls.append((int(hwnd), int(insert_after), x, y, cx, cy,
                               int(flags)))
        return True


def _identity(hwnd=11, pid=50, created=1000.0,
              cls="CASCADIA_HOSTING_WINDOW_CLASS") -> WindowIdentity:
    return WindowIdentity(hwnd=hwnd, pid=pid, process_created=created,
                          window_class=cls)


class _FakePsutil:
    def __init__(self, create_time=1000.0, fail=False):
        self.create_time = create_time
        self.fail = fail

    def Process(self, pid):
        if self.fail:
            raise RuntimeError("no such process")
        proc = type("P", (), {"create_time": lambda self: self.create_time})()
        proc.create_time = lambda: self.create_time
        return proc


class ValidateWindowTests(unittest.TestCase):
    def test_valid_window_passes(self):
        fake = _FakeUser32()
        with patch.object(winkeys, "user32", fake), \
             patch("psutil.Process", _FakePsutil(1000.0).Process):
            self.assertTrue(winkeys.validate_window(_identity()))

    def test_dead_hwnd_rejected(self):
        fake = _FakeUser32(is_window=False)
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.validate_window(_identity()))

    def test_pid_changed_rejected(self):
        # HWND 被复用到其他进程
        fake = _FakeUser32(pid=999)
        with patch.object(winkeys, "user32", fake), \
             patch("psutil.Process", _FakePsutil(1000.0).Process):
            self.assertFalse(winkeys.validate_window(_identity()))

    def test_class_changed_rejected(self):
        fake = _FakeUser32(cls="Notepad")
        with patch.object(winkeys, "user32", fake), \
             patch("psutil.Process", _FakePsutil(1000.0).Process):
            self.assertFalse(winkeys.validate_window(_identity()))

    def test_process_create_time_mismatch_rejected(self):
        # HWND+PID 都复用但 create_time 不同（新进程）→ 旧绑定失效
        fake = _FakeUser32()
        with patch.object(winkeys, "user32", fake), \
             patch("psutil.Process", _FakePsutil(2000.0).Process):
            self.assertFalse(winkeys.validate_window(_identity()))

    def test_process_unreadable_rejected(self):
        # psutil 读不到（进程已死/权限）：无法证明身份 → fail-closed
        fake = _FakeUser32()
        with patch.object(winkeys, "user32", fake), \
             patch("psutil.Process", _FakePsutil(fail=True).Process):
            self.assertFalse(winkeys.validate_window(_identity()))

    def test_get_window_thread_process_id_failure_rejected(self):
        fake = _FakeUser32(gwtpi_ret=0, pid=50)
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.validate_window(_identity()))

    def test_zero_owner_pid_rejected(self):
        fake = _FakeUser32(pid=0)
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.validate_window(_identity()))

    def test_get_class_name_failure_rejected(self):
        fake = _FakeUser32(gcnw_ret=0)
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.validate_window(_identity()))

    def test_missing_expected_window_pid_rejected(self):
        fake = _FakeUser32()
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.validate_window(_identity(pid=0)))

    def test_missing_expected_window_class_rejected(self):
        fake = _FakeUser32()
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.validate_window(_identity(cls="")))

    def test_missing_expected_process_created_rejected(self):
        """§9.1：process_created<=0 的期望身份 fail-closed（0 值不再穿透）。"""
        fake = _FakeUser32()
        with patch.object(winkeys, "user32", fake), \
             patch("psutil.Process", _FakePsutil(1000.0).Process):
            self.assertFalse(winkeys.validate_window(_identity(created=0.0)))


class ForegroundTests(unittest.TestCase):
    def test_foreground_denied_flashes_and_reports_false(self):
        user = _FakeUser32(fg_after=99)   # OS 拒绝：前台不是目标
        with patch.object(winkeys, "user32", user):
            self.assertFalse(winkeys.try_set_foreground(11))
            self.assertTrue(winkeys.flash_window(11))
        self.assertIn("foreground:11", user.calls)

    def test_foreground_success(self):
        user = _FakeUser32(fg_after=11)
        with patch.object(winkeys, "user32", user):
            self.assertTrue(winkeys.try_set_foreground(11))

    def test_restore_window_restores_iconic(self):
        user = _FakeUser32(iconic=True)
        with patch.object(winkeys, "user32", user):
            self.assertTrue(winkeys.restore_window(11))
        self.assertIn("restore", user.calls)

    def test_no_key_injection_surface(self):
        """V3/V4.1 不变量：winkeys 不提供任何键盘注入入口。"""
        for banned in ('send_key', 'send_input', 'keybd_event', 'SendInput',
                       'post_message', 'raise_terminal'):
            self.assertFalse(hasattr(winkeys, banned),
                             f"winkeys.{banned} 不应存在")


class ReassertZOrderTests(unittest.TestCase):
    """§10：DeskPet 自身 Pet Toplevel 的 no-activate Z-order 重声明。"""

    def _flags(self):
        return (winkeys.SWP_NOSIZE | winkeys.SWP_NOMOVE
                | winkeys.SWP_NOACTIVATE | winkeys.SWP_SHOWWINDOW)

    def test_topmost_reassert_uses_noactivate(self):
        fake = _FakeUser32()
        with patch.object(winkeys, "user32", fake):
            self.assertTrue(winkeys.reassert_window_z_order(11, topmost=True))
        hwnd, after, x, y, cx, cy, flags = fake.swp_calls[0]
        self.assertEqual((hwnd, after, x, y, cx, cy),
                         (11, winkeys.HWND_TOPMOST, 0, 0, 0, 0))
        self.assertEqual(flags, self._flags())

    def test_non_topmost_reassert_uses_hwnd_top(self):
        fake = _FakeUser32()
        with patch.object(winkeys, "user32", fake):
            self.assertTrue(winkeys.reassert_window_z_order(11, topmost=False))
        self.assertEqual(fake.swp_calls[0][1], winkeys.HWND_TOP)

    def test_dead_hwnd_reassert_fails(self):
        fake = _FakeUser32(is_window=False)
        with patch.object(winkeys, "user32", fake):
            self.assertFalse(winkeys.reassert_window_z_order(11, topmost=True))
        self.assertEqual(fake.swp_calls, [])


if __name__ == "__main__":
    unittest.main()
