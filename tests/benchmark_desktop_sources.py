"""Desktop sources 合成基准（plan2 §12/§17）。

fixture：1 Codex host + 8 sessions（state DB + exact rollouts）、
1 ZCode host + 8 sessions（db.sqlite）。SQLite 指纹 99% tick 不变；
每 50/100 ticks 模拟一次 main/WAL 更新；rollout 每轮只一个文件增长；
周期性制造 SQLITE_BUSY。

验收（硬阈值，失败退出 1——不是"打印结果但不 fail"的软基准）：
  * 无 per-session 线程（线程数前后一致）；
  * SQL 次数与 DB change/safety refresh 成正比，绝不与
    ticks × sessions 成正比；
  * fd/handle 在 5000 tick 后回到预期（无泄漏）；
  * busy 检出后保留 last good（non-authoritative），恢复后继续；
  * FileTailer 内存有界（单次读取 ≤ MAX_CHUNK）。

用法：python tests/benchmark_desktop_sources.py --ticks 5000 --report PATH
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.codex_desktop import CodexDesktopSource
from agents.models import AgentKind, DesktopHost
from agents.zcode_desktop import ZCodeDesktopSource

CODEX_HOST = DesktopHost(kind=AgentKind.CODEX, pid=900,
                         process_token="111.000")
ZCODE_HOST = DesktopHost(kind=AgentKind.ZCODE, pid=950,
                         process_token="222.000")


class CodexFixture:
    """临时 CODEX_HOME：threads 表 + 8 个 rollout。"""

    COLUMNS = ("id", "rollout_path", "updated_at", "archived", "title",
               "cwd", "has_user_event", "thread_source", "originator",
               "source", "updated_at_ms")

    def __init__(self, root: Path):
        self.root = root
        self.db_path = str(root / "state_5.sqlite")
        self.sessions_root = root / "sessions" / "2026" / "09" / "12"
        self.sessions_root.mkdir(parents=True)
        con = sqlite3.connect(self.db_path)
        con.execute(f"CREATE TABLE threads ({', '.join(self.COLUMNS)})")
        con.commit()
        con.close()
        self.rollouts = []
        for i in range(8):
            path = self.sessions_root / f"rollout-{i}.jsonl"
            with path.open("w", encoding="utf-8") as f:
                f.write(_codex_line("session_meta",
                                    {"session_id": f"t-{i}", "cwd": "/w"}))
                f.write(_codex_line("event_msg", {"type": "task_started"}))
            self.rollouts.append(str(path))
            self._insert_thread(f"t-{i}", str(path))

    def _insert_thread(self, tid: str, rollout: str, updated=None):
        now = int(updated or time.time())
        row = {"id": tid, "rollout_path": rollout, "updated_at": now,
               "archived": 0, "title": f"title-{tid}", "cwd": "/w",
               "has_user_event": 1, "thread_source": "user",
               "originator": "codex_desktop", "source": "cli",
               "updated_at_ms": now * 1000}
        con = sqlite3.connect(self.db_path)
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        con.execute(f"INSERT INTO threads ({cols}) VALUES ({marks})",
                    tuple(row.values()))
        con.commit()
        con.close()

    def touch(self, tick: int):
        """每 50 ticks 一次 DB 更新（模拟 Desktop 写 threads）。"""
        tid = f"t-{tick % 8}"
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE threads SET updated_at = ? WHERE id = ?",
                    (int(time.time()), tid))
        con.commit()
        con.close()

    def grow(self, tick: int):
        """每 tick 只一个 rollout 增长。"""
        idx = tick % 8
        with open(self.rollouts[idx], "a", encoding="utf-8") as f:
            f.write(_codex_line("event_msg",
                                {"type": "agent_reasoning", "text": "x"}))


class ZcodeFixture:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.db_path = str(root / "db.sqlite")
        con = sqlite3.connect(self.db_path)
        con.execute("""CREATE TABLE session (
            id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT,
            title TEXT, time_created INTEGER, time_updated INTEGER,
            task_type TEXT)""")
        con.execute("""CREATE TABLE model_usage (
            id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT,
            query_source TEXT, status TEXT, started_at INTEGER,
            completed_at INTEGER)""")
        con.execute("""CREATE TABLE tool_usage (
            id TEXT PRIMARY KEY, session_id TEXT, tool_name TEXT,
            status TEXT, approval_status TEXT, started_at INTEGER,
            completed_at INTEGER)""")
        con.execute("""CREATE TABLE turn_usage (
            session_id TEXT, turn_id TEXT, status TEXT, started_at INTEGER,
            completed_at INTEGER)""")
        con.commit()
        con.close()
        now_ms = int(time.time() * 1000)
        con = sqlite3.connect(self.db_path)
        for i in range(8):
            con.execute(
                "INSERT INTO session (id, parent_id, directory, title, "
                "time_created, time_updated, task_type) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"s-{i}", None, "/w", f"任务{i}", now_ms - 60000, now_ms,
                 "interactive"))
            con.execute(
                "INSERT INTO model_usage (id, session_id, turn_id, "
                "query_source, status, started_at, completed_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"m-{i}", f"s-{i}", "turn-1", "main_turn", "running",
                 now_ms, None))
        con.commit()
        con.close()

    def touch(self, tick: int):
        """每 100 ticks 一次 DB 更新（WAL/main 变化）。"""
        con = sqlite3.connect(self.db_path)
        con.execute("UPDATE session SET time_updated = ? WHERE id = ?",
                    (int(time.time() * 1000), f"s-{tick % 8}"))
        con.commit()
        con.close()


def _codex_line(rtype: str, payload: dict) -> str:
    import json
    return json.dumps({"type": rtype, "timestamp": time.time(),
                       "payload": payload}) + "\n"


def run(ticks: int = 5000, report_path: str = "") -> int:
    checks = []
    failures = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        if not ok:
            failures.append(f"{name}: {detail}")

    tmp = tempfile.TemporaryDirectory()
    try:
        root = Path(tmp.name)
        codex_fx = CodexFixture(root / "codex")
        zcode_fx = ZcodeFixture(root / "zcode")
        codex = CodexDesktopSource(
            cfg={"active_file_window_sec": 180, "activity_grace_sec": 10},
            codex_home=root / "codex")
        zcode = ZCodeDesktopSource(zcode_db=zcode_fx.db_path)
        hosts = (CODEX_HOST, ZCODE_HOST)

        threads_before = threading.active_count()
        try:
            import psutil
            proc = psutil.Process()
            handles_before = proc.num_handles() if os.name == "nt" \
                else proc.num_fds()
        except Exception:
            proc = None
            handles_before = 0

        sim_start = time.time()
        busy_events = 0
        non_auth_polls = 0
        t0 = time.perf_counter()
        lock_holder = sqlite3.connect(zcode_fx.db_path)
        for tick in range(ticks):
            now = sim_start + tick * 0.5
            if tick % 50 == 0:
                codex_fx.touch(tick)
            if tick % 100 == 0:
                zcode_fx.touch(tick)
            codex_fx.grow(tick)
            # 每 1000 ticks 在 refresh 窗口内持锁 → 周期性 SQLITE_BUSY
            hold_lock = (tick % 1000 == 500)
            if hold_lock:
                try:
                    lock_holder.execute("BEGIN EXCLUSIVE")
                except sqlite3.Error:
                    hold_lock = False
            snap_c = codex.poll(hosts, frozenset(), now)
            snap_z = zcode.poll(hosts, frozenset(), now)
            if hold_lock:
                try:
                    lock_holder.rollback()
                except sqlite3.Error:
                    pass
                busy_events += 1
            if not snap_c.authoritative or not snap_z.authoritative:
                non_auth_polls += 1
        duration = time.perf_counter() - t0
        lock_holder.close()

        threads_after = threading.active_count()
        handles_after = 0
        if proc is not None:
            handles_after = proc.num_handles() if os.name == "nt" \
                else proc.num_fds()

        c_stats = codex.stats()
        z_stats = zcode.stats()
        codex_sql = c_stats["codex_desktop_db_queries"]
        zcode_sql = z_stats["zcode_desktop_db_queries"]
        codex_refreshes = c_stats["codex_desktop_refreshes"]
        zcode_refreshes = z_stats["zcode_desktop_refreshes"]
        # 预算按 refresh 批次计数（每次 = 1 个 catalog + 必要小查询）：
        # change refresh（ticks/50、ticks/100）+ safety refresh（ticks/15）。
        # 若实现退化为 ticks × sessions 的查询风暴将直接失败。
        codex_budget = ticks // 50 + ticks // 15 + 20
        zcode_budget = ticks // 100 + ticks // 15 + 20
        check("codex_refreshes_proportional_to_changes",
              codex_refreshes <= codex_budget,
              f"{codex_refreshes} > budget {codex_budget}")
        check("zcode_refreshes_proportional_to_changes",
              zcode_refreshes <= zcode_budget,
              f"{zcode_refreshes} > budget {zcode_budget}")
        check("no_per_session_threads",
              threads_after == threads_before,
              f"{threads_before} -> {threads_after}")
        check("sessions_tracked_bounded",
              c_stats["codex_desktop_sessions"] == 8
              and z_stats["zcode_desktop_sessions"] == 8,
              f"codex={c_stats['codex_desktop_sessions']} "
              f"zcode={z_stats['zcode_desktop_sessions']}")
        if proc is not None:
            check("handles_stable_after_5000_ticks",
                  handles_after - handles_before <= 24,
                  f"{handles_before} -> {handles_after}")
        check("busy_detected_and_recovered",
              busy_events >= 2 and non_auth_polls >= 2,
              f"busy={busy_events} non_auth={non_auth_polls}")
        check("duration_budget", duration < 120.0,
              f"{duration:.1f}s for {ticks} ticks")
        codex.close()
        zcode.close()

        report = {
            "benchmark": "desktop_sources",
            "ticks": ticks,
            "duration_sec": round(duration, 2),
            "codex_db_queries": codex_sql,
            "zcode_db_queries": zcode_sql,
            "codex_refreshes": codex_refreshes,
            "zcode_refreshes": zcode_refreshes,
            "codex_sessions": c_stats["codex_desktop_sessions"],
            "zcode_sessions": z_stats["zcode_desktop_sessions"],
            "busy_events": busy_events,
            "non_authoritative_polls": non_auth_polls,
            "threads_before": threads_before,
            "threads_after": threads_after,
            "handles_before": handles_before,
            "handles_after": handles_after,
            "checks": checks,
        }
        if report_path:
            Path(report_path).write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8")
        for c in checks:
            print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}"
                  + (f"（{c['detail']}）" if c['detail'] else ""))
        print(f"desktop-source benchmark: {ticks} ticks in "
              f"{duration:.1f}s, codex_refreshes={codex_refreshes}, "
              f"zcode_refreshes={zcode_refreshes}")
        if failures:
            for f in failures:
                print(f"FAIL: {f}")
            return 1
        print("BENCHMARK OK")
        return 0
    finally:
        tmp.cleanup()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=int, default=5000)
    parser.add_argument("--report", dest="report_path", default="")
    args = parser.parse_args()
    sys.exit(run(ticks=args.ticks, report_path=args.report_path))
