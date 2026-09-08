"""WindowsExitWatcher + 退出生命周期测试（v4plan §18.1）。

真实 Win32 语义：spawn 短命子进程注册句柄、观察 signal 事件。
"""
from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.models import AgentInstance, AgentKind
from agents.process_watch import WindowsExitWatcher


def _instance(pid: int, token: str = "1.0") -> AgentInstance:
    return AgentInstance(AgentKind.CODEX, pid, "windows",
                         process_token=token, started_at=time.time())


def _spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@unittest.skipIf(sys.platform != "win32", "WindowsExitWatcher 仅 Windows")
class WindowsExitWatcherTests(unittest.TestCase):
    def setUp(self):
        self.watcher = WindowsExitWatcher()
        self.assertTrue(self.watcher.start())

    def tearDown(self):
        self.watcher.stop()

    def test_exit_signal_produces_exact_event(self):
        proc = _spawn_sleeper()
        try:
            inst = _instance(proc.pid, token=str(time.time()))
            self.assertTrue(self.watcher.register(inst))
            proc.terminate()
            proc.wait(timeout=10)
            deadline = time.time() + 10
            events = []
            while time.time() < deadline:
                events = self.watcher.drain()
                if events:
                    break
                time.sleep(0.1)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].key, inst.key)
            self.assertEqual(events[0].pid, proc.pid)
            self.assertEqual(events[0].process_token, inst.process_token)
            # 句柄已移除：不会再重复报同一退出
            time.sleep(0.2)
            self.assertEqual(self.watcher.drain(), [])
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_register_is_idempotent_and_unregister_closes(self):
        proc = _spawn_sleeper()
        try:
            inst = _instance(proc.pid)
            self.assertTrue(self.watcher.register(inst))
            self.assertTrue(self.watcher.register(inst))   # 幂等
            self.assertEqual(self.watcher.watched_count(), 1)
            self.watcher.unregister(inst.key)
            self.assertEqual(self.watcher.watched_count(), 0)
            proc.terminate()
            proc.wait(timeout=10)
            time.sleep(0.5)
            self.assertEqual(self.watcher.drain(), [])     # 已注销：无事件
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_register_missing_pid_fails_closed(self):
        # 不存在的 PID：OpenProcess 失败 → False，且绝不产生退出事件
        self.assertFalse(self.watcher.register(_instance(99999999)))
        self.assertEqual(self.watcher.watched_count(), 0)
        self.assertEqual(self.watcher.drain(), [])

    def test_capacity_cap_returns_false(self):
        watcher = WindowsExitWatcher(max_watched=1)
        self.assertTrue(watcher.start())
        try:
            p1 = _spawn_sleeper()
            p2 = _spawn_sleeper()
            try:
                self.assertTrue(watcher.register(_instance(p1.pid)))
                # 超上限：False（不判退出，census 兜底）
                self.assertFalse(watcher.register(_instance(p2.pid)))
                self.assertEqual(watcher.watched_count(), 1)
            finally:
                for p in (p1, p2):
                    if p.poll() is None:
                        p.kill()
        finally:
            watcher.stop()

    def test_stop_closes_all_handles(self):
        proc = _spawn_sleeper()
        try:
            self.watcher.register(_instance(proc.pid))
            self.watcher.stop()
            self.assertEqual(self.watcher.watched_count(), 0)
            self.assertFalse(self.watcher.started)
        finally:
            if proc.poll() is None:
                proc.kill()


if __name__ == "__main__":
    unittest.main()
