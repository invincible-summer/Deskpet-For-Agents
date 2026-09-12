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
from dataclasses import dataclass

from .base import BaseWatcher
from .claude import ClaudeWatcher
from .codex import CodexWatcher
from .codex_desktop import CodexDesktopSource
from .desktop import (
    DesktopSourceSnapshot,
    build_terminal_claims,
)
from .desktop_window import DesktopWindowService, is_desktop_instance
from .discovery import (
    ProbeUnavailable,
    WslProcessProbe,
    WindowsRuntimeInventory,
    scan_windows_inventory,
)
from .kimi import KimiWatcher
from .models import (
    ActivationCode,
    ActivationResult,
    AgentInstance,
    AgentKind,
    AgentSurface,
    AgentTarget,
    DesktopHost,
    EvidenceSource,
    Observation,
    ObservationBindingConfidence,
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


@dataclass
class NativeTerminalLease:
    """Windows native Agent 的 terminal-detach 观察租约（v4.2.3 §2.4）。

    runtime-only：上限天然等于 live native Agent 数，不持久化。
    只有曾获得 windows-ancestor CONFIRMED 强绑定的 Agent 才会被 arm。
    """
    armed_window_pid: int = 0
    last_probe_generation: int = 0
    broken_generations: int = 0


@dataclass(frozen=True)
class ActivationRepairRequest:
    """一次 stale-binding 修复请求（v4.3.1 DP43-R08 §16.5）。

    UI 在 activate_cached 得到 STALE_WINDOW 后提交；同 agent 天然
    coalesce（dict 键控），pending 上限 = Agent 上限。
    """
    request_id: int
    agent_key: str
    expires_at: float


@dataclass(frozen=True)
class ActivationRepairResult:
    request_id: int
    agent_key: str
    repaired: bool


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
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._snapshot: dict[str, SourceProbeSnapshot] = {}
        self._gen = 0
        # Windows 全局枚举失败时保留的上一轮权威实例（不判死）
        self._windows_cache: tuple[AgentInstance, ...] = ()
        # v4.4：同一次 census 的 DesktopHost 清单（plan2 §4.2）；
        # None 表示 Windows source 关闭或尚未成功扫描
        self._inventory: WindowsRuntimeInventory | None = None
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
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._loop, name="deskpet-probe", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def join(self, timeout: float = 2.0):
        """有界回收探测线程（plan §18）；绝不 join 调用线程自己。"""
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def snapshot(self) -> dict[str, SourceProbeSnapshot]:
        with self._lock:
            return dict(self._snapshot)

    def desktop_hosts(self) -> tuple[DesktopHost, ...]:
        """最新一次 census 的 DesktopHost 清单（plan2 §4.2）。"""
        with self._lock:
            inv = self._inventory
        return inv.desktop_hosts if inv is not None else ()

    def inventory_authoritative(self) -> bool:
        with self._lock:
            inv = self._inventory
        return bool(inv is not None and inv.authoritative)

    def set_wsl_root_metadata_fallback(self, enabled: bool) -> None:
        """DP43-R03：运行期切换 WSL root metadata 权限。O(1)、无 WSL
        call、无 Tk、线程安全（Event 承载）。"""
        self._wsl.set_allow_root_metadata(bool(enabled))

    def rescan(self):
        """Request an immediate probe pass without doing probe work here."""
        self._last_windows = 0.0
        self._last_wsl = 0.0
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            t0 = time.time()
            try:
                self._tick()
            except Exception:
                pass
            elapsed = time.time() - t0
            self._wake.wait(max(0.3, 1.0 - elapsed))

    def _tick(self):
        cfg_m = self.config.get("monitor") or {}
        now = time.time()
        windows_enabled = bool(cfg_m.get("windows_enabled", True))
        if windows_enabled:
            if now - self._last_windows >= _num(
                    cfg_m.get("windows_scan_sec", 3.0), 3.0):
                self._last_windows = now
                t0 = time.perf_counter()
                self._gen += 1
                try:
                    # 一次 census 同时产出 terminal instances 与
                    # desktop hosts（plan2 §4.2：禁止第二个 psutil 扫描线程）
                    inv = scan_windows_inventory()
                    self._windows_cache = inv.terminal_instances
                    snap = SourceProbeSnapshot(
                        source="windows", generation=self._gen, observed_at=now,
                        authoritative=inv.authoritative,
                        instances=self._windows_cache,
                        error="; ".join(inv.diagnostics))
                    with self._lock:
                        self._inventory = inv
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
        else:
            # 用户关闭 Windows source：真正停止扫描（plan §10.1）——
            # 清 source snapshot 与缓存；_last_windows 不再推进，
            # 重新开启时因间隔已过而立即恢复扫描，无需重启。
            with self._lock:
                self._snapshot.pop("windows", None)
                self._inventory = None
            self._windows_cache = ()
            self.windows_scan_ms = 0.0
            self.windows_probe_error = ""
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
        # 双绑定表（v4.1.1 §9.3）：window-only（能否唤起窗口）与
        # observation-only（能否安全归属终端证据）彻底分离。
        self.window_bindings: dict[str, object] = {}
        self.terminal_observation_bindings: dict[str, object] = {}
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
        # Desktop source 共享 watcher 配置（grace/隐私截断/活跃窗口）
        self._source_cfg = dict(watcher_cfg)
        # 终端观察/解析/激活统一由 TerminalService 收口（v4plan §5.8）
        try:
            observer = make_observer(monitor_cfg)
        except Exception:
            observer = None
        self._terminal_service = WindowsTerminalService(
            observer, cfg=monitor_cfg)
        # Desktop 宿主 app 级激活（plan2 §10）：无状态、fail-closed，
        # 与 terminal service 彻底分离
        self._desktop_window_service = DesktopWindowService()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._rescan_requested = threading.Event()
        self._rescan_lock = threading.Lock()
        self._rescan_request_count = 0
        self._rescan_complete_count = 0
        self._thread: threading.Thread | None = None
        self._terminal_boot: threading.Thread | None = None
        self._exit_watcher = None
        try:
            from .process_watch import WindowsExitWatcher
            self._exit_watcher = WindowsExitWatcher()
        except Exception:
            self._exit_watcher = None
        self._last_resolve = 0.0
        self._instance_sig: tuple = ()
        self._last_logs_trim = 0.0
        # Windows native 保守 orphan 识别租约（v4.2.3 §2.4，runtime-only）
        self._native_terminal_leases: dict[str, NativeTerminalLease] = {}
        # v4.3 §4.2 UI 语义 revision：signature 不含只影响新鲜度的时间戳
        self._ui_revision = 0
        self._ui_signature: tuple = ()
        # v4.3.1 DP43-R08 §16.5：stale binding 的异步 repair 队列。
        # _repair_requests 按 agent_key 键控（同 agent coalesce）；
        # _repair_results bounded（16），bridge 每 tick 有界收割。
        self._repair_lock = threading.Lock()
        self._repair_requests: dict[str, ActivationRepairRequest] = {}
        self._repair_next_id = 1
        self._repair_results: "queue.Queue[ActivationRepairResult]" = (
            queue.Queue(maxsize=16))
        # ---- v4.4 Desktop sources（plan2 §5/§9）----
        # 生产 source 在 Phase 3/4 注册；这里保持空列表即可让 Monitor
        # 行为与 4.3.1 完全一致（测试注入 fake source）。
        self._desktop_sources: list = []
        # 当前 live DesktopHost（host_key → DesktopHost，runtime-only）
        self._desktop_hosts: dict[str, DesktopHost] = {}
        # 本轮 non-authoritative desktop source 的 session keys（stale 标记）
        self._desktop_stale_keys: set[str] = set()
        self._desktop_poll_count = 0

    # ------------------------------------------------------------ 生命周期
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake.clear()
        self._rescan_requested.clear()
        self._register_desktop_sources()
        self._probe.start()
        # UIA 后端初始化（comtypes/typelib/control 发现）可能耗时数秒，
        # 绝不能阻塞 UI 线程：放独立引导线程异步启动（plan §15/§32）。
        if self._terminal_service.observer is not None:
            self._terminal_boot = threading.Thread(
                target=self._start_terminal, name="deskpet-uia-boot",
                daemon=True)
            self._terminal_boot.start()
        if self._exit_watcher is not None:
            try:
                self._exit_watcher.start()
            except Exception:
                self._exit_watcher = None
        self._thread = threading.Thread(
            target=self._loop, name="deskpet-monitor", daemon=True)
        self._thread.start()

    def _register_desktop_sources(self):
        """按启用 kind 注册生产 Desktop sources（plan2 §5/§11）。

        只在 start() 执行一次；单元测试直接 _tick 不经过这里，
        由测试注入 fake source。不在 __init__ 注册是为了让 Monitor
        构造保持零 IO。不新增 desktop 专属开关（plan2 §11）：kind
        toggle 即 CLI+Desktop surface 总开关。
        """
        if self._desktop_sources:
            return
        cfg_m = dict(self.config.get("monitor") or {})
        enabled = self._enabled_kinds(cfg_m)
        if AgentKind.CODEX in enabled:
            self._desktop_sources.append(
                CodexDesktopSource(cfg=dict(self._source_cfg)))
        if AgentKind.ZCODE in enabled:
            from .zcode_desktop import ZCodeDesktopSource
            self._desktop_sources.append(
                ZCodeDesktopSource(cfg=dict(self._source_cfg)))

    def _start_terminal(self):
        # 启动失败不锁存：backend.available() 是唯一可用性事实来源
        # （首次 typelib 生成慢于等待窗口时，backend 稍后会自行就绪）
        try:
            self._terminal_service.start()
        except Exception:
            pass

    def request_stop(self):
        """只发停止信号（DP43-R17 §8.3）：stop event、probe stop、
        terminal service request stop、exit watcher request stop。

        全部 O(1)/bounded native 调用，绝不 join——join 职责归
        join_for_shutdown(timeout)。
        """
        self._stop.set()
        self._wake.set()
        self._probe.stop()
        if self._exit_watcher is not None:
            try:
                self._exit_watcher.request_stop()
            except Exception:
                pass
        # 先置终态（service/backend 拒绝晚到的 start），再等 boot 线程
        try:
            self._terminal_service.request_stop()
        except Exception:
            pass

    def join_for_shutdown(self, timeout: float) -> bool:
        """有界回收（timeout = App 全局 deadline 的剩余量）。

        顺序 join boot/core/probe/uia/exit watcher，每段只使用剩余
        预算（绝不自建 6s/3s timeout 累加）；watcher 的 handle 释放
        只在剩余时间允许且线程已退出时完成。timeout 后返回 False，
        不清除仍 live thread 的 ownership reference。
        """
        deadline = time.monotonic() + max(0.0, timeout)

        def _remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        exited = True
        boot = self._terminal_boot
        if boot is not None and boot is not threading.current_thread():
            # typelib 首次生成可能数秒：只等剩余预算
            boot.join(timeout=_remaining())
            if boot.is_alive():
                exited = False
            else:
                self._terminal_boot = None
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_remaining())
            if thread.is_alive():
                exited = False
        self._probe.join(timeout=_remaining())
        if self._probe._thread is not None and self._probe._thread.is_alive():
            exited = False
        if self._exit_watcher is not None:
            try:
                if not self._exit_watcher.join_for_shutdown(_remaining()):
                    exited = False
            except Exception:
                pass
        try:
            if not self._terminal_service.join_for_shutdown(_remaining()):
                exited = False
        except Exception:
            pass
        for watcher in self._watchers.values():
            try:
                watcher.release()
            except Exception:
                pass
        for source in self._desktop_sources:
            try:
                source.close()
            except Exception:
                pass
        return exited

    def stop(self):
        """兼容薄 wrapper（测试/旧入口）：signal + 固定 8s 预算 join。

        生产主路径（PetApp.request_quit）只允许调用 request_stop +
        join_for_shutdown(全局 deadline 剩余量)。
        """
        self.request_stop()
        self.join_for_shutdown(8.0)

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
                    terminal_window=self.window_bindings.get(key))
            return out

    def get_target(self, key: str) -> AgentTarget | None:
        if not key:
            return None
        return self.get_targets().get(key)

    # ------------------------------------------------------------ UI revision
    def _ui_signature_value(self, snap: Snapshot, inst: AgentInstance,
                            binding) -> tuple:
        """UI 展示相关的稳定值（v4.3 §4.2）。

        不含 snapshot.ts / binding.last_seen / validated_at 等不断变化
        但不影响 UI 的时间戳；terminal_attachment 只在高级诊断需要时
        纳入（当前 Dashboard 不展示，不进 signature）。
        """
        return (
            snap.key,
            snap.kind.value, snap.source,
            inst.project, inst.cwd,
            snap.status.value, snap.phase.value,
            snap.mode.value, snap.mode_raw,
            snap.policy,
            snap.goal, snap.summary, snap.waiting_detail,
            bool(snap.stale), snap.parser_health, snap.parser_detail,
            bool(binding.wakeable) if binding is not None else False,
            binding.confidence.value if binding is not None else "",
            binding.reason if binding is not None else "",
            binding.title if binding is not None else "",
        )

    def _refresh_ui_revision(self):
        """每轮最终 snapshots/window_bindings 更新后调用（monitor 线程）。"""
        items = []
        for key in sorted(self.snapshots):
            snap = self.snapshots.get(key)
            inst = self.instances.get(key)
            if snap is None or inst is None:
                continue
            items.append(self._ui_signature_value(
                snap, inst, self.window_bindings.get(key)))
        sig = tuple(items)
        if sig != self._ui_signature:
            with self.lock:
                self._ui_signature = sig
                self._ui_revision += 1

    def get_targets_if_changed(
            self, last_revision: int) -> tuple[int, dict[str, AgentTarget] | None]:
        """revision 未变返回 (revision, None)；变化时一次锁内复制 targets。

        不是新的事件系统：只为避免 UI 桥每 tick 重复复制/重建相同数据。
        """
        with self.lock:
            revision = self._ui_revision
        if revision == last_revision:
            return revision, None
        return revision, self.get_targets()

    def is_live_key(self, key: str) -> bool:
        """exact key 是否仍存活（activate 前的二次复核入口）。"""
        with self.lock:
            return key in self.instances

    def set_wsl_root_metadata_fallback(self, enabled: bool) -> None:
        """DP43-R03：UI 隐私开关的运行期撤权/授权入口（透传 probe）。"""
        self._probe.set_wsl_root_metadata_fallback(bool(enabled))

    def rescan(self):
        """提交一次合并式重新扫描请求并立即返回。

        本方法可由 Tk callback 调用。它只设置 Event；watcher cache reset、
        UIA topology refresh 和重新绑定全部由现有 Monitor worker 执行。
        重复点击在 worker 消费前天然合并，不增加线程或队列。
        """
        with self._rescan_lock:
            if self._stop.is_set() or self._rescan_requested.is_set():
                return False
            self._rescan_requested.set()
            self._rescan_request_count += 1
        self._probe.rescan()
        self._wake.set()
        return True

    def _consume_rescan_request(self) -> None:
        with self._rescan_lock:
            if not self._rescan_requested.is_set():
                return
            self._rescan_requested.clear()
        for watcher in self._watchers.values():
            try:
                watcher.reset_scan_cache()
            except Exception:
                pass
        try:
            self._terminal_service.refresh_observed_controls(force=True)
        except Exception:
            pass
        self._instance_sig = ()
        self._last_resolve = 0.0
        with self._rescan_lock:
            self._rescan_complete_count += 1
        self._log("已请求重新扫描（清 Process/Session/Terminal 运行期缓存）")

    def activate_target(self, key: str) -> ActivationResult:
        """UI 激活窗口的唯一入口（v4.1.1 §9.2；plan2 §10 surface dispatch）。

        UI 只携带 exact agent_key；服务内部重新核验 Agent live、
        window binding、WindowIdentity，fail-closed。TERMINAL →
        WindowsTerminalService；DESKTOP → DesktopWindowService（app 级
        唤醒，不承诺会话级跳转）。不依赖 UIA、不做 UIA refresh
        （v4.3.1 DP43-R08：stale 由 request_activation_repair 异步
        修复，Tk 不阻塞）。
        """
        if not key:
            return ActivationResult(ActivationCode.NO_BINDING)
        if not self.is_live_key(key):
            return ActivationResult(ActivationCode.AGENT_GONE)
        with self.lock:
            inst = self.instances.get(key)
        if inst is not None and is_desktop_instance(inst):
            return self._desktop_window_service.activate_host(inst)
        return self._terminal_service.activate_cached(
            key, is_agent_live=self.is_live_key)

    # ------------------------------------------------------------ 异步 repair（DP43-R08 §16.5）
    def request_activation_repair(self, agent_key: str) -> int:
        """O(1)、线程安全、同 agent coalesce：UI 在 STALE_WINDOW 后调用。"""
        key = str(agent_key or "")
        if not key:
            return 0
        with self._repair_lock:
            existing = self._repair_requests.get(key)
            if existing is not None:
                return existing.request_id
            rid = self._repair_next_id
            self._repair_next_id += 1
            self._repair_requests[key] = ActivationRepairRequest(
                rid, key, time.time() + 10.0)
            return rid

    def drain_activation_repairs(self, max_items: int = 4):
        """UI bridge 每 tick 有界收割（<=4）repair 结果。"""
        out = []
        for _ in range(max_items):
            try:
                out.append(self._repair_results.get_nowait())
            except queue.Empty:
                break
        return out

    def _process_repair_requests(self, now: float) -> None:
        """Monitor _tick 专属（§16.5）：drain bounded repair 请求 →
        repair binding → publish bounded result。不新增线程。"""
        with self._repair_lock:
            pending = list(self._repair_requests.values())
            self._repair_requests.clear()
        for req in pending:
            if req.expires_at <= now:
                continue   # 过期：不执行也不发布（用户早已离开该动作）
            with self.lock:
                instances = list(self.instances.values())
                live = req.agent_key in self.instances
            if not live:
                self._publish_repair_result(req, False)
                continue
            repaired = self._terminal_service.repair_binding(
                req.agent_key, instances=instances, now=now)
            self._log(f"终端绑定修复{'成功' if repaired else '失败'}: "
                      f"{req.agent_key}")
            self._publish_repair_result(req, repaired)

    def _publish_repair_result(self, req: ActivationRepairRequest,
                               repaired: bool) -> None:
        try:
            self._repair_results.put_nowait(
                ActivationRepairResult(req.request_id, req.agent_key,
                                       repaired))
        except queue.Full:
            try:
                self._repair_results.get_nowait()
            except queue.Empty:
                pass
            try:
                self._repair_results.put_nowait(
                    ActivationRepairResult(req.request_id, req.agent_key,
                                           repaired))
            except queue.Full:
                pass

    def terminal_available(self) -> bool:
        # 不再使用 _terminal_failed 锁存：UIA 首次初始化（typelib 生成）
        # 可能超过启动等待窗口，之后 backend 会自行变为 available；以
        # backend 当前状态为准，避免 UI 永远显示 "UIA 不可用"。
        return self._terminal_service.available()

    def terminal_startup_error(self) -> str:
        """UIA 启动失败的非敏感原因（诊断展示用）。"""
        return self._terminal_service.startup_error()

    def rediscover_terminal(self):
        """兼容入口：终端重发现同样不得在调用线程同步等待 UIA。"""
        self.rescan()

    def stats(self) -> dict:
        wsl = self._probe._wsl
        out = {
            "targets": len(self.instances),
            "wsl_spawn_count": getattr(wsl, "spawn_count", 0),
            "wsl_scan_count": getattr(wsl, "scan_count", 0),
            "wsl_scan_ms": round(getattr(wsl, "scan_ms", 0.0), 1),
            "windows_scan_ms": round(self._probe.windows_scan_ms, 1),
            "metadata_pid_count": getattr(wsl, "metadata_pid_count", 0),
            "detached_filtered_count": getattr(wsl, "detached_filtered_count", 0),
            "exit_watched": (self._exit_watcher.watched_count()
                             if self._exit_watcher is not None else 0),
            "native_terminal_leases": len(self._native_terminal_leases),
            "rescan_requests": self._rescan_request_count,
            "rescan_completed": self._rescan_complete_count,
            "desktop_hosts": len(self._desktop_hosts),
            "desktop_sources": len(self._desktop_sources),
            "desktop_polls": self._desktop_poll_count,
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
    def _poll_sec(self) -> float:
        """每轮动态读取并 clamp file_poll_sec（plan §11）。

        配置文件手改异常值也不能制造高频 loop；运行中修改下一轮即生效，
        不需要重启。
        """
        try:
            value = float((self.config.get("monitor") or {}).get(
                "file_poll_sec", 0.5))
        except (TypeError, ValueError):
            value = 0.5
        return max(0.2, min(5.0, value))

    def _loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            t0 = time.time()
            try:
                self._tick()
            except Exception as exc:
                self._log(f"监控异常: {exc!r}")
            elapsed = time.time() - t0
            self._wake.wait(max(0.15, self._poll_sec() - elapsed))

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
                if getattr(inst, "surface", AgentSurface.TERMINAL) is AgentSurface.DESKTOP:
                    # Desktop 逻辑会话的生死由 desktop source 的
                    # authoritative snapshot 与 host lease 管理（plan2 §5/§9），
                    # 绝不按 process census absence 判死；本轮保留，
                    # 退场判定在 _poll_desktop_sources。
                    merged[key] = inst
                    continue
                source_disabled = (
                    (inst.source == "windows" and not windows_enabled)
                    or (inst.source.startswith("wsl:") and not wsl_enabled))
                kind_disabled = inst.kind not in enabled
                if source_disabled:
                    # 用户关闭该 source：按 authoritative empty 处理，不留 ghost
                    exits.append((key, "source-disabled"))
                elif kind_disabled:
                    # 用户显式关闭 kind 必须立即生效（v4.2.3 §4）：
                    # 用户配置意图高于 probe health，即使该 source 本轮
                    # authoritative=False 也必须退出。
                    exits.append((key, "kind-disabled"))
                elif inst.source in authoritative:
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
        self.window_bindings.pop(key, None)
        self.terminal_observation_bindings.pop(key, None)
        # 运行期 terminal runtime binding 级联失效（不再有 manual path）
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
        Desktop host 事件按 host_key 核对后 fan-out 删除该 host 的
        全部逻辑会话（plan2 §9：N sessions 共用一个 host handle）。
        """
        if self._exit_watcher is None:
            return
        dropped_hosts: list[str] = []
        for ev in self._exit_watcher.drain():
            with self.lock:
                inst = self.instances.get(ev.key)
                if inst is not None:
                    if (inst.pid != ev.pid
                            or str(inst.process_token) != ev.process_token):
                        continue
                    self._commit_exit(ev.key, "process-exit-event", now)
                    continue
                host = self._desktop_hosts.get(ev.key)
                if host is not None:
                    if (host.pid != ev.pid
                            or str(host.process_token) != ev.process_token):
                        continue
                    self._drop_desktop_host_locked(
                        ev.key, "desktop-host-exit-event", now)
                    dropped_hosts.append(ev.key)
        for host_key in dropped_hosts:
            self._notify_sources_host_dropped(host_key)

    # ------------------------------------------------------------ desktop lifecycle
    def _sync_desktop_hosts(self, now: float):
        """从最新 inventory 同步 host 表；census 权威缺席即 drop（plan2 §9）。"""
        hosts = self._probe.desktop_hosts()
        if self._probe.inventory_authoritative():
            live_keys = {h.host_key for h in hosts}
            dropped: list[str] = []
            with self.lock:
                for host_key in list(self._desktop_hosts):
                    if host_key not in live_keys:
                        self._drop_desktop_host_locked(
                            host_key, "desktop-host-census-absence", now)
                        dropped.append(host_key)
            for host_key in dropped:
                self._notify_sources_host_dropped(host_key)
        for host in hosts:
            self._desktop_hosts[host.host_key] = host

    def _drop_desktop_host_locked(self, host_key: str, reason: str,
                                  now: float) -> None:
        """删除一个 host 的全部逻辑会话（调用方必须已持有 self.lock）。"""
        self._desktop_hosts.pop(host_key, None)
        for key, inst in list(self.instances.items()):
            if (getattr(inst, "surface", AgentSurface.TERMINAL)
                    is AgentSurface.DESKTOP and inst.host_key == host_key):
                self._commit_exit(key, reason, now)
        for key in list(self._desktop_stale_keys):
            inst = self.instances.get(key)
            if inst is None or getattr(inst, "host_key", "") == host_key:
                self._desktop_stale_keys.discard(key)

    def _notify_sources_host_dropped(self, host_key: str) -> None:
        """锁外通知各 source 清空该 host 的运行期状态（plan2 §9）。"""
        for source in self._desktop_sources:
            try:
                source.drop_host(host_key)
            except Exception:
                pass

    def _poll_desktop_sources(self, instances: dict[str, AgentInstance],
                              session_obs: dict[str, Observation],
                              now: float):
        """plan2 §5 顺序 3–5：terminal claims → desktop poll → merge。

        source non-authoritative：保留 last good、标记 stale、绝不判死
        （plan2 §13）；authoritative 且某 session 缺席 → source 内部
        退场。SQL/rollout 细节全部在各 source 内部，Monitor 不感知。
        """
        if not self._desktop_sources:
            return
        claims = build_terminal_claims(self._watchers, instances)
        hosts = tuple(self._desktop_hosts.values())
        enabled = self._enabled_kinds(
            self.config.get("monitor") or {})
        for source in self._desktop_sources:
            if source.kind not in enabled:
                # kind toggle 关闭：立即清除该 source 的全部会话
                with self.lock:
                    for key, inst in list(self.instances.items()):
                        if (getattr(inst, "surface", AgentSurface.TERMINAL)
                                is AgentSurface.DESKTOP
                                and inst.kind == source.kind):
                            self._commit_exit(key, "kind-disabled", now)
                continue
            try:
                snap = source.poll(hosts, claims, now)
            except Exception as exc:
                self._log(f"{getattr(source, 'kind', '?')} desktop source 异常: {exc!r}")
                snap = DesktopSourceSnapshot(
                    authoritative=False, diagnostics=(repr(exc),))
            found: dict[str, AgentInstance] = {
                inst.key: inst for inst in snap.instances}
            with self.lock:
                self._desktop_poll_count += 1
                if snap.authoritative:
                    for key, inst in list(self.instances.items()):
                        if (getattr(inst, "surface", AgentSurface.TERMINAL)
                                is AgentSurface.DESKTOP
                                and inst.kind == source.kind
                                and key not in found):
                            self._commit_exit(
                                key, "desktop-source-absence", now)
                    for key, inst in found.items():
                        if key not in self.instances:
                            self._log(f"发现桌面会话 "
                                      f"{inst.kind.label} (host pid={inst.host_pid})")
                        self.instances[key] = inst
                        self._desktop_stale_keys.discard(key)
                    # 只合并权威观察；缺失的 key 由 reduce 保守降级
                    for key, obs in snap.observations.items():
                        if key in found:
                            session_obs[key] = obs
                else:
                    for key, inst in list(self.instances.items()):
                        if (getattr(inst, "surface", AgentSurface.TERMINAL)
                                is AgentSurface.DESKTOP
                                and inst.kind == source.kind):
                            self._desktop_stale_keys.add(key)

    def _prune_detached_native(self, instances: dict[str, AgentInstance],
                               probe_snap: dict[str, SourceProbeSnapshot],
                               now: float) -> list[str]:
        """Windows native 保守 orphan 识别（v4.2.3 §2.4）。

        有条件的防御性补强，不声称检测所有 native ConPTY orphan：
          * 只有曾获得 windows-ancestor CONFIRMED 强绑定的 Agent 才
            arm lease（从未强绑定的进程不能因没有窗口证据被删除）；
          * 外部父进程连续 2 个 authoritative generation 不存在且强
            绑定没有恢复才 commit exit（默认约 6 秒，吸收 parent()
            短暂读取失败/launcher 替换）；
          * source 非 authoritative / parent_alive 为 None/True / 只缺
            一轮 → 不递增、不判死。
        返回本轮 commit exit 的 key（调用方需同步从局部 instances/
        session_obs 删除，确保同一 tick 不再构建 snapshot）。
        """
        windows_snap = probe_snap.get("windows")
        if windows_snap is None or not windows_snap.authoritative:
            return []
        generation = windows_snap.generation
        commit_keys: list[str] = []
        with self.lock:
            for key, inst in instances.items():
                if inst.source != "windows":
                    continue
                if getattr(inst, "surface", AgentSurface.TERMINAL) is AgentSurface.DESKTOP:
                    # Desktop 会话无 terminal binding 概念，不参与 lease
                    continue
                binding = self.window_bindings.get(key)
                strong = (binding is not None
                          and binding.native_strong_binding)
                lease = self._native_terminal_leases.get(key)
                if strong:
                    if lease is None:
                        lease = NativeTerminalLease()
                        self._native_terminal_leases[key] = lease
                    lease.broken_generations = 0
                    lease.armed_window_pid = binding.window.pid
                    lease.last_probe_generation = generation
                    continue
                if lease is None or lease.armed_window_pid == 0:
                    # 从未被强绑定：保留 UNKNOWN，不判死
                    continue
                if lease.last_probe_generation == generation:
                    # 本 authoritative generation 已计数（同 generation
                    # 的多个 monitor tick 不得重复递增）
                    continue
                lease.last_probe_generation = generation
                if inst.external_parent_alive is False:
                    lease.broken_generations += 1
                    if lease.broken_generations >= 2:
                        commit_keys.append(key)
                # external_parent_alive 为 None/True：不递增，不判死
            for key in commit_keys:
                self._native_terminal_leases.pop(key, None)
                self._commit_exit(key, "terminal-detached-native", now)
            # 租约生命周期与实例一致：实例已消失（其他 exit 路径）即清理
            for key in list(self._native_terminal_leases):
                if key not in self.instances:
                    self._native_terminal_leases.pop(key, None)
        return commit_keys

    def _tick(self):
        cfg_m = dict(self.config.get("monitor") or {})
        now = time.time()
        self._consume_rescan_request()
        self._merge_instances(now)
        probe_snap = self._probe.snapshot()

        # WindowsExitWatcher 注册 + 事件驱动退出（census 兜底）。
        # 顺序契约（plan §10.3）：merge → snapshot → register → drain
        # → 重新 snapshot——保证 _commit_exit 后同一 tick 不再为已退出
        # Agent 重建 snapshot/binding/observation。
        # Desktop：每 host 一个 handle，绝不按 session 注册（plan2 §9）。
        self._sync_desktop_hosts(now)
        if self._exit_watcher is not None:
            with self.lock:
                insts = dict(self.instances)
            for inst in insts.values():
                if (inst.source == "windows"
                        and getattr(inst, "surface", AgentSurface.TERMINAL)
                        is AgentSurface.TERMINAL):
                    self._exit_watcher.register(inst)
            for host in list(self._desktop_hosts.values()):
                self._exit_watcher.register(host)
            self._drain_exit_events(now)

        with self.lock:
            instances = dict(self.instances)

        # 1) 会话观察：每个 watcher 每轮都 poll（即使该 kind 当前为 0），
        #    让 BaseWatcher 的全清理语义真正发生（v4plan §4.6）。
        #    DESKTOP 逻辑会话不进入 CLI watcher（plan2 §5：desktop source
        #    自己产出 Observation；CLI 的 mutual-unique 绑定不适用 1:N）。
        session_obs: dict[str, Observation] = {}
        by_kind: dict[AgentKind, list[AgentInstance]] = {}
        for inst in instances.values():
            if getattr(inst, "surface", AgentSurface.TERMINAL) is AgentSurface.DESKTOP:
                continue
            by_kind.setdefault(inst.kind, []).append(inst)
        for kind, watcher in self._watchers.items():
            insts = by_kind.get(kind, [])
            try:
                session_obs.update(watcher.poll(insts))
            except Exception as exc:
                self._log(f"{kind.value} 解析异常: {exc!r}")

        # 2) 终端观察（poll 内含 topology 事件重学习）
        self._terminal_service.poll(now)

        # 3) 终端绑定（节流：实例集合或 topology 变化、或每 3s）。
        #    DESKTOP target 不进 terminal service（plan2 §5.6/§10）。
        terminal_instances = [
            inst for inst in instances.values()
            if getattr(inst, "surface", AgentSurface.TERMINAL)
            is AgentSurface.TERMINAL]
        observer = self._terminal_service.observer
        topo_sig = (observer.topology_signature()
                    if observer is not None else ())
        sig = (tuple(sorted(inst.key for inst in terminal_instances)), topo_sig)
        if sig != self._instance_sig or now - self._last_resolve >= 3.0:
            self._instance_sig = sig
            self._last_resolve = now
            try:
                self.window_bindings, self.terminal_observation_bindings = \
                    self._terminal_service.resolve(terminal_instances, now)
            except Exception:
                self.window_bindings = {}
                self.terminal_observation_bindings = {}

        # 3.5) Windows native 保守 orphan 识别（v4.2.3 §2.4）：resolve
        # 之后、状态融合之前；本轮 commit exit 的 key 同 tick 不再
        # 构建 snapshot/session 观察。
        for key in self._prune_detached_native(instances, probe_snap, now):
            instances.pop(key, None)
            session_obs.pop(key, None)

        # 3.6) stale binding 异步 repair（v4.3.1 DP43-R08 §16.5）：
        # UI 提交的 repair 请求在本线程执行 UIA refresh + re-resolve
        # （Tk 绝不同步等待），结果发布到 bounded 队列由 bridge 收割。
        self._process_repair_requests(now)

        # 3.7) Desktop sources（plan2 §5 顺序 3–5）：terminal claims →
        # poll → 合并逻辑会话/观察。之后重新拷贝 instances 供融合使用。
        self._poll_desktop_sources(instances, session_obs, now)
        with self.lock:
            instances = dict(self.instances)

        # 4) 状态融合
        grace = _num(cfg_m.get("activity_grace_sec", 10.0), 10.0)
        stale_desktop = set(self._desktop_stale_keys)
        new_snaps: dict[str, Snapshot] = {}
        for key, inst in instances.items():
            session = session_obs.get(key)
            terminal = self._terminal_observation(inst, now, grace)
            prev = self.snapshots.get(key)
            snap = reduce_state(inst, session, terminal, prev, now)
            sp = probe_snap.get(inst.source)
            snap.stale = bool(
                (sp is not None and not sp.authoritative)
                or key in stale_desktop)
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
        # 最终 snapshots/window_bindings 已更新：计算本轮 UI 语义
        # revision（v4.3 §4.2，monitor 线程内，读取自身刚写入的数据）
        self._refresh_ui_revision()
        self._trim_logs(now)

    def _terminal_observation(self, inst: AgentInstance, now: float,
                              grace: float) -> Observation | None:
        """observation binding 驱动的终端证据归属（v4.1.3 §7/§23）。

        只有 CONFIRMED/HIGH 的 observation binding 才允许把 WAITING/
        activity 证据归给该 Agent；低置信 Window 候选（AMBIGUOUS/
        唯一窗口兜底）不生成 binding → 没有终端证据（宁可没有，
        也不错归）。
        """
        obs_binding = self.terminal_observation_bindings.get(inst.key)
        if obs_binding is None:
            return None
        if obs_binding.confidence not in (
                ObservationBindingConfidence.CONFIRMED,
                ObservationBindingConfidence.HIGH):
            return None
        waiting = self._terminal_service.waiting_observation(
            obs_binding.control_id)
        if waiting is not None and waiting.live(now):
            # 审批文案的识别器种类必须与绑定的 AgentKind 一致：
            # Codex control 上命中 Claude 审批 → 不能归属给 Codex。
            if waiting.agent_kind is None or waiting.agent_kind == inst.kind:
                return waiting
        # 泛化终端活动（agent_kind=None）只是 fallback 证据，
        # 能否覆盖会话状态由 StateReducer 的证据强弱规则决定。
        return self._terminal_service.activity_observation(
            obs_binding.control_id, now, grace)

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
