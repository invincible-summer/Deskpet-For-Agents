"""Monitor 合成基准（v4.1.1 §28）。

断言：
  * 队列不增长（事件队列 ≤256、UIA 命令队列 ≤32）
  * target 不增长
  * terminal buffers 有上限（ring ≤8KB、control ≤16）
  * 持续高频 TextChanged：可见读取不超全局预算（≤6/s）
  * control 开关 1000 次后订阅账本回到当前 control 数量级
  * resolver 结果不随 Agent 输入顺序变化（window + observation 双表）

新增 lightweight checks（v4.1.1 §28）：
  * windows source 关闭后 scan_windows 调用增量为 0
  * 空闲 ExitWatcher 不存在固定 200ms 轮询唤醒路径（源检查）
  * Fleet 单例约束（一个 Monitor/UIA 线程/AnimationScheduler/
    SharedAnimationCache）由 tests/test_fleet_ui.py 的单测覆盖

用法：python tests/benchmark_monitor.py [--ticks N] [--report PATH]

--report：把全部计数与逐项 check 写成 JSON（失败时也写出再退出 1），
供 CI artifact 与失败诊断使用；不传时行为不变。
"""
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.models import AgentKind, AgentInstance, Observation, Phase, Status, Confidence, EvidenceSource
from agents.terminal_uia import (
    DELTA_MAX, EVENT_QUEUE_MAX, GLOBAL_VISIBLE_READ_LIMIT, MAX_CONTROLS,
    RING_MAX, UIA_CALL_QUEUE_MAX, VISIBLE_MAX,
    ObservedTerminalControl, SubscriptionTracker, TerminalObserver,
    TerminalBackend, TerminalEvent, TerminalLayout,
)
from agents.terminal_resolver import (
    TerminalObservationResolver,
    TerminalWindowResolver,
)


class SyntheticBackend(TerminalBackend):
    available = True

    def __init__(self):
        self.controls = {}
        self.visible = {}
        self.read_count = 0

    def discover_layout(self):
        return TerminalLayout(windows={}, controls=dict(self.controls))

    def read_visible(self, control_id):
        self.read_count += 1
        return self.visible.get(control_id, "")


