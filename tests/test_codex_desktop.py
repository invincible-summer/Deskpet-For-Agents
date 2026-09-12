"""Codex Desktop source 测试（plan2 §7/§13/§15，Phase 3）。

全部合成 fixture：临时 CODEX_HOME + 最小 threads 表 + rollout JSONL。
不触碰真实用户数据；不连接任何 App Server。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from agents.codex_desktop import CodexDesktopSource
from agents.desktop import SessionClaimKey
from agents.models import (
    AgentKind,
    AgentSurface,
    DesktopHost,
    Mode,
    Status,
)

_CFG = {"activity_grace_sec": 10, "active_file_window_sec": 180,
        "goal_max_chars": 120, "summary_max_chars": 160}


def _thread_row(thread_id: str, rollout_path: str, **extra):
    row = {
        "id": thread_id, "rollout_path": rollout_path,
        "updated_at": int(time.time()), "archived": 0,
        "title": f"title-{thread_id}", "cwd": "/w",
        "has_user_event": 1, "thread_source": "user",
        "originator": "codex_desktop", "source": "cli",
        "updated_at_ms": int(time.time() * 1000),
    }
    row.update(extra)
    if "updated_at" in extra and "updated_at_ms" not in extra:
        row["updated_at_ms"] = int(extra["updated_at"] * 1000)
    return row


class CodexHomeFixture:
    """临时 CODEX_HOME：threads 表 + rollout 文件。"""

    COLUMNS = ("id", "rollout_path", "updated_at", "archived", "title",
               "cwd", "has_user_event", "thread_source", "originator",
               "source", "updated_at_ms")

    def __init__(self, root: Path):
        self.root = root
        self.db_path = str(root / "state_5.sqlite")
        self.sessions_root = root / "sessions"
        con = sqlite3.connect(self.db_path)
        cols = ", ".join(self.COLUMNS)
        con.execute(f"CREATE TABLE threads ({cols})")
        con.commit()
        con.close()

    def rollout_dir(self, day: str = "2026-09-12") -> Path:
        y, m, d = day.split("-")
        p = self.sessions_root / y / m / d
        p.mkdir(parents=True, exist_ok=True)
        return p

    def write_rollout(self, name: str, events: list[dict],
                      day: str = "2026-09-12") -> str:
        path = self.rollout_dir(day) / name
        with path.open("w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        return str(path)

    def add_thread(self, row: dict):
        con = sqlite3.connect(self.db_path)
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        con.execute(f"INSERT INTO threads ({cols}) VALUES ({marks})",
                    tuple(row.values()))
        con.commit()
        con.close()

    def update_rows(self, sql: str, params: tuple = ()):
        con = sqlite3.connect(self.db_path)
        con.execute(sql, params)
        con.commit()
        con.close()


def active_rollout(session_id: str, mode: str | None = None) -> list[dict]:
    now = time.time()
    start = {"type": "session_meta", "timestamp": now,
             "payload": {"session_id": session_id, "cwd": "/w"}}
    started = {"type": "event_msg", "timestamp": now,
               "payload": {"type": "task_started",
                           "collaboration_mode_kind": mode or "default"}}
    return [start, started]


def done_rollout(session_id: str) -> list[dict]:
    return active_rollout(session_id) + [{
        "type": "event_msg", "timestamp": time.time(),
        "payload": {"type": "task_complete", "last_agent_message": "ok"}}]


def _host(pid=900, token="111.000") -> DesktopHost:
    return DesktopHost(kind=AgentKind.CODEX, pid=pid, process_token=token)


class CodexDesktopSourceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = CodexHomeFixture(Path(self._tmp.name))
        self.host = _host()
        self._sources: list[CodexDesktopSource] = []

    def tearDown(self):
        for source in self._sources:
            source.close()
        self._tmp.cleanup()

    def _source(self, **kwargs) -> CodexDesktopSource:
        source = CodexDesktopSource(cfg=dict(_CFG),
                                    codex_home=self.fx.root, **kwargs)
        self._sources.append(source)
        return source

    def _poll(self, source, hosts=(None,), claims=frozenset()):
        hosts = tuple(_host() if h is None else h for h in hosts)
        return source.poll(hosts, frozenset(claims), time.time())

    # ---- 基础：3 threads → 3 独立 target（plan2 §15） ----

    def test_three_desktop_threads_produce_three_targets(self):
        for i in ("a", "b", "c"):
            path = self.fx.write_rollout(f"rollout-{i}.jsonl",
                                         active_rollout(f"t-{i}"))
            self.fx.add_thread(_thread_row(f"t-{i}", path))
        source = self._source()
        snap = self._poll(source)
        self.assertTrue(snap.authoritative)
        self.assertEqual(len(snap.instances), 3)
        keys = {inst.key for inst in snap.instances}
        self.assertEqual(len(keys), 3)
        for inst in snap.instances:
            self.assertIs(inst.surface, AgentSurface.DESKTOP)
            self.assertIs(inst.kind, AgentKind.CODEX)
            self.assertEqual(inst.host_key, self.host.host_key)
            self.assertTrue(inst.logical_session_id in ("t-a", "t-b", "t-c"))
        # 观察独立：每个 target 各自 WORKING
        for obs in snap.observations.values():
            self.assertEqual(obs.status, Status.WORKING)

    def test_active_done_plan_independent_observations(self):
        pa = self.fx.write_rollout("a.jsonl", active_rollout("t-a"))
        pb = self.fx.write_rollout("b.jsonl", done_rollout("t-b"))
        pc = self.fx.write_rollout("c.jsonl", active_rollout("t-c", "plan"))
        for tid, path in (("t-a", pa), ("t-b", pb), ("t-c", pc)):
            self.fx.add_thread(_thread_row(tid, path))
        snap = self._poll(self._source())
        status = {inst.logical_session_id: snap.observations[inst.key].status
                  for inst in snap.instances}
        self.assertEqual(status["t-a"], Status.WORKING)
        self.assertEqual(status["t-b"], Status.DONE)
        self.assertEqual(status["t-c"], Status.WORKING)
        mode_c = snap.observations[
            next(i.key for i in snap.instances
                 if i.logical_session_id == "t-c")].mode
        self.assertEqual(mode_c, Mode.PLAN)

    # ---- 过滤（plan2 §7.1） ----

    def test_generated_and_archived_threads_excluded(self):
        p1 = self.fx.write_rollout("s.jsonl", active_rollout("t-sub"))
        self.fx.add_thread(_thread_row("t-sub", p1,
                                       thread_source="subagent"))
        p2 = self.fx.write_rollout("g.jsonl", active_rollout("t-guard"))
        self.fx.add_thread(_thread_row("t-guard", p2,
                                       thread_source="guardian_review"))
        p3 = self.fx.write_rollout("ar.jsonl", active_rollout("t-arch"))
        self.fx.add_thread(_thread_row("t-arch", p3, archived=1))
        snap = self._poll(self._source())
        self.assertEqual(snap.instances, ())

    def test_unknown_originator_not_treated_as_desktop(self):
        p = self.fx.write_rollout("x.jsonl", active_rollout("t-x"))
        self.fx.add_thread(_thread_row("t-x", p, originator="codex_cli_rs"))
        source = self._source()
        snap = self._poll(source)
        self.assertEqual(snap.instances, ())

    def test_originator_column_absent_accepts_user_threads(self):
        # capability：本机上游可能没有 originator 列（migration < 0053）。
        # host 已被 role 分类确认为 Codex desktop → 依赖 thread_source。
        con = sqlite3.connect(self.fx.db_path)
        con.execute("""CREATE TABLE threads2 AS SELECT id, rollout_path,
            updated_at, archived, has_user_event, thread_source
            FROM threads""")
        con.execute("DROP TABLE threads")
        con.execute("ALTER TABLE threads2 RENAME TO threads")
        con.commit()
        con.close()
        p = self.fx.write_rollout("n.jsonl", active_rollout("t-n"))
        self.fx.add_thread({"id": "t-n", "rollout_path": p,
                            "updated_at": int(time.time()),
                            "archived": 0, "has_user_event": 1,
                            "thread_source": "user"})
        snap = self._poll(self._source())
        self.assertEqual(len(snap.instances), 1)
        self.assertEqual(snap.instances[0].logical_session_id, "t-n")

    # ---- admission（plan2 §7.4） ----

    def test_historical_cold_rows_not_admitted(self):
        old = int(time.time()) - 86400
        p = self.fx.write_rollout("cold.jsonl", done_rollout("t-cold"),
                                  day="2026-08-01")
        self.fx.add_thread(_thread_row("t-cold", p, updated_at=old))
        snap = self._poll(self._source())
        self.assertEqual(snap.instances, ())

    def test_growth_after_start_admits(self):
        p = self.fx.write_rollout("grow.jsonl", [
            {"type": "session_meta",
             "payload": {"session_id": "t-grow", "cwd": "/w"}}])
        self.fx.add_thread(_thread_row("t-grow", p, updated_at=0))
        source = self._source()
        snap = self._poll(source)      # 建立基线 size
        self.assertEqual(snap.instances, ())
        # DeskPet 启动后出现新增长（active turn）
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "event_msg", "timestamp": time.time(),
                                "payload": {"type": "task_started"}}) + "\n")
        snap = self._poll(source)
        self.assertEqual(len(snap.instances), 1)
        self.assertEqual(snap.observations[
            snap.instances[0].key].status, Status.WORKING)

    def test_max_eight_sessions_tracked(self):
        for i in range(10):
            p = self.fx.write_rollout(f"m{i}.jsonl", active_rollout(f"t-m{i}"))
            self.fx.add_thread(_thread_row(f"t-m{i}", p))
        snap = self._poll(self._source())
        self.assertEqual(len(snap.instances), 8)

    def test_active_session_survives_catalog_overflow_mark(self):
        # catalog 滚出（LIMIT 32 之外）不会被误标 archived——此处只验证
        # active 会话不因后续 catalog 变化被立即移除
        p = self.fx.write_rollout("keep.jsonl", active_rollout("t-keep"))
        self.fx.add_thread(_thread_row("t-keep", p))
        source = self._source()
        self._poll(source)
        # 第二轮 catalog 未变 → 仍发出
        snap = self._poll(source)
        self.assertEqual(len(snap.instances), 1)

    # ---- claim 去重（plan2 §3.4） ----

    def test_cli_claimed_thread_not_emitted_then_reclaimed(self):
        p = self.fx.write_rollout("cl.jsonl", active_rollout("t-cl"))
        self.fx.add_thread(_thread_row("t-cl", p))
        source = self._source()
        claim = {SessionClaimKey(kind="codex", session_id="t-cl")}
        snap = self._poll(source, claims=claim)
        self.assertEqual(snap.instances, ())
        # CLI 消失 → 下一轮 reclaim 同一逻辑 thread
        snap = self._poll(source)
        self.assertEqual(len(snap.instances), 1)
        self.assertEqual(snap.instances[0].logical_session_id, "t-cl")

    # ---- path 安全（plan2 §7.2） ----

    def test_out_of_bounds_rollout_path_rejected(self):
        evil = str(Path(self._tmp.name) / "evil.jsonl")
        Path(evil).write_text("{}", encoding="utf-8")
        self.fx.add_thread(_thread_row("t-evil", evil))
        outside = str(Path(self._tmp.name).parent / "outside.jsonl")
        self.fx.add_thread(_thread_row("t-out", outside))
        snap = self._poll(self._source())
        self.assertEqual(snap.instances, ())
        # 目录穿越形式同样拒绝
        sneaky = str(Path(self._tmp.name) / "sessions" /
                     ".." / ".." / "escape.jsonl")
        self.fx.add_thread(_thread_row("t-sneak", sneaky))
        snap = self._poll(self._source())
        self.assertEqual(snap.instances, ())

    def test_missing_rollout_never_synthesizes_state(self):
        self.fx.add_thread(_thread_row(
            "t-miss", str(self.fx.rollout_dir() / "ghost.jsonl")))
        snap = self._poll(self._source())
        self.assertEqual(snap.instances, ())
        self.assertEqual(snap.observations, {})

    # ---- WAITING 诚实降级（plan2 §7.6/§13） ----

    def test_no_approval_event_never_waits(self):
        # rollout 只含 active turn（审批事件 transient 不落盘）→
        # 必须是 WORKING，绝不从静默合成 WAITING
        p = self.fx.write_rollout("q.jsonl", active_rollout("t-q"))
        self.fx.add_thread(_thread_row("t-q", p))
        source = self._source()
        for _ in range(3):
            snap = self._poll(source)
        obs = snap.observations[snap.instances[0].key]
        self.assertEqual(obs.status, Status.WORKING)
        self.assertNotEqual(obs.status, Status.WAITING)

    # ---- 失败降级（plan2 §6.1/§13） ----

    def test_db_locked_returns_non_authoritative(self):
        import sqlite3 as sq
        p = self.fx.write_rollout("l.jsonl", active_rollout("t-l"))
        self.fx.add_thread(_thread_row("t-l", p))
        source = self._source()
        holder = sq.connect(self.fx.db_path)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            t0 = time.perf_counter()
            snap = self._poll(source)
            elapsed = time.perf_counter() - t0
            self.assertFalse(snap.authoritative)
            self.assertLess(elapsed, 2.0)   # 短 busy timeout，不阻塞 monitor
        finally:
            holder.rollback()
            holder.close()
        # 解锁后恢复
        snap = self._poll(source)
        self.assertTrue(snap.authoritative)
        self.assertEqual(len(snap.instances), 1)

    def test_db_corrupt_keeps_conservative(self):
        p = self.fx.write_rollout("c.jsonl", active_rollout("t-c"))
        self.fx.add_thread(_thread_row("t-c", p))
        source = self._source()
        good = self._poll(source)
        self.assertEqual(len(good.instances), 1)
        # 模拟 DB 损坏（garbage 覆盖文件头）——source 绝不 repair
        with open(self.fx.db_path, "wb") as f:
            f.write(b"not a sqlite database" * 16)
        snap = self._poll(source)
        self.assertFalse(snap.authoritative)
        self.assertEqual(source.stats()["codex_desktop_sessions"], 1)  # 不删状态

    def test_incompatible_schema_non_authoritative(self):
        con = sqlite3.connect(self.fx.db_path)
        con.execute("DROP TABLE threads")
        con.execute("CREATE TABLE threads (foo TEXT)")
        con.commit()
        con.close()
        source = self._source()
        snap = self._poll(source)
        self.assertFalse(snap.authoritative)
        self.assertTrue(any("INCOMPATIBLE_SCHEMA" in d
                            for d in snap.diagnostics))

    def test_no_state_db_diagnostics(self):
        os.remove(self.fx.db_path)
        source = self._source()
        snap = self._poll(source)
        self.assertTrue(snap.authoritative)   # 无会话可报 ≠ 暂时故障
        self.assertEqual(snap.instances, ())
        self.assertTrue(any("NO_STATE_DB" in d for d in snap.diagnostics))

    # ---- 指纹 gating（plan2 §6.2） ----

    def test_fingerprint_unchanged_skips_sql(self):
        p = self.fx.write_rollout("f.jsonl", active_rollout("t-f"))
        self.fx.add_thread(_thread_row("t-f", p))
        source = self._source()
        self._poll(source)
        q1 = source.stats()["codex_desktop_db_queries"]
        # DB 未变化 + safety refresh 未到期 → 不再执行 SQL
        self._poll(source)
        q2 = source.stats()["codex_desktop_db_queries"]
        self.assertEqual(q1, q2)
        # DB 变化 → 恰好刷新一次
        self.fx.update_rows("UPDATE threads SET title = 'new' WHERE id = ?",
                            ("t-f",))
        self._poll(source)
        q3 = source.stats()["codex_desktop_db_queries"]
        self.assertEqual(q3, q1 + 1)

    # ---- host 生命周期（plan2 §9） ----

    def test_drop_host_releases_all_sessions(self):
        p = self.fx.write_rollout("d.jsonl", active_rollout("t-d"))
        self.fx.add_thread(_thread_row("t-d", p))
        source = self._source()
        self._poll(source)
        self.assertEqual(source.stats()["codex_desktop_sessions"], 1)
        source.drop_host(self.host.host_key)
        self.assertEqual(source.stats()["codex_desktop_sessions"], 0)
        # host 消失后的 poll 不发 target
        snap = self._poll(source, hosts=())
        self.assertEqual(snap.instances, ())

    def test_idle_lease_expires_inactive_session(self):
        old = int(time.time()) - 7200
        p = self.fx.write_rollout("idle.jsonl", done_rollout("t-idle"),
                                  day="2026-08-01")
        self.fx.add_thread(_thread_row("t-idle", p, updated_at=old))
        source = self._source()
        # done rollout 冷启动不 admit（非进行中）；无实例
        snap = self._poll(source)
        self.assertEqual(snap.instances, ())

    def test_active_session_not_removed_by_lease(self):
        p = self.fx.write_rollout("act.jsonl", active_rollout("t-act"))
        self.fx.add_thread(_thread_row("t-act", p))
        source = self._source()
        for _ in range(2):
            snap = self._poll(source)
        self.assertEqual(len(snap.instances), 1)
        # WORKING 状态受保护：即使 last_activity 停留也不释放
        self.assertTrue(snap.observations[snap.instances[0].key].turn_active)


if __name__ == "__main__":
    unittest.main()
