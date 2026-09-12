"""v4.4 Desktop source substrate 测试（plan2 §6/§9/§15，Phase 2）。

全部使用合成 fixture / fake source；无 Tk 窗口、无子进程、不触碰真实
用户数据。SQLite fixture 在临时目录内创建，测试后销毁。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.desktop import (
    DesktopSessionSource,
    DesktopSourceSnapshot,
    SessionClaimKey,
    build_terminal_claims,
    bounded_diagnostics,
    claims_cover,
)
from agents.models import (
    AgentInstance,
    AgentKind,
    AgentSurface,
    Confidence,
    DesktopHost,
    EvidenceSource,
    Observation,
    Phase,
    Status,
)
from agents.monitor import Monitor
from agents.sqlite_ro import (
    DbFingerprint,
    ReadOnlySqlite,
    fingerprints_differ,
    stat_fingerprint,
)
from agents.terminal_service import WindowsTerminalService


def probe_instance(host: DesktopHost, session_id: str) -> AgentInstance:
    return AgentInstance(
        kind=host.kind, pid=0, source="windows",
        surface=AgentSurface.DESKTOP,
        host_pid=host.pid, host_process_token=host.process_token,
        host_key=host.host_key,
        logical_session_id=session_id)


def working_obs(key: str, status: Status = Status.WORKING) -> Observation:
    return Observation(
        source=EvidenceSource.SESSION, timestamp=time.time(),
        status=status, phase=Phase.THINKING,
        confidence=Confidence.HIGH, turn_active=status is Status.WORKING,
        goal=f"goal-{key}", summary="", session_bound=True)


class FakeDesktopSource(DesktopSessionSource):
    """脚本化 fake source：按次序返回快照；记录 drop_host 调用。"""

    def __init__(self, kind: AgentKind,
                 snapshots: list[DesktopSourceSnapshot] | None = None):
        self.kind = kind
        self._snapshots = list(snapshots or [])
        self.dropped_hosts: list[str] = []
        self.poll_calls: list[tuple] = []

    def push(self, snap: DesktopSourceSnapshot):
        self._snapshots.append(snap)

    def poll(self, hosts, claimed_sessions, now):
        self.poll_calls.append((tuple(h.host_key for h in hosts),
                                frozenset(claimed_sessions), now))
        if self._snapshots:
            return self._snapshots.pop(0)
        return DesktopSourceSnapshot(authoritative=True)

    def drop_host(self, host_key: str):
        self.dropped_hosts.append(host_key)


def _desktop_snap(source: FakeDesktopSource, host: DesktopHost,
                  session_ids: list[str], authoritative: bool = True):
    insts = tuple(probe_instance(host, sid) for sid in session_ids)
    obs = {i.key: working_obs(i.key) for i in insts}
    return DesktopSourceSnapshot(
        instances=insts, observations=obs,
        host_keys=frozenset({host.host_key}) if host.host_key else frozenset(),
        authoritative=authoritative)


class _RecordingExitWatcher:
    """记录 register/unregister 调用；drain 返回预置事件。"""

    def __init__(self):
        self.registered: list[str] = []
        self.unregistered: list[str] = []
        self.events: list = []

    def start(self):
        return True

    def request_stop(self):
        pass

    def join_for_shutdown(self, timeout=0.0):
        return True

    def register(self, instance):
        self.registered.append(str(getattr(instance, "key", "")))
        return True

    def unregister(self, key):
        self.unregistered.append(key)

    def drain(self):
        events, self.events = self.events, []
        return events

    def watched_count(self):
        return len(self.registered)


# ============================================================ sqlite_ro

class ReadOnlySqliteTests(unittest.TestCase):
    def _make_db(self, root: Path, name="db.sqlite", wal=False):
        path = str(root / name)
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b TEXT)")
        con.execute("INSERT INTO t (a, b) VALUES ('x', 'y')")
        if wal:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("INSERT INTO t (a) VALUES ('wal-row')")
        con.commit()
        con.close()
        return path

    def test_readonly_uri_and_query_only(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._make_db(Path(temp))
            ro = ReadOnlySqlite(path)
            self.assertTrue(ro.open())
            rows = ro.query("SELECT id, a, b FROM t ORDER BY id")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["a"], "x")
            # 非 SELECT 在入口即拒绝
            self.assertIsNone(ro.query("INSERT INTO t (a) VALUES ('no')"))
            self.assertIn("rejected", ro.last_error)
            self.assertIsNone(ro.query("PRAGMA journal_mode=DELETE"))
            # query_only 纵深防御：直接 execute 写语句失败
            with self.assertRaises(sqlite3.Error):
                ro._con.execute("DELETE FROM t")
            ro.close()

    def test_open_missing_db_fails_closed_without_creating_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "missing.sqlite")
            self.assertFalse(os.path.exists(path))
            ro = ReadOnlySqlite(path)
            self.assertFalse(ro.open())
            self.assertTrue(ro.last_error)
            self.assertFalse(os.path.exists(path))   # mode=ro 绝不建文件

    def test_locked_db_short_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._make_db(Path(temp))
            holder = sqlite3.connect(path)
            holder.execute("BEGIN EXCLUSIVE")
            ro = ReadOnlySqlite(path, busy_timeout_ms=40)
            self.assertTrue(ro.open())
            t0 = time.perf_counter()
            self.assertIsNone(ro.query("SELECT * FROM t"))
            elapsed = time.perf_counter() - t0
            self.assertLess(elapsed, 2.0)   # 绝不阻塞 monitor 数秒
            self.assertEqual(ro.busy_count, 1)
            holder.rollback()
            holder.close()
            rows = ro.query("SELECT * FROM t")
            self.assertEqual(len(rows), 1)
            ro.close()

    def test_wal_read_while_writer_alive(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._make_db(Path(temp), name="wal.sqlite")
            writer = sqlite3.connect(path)
            writer.execute("PRAGMA journal_mode=WAL")
            ro = ReadOnlySqlite(path)
            self.assertTrue(ro.open())
            writer.execute("INSERT INTO t (a) VALUES ('live')")
            writer.commit()
            rows = ro.query("SELECT a FROM t ORDER BY id")
            self.assertEqual([r["a"] for r in rows], ["x", "live"])
            ro.close()
            writer.close()

    def test_schema_capability_reports_missing_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._make_db(Path(temp), name="s.sqlite")
            ro = ReadOnlySqlite(path)
            self.assertTrue(ro.open())
            caps = ro.read_schema(["t", "absent"])
            self.assertIsNotNone(caps)
            self.assertTrue(caps.has_table("t"))
            self.assertFalse(caps.has_table("absent"))
            self.assertTrue(caps.has_columns("t", ("id", "a")))
            self.assertFalse(caps.has_columns("t", ("id", "originator")))
            self.assertEqual(caps.columns_of("absent"), ())
            ro.close()

    def test_fingerprint_detects_main_and_wal_change(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._make_db(Path(temp), name="f.sqlite")
            fp1 = stat_fingerprint(path)
            self.assertTrue(fp1.exists)
            self.assertEqual(stat_fingerprint(path), fp1)   # 未变化
            con = sqlite3.connect(path)
            con.execute("INSERT INTO t (a) VALUES ('z')")
            con.commit()
            con.close()
            fp2 = stat_fingerprint(path)
            self.assertTrue(fingerprints_differ(fp1, fp2))
            # WAL 变化也构成指纹变化
            con = sqlite3.connect(path)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("INSERT INTO t (a) VALUES ('w')")
            con.commit()
            con.close()
            fp3 = stat_fingerprint(path)
            self.assertTrue(fingerprints_differ(fp2, fp3))

    def test_query_row_limit_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "lim.sqlite")
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE n (v INTEGER)")
            con.executemany("INSERT INTO n VALUES (?)",
                            [(i,) for i in range(100)])
            con.commit()
            con.close()
            ro = ReadOnlySqlite(path)
            self.assertTrue(ro.open())
            rows = ro.query("SELECT v FROM n ORDER BY v", max_rows=8)
            self.assertEqual(len(rows), 8)
            ro.close()


# ============================================================ claims

class SessionClaimTests(unittest.TestCase):
    def test_claims_cover_exact_identity_only(self):
        claims = frozenset({
            SessionClaimKey(kind="codex", session_id="t1",
                            canonical_session_path="c:\\x\\a.jsonl"),
        })
        self.assertTrue(claims_cover(claims, "codex", session_id="t1"))
        self.assertTrue(claims_cover(
            claims, "codex", canonical_path="C:\\X\\a.jsonl"))   # normcase 后相等
        self.assertFalse(claims_cover(claims, "codex", session_id="t2"))
        self.assertFalse(claims_cover(claims, "claude", session_id="t1"))
        # 空身份不匹配（绝不凭 title/cwd 猜）
        self.assertFalse(claims_cover(claims, "codex"))
        self.assertFalse(claims_cover(claims, "codex", session_id="",
                                      canonical_path=""))

    def test_build_terminal_claims_from_watcher(self):
        from agents.codex import CodexWatcher
        with tempfile.TemporaryDirectory() as temp:
            first = Path(temp) / "one.jsonl"
            first.write_text(json.dumps({
                "type": "session_meta",
                "payload": {"session_id": "s1", "cwd": "/one"}}) + "\n",
                encoding="utf-8")
            watcher = CodexWatcher(
                {"active_file_window_sec": 3600, "session_scan_sec": 1})
            inst = AgentInstance(AgentKind.CODEX, 1, "windows", session_id="s1")
            active = [(first.stat().st_mtime, str(first))]
            with patch("agents.base.paths.session_files", return_value=active):
                watcher.poll([inst])
            claims = build_terminal_claims(
                {AgentKind.CODEX: watcher}, {inst.key: inst})
            self.assertEqual(len(claims), 1)
            claim = next(iter(claims))
            self.assertEqual(claim.kind, "codex")
            self.assertEqual(claim.session_id, "s1")
            self.assertTrue(claims_cover(claims, "codex", session_id="s1"))

    def test_bounded_diagnostics(self):
        items = [f"diag-{i}" for i in range(20)]
        out = bounded_diagnostics(items)
        self.assertLessEqual(len(out), 8)
        long = bounded_diagnostics(["x" * 500])
        self.assertLessEqual(len(long[0]), 200)


# ============================================================ Monitor 生命周期

class MonitorDesktopLifecycleTests(unittest.TestCase):
    """plan2 §15：1 host → N sessions；host exit 原子级联；
    source 失败保留 last good；DESKTOP 不进 terminal service / CLI watcher。"""

    def _monitor(self, source: FakeDesktopSource, hosts: list[DesktopHost]):
        from tests.test_monitoring import MemoryConfig
        config = MemoryConfig()
        monitor = Monitor(config)
        monitor._terminal_service = WindowsTerminalService(None)
        monitor._exit_watcher = _RecordingExitWatcher()
        monitor._desktop_sources = [source]
        monitor._probe = type("P", (), {
            "snapshot": lambda self: {},
            "desktop_hosts": lambda self: tuple(hosts),
            "inventory_authoritative": lambda self: True,
            "join": lambda self, timeout=0.0: None,
            "stop": lambda self: None,
            "_thread": None,
        })()
        return monitor

    def _host(self, pid=900, token="111.000", kind=AgentKind.CODEX):
        return DesktopHost(kind=kind, pid=pid, process_token=token)

    def test_desktop_three_sessions_share_one_host(self):
        host = self._host()
        source = FakeDesktopSource(AgentKind.CODEX)
        source.push(_desktop_snap(source, host, ["t1", "t2", "t3"]))
        monitor = self._monitor(source, [host])
        monitor._tick()
        targets = {k: t for k, t in monitor.get_targets().items()
                   if t.instance.surface is AgentSurface.DESKTOP}
        self.assertEqual(len(targets), 3)
        for key, target in targets.items():
            # key 含 host incarnation token + 逻辑会话 id（plan2 §3.3）
            self.assertIn("desktop", key)
            self.assertIn("111.000", key)
            self.assertTrue(target.instance.logical_session_id in
                            ("t1", "t2", "t3"))
            self.assertEqual(target.snapshot.status, Status.WORKING)
        self.assertEqual(len(monitor._desktop_hosts), 1)
        self.assertFalse(any(t.snapshot.stale for t in targets.values()))

    def test_host_exit_event_removes_sessions_atomically(self):
        host = self._host()
        source = FakeDesktopSource(AgentKind.CODEX)
        source.push(_desktop_snap(source, host, ["t1", "t2", "t3"]))
        monitor = self._monitor(source, [host])
        monitor._tick()
        self.assertEqual(len(monitor.get_targets()), 3)
        # host 进程 signal（exit watcher 事件，token 匹配）→ N 个会话
        # 同一 tick 原子消失（plan2 §9），且 source.drop_host 被通知
        from agents.process_watch import ProcessExitEvent
        monitor._exit_watcher.events = [ProcessExitEvent(
            key=host.host_key, pid=host.pid,
            process_token=host.process_token, timestamp=time.time())]
        source.push(DesktopSourceSnapshot(authoritative=True))
        monitor._tick()
        desktop = [k for k, t in monitor.get_targets().items()
                   if t.instance.surface is AgentSurface.DESKTOP]
        self.assertEqual(desktop, [])
        self.assertEqual(source.dropped_hosts, [host.host_key])
        self.assertNotIn(host.host_key, monitor._desktop_hosts)

    def test_host_census_absence_drops_sessions(self):
        host = self._host()
        source = FakeDesktopSource(AgentKind.CODEX)
        source.push(_desktop_snap(source, host, ["t1"]))
        monitor = self._monitor(source, [host])
        monitor._tick()
        self.assertEqual(len(monitor.get_targets()), 1)
        # census 权威确认 host 退出：probe 返回空 host 列表
        monitor._probe = type("P", (), {
            "snapshot": lambda self: {},
            "desktop_hosts": lambda self: (),
            "inventory_authoritative": lambda self: True,
        })()
        source.push(DesktopSourceSnapshot(authoritative=True))
        monitor._tick()
        self.assertEqual(monitor.get_targets(), {})
        self.assertEqual(source.dropped_hosts, [host.host_key])

    def test_desktop_source_error_retains_last_good_and_marks_stale(self):
        host = self._host()
        source = FakeDesktopSource(AgentKind.CODEX)
        source.push(_desktop_snap(source, host, ["t1"]))
        monitor = self._monitor(source, [host])
        monitor._tick()
        self.assertEqual(len(monitor.get_targets()), 1)
        # DB locked/corrupt → non-authoritative：保留 target + stale，
        # 绝不判死（plan2 §9/§13）
        source.push(DesktopSourceSnapshot(
            authoritative=False, diagnostics=("SQLITE_BUSY",)))
        monitor._tick()
        targets = monitor.get_targets()
        self.assertEqual(len(targets), 1)
        self.assertTrue(list(targets.values())[0].snapshot.stale)
        # 恢复 authoritative 后 stale 清除
        source.push(_desktop_snap(source, host, ["t1"]))
        monitor._tick()
        self.assertFalse(
            list(monitor.get_targets().values())[0].snapshot.stale)

    def test_desktop_session_absence_in_authoritative_snap_removes_it(self):
        host = self._host()
        source = FakeDesktopSource(AgentKind.CODEX)
        source.push(_desktop_snap(source, host, ["t1", "t2"]))
        monitor = self._monitor(source, [host])
        monitor._tick()
        self.assertEqual(len(monitor.get_targets()), 2)
        # source 权威确认 t2 退场（lease 过期/archive）：单会话移除，
        # host 与 t1 不受影响
        source.push(_desktop_snap(source, host, ["t1"]))
        monitor._tick()
        desktop = [t for t in monitor.get_targets().values()
                   if t.instance.surface is AgentSurface.DESKTOP]
        self.assertEqual(len(desktop), 1)
        self.assertEqual(desktop[0].instance.logical_session_id, "t1")

    def test_only_host_handle_registered_in_exit_watcher(self):
        host = self._host()
        source = FakeDesktopSource(AgentKind.CODEX)
        source.push(_desktop_snap(source, host, ["t1", "t2"]))
        monitor = self._monitor(source, [host])
        monitor._tick()
        self.assertEqual(monitor._exit_watcher.registered, [host.host_key])

    def test_terminal_service_never_receives_desktop_target(self):
        host = self._host()
        source = FakeDesktopSource(AgentKind.CODEX)
        source.push(_desktop_snap(source, host, ["t1"]))
        monitor = self._monitor(source, [host])
        terminal_inst = AgentInstance(AgentKind.CODEX, 101, "windows",
                                      process_token="7", session_id="s1")
        with patch.object(monitor._probe, "snapshot",
                          return_value={"windows": type("S", (), {
                              "authoritative": True,
                              "instances": (terminal_inst,),
                              "generation": 1,
                              "source": "windows",
                          })()}):
            seen: list[list] = []
            original = monitor._terminal_service.resolve

            def spy(instances, now):
                seen.append(list(instances))
                return original(instances, now)

            with patch.object(monitor._terminal_service, "resolve",
                              side_effect=spy):
                monitor._tick()
        self.assertTrue(seen)
        for batch in seen:
            for inst in batch:
                self.assertIs(inst.surface, AgentSurface.TERMINAL)

    def test_claimed_terminal_thread_not_duplicated(self):
        # plan2 §3.4/§5.3：CLI 已 exact claim 的 thread，desktop source
        # 不再发同一逻辑会话的 target
        host = self._host()
        from agents.codex import CodexWatcher
        with tempfile.TemporaryDirectory() as temp:
            rollout = Path(temp) / "rollout-t1.jsonl"
            rollout.write_text(json.dumps({
                "type": "session_meta",
                "payload": {"session_id": "t1", "cwd": "/w"}}) + "\n",
                encoding="utf-8")
            watcher = CodexWatcher(
                {"active_file_window_sec": 3600, "session_scan_sec": 1})
            cli_inst = AgentInstance(AgentKind.CODEX, 5, "windows",
                                     process_token="5", session_id="t1")
            with patch("agents.base.paths.session_files",
                       return_value=[(rollout.stat().st_mtime, str(rollout))]):
                watcher.poll([cli_inst])

            class DedupeSource(FakeDesktopSource):
                """模拟真实 source 合同：被 terminal claim 覆盖的会话
                不出现在返回快照中（plan2 §5 去重优先级）。"""

                def poll(self, hosts, claimed_sessions, now):
                    snap = super().poll(hosts, claimed_sessions, now)
                    if not snap.authoritative:
                        return snap
                    keep = [i for i in snap.instances
                            if not claims_cover(
                                claimed_sessions, "codex",
                                session_id=i.logical_session_id)]
                    if len(keep) == len(snap.instances):
                        return snap
                    keys = {i.key for i in keep}
                    return DesktopSourceSnapshot(
                        instances=tuple(keep),
                        observations={k: v for k, v in
                                      snap.observations.items() if k in keys},
                        host_keys=snap.host_keys, authoritative=True)

            source = DedupeSource(AgentKind.CODEX)
            source.push(_desktop_snap(source, host, ["t1", "t9"]))
            monitor = self._monitor(source, [host])
            monitor._watchers[AgentKind.CODEX] = watcher
            with patch.object(monitor._probe, "snapshot",
                              return_value={"windows": type("S", (), {
                                  "authoritative": True,
                                  "instances": (cli_inst,),
                                  "generation": 1,
                                  "source": "windows",
                              })()}):
                monitor._tick()
            codex = [t for t in monitor.get_targets().values()
                     if t.instance.kind is AgentKind.CODEX]
            # CLI target 保留；desktop 只发未被 claim 的 t9
            surfaces = {t.instance.surface for t in codex}
            self.assertIn(AgentSurface.TERMINAL, surfaces)
            desktop_ids = {t.instance.logical_session_id
                           for t in codex
                           if t.instance.surface is AgentSurface.DESKTOP}
            self.assertEqual(desktop_ids, {"t9"})

    def test_shutdown_closes_desktop_sources(self):
        # plan2 §14：join_for_shutdown 释放 source 的连接/文件句柄
        host = self._host()
        source = FakeDesktopSource(AgentKind.CODEX)
        source.push(_desktop_snap(source, host, ["t1"]))
        monitor = self._monitor(source, [host])
        monitor._tick()
        closed = {"count": 0}
        source.close = lambda: closed.__setitem__("count", closed["count"] + 1)
        monitor.join_for_shutdown(1.0)
        self.assertEqual(closed["count"], 1)

    def test_desktop_observation_never_persists_runtime_identity(self):
        # AC-PRIVACY-01（合成对照）：desktop 路径不触发任何 config 持久化，
        # runtime 身份（host token / session id）不进入可持久化对象
        from agents.desktop import DesktopSourceSnapshot
        host = self._host()
        source = FakeDesktopSource(AgentKind.CODEX)
        source.push(_desktop_snap(source, host, ["t1"]))
        monitor = self._monitor(source, [host])
        saves = {"n": 0}
        monitor.config.save = lambda: saves.__setitem__("n", saves["n"] + 1)
        monitor._tick()
        monitor._tick()
        self.assertEqual(saves["n"], 0)
        # DesktopHost/AgentInstance 是 runtime-only 对象：repr 中允许出现，
        # 但任何持久化路径（config.save）从未被调用即满足合同
        snap_repr = repr(DesktopSourceSnapshot())
        self.assertIn("DesktopSourceSnapshot", snap_repr)

    def test_monitor_without_desktop_sources_unchanged(self):
        # 生产默认（无 source 注册）：行为与 4.3.1 完全一致
        from tests.test_monitoring import MemoryConfig
        monitor = Monitor(MemoryConfig())
        monitor._terminal_service = WindowsTerminalService(None)
        monitor._tick()   # 不抛异常、不产生 target
        self.assertEqual(monitor.get_targets(), {})


if __name__ == "__main__":
    unittest.main()