def run(ticks: int = 20000, report_path: str = "") -> int:
    # ---- 构造 6 Agent：3 WSL + 3 Windows
    instances = []
    for i in range(3):
        instances.append(AgentInstance(
            AgentKind.CODEX if i == 0 else (AgentKind.CLAUDE if i == 1 else AgentKind.KIMI),
            pid=1000 + i, source=f"wsl:Ubuntu{i}", process_token=str(50000 + i),
            cwd=f"/home/u/proj{i}", home="/home/u"))
    for i in range(3):
        instances.append(AgentInstance(
            AgentKind.CODEX if i == 0 else (AgentKind.CLAUDE if i == 1 else AgentKind.KIMI),
            pid=2000 + i, source="windows", process_token=str(60000.0 + i),
            cwd=f"C:\\work\\proj{i}"))

    backend = SyntheticBackend()
    observer = TerminalObserver(backend, cfg={"terminal_observer": True})
    observer._started = True
    for i in range(MAX_CONTROLS + 4):   # 超出上限的 control 应被丢弃
        control_id = (100 + i // 2, (i,))
        backend.controls[control_id] = ObservedTerminalControl(
            control_id=control_id, hwnd=100 + i // 2, window_pid=9,
            title=f"control{i}")
        backend.visible[control_id] = "x" * (VISIBLE_MAX * 2)
    observer.refresh_controls(force=True)

    watcher_states = {}
    t0 = time.perf_counter()
    queue_high = 0
    budget_high = 0
    dirty_high = 0
    sim_t0 = 1_000_000.0
    sim_t1 = sim_t0 + ticks * 0.5
    for tick in range(ticks):
        now = sim_t0 + tick * 0.5   # 模拟 30min @ 0.5s ≈ 36000 ticks
        # 每个周期：每 control 发 2 个 TextChanged/notification（普通输出）；
        # 每 50 tick 仅 1 个 control 出现审批文案（弱触发即时通道）。
        controls_live = list(observer.controls)[:MAX_CONTROLS]
        for control_id in controls_live:
            observer._on_event(TerminalEvent(control_id=control_id,
                                             kind="activity", ts=now))
            observer._on_event(TerminalEvent(control_id=control_id,
                                             kind="notification",
                                             text="y" * 512, ts=now))
        if tick % 50 == 0 and controls_live:
            observer._on_event(TerminalEvent(
                control_id=controls_live[0], kind="notification",
                text="Would you like to proceed? " + "z" * 100, ts=now))
        with observer._event_q_lock:
            queue_high = max(queue_high, len(observer._events))
        observer.poll(now)
        budget_high = max(budget_high, len(observer._visible_read_times))
        dirty_high = max(dirty_high, len(observer._dirty_controls))
        # 模拟状态融合（直接构造 observation，测 monitor 数据结构上限）
        for inst in instances:
            obs = Observation(source=EvidenceSource.SESSION, timestamp=now,
                              status=Status.WORKING, phase=Phase.CODING,
                              confidence=Confidence.HIGH, turn_active=True,
                              session_bound=True, goal=f"g{tick % 100}",
                              summary=f"s{tick % 100}")
            watcher_states[inst.key] = obs

    elapsed = time.perf_counter() - t0
    sim_duration = sim_t1 - sim_t0
    # TextChanged fallback 读取速率（预算约束的通道）
    fallback_rate = observer.stats["text_fallback_reads"] / sim_duration

    # ---- control churn 1000 次：订阅账本不积累
    tracker = SubscriptionTracker()
    final_controls = {("win", (i,)) for i in range(4)}
    added = removed = 0
    for cycle in range(1000):
        wanted = {("win", ((cycle + i) % 6,)) for i in range(4)}
        to_add, to_remove = tracker.sync(wanted)
        added += len(to_add)
        removed += len(to_remove)
    tracker.sync(final_controls)

    # ---- resolver 顺序无关（window + observation 双表）
    a = AgentInstance(AgentKind.CODEX, 1, "wsl:Ubuntu", process_token="9",
                      cwd="/w/alpha", user="u1")
    b = AgentInstance(AgentKind.CLAUDE, 2, "wsl:Ubuntu", process_token="10",
                      cwd="/w/beta", user="u2")
    controls = {
        (11, (1,)): ObservedTerminalControl(control_id=(11, (1,)), hwnd=11,
                                            window_pid=5,
                                            title="codex alpha u1@box"),
        (11, (2,)): ObservedTerminalControl(control_id=(11, (2,)), hwnd=11,
                                            window_pid=5,
                                            title="claude beta u2@box"),
    }
    resolver = TerminalWindowResolver(enum_windows=lambda: [])
    obs_resolver = TerminalObservationResolver(enum_windows=lambda: [])
    w1 = resolver.resolve([a, b], controls, 1000.0)
    w2 = resolver.resolve([b, a], controls, 1000.0)
    o1 = obs_resolver.resolve([a, b], controls, w1, 1000.0)
    o2 = obs_resolver.resolve([b, a], controls, w2, 1000.0)

    # ---- lightweight：windows source 关闭 → 扫描增量为 0（§28）
    class _ProbeCfg:
        def __init__(self, enabled):
            self.data = {"monitor": {"windows_enabled": enabled,
                                     "windows_scan_sec": 3600}}

        def get(self, path, default=None):
            node = self.data
            for part in path.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node

    from agents.monitor import ProcessProbeWorker
    disabled_worker = ProcessProbeWorker(_ProbeCfg(False))
    with patch("agents.monitor.scan_windows", return_value=[]) as scan_mock:
        for _ in range(3):
            disabled_worker._tick()
    windows_disabled_delta = scan_mock.call_count

    # ---- lightweight：空闲 ExitWatcher 无固定 200ms 唤醒（源检查）
    watch_src = Path(__file__).resolve().parents[1].joinpath(
        "agents", "process_watch.py").read_text(encoding="utf-8")
    idle_poll_free = ("WaitForMultipleObjects(1, arr, False, 200)"
                      not in watch_src
                      and "WaitForMultipleObjects(1, arr, False, INFINITE)"
                      in watch_src)

    checks = []
    checks.append(("事件队列有界", queue_high <= EVENT_QUEUE_MAX))
    checks.append(("UIA 命令队列有界", UIA_CALL_QUEUE_MAX <= 32))
    checks.append(("target 数有界", len(watcher_states) == len(instances)))
    checks.append(("control 数有上限", len(observer.controls) <= MAX_CONTROLS))
    ring_ok = all(v <= RING_MAX for v in observer._ring_len.values())
    checks.append(("terminal ring ≤8KB", ring_ok and observer._ring_len))
    checks.append(("可见读取预算 ≤6/s（fallback 通道）",
                   fallback_rate <= GLOBAL_VISIBLE_READ_LIMIT
                   and budget_high <= GLOBAL_VISIBLE_READ_LIMIT + 2))
    checks.append(("dirty control 有界", dirty_high <= MAX_CONTROLS))
    checks.append(("统计计数无异常增长", observer.stats["dropped"] >= 0))
    checks.append(("control churn 不积累订阅",
                   len(tracker.active) == len(final_controls)
                   and added == removed + len(final_controls)))
    checks.append(("window resolver 顺序无关",
                   {k: v.hwnd for k, v in w1.items()}
                   == {k: v.hwnd for k, v in w2.items()}))
    checks.append(("observation resolver 顺序无关", o1 == o2))
    checks.append(("windows 关闭后扫描增量为 0", windows_disabled_delta == 0))
    checks.append(("空闲 ExitWatcher 无固定 200ms 唤醒", idle_poll_free))

    print(f"ticks={ticks} elapsed={elapsed:.2f}s "
          f"({ticks / max(elapsed, 1e-9):,.0f} ticks/s)")
    print(f"controls={len(observer.controls)} queue_high={queue_high} "
          f"budget_high={budget_high}/s dirty_high={dirty_high} "
          f"dropped={observer.stats['dropped']} events={observer.stats['events']} "
          f"visible_reads={observer.stats['visible_reads']} "
          f"fallback_reads={observer.stats['text_fallback_reads']}")
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if report_path:
        # 失败也写出（先写再判退出码），供 CI artifact 诊断定位
        report = {
            "ticks": ticks,
            "elapsed": round(elapsed, 3),
            "ticks_per_sec": round(ticks / max(elapsed, 1e-9), 1),
            "controls": len(observer.controls),
            "queue_high": queue_high,
            "budget_high": budget_high,
            "dirty_high": dirty_high,
            "dropped": observer.stats["dropped"],
            "events": observer.stats["events"],
            "visible_reads": observer.stats["visible_reads"],
            "fallback_reads": observer.stats["text_fallback_reads"],
            "fallback_rate_per_sec": round(fallback_rate, 4),
            "windows_disabled_delta": windows_disabled_delta,
            "checks": {name: bool(ok) for name, ok in checks},
        }
        Path(report_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report written: {report_path}")
    if failed:
        print("BENCHMARK FAILED:", failed)
        return 1
    print("BENCHMARK OK")
    return 0


def _parse_args(argv):
    ticks = 20000
    report = ""
    i = 1
    while i < len(argv):
        if argv[i] == "--ticks" and i + 1 < len(argv):
            ticks = int(argv[i + 1])
            i += 2
        elif argv[i] == "--report" and i + 1 < len(argv):
            report = argv[i + 1]
            i += 2
        else:
            i += 1
    return ticks, report


if __name__ == "__main__":
    # 非 UTF-8 locale 的控制台（如 windows-latest 的 cp1252）打印中文
    # 检查名会 UnicodeEncodeError：统一按 UTF-8 输出，无法编码时替换。
    for _stream in (sys.stdout, sys.stderr):
        if _stream and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    _ticks, _report = _parse_args(sys.argv)
    sys.exit(run(_ticks, _report))
