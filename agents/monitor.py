"""V3 监控核心：被动观察，无控制通道（plan.md §30/§32/§48）。

线程架构（plan §15/§32）：
  Thread 1  Tk UI
  Thread 2  Monitor Core（本模块，约 0.5s：读最新进程快照、poll 会话
            文件、drain 终端事件、状态融合）
  Thread 3  ProcessProbeWorker（3s：Windows + WSL 进程扫描；single-slot
            最新快照，新扫描覆盖旧扫描，不排队积压）
  Thread 4  Terminal UIA MTA（terminal_uia.UiaBackend 内部）

因此 WSL 命令卡顿不会卡气泡。UI 只看到 AgentTarget。
"""
import queue
import threading
import time

from .claude import ClaudeWatcher
from .codex import CodexWatcher
from .discovery import WslProcessProbe, scan_windows
from .kimi import KimiWatcher
from .models import (
    AgentInstance,
    AgentKind,
    AgentTarget,
    BindingConfidence,
    EvidenceSource,
    Observation,
    Phase,
    Snapshot,
    Status,
)
from .pi import PiWatcher
from .state import reduce_state
from .terminal_uia import TerminalObserver, TerminalResolver, make_observer

WATCHERS = {
    AgentKind.CLAUDE: ClaudeWatcher,
    AgentKind.CODEX: CodexWatcher,
    AgentKind.KIMI: KimiWatcher,
    AgentKind.PI: PiWatcher,
}

LOG_MAX = 300

# plan.md §31 自动跟随优先级（数值越小越优先）
_FOLLOW_PRIORITY = {
    Status.WAITING: 0,
    Status.INPUT: 1,
    Status.ERROR: 2,
    Status.WORKING: 3,
    Status.DONE: 4,
    Status.IDLE: 5,
    Status.UNKNOWN: 6,
}


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _kind(value):
    if isinstance(value, AgentKind):
        return value
    try:
        return AgentKind(str(value))
    except (TypeError, ValueError):
        return None


class ProcessProbeWorker:
    """独立探测线程：结果写入 single-slot 最新快照（plan §32/§48）。

    快照按真实 source 键控（windows / wsl:Ubuntu / wsl:Debian…），
    健康位同样按 source 隔离：一个 distro 失败不污染其他来源。
    """

    def __init__(self, config):
        self.config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._snapshot: dict[str, list[AgentInstance]] = {}
        self._ok: dict[str, bool] = {}
        self._wsl = WslProcessProbe(
            allow_root_metadata=bool(config.get(
                "privacy.wsl_root_metadata_fallback", False)))
        self._last_windows = 0.0
        self._last_wsl = 0.0
        self.windows_scan_ms = 0.0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="deskpet-probe", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self) -> tuple[dict[str, list[AgentInstance]], dict[str, bool]]:
        with self._lock:
            return (dict(self._snapshot), dict(self._ok))

    def rescan(self):
        self._last_windows = 0.0
        self._last_wsl = 0.0

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._tick()
            except Exception:
                pass
            elapsed = time.time() - t0
            self._stop.wait(max(0.3, 1.0 - elapsed))

    def _tick(self):
        cfg_m = self.config.get("monitor") or {}
        now = time.time()
        if now - self._last_windows >= _num(cfg_m.get("windows_scan_sec", 3.0), 3.0):
            self._last_windows = now
            t0 = time.perf_counter()
            try:
                found = scan_windows()
                with self._lock:
                    self._snapshot["windows"] = found
                    self._ok["windows"] = True
            except Exception:
                with self._lock:
                    self._ok["windows"] = False
            self.windows_scan_ms = time.perf_counter() - t0
        wsl_enabled = bool(cfg_m.get("wsl_enabled", True))
        if wsl_enabled and now - self._last_wsl >= _num(
                cfg_m.get("wsl_scan_sec", 3.0), 3.0):
            self._last_wsl = now
            try:
                by_source, healthy = self._wsl.scan()
                with self._lock:
                    # 清掉已消失的 wsl source 键
                    for key in list(self._snapshot):
                        if key.startswith("wsl:") and key not in by_source:
                            self._snapshot.pop(key, None)
                            self._ok.pop(key, None)
                    self._snapshot.update(by_source)
                    self._ok.update(healthy)
            except Exception:
                with self._lock:
                    for key in list(self._ok):
                        if key.startswith("wsl:"):
                            self._ok[key] = False
        elif not wsl_enabled:
            with self._lock:
                for key in list(self._snapshot):
                    if key.startswith("wsl:"):
                        self._snapshot.pop(key, None)
                        self._ok.pop(key, None)


