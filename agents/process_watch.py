"""Windows 进程退出事件观察器（v4plan §4.3；v4.2.3 §3 handle ownership）。

一个 daemon 线程 + WaitForMultipleObjects 阻塞等待：
  * ``OpenProcess(SYNCHRONIZE)`` —— 只申请同步等待所需的最小权限，
    不请求 VM_READ/VM_WRITE，不读 Agent 内存；
  * 一个 control event handle：register/unregister/stop 后唤醒线程、
    重建 wait set，避免任何轮询；
  * process handle signal 只产生**候选** ProcessExitEvent；Monitor 收到后
    还要核对 key/process_token 仍是当前 exact incarnation（防竞态）；
  * handle 打不开（权限不足/进程刚好已死）不直接宣布退出，返回 False，
    由下一轮 authoritative census 兜底。

handle ownership（v4.2.3 §3.1，不可破坏的规则）：
  * process handle 一旦进入 pending/active 集合，只有
    ``deskpet-exit-watch`` 线程可以 CloseHandle；
  * 其他线程（register/unregister/stop 调用方）只发布命令并
    SetEvent(control)，绝不跨线程 CloseHandle——
    WaitForMultipleObjects pending 时关闭其 handle 属 undefined
    behavior（Microsoft Learn: WaitForMultipleObjects）；
  * register 在调用线程 OpenProcess 后、发布到 _pending_add 之前仍
    拥有该 handle；检测到 duplicate/capacity/stop 时可安全 close 自己
    尚未发布的 handle；
  * stop() 在 watcher 线程成功 join 后才关闭 control handle；join
    超时则留下 daemon/control handle 由进程退出时 OS 回收，绝不制造
    pending-wait CloseHandle UB。

性能合同（v4.2.3 §3.2）：
  * 没有 Agent：仍 WaitForMultipleObjects(1, [control], INFINITE)，
    零固定轮询；
  * 有 Agent：单次 WaitForMultipleObjects，无 per-Agent 线程；
  * WAIT_FAILED 仅保留罕见错误 200ms backoff；
  * watched_count() 返回 logical active/pending 数，不因异步命令闪 0。
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


@dataclass(frozen=True)
class _PendingWatch:
    """一个已打开、所有权归属 watcher 线程的 process handle。"""
    key: str
    handle: int
    pid: int
    process_token: str


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
    线程安全：register/unregister/drain/stop 可从任意线程调用；
    process handle 只由 watcher 线程关闭（v4.2.3 §3.1）。
    """

    def __init__(self, max_watched: int = MAX_WATCHED):
        self.max_watched = max(1, int(max_watched))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # active set：仅 watcher 线程读写（handle 所有权）
        self._entries: dict[str, _PendingWatch] = {}
        # 命令队列：任意线程发布，watcher 线程消费
        self._pending_add: dict[str, _PendingWatch] = {}
        self._pending_remove: set[str] = set()
        # logical view（active+pending）：watched_count 的稳定来源
        self._logical_keys: set[str] = set()
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

    def request_stop(self) -> None:
        """只发停止信号（O(1)）：stop event + SetEvent(control) 唤醒等待。

        DP43-R17：signal 与 join 分离——运行期任何线程可以调用；
        不做任何 join/handle 操作。
        """
        self._stop.set()
        control = self._control
        if control:
            kernel32.SetEvent(control)

    def join_for_shutdown(self, timeout: float = 3.0) -> bool:
        """有界回收 watcher 线程（timeout 来自 App 全局 deadline 的
        剩余量）；返回线程是否退出。

        线程退出（不再有任何 pending wait）后才由本调用线程关闭
        register/stop 竞态中未及处理的残留 pending handle；超时绝不
        跨线程 CloseHandle 可能仍在 WaitForMultipleObjects 中的
        process/control handle（UB）——daemon 线程 + handle 由 OS 在
        进程退出时回收。
        """
        thread = self._thread
        exited = True
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
            exited = not thread.is_alive()
        self._thread = None
        if not exited:
            self.started = False
            return False
        # watcher 线程已退出：关闭残留 pending handle 是安全的
        with self._lock:
            leftovers = list(self._pending_add.values())
            self._pending_add.clear()
            self._pending_remove.clear()
            self._logical_keys.clear()
        for entry in leftovers:
            if entry.handle:
                kernel32.CloseHandle(entry.handle)
        self._entries = {}
        control = self._control
        if control:
            kernel32.CloseHandle(control)
            self._control = 0
        self.started = False
        return True

    def stop(self, timeout: float = 3.0) -> None:
        """兼容薄 wrapper：request_stop + bounded join（测试/旧入口）。"""
        self.request_stop()
        self.join_for_shutdown(timeout)

    # ------------------------------------------------------------ 注册
    def register(self, instance) -> bool:
        """注册一个 Windows Agent 实例；句柄打不开/超上限返回 False。

        返回 False 绝不代表进程已退出——退出判定只能来自 signal 事件
        或 authoritative census（fail-closed）。
        调用线程 OpenProcess 后立即把所有权转移给 _pending_add；转移后
        本线程绝不再 close 该 handle。
        """
        if kernel32 is None or not self.started:
            return False
        key = str(getattr(instance, "key", ""))
        pid = int(getattr(instance, "pid", 0) or 0)
        token = str(getattr(instance, "process_token", "") or "")
        if not key or pid <= 0:
            return False
        with self._lock:
            if key in self._logical_keys:
                return True   # 幂等
            if len(self._logical_keys) >= self.max_watched:
                return False  # census 兜底
            # 预留 logical 槽位（防止并发 register 超上限）
            self._logical_keys.add(key)
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            self.open_failures += 1
            with self._lock:
                self._logical_keys.discard(key)
            return False
        with self._lock:
            if self._stop.is_set():
                # stop 已发生且 watcher 可能已退出：handle 尚未发布，
                # 调用线程仍拥有它，可安全关闭。
                self._logical_keys.discard(key)
                close_now = True
            else:
                self._pending_add[key] = _PendingWatch(
                    key=key, handle=int(handle), pid=pid,
                    process_token=token)
                close_now = False
        if close_now:
            kernel32.CloseHandle(handle)
            return False
        kernel32.SetEvent(self._control)
        return True

    def unregister(self, key: str) -> None:
        """只发布 remove 命令；绝不 CloseHandle（v4.2.3 §3.1）。"""
        with self._lock:
            self._logical_keys.discard(key)
            self._pending_remove.add(key)
        control = self._control
        if control:
            kernel32.SetEvent(control)

    def drain(self) -> list[ProcessExitEvent]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def watched_count(self) -> int:
        """logical active+pending 数；命令异步处理期间不闪 0。"""
        with self._lock:
            return len(self._logical_keys)

    # ------------------------------------------------------------ 等待线程
    def _drain_commands(self) -> None:
        """watcher 线程消费命令；remove → add 顺序（v4.2.3 §3.1）。"""
        with self._lock:
            removes = self._pending_remove
            self._pending_remove = set()
            adds = self._pending_add
            self._pending_add = {}
        for key in removes:
            entry = self._entries.pop(key, None)
            if entry is not None and entry.handle:
                kernel32.CloseHandle(entry.handle)
        for key, entry in adds.items():
            if key in removes:
                # register 后立刻 unregister 的竞态：从未激活，直接由
                # watcher 线程关闭。
                if entry.handle:
                    kernel32.CloseHandle(entry.handle)
                continue
            old = self._entries.get(key)
            if old is not None:
                # 重复 add（幂等保护外的防御）：只保留已有 handle
                if entry.handle:
                    kernel32.CloseHandle(entry.handle)
                continue
            self._entries[key] = entry
        # 命令消费完：logical 集合与 active 对齐（signal 路径会自行对齐）；
        # 已有更新 remove 命令的 key 不得被 realign 复活。
        with self._lock:
            for key in removes:
                self._logical_keys.discard(key)
            for key in self._entries:
                if key not in self._pending_remove:
                    self._logical_keys.add(key)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                items = list(self._entries.items())
                handles = [self._control] + [e.handle for _k, e in items]
                count = len(handles)
                if count <= 1:
                    # 只有 control：INFINITE 阻塞等待（v4.1.1 §10.2）。
                    # register/unregister/stop 都会 SetEvent(control) 安全
                    # 唤醒，不存在固定 200ms 周期唤醒——空闲时 CPU 为零。
                    arr = (wt.HANDLE * 1)(self._control)
                    kernel32.WaitForMultipleObjects(1, arr, False, INFINITE)
                    if self._stop.is_set():
                        break
                    self._drain_commands()
                    continue
                arr = (wt.HANDLE * count)(*handles)
                index = kernel32.WaitForMultipleObjects(count, arr, False,
                                                        INFINITE)
                if index == WAIT_FAILED:
                    time.sleep(0.2)   # 等待失败（罕见错误路径）：短暂退避后重建
                    continue
                i = index - WAIT_OBJECT_0
                if i == 0:
                    if self._stop.is_set():
                        break
                    self._drain_commands()
                    continue         # control：重读命令（含 stop 检查）
                if i < 1 or i >= count:
                    continue
                key = items[i - 1][0]
                entry = self._entries.pop(key, None)
                if entry is None:
                    continue
                if entry.handle:
                    kernel32.CloseHandle(entry.handle)
                # signal 后只产生候选事件；Monitor 再核对 exact incarnation
                with self._lock:
                    self._logical_keys.discard(key)
                    self._events.append(ProcessExitEvent(
                        key=key, pid=entry.pid,
                        process_token=entry.process_token,
                        timestamp=time.time()))
        finally:
            # 退出路径：watcher 线程关闭自己拥有的全部 process handle。
            # 无论正常 stop 还是异常，_entries/_pending_add 中的 handle
            # 都由本线程（唯一 owner）关闭。
            for entry in self._entries.values():
                if entry.handle:
                    kernel32.CloseHandle(entry.handle)
            self._entries.clear()
            with self._lock:
                leftovers = list(self._pending_add.values())
                self._pending_add.clear()
                self._pending_remove.clear()
                self._logical_keys.clear()
            for entry in leftovers:
                if entry.handle:
                    kernel32.CloseHandle(entry.handle)
