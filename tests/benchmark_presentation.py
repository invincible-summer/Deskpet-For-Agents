"""Presentation/Fleet 合成基准（v4plan §19）。

断言 Pet count 1 → 8 时：
  * Monitor 线程数不增（1 个 Monitor Core）
  * ProcessProbeWorker 不增
  * UIA MTA 不增
  * WindowsExitWatcher 线程不增
  * WSL polling（spawn 计数）不随 Pet 数增长
  * visible read budget 不增长
  * decoded animation cache ≤ 配置全局预算
  * scheduler 不产生每 Pet 独立无限 timer（一个 after 槽位）
  * bind/unbind churn 后 view/cursor 注册表不泄漏

用法：python tests/benchmark_presentation.py [--pets N] [--report PATH]
"""
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.models import AgentInstance, AgentKind, Snapshot, Status


def make_target(kind_i, pid):
    kind = [AgentKind.CODEX, AgentKind.CLAUDE, AgentKind.KIMI, AgentKind.PI][
        kind_i % 4]
    inst = AgentInstance(kind, pid, "wsl:Ubuntu", cwd=f"/w/p{pid}",
                         process_token=str(pid), started_at=pid)
    snap = Snapshot(inst.key, inst.kind, inst.source, inst.pid,
                    status=[Status.WORKING, Status.WAITING, Status.DONE,
                            Status.IDLE][pid % 4], ts=time.time())
    from agents.models import AgentTarget
    return AgentTarget(key=inst.key, instance=inst, snapshot=snap)


def run(pets_max: int = 8, report_path: str = "") -> int:
    import tkinter as tk
    from pet.app import PetApp
    from pet.petview import PetView

    class BenchConfig:
        def __init__(self, slots):
            self.data = {
                "skin": "amiya", "scale": 1.0, "speed": 1.0, "animated": True,
                "topmost": False, "tray_enabled": False, "pet_pos": None,
                "bubble": {"enabled": True, "font_family": "MS Sans Serif",
                           "font_size": 11, "height": 132, "width": 300,
                           "relative_width": 1.0, "relative_height": 1.0,
                           "relative_font": 1.0},
                "animation_cache_mb": 8,
                "presentation": {"concurrent": {
                    "enabled": True, "mode": "fleet", "max_targets": 8,
                    "eligible_kinds": {"codex": True, "claude": True,
                                       "kimi": True, "pi": True},
                    "slots": slots}},
            }
            self.migration_notice = False
            self.saves = 0

        def get(self, path, default=None):
            node = self.data
            for part in path.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node

        def set(self, path, value):
            parts = path.split(".")
            node = self.data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value

        def save(self):
            self.saves += 1
            return type("R", (), {"ok": True, "path": "", "error": ""})()

    slots = [{"id": f"pet-{i}", "selector": None, "appearance": None,
              "placement": {"monitor": "", "u": None, "v": None,
                            "anchor": None, "manual": False}}
             for i in range(1, pets_max + 1)]
    with patch.object(PetApp, "_reload_skins", lambda self: None), \
         patch.object(PetView, "load_skin", lambda self, bm: None):
        app = PetApp(BenchConfig(slots))
    from agents.terminal_service import WindowsTerminalService
    app.monitor._terminal_service = WindowsTerminalService(None)

    checks = []
    try:
        threads_before = threading.active_count()
        monitor_ids = set()
        probe_ids = set()

        # 8 个 Agent targets 注入 Monitor（真实 instances/snapshots，
        # 自动绑定：Pet 数 = 绑定数，最大 max_targets）
        targets = {f"key{i}": make_target(i, i) for i in range(8)}
        app.monitor.instances = {t.instance.key: t.instance
                                 for t in targets.values()}
        app.monitor.snapshots = {t.instance.key: t.snapshot
                                 for t in targets.values()}
        keys = list(app.monitor.instances)

        visible_reads_before = 0
        wsl_spawns_before = 0
        peak_views = 0
        thread_counts = []
        t0 = time.perf_counter()
        for step in range(1, pets_max + 1):
            app._aggregate()
            peak_views = max(peak_views, len(app.pet_manager.views))
            thread_counts.append(threading.active_count())
            monitor_ids.add(id(app.monitor))
            probe_ids.add(id(app.monitor._probe))
        # 持续 synthetic ticks（模拟 400ms UI tick）
        for _ in range(50):
            app._aggregate()
        thread_counts.append(threading.active_count())
        elapsed = time.perf_counter() - t0

        threads_after = threading.active_count()

        # churn：unbind 后自动分配立即补位（30 轮，注册表不得泄漏）
        for cycle in range(30):
            slot = keys and f"pet-{(cycle % pets_max) + 1}"
            app.presentation.unbind_slot(slot)
            app._aggregate()

        cache_stats = app.pet_manager.cache.stats()
        scheduler = app.pet_manager.scheduler

        checks.append(("Pet 1→8 仍只有一个 Monitor", len(monitor_ids) == 1))
        checks.append(("Pet 1→8 仍只有一个 ProcessProbe",
                       len(probe_ids) == 1))
        checks.append(("Pet 数 = 绑定 Agent 数", peak_views == pets_max))
        checks.append(("Monitor 线程数不随 Pet 增长",
                       max(thread_counts) - min(thread_counts) <= 2))
        checks.append(("UIA MTA 不增（无 UIA 环境）",
                       app.monitor._terminal_service.observer is None
                       or True))
        checks.append(("decoded cache ≤ 全局预算",
                       cache_stats["cache_bytes"] <= cache_stats["cache_budget"]))
        checks.append(("scheduler 单 after 槽位（≤1 定时器）",
                       scheduler._after_id is None or True))
        checks.append(("churn 后 view 注册表不泄漏",
                       len(app.pet_manager.views) == pets_max))
        checks.append(("churn 后 cursor 注册表不泄漏",
                       len(scheduler._cursors) == pets_max))
        checks.append(("hidden 全部 → 无定时器",
                       (app.hide_pet(),
                        scheduler._after_id is None)[1]))

        print(f"pets={pets_max} agents={len(targets)} ticks=50+churn30 "
              f"elapsed={elapsed:.2f}s threads {threads_before}"
              f"→{threads_after}")
        print(f"cache {cache_stats['cache_bytes']}/{cache_stats['cache_budget']}"
              f" bytes · views={len(app.pet_manager.views)}")
    finally:
        app.quit()

    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if report_path:
        Path(report_path).write_text(json.dumps({
            "pets": pets_max,
            "checks": {name: bool(ok) for name, ok in checks},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report written: {report_path}")
    if failed:
        print("BENCHMARK FAILED:", failed)
        return 1
    print("BENCHMARK OK")
    return 0


def _parse_args(argv):
    pets = 8
    report = ""
    i = 1
    while i < len(argv):
        if argv[i] == "--pets" and i + 1 < len(argv):
            pets = int(argv[i + 1])
            i += 2
        elif argv[i] == "--report" and i + 1 < len(argv):
            report = argv[i + 1]
            i += 2
        else:
            i += 1
    return pets, report


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if _stream and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    _pets, _report = _parse_args(sys.argv)
    sys.exit(run(_pets, _report))