class Monitor:
    """被动观察监控器：唯一职责是发现、观察、融合与呈现。"""

    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.instances: dict[str, AgentInstance] = {}
        self.snapshots: dict[str, Snapshot] = {}
        self.bindings: dict[str, object] = {}
        self.primary_key: str = ""
        self.log_q: queue.Queue = queue.Queue(maxsize=200)
        self._log_ring: list[str] = []
        self._probe = ProcessProbeWorker(config)
        monitor_cfg = dict(config.get("monitor") or {})
        privacy = dict(config.get("privacy") or {})
        watcher_cfg = dict(monitor_cfg)
        watcher_cfg.setdefault("activity_grace_sec",
                               monitor_cfg.get("activity_grace_sec", 10.0))
        watcher_cfg.setdefault("goal_max_chars", privacy.get("goal_max_chars", 120))
        watcher_cfg.setdefault("summary_max_chars", privacy.get("summary_max_chars", 160))
        self._watchers = {k: v(dict(watcher_cfg)) for k, v in WATCHERS.items()}
        self._terminal: TerminalObserver | None = None
        self._terminal_failed = False
        try:
            self._terminal = make_observer(monitor_cfg)
        except Exception:
            self._terminal = None
            self._terminal_failed = True
        self._resolver = TerminalResolver()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._gone_since: dict[str, float] = {}
        self._last_resolve = 0.0
        self._instance_sig: tuple = ()
        self._last_logs_trim = 0.0

    # ------------------------------------------------------------ 生命周期
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._probe.start()
        # UIA 后端初始化（comtypes/typelib/pane 发现）可能耗时数秒，
        # 绝不能阻塞 UI 线程：放独立引导线程异步启动（plan §15/§32）。
        if self._terminal is not None:
            threading.Thread(target=self._start_terminal, name="deskpet-uia-boot",
                             daemon=True).start()
        self._thread = threading.Thread(
            target=self._loop, name="deskpet-monitor", daemon=True)
        self._thread.start()

    def _start_terminal(self):
        try:
            if self._terminal is None or not self._terminal.start():
                self._terminal_failed = True
        except Exception:
            self._terminal_failed = True

    def stop(self):
        self._stop.set()
        self._probe.stop()
        if self._terminal is not None:
            try:
                self._terminal.stop()
            except Exception:
                pass
        for watcher in self._watchers.values():
            try:
                watcher.release()
            except Exception:
                pass

    # ------------------------------------------------------------ UI API
    def get_targets(self) -> dict[str, AgentTarget]:
        with self.lock:
            self._select_primary()
            out = {}
            for key, inst in self.instances.items():
                snap = self.snapshots.get(key)
                if snap is None:
                    continue
                out[key] = AgentTarget(
                    key=key, instance=inst, snapshot=snap,
                    terminal=self.bindings.get(key))
            return out

    def primary_target(self) -> AgentTarget | None:
        targets = self.get_targets()
        return targets.get(self.primary_key) if self.primary_key else None

    def primary_snapshot(self) -> Snapshot | None:
        target = self.primary_target()
        return target.snapshot if target else None

    def get_target(self, key: str) -> AgentTarget | None:
        if not key:
            return None
        return self.get_targets().get(key)

    def set_primary(self, key: str, manual: bool = True):
        """手动钉住；key 为空 = 恢复自动跟随。"""
        with self.lock:
            if manual and key:
                self.config.set("monitor.pinned", key)
                self.primary_key = key
            else:
                self.config.set("monitor.pinned", "")
                self.primary_key = ""
            self.config.save()
            self._select_primary()

    def reset_primary(self):
        """恢复自动跟随（plan §30 API）。"""
        self.set_primary("", manual=False)

    def is_bound(self, key: str) -> bool:
        return key == self.primary_key

    def rescan(self):
        """重新扫描：只清缓存与运行期绑定，不动 Agent 数据目录（plan §46）。"""
        self._probe.rescan()
        for watcher in self._watchers.values():
            try:
                watcher.reset_scan_cache()
            except Exception:
                pass
        if self._terminal is not None:
            try:
                self._terminal.refresh_panes(force=True)
            except Exception:
                pass
        self._log("已请求重新扫描（清 Process/Session/Terminal 运行期缓存）")

    def bind_focused_pane(self, key: str) -> bool:
        """高级修复：把当前焦点的 TermControl pane 关联到该 Agent（运行期）。"""
        if self._terminal is None:
            return False
        pane = self._terminal.manual_bind_focused()
        if pane is None:
            return False
        self._resolver.set_manual_binding(key, pane.pane_id)
        self._log(f"终端 pane 手动关联 → {key}")
        return True

    def terminal_available(self) -> bool:
        return bool(self._terminal is not None and not self._terminal_failed)

    def terminal_startup_error(self) -> str:
        """UIA 启动失败的非敏感原因（诊断展示用）。"""
        if self._terminal is None:
            return ""
        backend = getattr(self._terminal.backend, "startup_error", "")
        return str(backend or "")

    def rediscover_terminal(self):
        """HWND 失效等场景下的终端重发现（只刷新运行期 pane 缓存）。"""
        if self._terminal is not None:
            try:
                self._terminal.refresh_panes(force=True)
            except Exception:
                pass

    def stats(self) -> dict:
        wsl = self._probe._wsl
        out = {
            "targets": len(self.instances),
            "wsl_spawn_count": getattr(wsl, "spawn_count", 0),
            "wsl_scan_count": getattr(wsl, "scan_count", 0),
            "wsl_scan_ms": round(getattr(wsl, "scan_ms", 0.0), 1),
            "windows_scan_ms": round(self._probe.windows_scan_ms, 1),
            "metadata_pid_count": getattr(wsl, "metadata_pid_count", 0),
        }
        if self._terminal is not None:
            out.update(self._terminal.stats)
            backend = getattr(self._terminal.backend, "stats", None)
            if callable(backend):
                out.update(backend())
        return out

    def recent_logs(self) -> list[str]:
        return list(self._log_ring)

    def trim(self):
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

    # ------------------------------------------------------------ 主循环
    def _loop(self):
        poll_sec = _num(self.config.get("monitor.file_poll_sec", 0.5), 0.5)
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self._tick()
            except Exception as exc:
                self._log(f"监控异常: {exc!r}")
            elapsed = time.time() - t0
            self._stop.wait(max(0.15, poll_sec - elapsed))

    def _enabled_kinds(self, cfg_m: dict) -> set:
        out = set()
        for value, enabled in (cfg_m.get("agents", {}) or {}).items():
            if enabled:
                kind = _kind(value)
                if kind is not None:
                    out.add(kind)
        return out

    def _merge_instances(self, now: float):
        """读 latest probe 快照并合并；失败来源按真实 source 保留缓存（plan §54）。

        authoritative 按 inst.source 判定：Ubuntu 扫描失败不会阻止
        Windows / Debian 实例的正常退出清理。
        """
        snap, ok = self._probe.snapshot()
        cfg_m = self.config.get("monitor") or {}
        enabled = self._enabled_kinds(cfg_m)
        grace = _num(cfg_m.get("gone_grace_sec", 15.0), 15.0)
        found: dict[str, AgentInstance] = {}
        authoritative: set[str] = set()
        for source, instances in snap.items():
            if source == "windows" and not bool(cfg_m.get("windows_enabled", True)):
                continue
            for inst in instances:
                if inst.kind in enabled:
                    found[inst.key] = inst
            if ok.get(source):
                authoritative.add(source)
        with self.lock:
            old = dict(self.instances)
            merged = dict(found)
            for key, inst in old.items():
                if key in found:
                    self._gone_since.pop(key, None)
                    continue
                if inst.source not in authoritative:
                    # 该来源本轮不 authoritative（扫描失败）：保留缓存实例
                    merged[key] = inst
                    continue
                since = self._gone_since.setdefault(key, now)
                if now - since < grace:
                    merged[key] = inst
                else:
                    self._gone_since.pop(key, None)
                    self._log(f"实例退出: {key}")
            self.instances = merged
            live = set(merged)
            for table in (self._gone_since,):
                for key in list(table):
                    if key not in live:
                        table.pop(key, None)

    def _tick(self):
        cfg_m = dict(self.config.get("monitor") or {})
        now = time.time()
        self._merge_instances(now)
        _snap, probe_ok = self._probe.snapshot()

        with self.lock:
            instances = dict(self.instances)

        # 1) 会话观察
        session_obs: dict[str, Observation] = {}
        by_kind: dict[AgentKind, list[AgentInstance]] = {}
        for inst in instances.values():
            by_kind.setdefault(inst.kind, []).append(inst)
        for kind, insts in by_kind.items():
            watcher = self._watchers.get(kind)
            if watcher is None:
                continue
            try:
                session_obs.update(watcher.poll(insts))
            except Exception as exc:
                self._log(f"{kind.value} 解析异常: {exc!r}")

        # 2) 终端观察
        if self._terminal is not None:
            try:
                self._terminal.poll(now)
            except Exception:
                pass
        panes = self._terminal.panes if self._terminal is not None else {}

        # 3) 终端绑定（节流：实例集合变化或每 3s）
        sig = (tuple(sorted(instances)), tuple(sorted(panes)))
        if sig != self._instance_sig or now - self._last_resolve >= 3.0:
            self._instance_sig = sig
            self._last_resolve = now
            try:
                self.bindings = self._resolver.resolve(
                    list(instances.values()), panes, now)
            except Exception:
                self.bindings = {}

        # 4) 状态融合
        grace = _num(cfg_m.get("activity_grace_sec", 10.0), 10.0)
        new_snaps: dict[str, Snapshot] = {}
        for key, inst in instances.items():
            session = session_obs.get(key)
            terminal = self._terminal_observation(inst, now, grace)
            prev = self.snapshots.get(key)
            snap = reduce_state(inst, session, terminal, prev, now)
            snap.stale = not probe_ok.get(inst.source, True)
            new_snaps[key] = snap
            self._fill_policy(snap, key)
            self._fill_parser_health(snap, key)

        with self.lock:
            prev_snaps = self.snapshots
            self.snapshots = new_snaps
            self._select_primary()
        for key, snap in new_snaps.items():
            old = prev_snaps.get(key)
            if old is None:
                self._log(f"发现 {snap.kind.label} ({snap.source} 项目={snap.cwd or '?'})")
            elif old.status != snap.status:
                extra = ""
                if snap.status in (Status.WAITING, Status.INPUT):
                    extra = f" -> {snap.waiting_detail or snap.summary}"
                self._log(f"{snap.kind.label} [{snap.status.value}]{extra}")
        self._trim_logs(now)

    def _terminal_observation(self, inst: AgentInstance, now: float,
                              grace: float) -> Observation | None:
        if self._terminal is None:
            return None
        binding = self.bindings.get(inst.key)
        if binding is None or binding.pane_id is None:
            return None
        if binding.confidence not in (BindingConfidence.CONFIRMED,
                                      BindingConfidence.HIGH):
            return None   # 绑定不唯一时绝不归属终端审批（plan §25）
        waiting = self._terminal.waiting_observation(binding.pane_id)
        if waiting is not None and waiting.live(now):
            # 审批文案的识别器种类必须与绑定的 AgentKind 一致：
            # Codex pane 上命中 Claude 审批 → 不能归属给 Codex。
            if waiting.agent_kind is None or waiting.agent_kind == inst.kind:
                return waiting
        # 泛化终端活动（agent_kind=None）只是 fallback 证据，
        # 能否覆盖会话状态由 StateReducer 的证据强弱规则决定。
        return self._terminal.pane_activity_observation(
            binding.pane_id, now, grace)

    def _fill_parser_health(self, snap: Snapshot, key: str):
        """把 watcher 的解析器兼容性诊断写入快照（不含任何事件内容）。"""
        watcher = self._watchers.get(snap.kind)
        if watcher is None:
            return
        try:
            diag = watcher.diagnostics_for(key)
        except Exception:
            return
        snap.parser_health = diag.health
        if diag.health == "PARTIAL":
            parts = []
            if diag.unknown_types:
                parts.append("未知记录：" + ", ".join(diag.unknown_types))
            if diag.parse_errors:
                parts.append(f"解析错误 {diag.parse_errors} 条")
            snap.parser_detail = "；".join(parts)

    def _fill_policy(self, snap: Snapshot, key: str):
        watcher = self._watchers.get(snap.kind)
        if watcher is None:
            return
        path = watcher._instance_files.get(key)
        st = watcher.files.get(path) if path else None
        if st is not None:
            policy = getattr(st, "policy", "")
            if policy:
                snap.policy = policy

    def _trim_logs(self, now: float):
        if now - self._last_logs_trim < 30:
            return
        self._last_logs_trim = now
        if len(self._log_ring) > LOG_MAX:
            del self._log_ring[: len(self._log_ring) - LOG_MAX]

    # ------------------------------------------------------------ 主绑定选择
    def _select_primary(self):
        alive = self.instances
        if not alive:
            pinned = str(self.config.get("monitor.pinned") or "")
            if pinned and pinned == self.primary_key:
                return
            self.primary_key = ""
            return
        pinned = str(self.config.get("monitor.pinned") or "")
        if pinned and pinned in alive:
            self.primary_key = pinned
            return
        if pinned and pinned not in alive:
            # 钉住的 Agent 已退出 → 恢复自动（plan §31）
            self.config.set("monitor.pinned", "")
            self.config.save()

        def prio(key):
            s = self.snapshots.get(key)
            status = s.status if s is not None else Status.UNKNOWN
            return (_FOLLOW_PRIORITY.get(status, 9),
                    -_num(getattr(alive[key], "started_at", 0)), key)

        best = sorted(alive, key=prio)[0]
        if best == self.primary_key:
            return
        cur = self.primary_key if self.primary_key in alive else ""
        if cur:
            cur_prio = prio(cur)[0]
            best_prio = prio(best)[0]
            # 粘性：当前仍在工作时不因同优先级切换；更高优先级（等待/输入/
            # 错误）可抢占（plan §31）。
            if best_prio >= cur_prio:
                return
        self.primary_key = best
        self._log(f"主绑定切换 → {best}")
