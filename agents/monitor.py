"""监控线程：进程发现、会话 tail、主绑定以及可选受控会话聚合。

只读进程扫描、文件扫描和受控 manager 彼此隔离：一个来源失败时保留
自己的旧缓存，不会把另一个来源误判为退出；目录扫描低频进行，而已
绑定的 JSONL 文件每轮继续增量读取。
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


def _kind(value):
    if isinstance(value, AgentKind):
        return value
    try:
        return AgentKind(str(value))
    except (TypeError, ValueError):
        return None


def _source(value) -> str:
    return str(value or "")


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class Monitor:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.snapshots: dict[str, Snapshot] = {}
        # instances 是只读发现与 managed 的合并视图；保留两个来源表，
        # 避免下一次扫描把受控会话或未更新来源清掉。
        self.instances: dict[str, AgentInstance] = {}
        self._readonly_instances: dict[str, AgentInstance] = {}
        self._managed_instances: dict[str, AgentInstance] = {}
        self._managed_snapshots: dict[str, Snapshot] = {}
        self.managed = None
        self.primary_key: str = ""                      # 当前主绑定
        self.log_q: queue.Queue = queue.Queue(maxsize=200)
        self._log_ring: list[str] = []
        self._wsl = WslScanner()
        monitor_cfg = config.get("monitor") or {}
        self._watchers = {k: v(dict(monitor_cfg)) for k, v in WATCHERS.items()}
        self._last_windows_scan = 0.0
        self._last_wsl_scan = 0.0
        self._window_cache: dict[str, AgentInstance] = {}
        self._wsl_cache: dict[str, AgentInstance] = {}
        self._instances_cache: list[AgentInstance] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._gone_since: dict[str, float] = {}         # key -> 首次消失时间
        self._active_ts: dict[str, float] = {}          # key -> 最近活动时间
        self._prev_status: dict[str, Status] = {}       # key -> 上一轮状态

    # ---- 供 UI / root 调用 ----
    def attach_managed(self, manager):
        """挂接可选受控 manager；manager 的调用仍在监控线程内读取。"""
        with self.lock:
            self.managed = manager
            if manager is None:
                self._managed_instances = {}
                self._managed_snapshots = {}
                self._rebuild_instances()

    # 兼容 root 侧更直观的命名。
    set_managed = attach_managed

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="deskpet-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        manager = self.managed
        if manager is not None:
            try:
                manager.stop()
            except Exception:
                pass

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
            # A managed record is created before its app-server worker has a
            # PID.  Keep a pinned key through that short empty/connecting
            # window so the UI does not lose its selected session.
            pinned = str(self.config.get("monitor.pinned") or "")
            if pinned:
                self.primary_key = pinned
            elif self.primary_key not in self._managed_instances:
                self.primary_key = ""
            return
        pinned = str(self.config.get("monitor.pinned") or "")
        if pinned and pinned in alive:
            self.primary_key = pinned
            return
        if pinned and pinned not in alive:
            self.config.set("monitor.pinned", "")   # 钉住的实例已退出，恢复自动
            self.config.save()
        # 当前主绑定仍存活且处于活动状态时保持粘性；更高优先级的等待/
        # 输入请求可以接管，方便用户看到真正需要操作的会话。
        cur = self.primary_key if self.primary_key in alive else ""
        if cur:
            s = self.snapshots.get(cur)
            if s and s.status in (Status.WAITING, Status.INPUT, Status.WORKING):
                return

        def prio(key):
            s = self.snapshots.get(key)
            if s and s.status == Status.WAITING:
                return (0, 0, key)
            if s and s.status == Status.INPUT:
                return (1, 0, key)
            if s and s.status == Status.WORKING:
                return (2, 0, key)
            if s and s.status == Status.ERROR:
                return (3, 0, key)
            return (4, -_num(getattr(alive[key], "started_at", 0)), key)

        best = sorted(alive, key=prio)[0]
        if best != self.primary_key:
            self.primary_key = best
            self._log(f"主绑定切换 → {best}")

    # ---- 受控会话读取 ----
    def _coerce_instance(self, value):
        if isinstance(value, AgentInstance):
            inst = value
        elif isinstance(value, dict):
            kind = _kind(value.get("kind"))
            if kind is None:
                return None
            inst = AgentInstance(
                kind=kind, pid=int(value.get("pid") or 0),
                source=_source(value.get("source") or "managed"),
                started_at=_num(value.get("started_at")),
                cwd=_source(value.get("cwd")),
                key=_source(value.get("key")),
                session_id=_source(value.get("session_id") or value.get("sessionId")),
            )
        else:
            return None
        try:
            setattr(inst, "connection", "managed")
        except Exception:
            pass
        return inst

    def _coerce_snapshot(self, value, fallback_key=""):
        if isinstance(value, Snapshot):
            snap = value
        elif isinstance(value, dict):
            kind = _kind(value.get("kind"))
            if kind is None:
                return None
            status = value.get("status", Status.UNKNOWN)
            try:
                status = status if isinstance(status, Status) else Status(str(status))
            except (TypeError, ValueError):
                status = Status.UNKNOWN
            snap = Snapshot(
                key=_source(value.get("key") or fallback_key), kind=kind,
                source=_source(value.get("source") or "managed"),
                pid=int(value.get("pid") or 0), status=status,
                title=_source(value.get("title")), last_line=_source(value.get("last_line")),
                mode=_source(value.get("mode")), session_file=_source(value.get("session_file")),
                phase=_source(value.get("phase")), goal=_source(value.get("goal")),
                summary=_source(value.get("summary")), connection="managed",
                session_id=_source(value.get("session_id") or value.get("sessionId")),
                turn_id=_source(value.get("turn_id") or value.get("turnId")),
                cwd=_source(value.get("cwd")), freshness=_num(value.get("freshness")),
                can_approve=bool(value.get("can_approve", False)),
            )
        else:
            return None
        snap.connection = "managed"
        return snap

    def _read_managed(self):
        manager = self.managed
        if manager is None:
            return {}, {}
        try:
            raw_instances = list(manager.instances() or [])
            raw_snapshots = manager.snapshots() or {}
            managed_instances = {}
            for raw in raw_instances:
                inst = self._coerce_instance(raw)
                if inst is not None:
                    managed_instances[inst.key] = inst
            managed_snapshots = {}
            if isinstance(raw_snapshots, dict):
                for key, raw in raw_snapshots.items():
                    snap = self._coerce_snapshot(raw, key)
                    if snap is not None:
                        if not snap.key:
                            snap.key = str(key)
                        managed_snapshots[snap.key or str(key)] = snap
            # ManagedManager exposes a session immediately, but a lightweight
            # adapter may momentarily return an empty list while create() is
            # still publishing its record.  Preserve the last view during
            # that non-terminal transition; a stopped manager is terminal.
            if (not managed_instances and self._managed_instances and
                    not bool(getattr(manager, "_stopped", False))):
                managed_instances = dict(self._managed_instances)
                if not managed_snapshots:
                    managed_snapshots = dict(self._managed_snapshots)
            # manager may expose an instance before its first protocol snapshot.
            for key, inst in managed_instances.items():
                if key not in managed_snapshots:
                    managed_snapshots[key] = Snapshot(
                        key=key, kind=inst.kind, source=inst.source, pid=inst.pid,
                        cwd=inst.cwd, session_id=getattr(inst, "session_id", ""),
                        connection="managed", status=Status.UNKNOWN,
                        last_line="受控会话正在连接", summary="受控会话正在连接",
                    )
            return managed_instances, managed_snapshots
        except Exception as exc:
            self._log(f"受控会话读取异常: {exc!r}")
            return None

    def _owned_pids(self):
        windows = set()
        wsl = set()
        for inst in self._managed_instances.values():
            pid = int(getattr(inst, "pid", 0) or 0)
            if pid <= 0:
                continue
            source = _source(getattr(inst, "source", ""))
            if source.startswith("wsl:"):
                wsl.add(pid)
            else:
                windows.add(pid)
        return windows, wsl

    # ---- 监控主循环（线程内） ----
    def _loop(self):
        poll_sec = _num(self.config.get("monitor.file_poll_sec", 0.6), 0.6)
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._tick()
            except Exception as exc:
                self._log(f"监控异常: {exc!r}")
            elapsed = time.time() - t0
            self._stop.wait(max(0.2, poll_sec - elapsed))

    def _refresh_discovery(self, cfg_m: dict, now: float):
        windows_due = now - self._last_windows_scan >= _num(
            cfg_m.get("windows_scan_sec", 3.0), 3.0)
        wsl_enabled = bool(cfg_m.get("wsl_enabled", True))
        wsl_due = wsl_enabled and now - self._last_wsl_scan >= _num(
            cfg_m.get("wsl_scan_sec", 5.0), 5.0)
        authoritative: set[str] = set()
        owned_windows, owned_wsl = self._owned_pids()

        if windows_due:
            self._last_windows_scan = now
            try:
                try:
                    found = scan_windows(exclude_pids=owned_windows)
                except TypeError:
                    # 兼容测试替身或旧外部调用的无参 scan_windows。
                    found = scan_windows()
                self._window_cache = {
                    inst.key: inst for inst in found if inst.kind in self._enabled_kinds(cfg_m)
                }
                authoritative.add("windows")
            except Exception as exc:
                self._log(f"Windows 进程扫描异常: {exc!r}")

        if wsl_due:
            self._last_wsl_scan = now
            try:
                try:
                    found = self._wsl.scan(exclude_pids=owned_wsl)
                except TypeError:
                    found = self._wsl.scan()
                if getattr(self._wsl, "last_ok", True):
                    self._wsl_cache = {
                        inst.key: inst for inst in found if inst.kind in self._enabled_kinds(cfg_m)
                    }
                    authoritative.add("__wsl__")
            except Exception as exc:
                self._log(f"WSL 进程扫描异常: {exc!r}")

        if not wsl_enabled:
            self._wsl_cache = {}
            authoritative.add("__wsl__")

        if authoritative:
            found = {**self._window_cache, **self._wsl_cache}
            self._merge_instances(found, authoritative)

    @staticmethod
    def _enabled_kinds(cfg_m: dict):
        out = set()
        for value, enabled in (cfg_m.get("agents", {}) or {}).items():
            if enabled:
                kind = _kind(value)
                if kind is not None:
                    out.add(kind)
        return out

    def _tick(self):
        cfg_m = dict(self.config.get("monitor") or {})
        enabled = self._enabled_kinds(cfg_m)
        now = time.time()

        managed_state = self._read_managed()
        if managed_state is not None:
            self._managed_instances, self._managed_snapshots = managed_state
            with self.lock:
                self._rebuild_instances()
        self._refresh_discovery(cfg_m, now)

        with self.lock:
            instances = dict(self.instances)
        if not instances:
            with self.lock:
                self.snapshots = dict(self._managed_snapshots)
                self._select_primary()
            return

        readonly = [inst for inst in instances.values()
                    if getattr(inst, "connection", "readonly") != "managed"
                    and inst.key not in self._managed_instances]
        by_kind: dict[AgentKind, list[AgentInstance]] = {}
        for inst in readonly:
            if inst.kind in enabled:
                by_kind.setdefault(inst.kind, []).append(inst)

        new_snaps: list[Snapshot] = list(self._managed_snapshots.values())
        for kind, insts in by_kind.items():
            try:
                new_snaps += self._watchers[kind].poll(insts)
            except Exception as exc:
                self._log(f"{kind.value} 解析异常: {exc!r}")

        new_snaps = self._stabilize(new_snaps, now)
        with self.lock:
            prev = self.snapshots
            self.snapshots = {s.key: s for s in new_snaps}
            self._select_primary()
        for snap in new_snaps:
            old = prev.get(snap.key)
            if old is None:
                self._log(f"发现 {snap.kind.label} ({snap.source} pid={snap.pid})")
            elif old.status != snap.status:
                extra = f" -> {snap.last_line}" if snap.status in (Status.WAITING, Status.INPUT, Status.ERROR) else ""
                self._log(f"{snap.kind.label} [{snap.status.value}]{extra}")

    def _rebuild_instances(self):
        self.instances = {**self._readonly_instances, **self._managed_instances}
        self._instances_cache = list(self.instances.values())

    def _source_authoritative(self, source: str, authoritative: set[str]) -> bool:
        return source in authoritative or (source.startswith("wsl:") and "__wsl__" in authoritative)

    def _merge_instances(self, found: dict[str, AgentInstance],
                         authoritative_sources: set[str] | None = None):
        """合并扫描结果；未更新来源与受控实例不会因一次空扫描消失。"""
        now = time.time()
        grace = _num(self.config.get("monitor.gone_grace_sec", 45.0), 45.0)
        with self.lock:
            # 兼容旧测试/调用方直接写 self.instances 的场景。
            old = dict(self._readonly_instances)
            if not old and self.instances and not self._managed_instances:
                old = {key: inst for key, inst in self.instances.items()
                       if getattr(inst, "connection", "readonly") != "managed"}
            if authoritative_sources is None:
                # A direct caller is reporting a complete scan, preserving
                # the behavior of the old API.  The normal discovery path
                # passes an explicit set so Windows and WSL stay isolated
                # when one source fails or has not reached its scan interval.
                authoritative = {
                    _source(getattr(inst, "source", ""))
                    for inst in list(old.values()) + list(found.values())
                    if _source(getattr(inst, "source", ""))
                }
            else:
                authoritative = set(authoritative_sources)
            merged = dict(found)
            for key, inst in old.items():
                if key in found:
                    self._gone_since.pop(key, None)
                    continue
                source = _source(getattr(inst, "source", ""))
                if not self._source_authoritative(source, authoritative):
                    merged[key] = inst
                    continue
                since = self._gone_since.setdefault(key, now)
                if now - since < grace:
                    merged[key] = inst
                else:
                    self._gone_since.pop(key, None)
                    self._log(f"实例退出: {key}")
            self._readonly_instances = merged
            self._rebuild_instances()
            live_keys = set(self.instances)
            # Process churn should not make these small stabilisation maps grow
            # without bound over the lifetime of the desktop pet.
            for table in (self._gone_since, self._active_ts, self._prev_status):
                for key in list(table):
                    if key not in live_keys:
                        table.pop(key, None)

    def _stabilize(self, snaps: list[Snapshot], now: float) -> list[Snapshot]:
        """状态平滑：只读 WORKING 保持期，且不改变受控协议状态。"""
        hold = _num(self.config.get("monitor.working_hold_sec", 90.0), 90.0)
        for snap in snaps:
            prev = self._prev_status.get(snap.key)
            last_active = self._active_ts.get(snap.key, 0)
            if snap.status != Status.IDLE:
                self._active_ts[snap.key] = now
            elif (snap.connection == "readonly" and prev == Status.WORKING and
                  now - last_active < hold):
                snap.status = Status.WORKING            # 防止长思考期间闪回空闲
            self._prev_status[snap.key] = snap.status
        return snaps
