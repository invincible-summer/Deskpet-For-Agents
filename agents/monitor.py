"""V4.1 监控核心：被动观察，无控制通道（v4plan §4/§5/§6）。

线程架构（与 Agent/Pet 数量解耦）：
  Thread 1  Tk UI
  Thread 2  Monitor Core（本模块，约 0.5s：读最新进程快照、poll 会话
            文件、drain 终端事件、状态融合）
  Thread 3  ProcessProbeWorker（3s：Windows + WSL 进程扫描；single-slot
            最新快照，新扫描覆盖旧扫描，不排队积压）
  Thread 4  Terminal UIA MTA（terminal_uia.UiaBackend 内部）
  Thread 5  WindowsExitWatcher（仅 Windows：一个线程阻塞等待所有
            native Agent 进程句柄 signal；不读内存、不轮询）

退出生命周期（v4plan §4）：
  * probe 结果是三态 SourceProbeSnapshot（authoritative 与实例列表
    不可拆开），只有 authoritative absence 才允许 commit exit；
  * 扫描失败保留旧实例并标记 stale，绝不判死；
  * Windows native Agent 优先由 WindowsExitWatcher 事件驱动退出，
    census 兜底；WSL 依赖健康权威 census；
  * `_commit_exit()` 是唯一退出入口，级联清理 session/terminal/watcher
    的全部运行期状态。

因此 WSL 命令卡顿不会卡气泡。UI 只看到 AgentTarget。
"""
import queue
import threading
import time

from .base import BaseWatcher
from .claude import ClaudeWatcher
from .codex import CodexWatcher
from .discovery import ProbeUnavailable, WslProcessProbe, scan_windows
from .kimi import KimiWatcher
from .models import (
    ActivationCode,
    ActivationResult,
    AgentInstance,
    AgentKind,
    AgentTarget,
    BindingConfidence,
    EvidenceSource,
    Observation,
    Phase,
    Snapshot,
    SourceProbeSnapshot,
    Status,
)
from .pi import PiWatcher
from .state import reduce_state
from .terminal_service import WindowsTerminalService
from .terminal_uia import make_observer

WATCHERS = {
    AgentKind.CLAUDE: ClaudeWatcher,
    AgentKind.CODEX: CodexWatcher,
    AgentKind.KIMI: KimiWatcher,
    AgentKind.PI: PiWatcher,
}

