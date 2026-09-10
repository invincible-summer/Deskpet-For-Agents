"""ConfigSaveCoordinator（v4.3 §8.2；v4.3.1 DP43-R02 重写）：配置内存
变更立即，磁盘写入 debounce + 最多一个 transient worker 的真正
single-writer 状态机。

保存流程（全部 Tk 主线程发起；磁盘 I/O 只在 worker）：

    setting changed → Config.set（revision+1, dirty=True）
    → 650ms debounce
    → snapshot_for_save()（UI，短临界区 deepcopy）
    → 最多一个 deskpet-config-save transient 线程（带 token）
    → worker write_snapshot（temp/flush/backup/replace，绝不持锁）
    → bridge poll result → acknowledge_save
    → 写期间又有新 revision → 重新 debounce 保存最新快照

v4.3.1 修复的合同（DP43-R02）：

  * worker harvest 后 `_worker` 引用清除（dead Thread 不再让
    pending()==True 永久成立，bridge 空闲时回到 200/500ms 档）；
  * 同一失败 revision 不自动无限重试——只有新用户修改（revision
    前进）或显式 request_save(immediate=True, force=True) 才再次
    提交，权限/磁盘满场景不再形成永久 retry loop；
  * shutdown 使用绝对 deadline 的 bounded 流程：有活跃 writer 时
    只等它到 deadline；deadline 内无 writer 且 dirty 才启动恰好
    一个最新快照 writer；到期不再启动第二个 writer、绝不在存活
    writer 之后再同步 commit（避免双 writer 数据竞争）；
  * worker 唯一职责是 write_snapshot → publish immutable result →
    return；不触碰 Tk、不 acknowledge、不请求下一次保存。

Monitor/source/privacy 开关同样走此保存器：内存 set 立即生效，
磁盘延迟不影响用户隐私意图的运行期效果。

兼容性：无 snapshot_for_save API 的 Config（测试 fake）退化为
debounce 到期后同步 save()——假 config 无磁盘 I/O，语义不变。
"""
from __future__ import annotations

