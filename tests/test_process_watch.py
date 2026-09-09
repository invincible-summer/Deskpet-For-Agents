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


def terminate_process(proc: subprocess.Popen):
    """统一 Popen 清理（v4.1.3 §31）：kill 后有界 wait。

    只 kill 不 wait 会让 Popen 对象在进程仍运行时被 GC——CI stderr
    出现 subprocess ResourceWarning。"""
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


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
            terminate_process(proc)

    def test_register_is_idempotent_and_unregister_async_closes(self):
        proc = _spawn_sleeper()
        try:
            inst = _instance(proc.pid)
            self.assertTrue(self.watcher.register(inst))
            self.assertTrue(self.watcher.register(inst))   # 幂等
            self.assertEqual(self.watcher.watched_count(), 1)
            self.watcher.unregister(inst.key)
            self.assertEqual(self.watcher.watched_count(), 0)
            # unregister 是异步命令：等 watcher 线程消费 remove（handle
            # 由 watcher 关闭）后再终止进程，确保不再产生事件。
            self._wait_removed(inst.key)
            proc.terminate()
            proc.wait(timeout=10)
            time.sleep(0.5)
            self.assertEqual(self.watcher.drain(), [])     # 已注销：无事件
        finally:
            terminate_process(proc)

    def _wait_removed(self, key: str, timeout: float = 5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.watcher._lock:
                drained = (key not in self.watcher._pending_add
                           and key not in self.watcher._pending_remove
                           and key not in self.watcher._logical_keys)
            if drained and key not in self.watcher._entries:
                return
            time.sleep(0.02)
        self.fail(f"remove command for {key} not drained in time")

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
                    terminate_process(p)
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
            terminate_process(proc)

    def test_churn_stress_no_crash_duplicate_or_handle_leak(self):
        """AC-EXIT-03：≥500 次真实 register/unregister/terminate churn。

        不得 access violation、WAIT_FAILED storm、重复 exit event、
        handle count 线性增长。
        """
        import ctypes

        def _own_handle_count() -> int:
            kernel32 = ctypes.windll.kernel32
            count = ctypes.c_ulong(0)
            ok = kernel32.GetProcessHandleCount(
                kernel32.GetCurrentProcess(), ctypes.byref(count))
            return count.value if ok else -1

        watcher = WindowsExitWatcher()
        self.assertTrue(watcher.start())
        baseline = _own_handle_count()
        try:
            for i in range(500):
                proc = _spawn_sleeper()
                try:
                    inst = _instance(proc.pid, token=f"{i}")
                    if not watcher.register(inst):
                        continue
                    if i % 3 == 0:
                        watcher.unregister(inst.key)
                        self._wait_removed(inst.key, timeout=2.0)
                    proc.terminate()
                    proc.wait(timeout=10)
                finally:
                    terminate_process(proc)
            time.sleep(0.5)
            drained = watcher.drain()
            # 重复事件检测：同 key 最多一个事件
            keys = [e.key for e in drained]
            self.assertEqual(len(keys), len(set(keys)))
            watcher.stop()
            self.assertEqual(watcher.watched_count(), 0)
        finally:
            watcher.stop()
            self.assertFalse(watcher.started)
        # churn + stop 后句柄数不线性增长（允许小幅噪声）
        after = _own_handle_count()
        if baseline >= 0 and after >= 0:
            self.assertLessEqual(after, baseline + 8,
                                 f"handle leak: {baseline} -> {after}")


if __name__ == "__main__":
    unittest.main()
