"""Windows 进程退出事件观察器（v4plan §4.3）。

一个 daemon 线程 + WaitForMultipleObjects 阻塞等待：
  * ``OpenProcess(SYNCHRONIZE)`` —— 只申请同步等待所需的最小权限，
    不请求 VM_READ/VM_WRITE，不读 Agent 内存；
  * 一个 control event handle：register/unregister/stop 后唤醒线程、
    重建 wait set，避免任何轮询；
  * process handle signal 只产生**候选** ProcessExitEvent；Monitor 收到后
    还要核对 key/process_token 仍是当前 exact incarnation（防竞态）；
  * handle 打不开（权限不足/进程刚好已死）不直接宣布退出，返回 False，
    由下一轮 authoritative census 兜底。

依据（Microsoft Learn，实现合同）：
  * Terminating a Process：进程终止时 process object 变为 signaled，
    释放所有等待线程；
  * WaitForMultipleObjects 支持 process handle，单次最多
    MAXIMUM_WAIT_OBJECTS(=64) 个；本 watcher 注册上限 16（与 Monitor
    的 process/pane 上限同级），远低于系统上限，不需要 per-Agent 线程。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import threading
import time
from dataclasses import dataclass

kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None

SYNCHRONIZE = 0x00100000
INFINITE = 0xFFFFFFFF
WAIT_FAILED = 0xFFFFFFFF
WAIT_OBJECT_0 = 0

# 与 Monitor 的 process/terminal 上限同级；远低于 MAXIMUM_WAIT_OBJECTS=64
MAX_WATCHED = 16


@dataclass(frozen=True)
class ProcessExitEvent:
    """一个已 signal 的进程句柄对应的候选退出事件。

    key/process_token 来自注册时的 AgentInstance；Monitor 必须核对
    它们仍匹配当前实例后才允许 _commit_exit。
    """
    key: str
    pid: int
    process_token: str
    timestamp: float


if kernel32 is not None:
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL,
                                      wt.LPCWSTR]
    kernel32.CreateEventW.restype = wt.HANDLE
    kernel32.SetEvent.argtypes = [wt.HANDLE]
    kernel32.SetEvent.restype = wt.BOOL
    kernel32.WaitForMultipleObjects.argtypes = [
        wt.DWORD, ctypes.POINTER(wt.HANDLE), wt.BOOL, wt.DWORD]
    kernel32.WaitForMultipleObjects.restype = wt.DWORD


class WindowsExitWatcher:
    """单个阻塞等待线程管理所有 Windows Agent 进程句柄。

    不是每 Agent 一个线程；空闲时 CPU 为零（纯阻塞等待）。
    线程安全：register/unregister/drain/stop 可从任意线程调用。
    """

    def __init__(self, max_watched: int = MAX_WATCHED):
        self.max_watched = max(1, int(max_watched))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # key -> (handle, pid, process_token)
        self._entries: dict[str, tuple[int, int, str]] = {}
        self._events: list[ProcessExitEvent] = []
        self._control: int = 0
        self.started = False
        self.open_failures = 0

    # ------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        if kernel32 is None:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        if self._stop.is_set():
            return False
        self._control = kernel32.CreateEventW(None, False, False, None)
        if not self._control:
            return False
        self._thread = threading.Thread(
            target=self._run, name="deskpet-exit-watch", daemon=True)
        self._thread.start()
        self.started = True
        return True

    def stop(self) -> None:
        self._stop.set()
        control = self._control
        if control:
            kernel32.SetEvent(control)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
        self._thread = None
        with self._lock:
            for _key, (handle, _pid, _token) in list(self._entries.items()):
                if handle:
                    kernel32.CloseHandle(handle)
            self._entries.clear()
        if control:
            kernel32.CloseHandle(control)
            self._control = 0
        self.started = False

    # ------------------------------------------------------------ 注册
    def register(self, instance) -> bool:
        """注册一个 Windows Agent 实例；句柄打不开/超上限返回 False。

        返回 False 绝不代表进程已退出——退出判定只能来自 signal 事件
        或 authoritative census（fail-closed）。
        """
        if kernel32 is None or not self.started:
            return False
        key = str(getattr(instance, "key", ""))
        pid = int(getattr(instance, "pid", 0) or 0)
        token = str(getattr(instance, "process_token", "") or "")
        if not key or pid <= 0:
            return False
        with self._lock:
            if key in self._entries:
                return True   # 幂等
            if len(self._entries) >= self.max_watched:
                return False  # census 兜底
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            self.open_failures += 1
            return False
        with self._lock:
            self._entries[key] = (int(handle), pid, token)
        kernel32.SetEvent(self._control)
        return True

    def unregister(self, key: str) -> None:
        with self._lock:
            entry = self._entries.pop(key, None)
        if entry is not None and entry[0]:
            kernel32.CloseHandle(entry[0])
        if self._control:
            kernel32.SetEvent(self._control)

    def drain(self) -> list[ProcessExitEvent]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def watched_count(self) -> int:
        with self._lock:
            return len(self._entries)

    # ------------------------------------------------------------ 等待线程
    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                items = list(self._entries.items())
            handles = [self._control] + [h for (_k, (h, _p, _t)) in items]
            count = len(handles)
            if count <= 1:
                # 只有 control：INFINITE 阻塞等待（v4.1.1 §10.2）。
                # register/unregister/stop 都会 SetEvent(control) 安全唤醒，
                # 不存在固定 200ms 周期唤醒——空闲时 CPU 为零。
                arr = (wt.HANDLE * 1)(self._control)
                kernel32.WaitForMultipleObjects(1, arr, False, INFINITE)
                continue
            arr = (wt.HANDLE * count)(*handles)
            index = kernel32.WaitForMultipleObjects(count, arr, False, INFINITE)
            if index == WAIT_FAILED:
                time.sleep(0.2)   # 等待失败（罕见错误路径）：短暂退避后重建
                continue
            i = index - WAIT_OBJECT_0
            if i == 0:
                continue         # control：重读注册表（含 stop 检查）
            if i < 1 or i >= count:
                continue
            key = items[i - 1][0]
            with self._lock:
                entry = self._entries.pop(key, None)
            if entry is None:
                continue
            handle, pid, token = entry
            if handle:
                kernel32.CloseHandle(handle)
            # signal 后只产生候选事件；Monitor 再核对 exact incarnation
            with self._lock:
                self._events.append(ProcessExitEvent(
                    key=key, pid=pid, process_token=token,
                    timestamp=time.time()))