LOG_MAX = 300


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
    每个 source 是不可拆开的 SourceProbeSnapshot：一个 distro 失败
    不污染其他来源（v4plan §3.2/§4.1）。
    """

    def __init__(self, config):
        self.config = config
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._snapshot: dict[str, SourceProbeSnapshot] = {}
        self._gen = 0
        # Windows 全局枚举失败时保留的上一轮权威实例（不判死）
        self._windows_cache: tuple[AgentInstance, ...] = ()
        self._wsl = WslProcessProbe(
            allow_root_metadata=bool(config.get(
                "privacy.wsl_root_metadata_fallback", False)))
        self._last_windows = 0.0
        self._last_wsl = 0.0
        self.windows_scan_ms = 0.0
        self.windows_probe_error = ""

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="deskpet-probe", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self) -> dict[str, SourceProbeSnapshot]:
        with self._lock:
            return dict(self._snapshot)

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
            self._gen += 1
            try:
                found = scan_windows()
                self._windows_cache = tuple(found)
                snap = SourceProbeSnapshot(
                    source="windows", generation=self._gen, observed_at=now,
                    authoritative=True, instances=self._windows_cache)
                self.windows_probe_error = ""
            except ProbeUnavailable as exc:
                # 全局枚举失败：authoritative=False，保留旧实例（v4plan §4.1）
                snap = SourceProbeSnapshot(
                    source="windows", generation=self._gen, observed_at=now,
                    authoritative=False, instances=self._windows_cache,
                    error=str(exc))
                self.windows_probe_error = str(exc)
            except Exception as exc:   # 防御：未知异常同样不得冒充空结果
                snap = SourceProbeSnapshot(
                    source="windows", generation=self._gen, observed_at=now,
                    authoritative=False, instances=self._windows_cache,
                    error=repr(exc))
                self.windows_probe_error = repr(exc)
            with self._lock:
                self._snapshot["windows"] = snap
            self.windows_scan_ms = time.perf_counter() - t0
        wsl_enabled = bool(cfg_m.get("wsl_enabled", True))
        if wsl_enabled and now - self._last_wsl >= _num(
                cfg_m.get("wsl_scan_sec", 3.0), 3.0):
            self._last_wsl = now
            try:
                by_source = self._wsl.scan()
                with self._lock:
                    self._snapshot.update(by_source)
            except Exception:
                # WslProcessProbe 内部已按 source 输出三态；这里只是防线
                with self._lock:
                    for source in list(self._snapshot):
                        if source.startswith("wsl:"):
                            sp = self._snapshot[source]
                            self._snapshot[source] = SourceProbeSnapshot(
                                source=source, generation=sp.generation,
                                observed_at=now, authoritative=False,
                                instances=sp.instances, error="probe crashed")
        elif not wsl_enabled:
            with self._lock:
                for source in list(self._snapshot):
                    if source.startswith("wsl:"):
                        self._snapshot.pop(source, None)


class Monitor:
    """被动观察监控器：唯一职责是发现、观察、融合与呈现。"""

    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.instances: dict[str, AgentInstance] = {}
        self.snapshots: dict[str, Snapshot] = {}
        self.bindings: dict[str, object] = {}
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
        self._watchers: dict[AgentKind, BaseWatcher] = {
            k: v(dict(watcher_cfg)) for k, v in WATCHERS.items()}
        # 终端观察/解析/激活统一由 TerminalService 收口（v4plan §5.8）
        try:
            observer = make_observer(monitor_cfg)
        except Exception:
            observer = None
        self._terminal_service = WindowsTerminalService(
            observer, cfg=monitor_cfg)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._exit_watcher = None
        try:
            from .process_watch import WindowsExitWatcher
            self._exit_watcher = WindowsExitWatcher()
        except Exception:
            self._exit_watcher = None
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
        if self._terminal_service.observer is not None:
            threading.Thread(target=self._start_terminal, name="deskpet-uia-boot",
                             daemon=True).start()
        if self._exit_watcher is not None:
            try:
                self._exit_watcher.start()
            except Exception:
                self._exit_watcher = None
        self._thread = threading.Thread(
            target=self._loop, name="deskpet-monitor", daemon=True)
        self._thread.start()

    def _start_terminal(self):
        # 启动失败不锁存：backend.available() 是唯一可用性事实来源
        # （首次 typelib 生成慢于等待窗口时，backend 稍后会自行就绪）
        try:
            self._terminal_service.start()
        except Exception:
            pass

    def stop(self):
        self._stop.set()
        self._probe.stop()
        if self._exit_watcher is not None:
            try:
                self._exit_watcher.stop()
            except Exception:
                pass
        try:
            self._terminal_service.stop()
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
            out = {}
            for key, inst in self.instances.items():
                snap = self.snapshots.get(key)
                if snap is None:
                    continue
                out[key] = AgentTarget(
                    key=key, instance=inst, snapshot=snap,
                    terminal=self.bindings.get(key))
            return out

    def get_target(self, key: str) -> AgentTarget | None:
        if not key:
            return None
        return self.get_targets().get(key)

    def is_live_key(self, key: str) -> bool:
        """exact key 是否仍存活（activate 前的二次复核入口）。"""
        with self.lock:
            return key in self.instances

    def rescan(self):
        """重新扫描：只清缓存与运行期绑定，不动 Agent 数据目录（plan §46）。"""
        self._probe.rescan()
        for watcher in self._watchers.values():
            try:
                watcher.reset_scan_cache()
            except Exception:
                pass
        try:
            self._terminal_service.refresh_topology()
        except Exception:
            pass
        self._log("已请求重新扫描（清 Process/Session/Terminal 运行期缓存）")

    def bind_focused_location(self, key: str):
        """高级修复：把当前焦点的 (Window, Tab, Pane) 关联到该 Agent。

        一次 MTA transaction 捕获完整位置；结果 CONFIRMED + MANUAL，
        只在本应用运行期有效（v4plan §5.10）。返回 TerminalLocation。
        """
        location = self._terminal_service.bind_focused_location(key)
        if location is not None:
            self._log(f"终端位置手动关联 → {key}")
        return location

    def activate_target(self, key: str) -> ActivationResult:
        """UI 激活 Terminal 的唯一入口（v4plan §5.9）。

        UI 只携带 exact agent_key；服务内部重新核验 Agent live、
        binding、Window/Tab/Pane identity，fail-closed。
        """
        target = self.get_target(key)
        if target is None:
            return ActivationResult(ActivationCode.AGENT_GONE)
        return self._terminal_service.activate(
            target, is_agent_live=self.is_live_key)

    def terminal_available(self) -> bool:
        # 不再使用 _terminal_failed 锁存：UIA 首次初始化（typelib 生成）
        # 可能超过启动等待窗口，之后 backend 会自行变为 available；以
        # backend 当前状态为准，避免 UI 永远显示 "UIA 不可用"。
        return self._terminal_service.available()

    def terminal_startup_error(self) -> str:
        """UIA 启动失败的非敏感原因（诊断展示用）。"""
        return self._terminal_service.startup_error()

    def rediscover_terminal(self):
        """HWND 失效等场景下的终端重发现（只刷新运行期拓扑缓存）。"""
        try:
            self._terminal_service.refresh_topology()
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
            "exit_watched": (self._exit_watcher.watched_count()
                             if self._exit_watcher is not None else 0),
        }
        out.update(self._terminal_service.stats())
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
        """读 latest probe 快照并合并（v4plan §4.2）。

        只有 `authoritative=True AND exact key 不在 instances` 才 commit
        exit；不 authoritative 的 source 保留旧实例（stale），绝不判死。
        禁用的 kind/source 按 authoritative empty 处理（不留 ghost）。
        """
        probe_snap = self._probe.snapshot()
        cfg_m = self.config.get("monitor") or {}
        enabled = self._enabled_kinds(cfg_m)
        windows_enabled = bool(cfg_m.get("windows_enabled", True))
        wsl_enabled = bool(cfg_m.get("wsl_enabled", True))
        found: dict[str, AgentInstance] = {}
        authoritative: set[str] = set()
        for source, sp in probe_snap.items():
            if source == "windows" and not windows_enabled:
                continue
            for inst in sp.instances:
                if inst.kind in enabled:
                    found[inst.key] = inst
            if sp.authoritative:
                authoritative.add(source)
        with self.lock:
            old = dict(self.instances)
            merged = dict(found)
            exits: list[tuple[str, str]] = []
            for key, inst in old.items():
                if key in found:
                    continue
                source_disabled = (
                    (inst.source == "windows" and not windows_enabled)
                    or (inst.source.startswith("wsl:") and not wsl_enabled))
                if source_disabled:
                    # 用户关闭该 source：按 authoritative empty 处理，不留 ghost
                    exits.append((key, "source-disabled"))
                elif inst.source in authoritative:
                    if inst.kind not in enabled:
                        exits.append((key, "kind-disabled"))
                    else:
                        exits.append((key, "authoritative-absence"))
                else:
                    # 该来源本轮不 authoritative（扫描失败）：保留缓存实例，不判死
                    merged[key] = inst
            self.instances = merged
            for key, reason in exits:
                self._commit_exit(key, reason, now)

    def _commit_exit(self, key: str, reason: str, now: float) -> bool:
        """唯一退出入口（v4plan §4.5）。调用方必须已持有 self.lock。

        级联清理无条件执行（key 可能已被调用方从实例表移除）；返回值
        表示该 key 是否还在实例表中。成功后任何旧 UI action 再传此
        key 只能得到 AGENT_GONE。不直接操作 Tk/Presentation——UI 由
        下一轮 reconcile 消费事实变化。
        """
        inst = self.instances.pop(key, None)
        self.snapshots.pop(key, None)
        self.bindings.pop(key, None)
        # manual/observed terminal runtime binding 级联失效
        self._terminal_service.drop_instance(key)
        for watcher in self._watchers.values():
            try:
                watcher.drop_instance(key)
            except Exception:
                pass
        if self._exit_watcher is not None:
            try:
                self._exit_watcher.unregister(key)
            except Exception:
                pass
        self._log(f"实例退出: {key} ({reason})")
        return inst is not None

    def _drain_exit_events(self, now: float):
        """消费 WindowsExitWatcher 的候选退出事件（v4plan §4.3）。

        事件只携带注册时的 key/pid/process_token；必须核对仍是当前
        exact incarnation（防 PID 复用/替换竞态），不匹配即丢弃。
        """
        if self._exit_watcher is None:
            return
        for ev in self._exit_watcher.drain():
            with self.lock:
                inst = self.instances.get(ev.key)
                if inst is None:
                    continue
                if (inst.pid != ev.pid
                        or str(inst.process_token) != ev.process_token):
                    continue
                self._commit_exit(ev.key, "process-exit-event", now)

    def _tick(self):
        cfg_m = dict(self.config.get("monitor") or {})
        now = time.time()
        self._merge_instances(now)
        probe_snap = self._probe.snapshot()

        with self.lock:
            instances = dict(self.instances)

        # WindowsExitWatcher 注册 + 事件驱动退出（census 兜底）
        if self._exit_watcher is not None:
            for inst in instances.values():
                if inst.source == "windows":
                    self._exit_watcher.register(inst)
            self._drain_exit_events(now)

        # 1) 会话观察：每个 watcher 每轮都 poll（即使该 kind 当前为 0），
        #    让 BaseWatcher 的全清理语义真正发生（v4plan §4.6）。
        session_obs: dict[str, Observation] = {}
        by_kind: dict[AgentKind, list[AgentInstance]] = {}
        for inst in instances.values():
            by_kind.setdefault(inst.kind, []).append(inst)
        for kind, watcher in self._watchers.items():
            insts = by_kind.get(kind, [])
            try:
                session_obs.update(watcher.poll(insts))
            except Exception as exc:
                self._log(f"{kind.value} 解析异常: {exc!r}")

        # 2) 终端观察（poll 内含 topology 事件重学习）
        self._terminal_service.poll(now)

        # 3) 终端绑定（节流：实例集合或 topology 变化、或每 3s）
        observer = self._terminal_service.observer
        topo_sig = (observer.topology_signature()
                    if observer is not None else ())
        sig = (tuple(sorted(instances)), topo_sig)
        if sig != self._instance_sig or now - self._last_resolve >= 3.0:
            self._instance_sig = sig
            self._last_resolve = now
            try:
                self.bindings = self._terminal_service.resolve(
                    list(instances.values()), now)
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
            sp = probe_snap.get(inst.source)
            snap.stale = bool(sp is not None and not sp.authoritative)
            new_snaps[key] = snap
            self._fill_policy(snap, key)
            self._fill_parser_health(snap, key)

        with self.lock:
            prev_snaps = self.snapshots
            self.snapshots = new_snaps
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
        binding = self.bindings.get(inst.key)
        if binding is None or binding.pane_id is None:
            return None
        if binding.confidence not in (BindingConfidence.CONFIRMED,
                                      BindingConfidence.HIGH):
            return None   # 绑定不唯一时绝不归属终端审批（plan §25）
        waiting = self._terminal_service.waiting_observation(binding)
        if waiting is not None and waiting.live(now):
            # 审批文案的识别器种类必须与绑定的 AgentKind 一致：
            # Codex pane 上命中 Claude 审批 → 不能归属给 Codex。
            if waiting.agent_kind is None or waiting.agent_kind == inst.kind:
                return waiting
        # 泛化终端活动（agent_kind=None）只是 fallback 证据，
        # 能否覆盖会话状态由 StateReducer 的证据强弱规则决定。
        return self._terminal_service.activity_observation(
            binding, now, grace)

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