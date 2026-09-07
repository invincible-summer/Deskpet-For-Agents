"""监控线程：进程发现 + 会话 tail + 快照聚合 + 主绑定管理。

稳定性设计：
- 主绑定（primary）：默认自动选择一个实例（等待批复 > 工作中 > 最新启动），
  选中后保持粘性；用户可手动钉住（monitor.pinned），钉住实例存活期间不自动切换
- 消失宽限期：进程从扫描结果里短暂消失（扫描抖动/WSL 卡顿）不立刻判定退出
- WORKING 保持：Agent 活动后即使会话文件静默，也保持“工作中”一段时间，
  避免长思考/长命令期间闪跳回空闲
"""
import queue
import threading
import time

from .claude import ClaudeWatcher
from .codex import CodexWatcher
from .discovery import WslScanner, scan_windows
from .kimi import KimiWatcher
from .models import AgentInstance, AgentKind, Snapshot, Status
from .pi import PiWatcher

WATCHERS = {
    AgentKind.CLAUDE: ClaudeWatcher,
    AgentKind.CODEX: CodexWatcher,
    AgentKind.KIMI: KimiWatcher,
    AgentKind.PI: PiWatcher,
}

LOG_MAX = 300


class Monitor:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.snapshots: dict[str, Snapshot] = {}
        self.instances: dict[str, AgentInstance] = {}
        self.primary_key: str = ""                      # 当前主绑定
        self.log_q: queue.Queue = queue.Queue(maxsize=200)
        self._log_ring: list[str] = []
        self._wsl = WslScanner()
        self._watchers = {k: v(dict(config.get("monitor"))) for k, v in WATCHERS.items()}
        self._last_windows_scan = 0.0
        self._last_wsl_scan = 0.0
        self._instances_cache: list[AgentInstance] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._gone_since: dict[str, float] = {}         # key -> 首次消失时间
        self._active_ts: dict[str, float] = {}          # key -> 最近活动时间
        self._prev_status: dict[str, Status] = {}       # key -> 上一轮状态

    # ---- 供 UI 调用 ----
    def start(self):
        self._thread = threading.Thread(target=self._loop, name="deskpet-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def get_state(self) -> tuple[list[AgentInstance], dict[str, Snapshot]]:
        with self.lock:
            self._select_primary()
            return list(self.instances.values()), dict(self.snapshots)

    def primary_snapshot(self) -> Snapshot | None:
        _i, snaps = self.get_state()
        return snaps.get(self.primary_key) if self.primary_key else None

    def is_bound(self, key: str) -> bool:
        return key == self.primary_key

    def set_primary(self, key: str, manual: bool = True):
        """手动钉住或清除钉住（key 为空 = 恢复自动选择）。"""
        with self.lock:
            if manual:
                self.config.set("monitor.pinned", key)
                self.primary_key = key
            else:
                self.config.set("monitor.pinned", "")
                self.primary_key = ""
            self.config.save()
            self._select_primary()

    def recent_logs(self) -> list[str]:
        return list(self._log_ring)

    def trim(self):
        """定时清理：日志环、事件队列。"""
        del self._log_ring[:-80]
        try:
            while True:
                self.log_q.get_nowait()
        except queue.Empty:
            pass

    def _log(self, msg: str):
        line = time.strftime("[%H:%M:%S] ") + msg
        self._log_ring.append(line)
        if len(self._log_ring) > LOG_MAX:
            del self._log_ring[: len(self._log_ring) - LOG_MAX]
        try:
            self.log_q.put_nowait(line)
        except queue.Full:
            pass

    # ---- 主绑定选择（粘性 + 优先级 + 手动钉住） ----
    def _select_primary(self):
        alive = self.instances
        if not alive:
            self.primary_key = ""
            return
        pinned = str(self.config.get("monitor.pinned") or "")
        if pinned and pinned in alive:
            self.primary_key = pinned
            return
        if pinned and pinned not in alive:
            self.config.set("monitor.pinned", "")   # 钉住的实例已退出，恢复自动
            self.config.save()
        # 当前主绑定仍存活且不处于空闲时，保持粘性（除非出现等待批复的其他实例）
        cur = self.primary_key if self.primary_key in alive else ""
        if cur:
            s = self.snapshots.get(cur)
            if s and s.status in (Status.WAITING, Status.WORKING):
                return
        # 候选优先级：等待批复 > 工作中 > 启动时间最新
        def prio(key):
            s = self.snapshots.get(key)
            if s and s.status == Status.WAITING:
                return (0, 0)
            if s and s.status == Status.WORKING:
                return (1, 0)
            return (2, -alive[key].started_at)
        best = sorted(alive, key=prio)[0]
        if best != self.primary_key:
            self.primary_key = best
            self._log(f"主绑定切换 → {best}")

    # ---- 监控主循环（线程内） ----
    def _loop(self):
        poll_sec = float(self.config.get("monitor.file_poll_sec", 0.6))
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._tick()
            except Exception as e:
                self._log(f"监控异常: {e!r}")
            elapsed = time.time() - t0
            self._stop.wait(max(0.2, poll_sec - elapsed))

    def _tick(self):
        cfg_m = dict(self.config.get("monitor"))
        enabled = {AgentKind(k) for k, v in cfg_m.get("agents", {}).items() if v}
        now = time.time()

        if now - self._last_windows_scan >= float(cfg_m.get("windows_scan_sec", 3.0)):
            self._last_windows_scan = now
            found = {i.key: i for i in scan_windows() if i.kind in enabled}
            if cfg_m.get("wsl_enabled", True) and \
                    now - self._last_wsl_scan >= float(cfg_m.get("wsl_scan_sec", 5.0)):
                self._last_wsl_scan = now
                for i in self._wsl.scan():
                    if i.kind in enabled:
                        found[i.key] = i
            self._merge_instances(found)

        with self.lock:
            instances = dict(self.instances)
        if not instances:
            with self.lock:
                self.snapshots = {}
                self._select_primary()
            return

        by_kind: dict[AgentKind, list[AgentInstance]] = {}
        for inst in instances.values():
            by_kind.setdefault(inst.kind, []).append(inst)

        new_snaps: list[Snapshot] = []
        for kind, insts in by_kind.items():
            try:
                new_snaps += self._watchers[kind].poll(insts)
            except Exception as e:
                self._log(f"{kind.value} 解析异常: {e!r}")

        new_snaps = self._stabilize(new_snaps, now)
        with self.lock:
            prev = self.snapshots
            self.snapshots = {s.key: s for s in new_snaps}
            self._select_primary()
        for s in new_snaps:
            old = prev.get(s.key)
            if old is None:
                self._log(f"发现 {s.kind.label} ({s.source} pid={s.pid})")
            elif old.status != s.status:
                extra = f" -> {s.last_line}" if s.status == Status.WAITING else ""
                self._log(f"{s.kind.label} [{s.status.value}]{extra}")

    def _merge_instances(self, found: dict[str, AgentInstance]):
        """合并扫描结果：短暂消失的实例在宽限期内保留，避免识别闪跳。"""
        now = time.time()
        grace = float(self.config.get("monitor.gone_grace_sec", 45.0))
        with self.lock:
            merged = dict(found)
            for k, inst in self.instances.items():
                if k not in merged:
                    since = self._gone_since.setdefault(k, now)
                    if now - since < grace:
                        merged[k] = inst
            for k in list(self._gone_since):
                if k in found:
                    self._gone_since.pop(k, None)
                elif now - self._gone_since[k] >= grace:
                    self._gone_since.pop(k, None)
                    self.instances.pop(k, None)
                    self._log(f"实例退出: {k}")
            self.instances = merged

    def _stabilize(self, snaps: list[Snapshot], now: float) -> list[Snapshot]:
        """状态平滑：WORKING 保持期 + DONE 后允许立即转空闲。"""
        hold = float(self.config.get("monitor.working_hold_sec", 90.0))
        for s in snaps:
            prev = self._prev_status.get(s.key)
            last_active = self._active_ts.get(s.key, 0)
            if s.status != Status.IDLE:
                self._active_ts[s.key] = now
            elif prev == Status.WORKING and now - last_active < hold:
                s.status = Status.WORKING            # 保持工作中，不闪跳回空闲
            self._prev_status[s.key] = s.status
        return snaps