import threading
import time

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
        # worker 身份三元组：token 用于识别结果归属，revision 用于
        # stale snapshot 裁决（DP43-R02 §6.6）
        self._worker_token: int | None = None
        self._worker_revision: int | None = None
        self._result_lock = threading.Lock()
        self._result = None            # (token, revision, ConfigSaveResult)
        # 最近一次失败且未被新 revision 取代的 revision；同一 revision
        # 不自动重试（None = 无失败门）
        self._failed_revision: int | None = None
        self._next_token = 1
        self._stopping = False
        self._async = hasattr(config, "snapshot_for_save")
        # production 轻量统计（§19.1）
        self.save_count = 0
        self.last_result = None

    # ------------------------------------------------------------ UI 线程入口
    def request_save(self, *, immediate: bool = False,
                     force: bool = False) -> None:
        """（重）启动保存。

        normal（immediate=False）：650ms debounce，多次请求天然合并。
        explicit retry（immediate=True）：取消 debounce，UI 线程做短
        临界区 snapshot 后立即启动 worker 并返回——仍不在 Tk 写盘。
        已有活跃 writer 时只保留 Config.dirty/revision，绝不创建第二
        个 writer；旧 writer harvest 后由 poll 保存最新 revision。
        force 目前只用于文档化显式重试意图（同 revision 允许再提交，
        由 poll 的 failed gate 区分自动/手动路径）。
        """
        if self._stopping:
            return
        if immediate:
            self._cancel_timer()
            self._drain_result()
            if self._worker_busy():
                return
            self._start_worker()
            return
        self._cancel_timer()
        self._timer = self.root.after(SAVE_DEBOUNCE_MS, self._fire)

    def pending(self) -> bool:
        """bridge 判断是否有待收割/进行中的保存工作（O(1)）。

        dead Thread 对象不算 pending（DP43-R02 问题 A：worker 完成且
        结果收割后必须回到 False，否则 bridge 永久停在 125ms 档）。
        """
        return (self._timer is not None
                or self._worker_busy()
                or self._result is not None)

    def poll(self) -> None:
        """bridge 调用（UI 线程）：收割 worker 结果；dirty 又出现 → 重新安排。

        同一失败 revision 不自动重试（DP43-R02 问题 B）：新用户修改
        使 revision 前进后才重新有资格保存。
        """
        if self._stopping:
            return
        self._drain_result()
        if (self._timer is None
                and not self._worker_busy()
                and getattr(self.config, "dirty", False)):
            failed = self._failed_revision
            if (failed is not None
                    and failed == getattr(self.config, "revision", None)):
                return   # 同一失败 revision：等新修改或显式 retry
            self.request_save()

    def flush_for_shutdown(self, timeout: float = FLUSH_TIMEOUT_SEC) -> bool:
        """退出路径（DP43-R02 §6.14）：绝对 deadline 的 bounded 流程。

        bounded shutdown + single writer 优先于"再赌一次同步写"：
        deadline 到期时不再启动另一个 writer、绝不在可能仍存活的
        writer 之后同步 commit（那会制造双 writer 竞争）。返回值 =
        配置当前是否已确认落盘（dirty==False）。
        """
        deadline = time.monotonic() + max(0.0, timeout)
        self._stopping = True
        self._cancel_timer()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        # 当前就是 UI 线程：直接 ack（不经 bridge）
        self._drain_result()
        if (getattr(self.config, "dirty", False)
                and not self._worker_busy()
                and time.monotonic() < deadline):
            # 恰好一个最新快照 writer，只在剩余时间内等待
            if self._async:
                self._start_worker()
                worker = self._worker
                if worker is not None:
                    worker.join(
                        timeout=max(0.0, deadline - time.monotonic()))
                self._drain_result()
            else:
                # 测试 fake config：无磁盘 I/O，直接同步保存
                try:
                    self.last_result = self.config.save()
                    self.save_count += 1
                except Exception:
                    pass
        return not getattr(self.config, "dirty", False)

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
        self._start_worker()

    def _start_worker(self):
        if not self._async:
            # 测试 fake config：无磁盘，debounce 到期同步保存
            try:
                self.last_result = self.config.save()
                self.save_count += 1
            except Exception:
                pass
            return
        revision, data = self.config.snapshot_for_save()
        token = self._next_token
        self._next_token += 1
        self._worker_token = token
        self._worker_revision = revision
        self._worker = threading.Thread(
            target=self._work, args=(token, revision, data),
            name="deskpet-config-save", daemon=True)
        self._worker.start()

    def _work(self, token: int, revision: int, data: dict) -> None:
        """worker 唯一职责：写盘 → 发布 immutable result → return。"""
        try:
            result = self.config.write_snapshot(revision, data)
        except Exception as exc:   # worker 绝不抛出到线程外
            result = type("R", (), {"ok": False, "path": "", "error": str(exc)})()
        with self._result_lock:
            self._result = (token, revision, result)

    def _drain_result(self) -> None:
        with self._result_lock:
            item, self._result = self._result, None
        if item is None:
            return
        token, revision, result = item
        if token == self._worker_token:
            # harvest：清 worker 身份（pending 回到 False 的关键）
            self._worker = None
            self._worker_token = None
            self._worker_revision = None
        self.save_count += 1
        self.last_result = result
        try:
            self.config.acknowledge_save(revision, result)
        except Exception:
            pass
        if result.ok:
            if self._failed_revision == revision:
                self._failed_revision = None
        else:
            try:
                current = self.config.revision
            except Exception:
                current = None
            if revision == current:
                # 最新 revision 保存失败：挂起自动重试（显式 retry/新修改解除）
                self._failed_revision = revision
        if self.on_result is not None:
            try:
                self.on_result(result)
            except Exception:
                pass
