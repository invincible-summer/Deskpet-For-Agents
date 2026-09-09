"""ConfigSaveCoordinator（v4.3 §8.2）：配置内存变更立即，磁盘写入
debounce + 最多一个 transient worker。

保存流程（全部 Tk 主线程发起；磁盘 I/O 只在 worker）：

    setting changed → Config.set（revision+1, dirty=True）
    → 650ms debounce
    → snapshot_for_save()（UI，短临界区 deepcopy）
    → 最多一个 deskpet-config-save transient 线程
    → worker write_snapshot（temp/flush/backup/replace，绝不持锁）
    → bridge poll result → acknowledge_save
    → 写期间又有新 revision → 重新 debounce 保存最新快照

最大 worker 数恒为 1。退出走 flush_for_shutdown(1.5s)：有界等待
worker，超时不跨线程操作 Tk、不无限阻塞；仍有 dirty 时做一次同步
commit 兜底（与 v4.2 退出保存等价的有界磁盘写）。

Monitor/source/privacy 开关同样走此保存器：内存 set 立即生效，
磁盘延迟不影响用户隐私意图的运行期效果。

兼容性：无 snapshot_for_save API 的 Config（测试 fake）退化为
debounce 到期后同步 save()——假 config 无磁盘 I/O，语义不变。
"""
from __future__ import annotations

import threading

SAVE_DEBOUNCE_MS = 650
FLUSH_TIMEOUT_SEC = 1.5


class ConfigSaveCoordinator:
    """一个 debounce after + 最多一个 worker + 一个待收割结果槽。"""

    def __init__(self, root, config, *, on_result=None):
        self.root = root
        self.config = config
        self.on_result = on_result     # ack 后的 UI 回调（如失败 toast）
        self._timer = None             # Tk after token（UI 线程）
        self._worker: threading.Thread | None = None
        self._result_lock = threading.Lock()
        self._result = None            # (revision, ConfigSaveResult)
        self._stopping = False
        self._async = hasattr(config, "snapshot_for_save")
        # production 轻量统计（§19.1）
        self.save_count = 0
        self.last_result = None

    # ------------------------------------------------------------ UI 线程入口
    def request_save(self) -> None:
        """（重）启动 650ms debounce；多次请求天然合并。"""
        if self._stopping:
            return
        self._cancel_timer()
        self._timer = self.root.after(SAVE_DEBOUNCE_MS, self._fire)

    def pending(self) -> bool:
        """bridge 判断是否有待收割/进行中的保存工作（O(1)）。"""
        return (self._timer is not None
                or self._worker is not None
                or self._result is not None)

    def poll(self) -> None:
        """bridge 调用（UI 线程）：收割 worker 结果；dirty 又出现 → 重新安排。"""
        if self._stopping:
            return
        self._drain_result()
        if (self._timer is None
                and not self._worker_busy()
                and getattr(self.config, "dirty", False)):
            self.request_save()

    def flush_for_shutdown(self, timeout: float = FLUSH_TIMEOUT_SEC) -> None:
        """退出路径：取消 debounce；有界等待 worker；必要时同步兜底。"""
        self._stopping = True
        self._cancel_timer()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(0.0, timeout))
        # 当前就是 UI 线程：直接 ack（不经 bridge）
        self._drain_result()
        if getattr(self.config, "dirty", False):
            try:
                self.config.commit(fsync=True)
            except Exception:
                pass

    def stop(self) -> None:
        self._stopping = True
        self._cancel_timer()

    # ------------------------------------------------------------ 内部
    def _cancel_timer(self):
        token = self._timer
        self._timer = None
        if token is not None:
            try:
                self.root.after_cancel(token)
            except Exception:
                pass

    def _worker_busy(self) -> bool:
        worker = self._worker
        return worker is not None and worker.is_alive()

    def _fire(self):
        self._timer = None
        if self._stopping:
            return
        if self._worker_busy():
            # worker 仍在写：完成后的 poll 会看到新的 dirty 并重新安排
            return
        self._drain_result()   # 不让旧结果被覆盖丢失
        if not self._async:
            # 测试 fake config：无磁盘，debounce 到期同步保存
            try:
                self.last_result = self.config.save()
                self.save_count += 1
            except Exception:
                pass
            return
        revision, data = self.config.snapshot_for_save()
        self._worker = threading.Thread(
            target=self._work, args=(revision, data),
            name="deskpet-config-save", daemon=True)
        self._worker.start()

    def _work(self, revision: int, data: dict) -> None:
        try:
            result = self.config.write_snapshot(revision, data)
        except Exception as exc:   # worker 绝不抛出到线程外
            result = type("R", (), {"ok": False, "path": "", "error": str(exc)})()
        with self._result_lock:
            self._result = (revision, result)

    def _drain_result(self) -> None:
        with self._result_lock:
            item, self._result = self._result, None
        if item is None:
            return
        revision, result = item
        self.save_count += 1
        self.last_result = result
        try:
            self.config.acknowledge_save(revision, result)
        except Exception:
            pass
        if self.on_result is not None:
            try:
                self.on_result(result)
            except Exception:
                pass
