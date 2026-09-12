"""ZCode Desktop source 测试（plan2 §8/§13/§15，Phase 4）。

全部合成 fixture（临时 sqlite）；枚举值来自实机脱敏 probe 快照
（.research/probe-snapshot.json）固化，不触碰真实用户数据。
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from agents.desktop import SessionClaimKey
from agents.models import (
    AgentKind,
    AgentSurface,
    Confidence,
    DesktopHost,
    Mode,
    Phase,
    Status,
)
from agents.monitor import Monitor
from agents.terminal_service import WindowsTerminalService
from agents.zcode_desktop import ZCodeDesktopSource


def _host(pid=950, token="222.000") -> DesktopHost:
    return DesktopHost(kind=AgentKind.ZCODE, pid=pid, process_token=token)


class ZcodeDbFixture:
    """按实机 schema 最小化复刻（列名来自脱敏 probe）。"""

    def __init__(self, root: Path):
        self.db_path = str(root / "db.sqlite")
        con = sqlite3.connect(self.db_path)
        con.execute("""CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT, workspace_id TEXT,
            parent_id TEXT, slug TEXT, directory TEXT, path TEXT,
            title TEXT, version TEXT, permission TEXT,
            time_created INTEGER, time_updated INTEGER,
            time_archived INTEGER, task_type TEXT)""")
        con.execute("""CREATE TABLE model_usage (
            id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT,
            query_source TEXT, status TEXT, started_at INTEGER,
            completed_at INTEGER)""")
        con.execute("""CREATE TABLE tool_usage (
            id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT,
            tool_call_id TEXT, tool_name TEXT, status TEXT,
            approval_status TEXT, started_at INTEGER, completed_at INTEGER)""")
        con.execute("""CREATE TABLE turn_usage (
            session_id TEXT, turn_id TEXT, status TEXT, started_at INTEGER,
            completed_at INTEGER)""")
        con.commit()
        con.close()

    def add_session(self, sid: str, root: bool = True, task_type="interactive",
                    title="", directory="/w", updated_ms=None):
        con = sqlite3.connect(self.db_path)
        now_ms = updated_ms if updated_ms is not None else int(time.time() * 1000)
        con.execute(
            "INSERT INTO session (id, parent_id, directory, title, "
            "time_created, time_updated, task_type) VALUES (?,?,?,?,?,?,?)",
            (sid, None if root else "parent-x", directory, title,
             now_ms - 60000, now_ms, task_type))
        con.commit()
        con.close()

    def add_model(self, sid: str, status="running", query_source="main_turn",
                  started_ms=None):
        con = sqlite3.connect(self.db_path)
        now_ms = started_ms if started_ms is not None else int(time.time() * 1000)
        con.execute(
            "INSERT INTO model_usage (id, session_id, turn_id, query_source, "
            "status, started_at, completed_at) VALUES (?,?,?,?,?,?,?)",
            (f"m-{sid}-{now_ms}-{query_source}", sid, "turn-1", query_source,
             status, now_ms, None))
        con.commit()
        con.close()

    def add_tool(self, sid: str, tool="Bash", status="running",
                 approval="none", started_ms=None):
        con = sqlite3.connect(self.db_path)
        now_ms = started_ms if started_ms is not None else int(time.time() * 1000)
        con.execute(
            "INSERT INTO tool_usage (id, session_id, turn_id, tool_call_id, "
            "tool_name, status, approval_status, started_at, completed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"t-{sid}-{now_ms}-{tool}", sid, "turn-1", "call-1", tool,
             status, approval, now_ms, None))
        con.commit()
        con.close()

    def add_turn(self, sid: str, status="completed", started_ms=None,
                 completed_ms=None):
        con = sqlite3.connect(self.db_path)
        now_ms = int(time.time() * 1000)
        s = started_ms if started_ms is not None else now_ms - 5000
        c = completed_ms if completed_ms is not None else now_ms - 1000
        con.execute(
            "INSERT INTO turn_usage (session_id, turn_id, status, "
            "started_at, completed_at) VALUES (?,?,?,?,?)",
            (sid, f"turn-{sid}-{s}", status, s, c))
        con.commit()
        con.close()

    def exec(self, sql, params=()):
        con = sqlite3.connect(self.db_path)
        con.execute(sql, params)
        con.commit()
        con.close()


class ZCodeDesktopSourceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = ZcodeDbFixture(Path(self._tmp.name))
        self.host = _host()
        self._sources: list[ZCodeDesktopSource] = []

    def tearDown(self):
        for source in self._sources:
            source.close()
        self._tmp.cleanup()

    def _source(self) -> ZCodeDesktopSource:
        source = ZCodeDesktopSource(zcode_db=self.fx.db_path)
        self._sources.append(source)
        return source

    def _poll(self, source, hosts=(None,), claims=frozenset()):
        hosts = tuple(_host() if h is None else h for h in hosts)
        return source.poll(hosts, frozenset(claims), time.time())

    # ---- 基础：3 root tasks → 3 独立 target（AC-ZCODE-01 合成对照） ----

    def test_three_root_sessions_produce_three_targets(self):
        for i in ("a", "b", "c"):
            self.fx.add_session(f"s-{i}", title=f"任务 {i}")
            self.fx.add_model(f"s-{i}", status="running")
        snap = self._poll(self._source())
        self.assertTrue(snap.authoritative)
        self.assertEqual(len(snap.instances), 3)
        for inst in snap.instances:
            self.assertIs(inst.surface, AgentSurface.DESKTOP)
            self.assertIs(inst.kind, AgentKind.ZCODE)
            self.assertIn("desktop", inst.key)
            self.assertIn("222.000", inst.key)
        for obs in snap.observations.values():
            self.assertEqual(obs.status, Status.WORKING)
            self.assertEqual(obs.phase, Phase.THINKING)

    # ---- subagent 排除与归并（AC-ZCODE-04 合成对照） ----

    def test_subagent_session_not_a_target(self):
        self.fx.add_session("s-root", title="父任务")
        self.fx.add_session("s-child", root=False,
                            task_type="subagent_child")
        self.fx.add_model("s-child", status="running",
                          query_source="subagent")
        snap = self._poll(self._source())
        ids = {i.logical_session_id for i in snap.instances}
        self.assertNotIn("s-child", ids)

    def test_subagent_activity_supports_parent_working(self):
        self.fx.add_session("s-root", title="父任务")
        self.fx.add_session("s-child", root=False,
                            task_type="subagent_child",
                            updated_ms=int(time.time() * 1000) - 1)
        # child 无 model running；父无 model running —— 但 child 的
        # parent_id 指向真实父（fixture 简化：parent_id 列值）
        con = sqlite3.connect(self.fx.db_path)
        con.execute("UPDATE session SET parent_id = 's-root' "
                    "WHERE id = 's-child'")
        con.commit()
        con.close()
        # child session 的 running model 归并入父
        self.fx.add_model("s-child", status="running",
                          query_source="subagent")
        snap = self._poll(self._source())
        ids = {i.logical_session_id for i in snap.instances}
        self.assertEqual(ids, {"s-root"})
        obs = snap.observations[snap.instances[0].key]
        self.assertEqual(obs.status, Status.WORKING)

    # ---- WAITING（AC-ZCODE-02 合成对照） ----

    def test_unresolved_permission_waiting_only_that_session(self):
        self.fx.add_session("s-a", title="A")
        self.fx.add_session("s-b", title="B")
        self.fx.add_session("s-c", title="C")
        self.fx.add_model("s-a", status="running")
        self.fx.add_model("s-c", status="running")
        self.fx.add_tool("s-b", tool="Bash", status="running",
                         approval="requested")
        snap = self._poll(self._source())
        self.assertEqual(len(snap.instances), 3)
        waiting = [
            snap.observations[i.key] for i in snap.instances
            if snap.observations[i.key].status is Status.WAITING]
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0].phase, Phase.APPROVAL)
        self.assertIs(waiting[0].confidence, Confidence.EXACT)
        others = [snap.observations[i.key].status for i in snap.instances
                  if snap.observations[i.key].status is not Status.WAITING]
        self.assertNotIn(Status.WAITING, others)

    def test_permission_resolve_exits_waiting(self):
        self.fx.add_session("s-b", title="B")
        self.fx.add_tool("s-b", status="running", approval="requested")
        source = self._source()
        snap = self._poll(source)
        self.assertEqual(
            snap.observations[snap.instances[0].key].status, Status.WAITING)
        # 批复落库（行状态变化 = 明确 resolved 事实，plan2 §8.3）
        self.fx.exec("UPDATE tool_usage SET approval_status = 'approved', "
                     "status = 'completed'")
        snap = self._poll(source)
        obs = snap.observations[snap.instances[0].key]
        # resolve 后必须在下一次 DB 证据内离开 WAITING（AC-ZCODE-02）；
        # fixture 无后续活动 → 保守 IDLE，绝不残留 WAITING
        self.assertNotEqual(obs.status, Status.WAITING)
        self.assertEqual(obs.status, Status.IDLE)

    def test_running_tool_phase_is_executing(self):
        self.fx.add_session("s-a")
        self.fx.add_tool("s-a", tool="Edit", status="running",
                         approval="none")
        snap = self._poll(self._source())
        obs = snap.observations[snap.instances[0].key]
        self.assertEqual(obs.status, Status.WORKING)
        self.assertEqual(obs.phase, Phase.CODING)   # classify_phase("Edit")

    # ---- DONE / ERROR 窗口 ----

    def test_completed_turn_done_then_idle(self):
        self.fx.add_session("s-d", title="D")
        self.fx.add_turn("s-d", status="completed")   # 1s 前完成
        snap = self._poll(self._source())
        self.assertEqual(
            snap.observations[snap.instances[0].key].status, Status.DONE)
        # 10s 后（模拟时间前进）→ IDLE
        source = self._sources[0]
        future = time.time() + 20
        hosts = (_host(),)
        snap2 = source.poll(hosts, frozenset(), future)
        self.assertEqual(
            snap2.observations[snap2.instances[0].key].status, Status.IDLE)

    def test_error_turn_shows_error_window(self):
        self.fx.add_session("s-e", title="E")
        self.fx.add_turn("s-e", status="error")       # 1s 前失败
        snap = self._poll(self._source())
        self.assertEqual(
            snap.observations[snap.instances[0].key].status, Status.ERROR)

    def test_cancelled_turn_is_not_done(self):
        self.fx.add_session("s-x", title="X")
        self.fx.add_turn("s-x", status="cancelled")
        snap = self._poll(self._source())
        obs = snap.observations[snap.instances[0].key]
        self.assertEqual(obs.status, Status.IDLE)   # 不伪造成功庆祝

    # ---- admission ----

    def test_historical_cold_session_not_admitted(self):
        old_ms = int((time.time() - 7200) * 1000)
        self.fx.add_session("s-old", title="旧任务", updated_ms=old_ms)
        self.fx.add_turn("s-old", status="completed",
                         started_ms=old_ms - 5000, completed_ms=old_ms)
        snap = self._poll(self._source())
        self.assertEqual(snap.instances, ())

    def test_max_eight_sessions(self):
        for i in range(10):
            self.fx.add_session(f"s-m{i}", title=f"任务{i}")
            self.fx.add_model(f"s-m{i}", status="running")
        snap = self._poll(self._source())
        self.assertEqual(len(snap.instances), 8)

    # ---- claim 去重（与 substrate 合同一致） ----

    def test_claimed_session_not_emitted(self):
        self.fx.add_session("s-claim", title="已被 claim")
        self.fx.add_model("s-claim", status="running")
        source = self._source()
        claim = {SessionClaimKey(kind="zcode", session_id="s-claim")}
        snap = self._poll(source, claims=claim)
        self.assertEqual(snap.instances, ())
        snap = self._poll(source)
        self.assertEqual(len(snap.instances), 1)

    # ---- 失败降级（plan2 §13） ----

    def test_db_locked_non_authoritative(self):
        self.fx.add_session("s-l", title="L")
        self.fx.add_model("s-l", status="running")
        source = self._source()
        self._poll(source)
        holder = sqlite3.connect(self.fx.db_path)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            # 指纹未变化 + safety refresh 未到期 → 使用缓存（权威）；
            # 越过 refresh 窗口后才真正尝试 SQL，才探测到锁
            snap = source.poll((_host(),), frozenset(), time.time())
            self.assertTrue(snap.authoritative)
            snap = source.poll((_host(),), frozenset(),
                               time.time() + 8.0)
            self.assertFalse(snap.authoritative)
        finally:
            holder.rollback()
            holder.close()
        snap = self._poll(source)
        self.assertTrue(snap.authoritative)
        self.assertEqual(len(snap.instances), 1)

    def test_incompatible_schema(self):
        self.fx.exec("DROP TABLE session")
        self.fx.exec("CREATE TABLE session (foo TEXT)")
        snap = self._poll(self._source())
        self.assertFalse(snap.authoritative)
        self.assertTrue(any("INCOMPATIBLE_SCHEMA" in d
                            for d in snap.diagnostics))

    def test_missing_optional_tables_degrade_without_waiting(self):
        # 无 tool_usage/turn_usage 表：仍可做 session/turn monitoring，
        # WAITING capability 不可用（plan2 §13）
        self.fx.exec("DROP TABLE tool_usage")
        self.fx.exec("DROP TABLE turn_usage")
        self.fx.add_session("s-g")
        self.fx.add_model("s-g", status="running")
        snap = self._poll(self._source())
        self.assertTrue(snap.authoritative)
        self.assertEqual(len(snap.instances), 1)
        self.assertEqual(
            snap.observations[snap.instances[0].key].status, Status.WORKING)

    def test_no_db_reports_no_state(self):
        source = ZCodeDesktopSource(
            zcode_db=str(Path(self._tmp.name) / "missing.sqlite"))
        self._sources.append(source)
        snap = self._poll(source)
        self.assertTrue(any("NO_STATE_DB" in d for d in snap.diagnostics))
        self.assertEqual(snap.instances, ())

    # ---- host 生命周期 ----

    def test_drop_host_clears_sessions(self):
        self.fx.add_session("s-h")
        self.fx.add_model("s-h", status="running")
        source = self._source()
        self._poll(source)
        self.assertEqual(source.stats()["zcode_desktop_sessions"], 1)
        source.drop_host(self.host.host_key)
        self.assertEqual(source.stats()["zcode_desktop_sessions"], 0)
        snap = self._poll(source, hosts=())
        self.assertEqual(snap.instances, ())

    def test_fingerprint_gating(self):
        self.fx.add_session("s-f")
        self.fx.add_model("s-f", status="running")
        source = self._source()
        self._poll(source)
        q1 = source.stats()["zcode_desktop_db_queries"]
        self._poll(source)
        self.assertEqual(source.stats()["zcode_desktop_db_queries"], q1)


class MonitorZCodeIntegrationTests(unittest.TestCase):
    """Monitor 注册 ZCode source 后的端到端（合成 DB）。"""

    def test_monitor_start_registers_zcode_source(self):
        from tests.test_monitoring import MemoryConfig
        with tempfile.TemporaryDirectory() as temp:
            fx = ZcodeDbFixture(Path(temp))
            fx.add_session("s-live", title="运行中")
            fx.add_model("s-live", status="running")
            config = MemoryConfig()
            monitor = Monitor(config)
            monitor._terminal_service = WindowsTerminalService(None)
            monitor._register_desktop_sources()
            monitor._probe = type("P", (), {
                "snapshot": lambda self: {},
                "desktop_hosts": lambda self: (_host(),),
                "inventory_authoritative": lambda self: True,
                "stop": lambda self: None,
                "join": lambda self, timeout=0.0: None,
            })()
            kinds = {s.kind for s in monitor._desktop_sources}
            self.assertIn(AgentKind.ZCODE, kinds)
            self.assertIn(AgentKind.CODEX, kinds)
            # 注入合成 DB 路径
            for source in monitor._desktop_sources:
                if isinstance(source, ZCodeDesktopSource):
                    source._db_path = fx.db_path
                    source._ro = None
                    source._caps = None
            monitor._tick()
            targets = [t for t in monitor.get_targets().values()
                       if t.instance.surface is AgentSurface.DESKTOP
                       and t.instance.kind is AgentKind.ZCODE]
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0].snapshot.status, Status.WORKING)
            monitor.request_stop()
            for source in monitor._desktop_sources:
                source.close()


if __name__ == "__main__":
    unittest.main()
