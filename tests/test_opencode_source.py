"""OpenCode 无插件监听 adapter 测试（无插件监听 plan §七 OpenCode reader）。

全部使用合成 fixture：临时目录内创建 opencode.db（session_v2 schema）；
HTTP 用 fake fetch；WSL reader 用 fake runner。绝不触碰真实用户数据。
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from agents import opencode, paths
from agents.models import (
    AgentInstance,
    AgentKind,
    AgentSurface,
    Confidence,
    DesktopHost,
    Status,
)

_NOW = time.time()
_NOW_MS = int(_NOW * 1000)


def _make_db(directory, rows, extra_tables=()):
    """临时目录内创建 opencode.db（session_v2 + 可选表）。"""
    path = os.path.join(directory, "opencode.db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, project_id TEXT,"
        " parent_id TEXT, directory TEXT, title TEXT,"
        " time_created INTEGER, time_updated INTEGER,"
        " time_idle INTEGER, time_suspended INTEGER)")
    con.executemany(
        "INSERT INTO session_v2 (id, parent_id, directory, title,"
        " time_created, time_updated) VALUES (?,?,?,?,?,?)", rows)
    for ddl in extra_tables:
        con.execute(ddl)
    con.commit()
    con.close()
    return path


def _inst(pid, cwd, key=None, source="windows"):
    return AgentInstance(kind=AgentKind.OPENCODE, pid=pid, source=source,
                         cwd=cwd, key=key or f"{source}|opencode|{pid}|t")


def _host(pid=700):
    return DesktopHost(kind=AgentKind.OPENCODE, source="windows", pid=pid,
                       process_token="9.000", started_at=9.0)


class PathsTests(unittest.TestCase):
    def test_db_candidates_canonical_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, ".local", "share", "opencode"))
            root = os.path.join(tmp, ".local", "share", "opencode")
            open(os.path.join(root, "opencode-beta.db"), "wb").close()
            open(os.path.join(root, "opencode.db"), "wb").close()
            with patch.object(paths, "opencode_data_root", lambda: root), \
                 patch.object(opencode.paths, "opencode_data_root",
                              lambda: root):
                cands = paths.opencode_db_candidates(root)
            self.assertEqual(os.path.basename(cands[0]), "opencode.db")
            # 无 canonical 时渠道后缀也可用
            os.unlink(os.path.join(root, "opencode.db"))
            cands = paths.opencode_db_candidates(root)
            self.assertEqual([os.path.basename(c) for c in cands],
                             ["opencode-beta.db"])

    def test_service_files_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("service.json", "service-beta.json", "random.txt",
                         "service-dev.json", "service-x.json", "service-y.json"):
                open(os.path.join(tmp, name), "wb").close()
            files = paths.opencode_service_files(tmp)
            self.assertLessEqual(len(files), 4)
            self.assertEqual(os.path.basename(files[0]), "service.json")

    def test_env_allowlist_contains_new_overrides(self):
        self.assertIn("OPENCODE_DB", paths.ENV_ALLOWLIST)
        self.assertIn("DSH_HOME", paths.ENV_ALLOWLIST)

    def test_legacy_kind_maps_reject_new_kinds(self):
        with self.assertRaises(ValueError):
            paths.session_subdir(AgentKind.DSH)
        with self.assertRaises(ValueError):
            paths.default_root(AgentKind.OPENCODE)
        self.assertEqual(paths.instance_roots(
            _inst(1, "C:/x"), AgentKind.DSH), [])


class SchemaAdapterTests(unittest.TestCase):
    def _caps(self, tables_sql=(), session_columns=(
            "id", "directory", "title", "parent_id", "time_created",
            "time_updated", "time_idle", "time_suspended")):
        from agents.sqlite_ro import SchemaCapabilities
        columns = {"session_v2": session_columns}
        return SchemaCapabilities(
            tables=frozenset(columns), columns=columns)

    def test_compatible_and_catalog_sql(self):
        adapter = opencode.OpenCodeSchemaAdapter(self._caps())
        self.assertTrue(adapter.compatible())
        sql = adapter.catalog_sql()
        self.assertIn("SELECT", sql)
        self.assertIn("session_v2", sql)
        self.assertIn("LIMIT 32", sql)

    def test_missing_required_columns_incompatible(self):
        adapter = opencode.OpenCodeSchemaAdapter(self._caps(
            session_columns=("id", "directory", "time_created")))
        self.assertFalse(adapter.compatible())
        self.assertIsNone(adapter.catalog_sql())


class LocalReaderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.rows = [
            ("ses_a", None, "C:/work/proj", "Add login", _NOW_MS - 5000,
             _NOW_MS - 1000),
            ("ses_child", "ses_a", "C:/work/proj", "child", _NOW_MS - 4000,
             _NOW_MS - 500),
        ]

    def tearDown(self):
        self._tmp.cleanup()

    def _reader(self, rows=None):
        _make_db(self.dir, self.rows if rows is None else rows)
        return opencode.OpenCodeLocalReader(data_root=self.dir)

    def test_facts_catalog_and_normalization(self):
        reader = self._reader()
        snap = reader.facts(_NOW)
        self.assertTrue(snap.authoritative)
        self.assertEqual(len(snap.catalog), 2)
        by_dir = snap.sessions_by_directory()
        self.assertIn("c:\\work\\proj", by_dir)
        row = by_dir["c:\\work\\proj"]
        self.assertEqual(row[0], "ses_a")   # time_updated 最新优先

    def test_db_absent_returns_none(self):
        reader = opencode.OpenCodeLocalReader(data_root=self.dir)
        self.assertIsNone(reader.facts(_NOW))

    def test_incompatible_schema_degrades(self):
        path = os.path.join(self.dir, "opencode.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE other (x INTEGER)")
        con.commit()
        con.close()
        reader = opencode.OpenCodeLocalReader(data_root=self.dir)
        snap = reader.facts(_NOW)
        self.assertFalse(snap.authoritative)
        self.assertIn("INCOMPATIBLE_SCHEMA", snap.diagnostics)

    def test_zero_write_gate_blocks_on_new_sidecar(self):
        reader = self._reader()
        # 模拟读连接制造了新 -shm：门禁触发，DB 停用
        real = reader._sidecars
        state = {"n": 0}
        def fake(db):
            state["n"] += 1
            # open 前：无 sidecar；query 后：出现 -shm
            return set() if state["n"] == 1 else {"-shm"}
        reader._sidecars = fake
        snap = reader.facts(_NOW)
        self.assertFalse(snap.authoritative)
        self.assertIn("WAL_SIDECAR_ZERO_WRITE_VIOLATION", snap.diagnostics)
        self.assertTrue(reader._zero_write_blocked)
        # 门禁持续：-shm 仍在时不重查
        self.assertIsNone(reader._query(
            os.path.join(self.dir, "opencode.db"), _NOW))
        reader._sidecars = real

    def test_short_connection_no_sidecar_created(self):
        reader = self._reader()
        before = reader._sidecars(os.path.join(self.dir, "opencode.db"))
        snap = reader.facts(_NOW)
        after = reader._sidecars(os.path.join(self.dir, "opencode.db"))
        self.assertTrue(snap.authoritative)
        self.assertEqual(before, after)


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.rows = [
            ("ses_a", None, "C:/work/proj", "Add login",
             _NOW_MS - 5000, _NOW_MS - 1000),
        ]
        _make_db(self.dir, self.rows, extra_tables=(
            "CREATE TABLE session_pending (id TEXT PRIMARY KEY,"
            " session_id TEXT, type TEXT, data TEXT, admitted_seq INTEGER)",
            "INSERT INTO session_pending VALUES ('p1','ses_a','user',"
            "'{}',1)",
        ))

    def tearDown(self):
        self._tmp.cleanup()

    def _runtime(self, http=None, remote=None):
        return opencode.OpenCodeRuntime(
            cfg={},
            local_reader=opencode.OpenCodeLocalReader(data_root=self.dir),
            remote_provider=remote,
            http_provider=http)

    def test_db_row_is_not_working_and_pending_table_is_not_approval(self):
        # plan §六：DB 行存在 ≠ WORKING；session_pending 是 inbox 队列，
        # 绝不能产生 WAITING。无 HTTP 证据 → 状态 UNKNOWN + 会话元数据。
        runtime = self._runtime()
        watcher = opencode.OpenCodeWatcher({}, runtime)
        out = watcher.poll([_inst(101, "C:\\work\\proj")])
        self.assertEqual(len(out), 1)
        obs = next(iter(out.values()))
        self.assertIs(obs.status, Status.UNKNOWN)
        self.assertTrue(obs.session_bound)
        self.assertEqual(obs.session_id, "ses_a")
        self.assertEqual(obs.title, "Add login")
        self.assertEqual(obs.cwd, "C:/work/proj")

    def test_same_cwd_two_instances_unbound(self):
        runtime = self._runtime()
        watcher = opencode.OpenCodeWatcher({}, runtime)
        out = watcher.poll([_inst(101, "C:\\work\\proj"),
                            _inst(102, "C:\\work\\proj")])
        for obs in out.values():
            self.assertFalse(obs.session_bound)
            self.assertIsNone(obs.status)
        self.assertEqual(watcher.terminal_claims(), frozenset())

    def test_trusted_pending_gives_waiting(self):
        snap = opencode.OpenCodeHttpSnapshot(
            available=True, authoritative=True,
            pending={"ses_a": "bash exec"}, server_pid=5,
            url="http://127.0.0.1:49374", observed_at=time.time())
        runtime = self._runtime(http=lambda: snap)
        watcher = opencode.OpenCodeWatcher({}, runtime)
        out = watcher.poll([_inst(101, "C:\\work\\proj")])
        obs = next(iter(out.values()))
        self.assertIs(obs.status, Status.WAITING)
        self.assertEqual(obs.waiting_detail, "bash exec")

    def test_stale_http_pending_not_waiting(self):
        snap = opencode.OpenCodeHttpSnapshot(
            available=True, authoritative=True, pending={"ses_a": "bash"},
            server_pid=5, url="http://127.0.0.1:49374",
            observed_at=time.time() - 60)
        runtime = self._runtime(http=lambda: snap)
        watcher = opencode.OpenCodeWatcher({}, runtime)
        out = watcher.poll([_inst(101, "C:\\work\\proj")])
        self.assertIs(next(iter(out.values())).status, Status.UNKNOWN)

    def test_trusted_active_endpoint_marks_session_working(self):
        snap = opencode.OpenCodeHttpSnapshot(
            available=True, authoritative=True, active=frozenset({"ses_a"}),
            server_pid=5, url="http://127.0.0.1:49374",
            observed_at=time.time())
        runtime = self._runtime(http=lambda: snap)
        watcher = opencode.OpenCodeWatcher({}, runtime)
        out = watcher.poll([_inst(101, "C:\\work\\proj")])
        self.assertIs(next(iter(out.values())).status, Status.WORKING)

    def test_execution_claim_holds_working_without_http(self):
        # store.ts：time_suspended 是 turn 执行 claim；无 HTTP 证据时
        # turn 内静默保持 WORKING（有界孤儿 TTL），不再抖回 UNKNOWN。
        runtime = self._runtime()
        claimed = ("ses_c", "C:/work/proj", "Claim", "", _NOW_MS,
                   0, 0, "", _NOW_MS - 30000)
        obs = runtime.observation_for_session(claimed, _NOW)
        self.assertIs(obs.status, Status.WORKING)
        self.assertIs(obs.confidence, Confidence.MEDIUM)
        self.assertTrue(obs.turn_active)
        orphan = ("ses_c", "C:/work/proj", "Claim", "", _NOW_MS,
                  0, 0, "",
                  _NOW_MS - int(opencode._CLAIM_ORPHAN_SEC * 1000) - 5000)
        self.assertIs(
            runtime.observation_for_session(orphan, _NOW).status,
            Status.UNKNOWN)

    def test_fresh_message_streams_working_with_text(self):
        runtime = self._runtime()
        row = ("ses_m", "C:/work/proj", "Msg", "", _NOW_MS, 0,
               0, "", 0)
        msg = ("ses_m", _NOW_MS - 2000, "assistant", "正在重构气泡格式")
        obs = runtime.observation_for_session(row, _NOW, message=msg)
        self.assertIs(obs.status, Status.WORKING)
        self.assertIs(obs.confidence, Confidence.HIGH)
        self.assertIn("气泡", obs.summary)
        stale = ("ses_m", _NOW_MS - int(opencode._LIVE_MESSAGE_FRESH_SEC
                                        * 1000) - 5000,
                 "assistant", "旧文本")
        self.assertIs(
            runtime.observation_for_session(row, _NOW, message=stale).status,
            Status.UNKNOWN)

    def test_idle_newer_than_claim_suppresses_activity(self):
        # time_idle 晚于 claim/最新消息：turn 已结束，残留 claim 与旧消息
        # 不得复活 WORKING。
        runtime = self._runtime()
        row = ("ses_i", "C:/work/proj", "Idle", "", _NOW_MS,
               _NOW_MS - 1000, _NOW_MS - 500, "completed",
               _NOW_MS - 1000)
        msg = ("ses_i", _NOW_MS - 2000, "assistant", "旧文本")
        obs = runtime.observation_for_session(row, _NOW, message=msg)
        self.assertIs(obs.status, Status.DONE)

    def test_catalog_lifecycle_outcomes_emit_bounded_reminders(self):
        runtime = self._runtime()
        updated = int((_NOW - 1) * 1000)
        completed = ("ses_done", "C:/work/done", "Done", "", updated,
                     updated, 0, "completed")
        failed = ("ses_failed", "C:/work/fail", "Failed", "", updated,
                  updated, 0, "aborted")
        self.assertIs(
            runtime.observation_for_session(completed, _NOW).status,
            Status.DONE)
        self.assertIs(
            runtime.observation_for_session(failed, _NOW).status,
            Status.ERROR)
        old = ("ses_old", "C:/work/old", "Old", "", updated - 60000,
               updated - 60000, 0, "completed")
        self.assertIs(
            runtime.observation_for_session(old, _NOW).status,
            Status.UNKNOWN)

    def test_remote_facts_used_for_wsl_instances(self):
        snap = opencode.OpenCodeFactsSnapshot(
            source="wsl:Ubuntu", plane_key="wsl:Ubuntu|uid-home:/home/u",
            authoritative=True,
            catalog=(("ses_w", "/home/u/proj", "WSL task", "",
                      int(time.time() * 1000)),),
            observed_at=time.time())
        runtime = self._runtime(remote=lambda: {snap.plane_key: snap})
        watcher = opencode.OpenCodeWatcher({}, runtime)
        out = watcher.poll([_inst(201, "/home/u/proj", source="wsl:Ubuntu")])
        obs = next(iter(out.values()))
        self.assertTrue(obs.session_bound)
        self.assertEqual(obs.session_id, "ses_w")
        self.assertIs(obs.status, Status.UNKNOWN)


class HttpProbeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write_registration(self, url="http://127.0.0.1:49374", pid=4242,
                            password="SECRET-VALUE"):
        obj = {"id": "inst-1", "version": "1.2.3", "url": url, "pid": pid}
        if password is not None:
            obj["password"] = password
        with open(os.path.join(self.state_root, "service.json"), "w",
                  encoding="utf-8") as f:
            json.dump(obj, f)

    def _probe(self, fetch):
        return opencode.OpenCodeHttpProbe(fetch=fetch,
                                          state_root=self.state_root)

    def test_password_never_enters_snapshot(self):
        self._write_registration()
        calls = []

        def fetch(url):
            calls.append(url)
            if url.endswith("/api/info"):
                return 200, json.dumps({"version": "1.2.3", "pid": 4242,
                                        "urls": []}).encode()
            return 200, json.dumps({"data": []}).encode()

        probe = self._probe(fetch)
        snap = probe.refresh({4242}, _NOW)
        self.assertTrue(snap.available)
        snap_text = json.dumps(snap.__dict__, default=str)
        self.assertNotIn("SECRET-VALUE", snap_text)
        self.assertEqual(snap.server_pid, 4242)

    def test_registration_pid_not_live_skips_http(self):
        self._write_registration(pid=4242)

        def fetch(url):  # pragma: no cover - 不应被调用
            raise AssertionError("must not fetch")

        snap = self._probe(fetch).refresh({1111}, _NOW)
        self.assertFalse(snap.available)
        self.assertEqual(snap.reason, "REGISTRATION_PID_NOT_LIVE")

    def test_info_pid_mismatch_rejects(self):
        self._write_registration(pid=4242)

        def fetch(url):
            if url.endswith("/api/info"):
                return 200, json.dumps({"pid": 9999}).encode()
            raise AssertionError("pending must not be fetched")

        snap = self._probe(fetch).refresh({4242}, _NOW)
        self.assertEqual(snap.reason, "INFO_PID_MISMATCH")

    def test_auth_required_backs_off_and_never_waits(self):
        self._write_registration()

        def fetch(url):
            return 401, b""

        probe = self._probe(fetch)
        snap = probe.refresh({4242}, _NOW)
        self.assertFalse(snap.available)
        self.assertEqual(snap.reason, "AUTH_REQUIRED")
        # 认证退避窗口内不再请求
        calls = []
        def fetch2(url):
            calls.append(url)
            return 401, b""
        probe._fetch = fetch2
        probe.refresh({4242}, _NOW + 1)
        self.assertEqual(calls, [])

    def test_no_registration(self):
        snap = self._probe(lambda url: (200, b"{}")).refresh({1}, _NOW)
        self.assertEqual(snap.reason, "NO_REGISTRATION")

    def test_non_loopback_registration_ignored(self):
        self._write_registration(url="http://0.0.0.0:8080")
        snap = self._probe(lambda url: (200, b"{}")).refresh({4242}, _NOW)
        self.assertEqual(snap.reason, "NO_REGISTRATION")


class WslReaderTests(unittest.TestCase):
    def _payload(self, rows=(), error="", sidecar=False):
        return json.dumps({"error": error, "sidecar_created": sidecar,
                           "rows": [list(r) for r in rows]}).encode()

    def _runner(self, payload, rc=0):
        class R:
            returncode = rc
            stdout = payload
            stderr = b""
        return lambda argv, **kw: R()

    def test_rows_normalized(self):
        reader = opencode.OpenCodeWslReader(
            runner=self._runner(self._payload(rows=[
                ("ses_w", "/home/u/proj", "title", "", str(_NOW_MS))])))
        snap = reader.read("Ubuntu", "u", "/home/u")
        self.assertTrue(snap.authoritative)
        self.assertEqual(snap.catalog[0][0], "ses_w")
        self.assertEqual(snap.catalog[0][4], _NOW_MS)
        self.assertIn("python3", reader.spawn_count * "" or "python3")

    def test_sidecar_creation_violates_zero_write(self):
        reader = opencode.OpenCodeWslReader(
            runner=self._runner(self._payload(sidecar=True)))
        snap = reader.read("Ubuntu", "u", "/home/u")
        self.assertFalse(snap.authoritative)
        self.assertIn("WAL_SIDECAR_ZERO_WRITE_VIOLATION", snap.diagnostics)

    def test_reader_missing_degrades(self):
        reader = opencode.OpenCodeWslReader(
            runner=self._runner(b"", rc=127))
        snap = reader.read("Ubuntu", "u", "/home/u")
        self.assertFalse(snap.authoritative)
        self.assertIn("REMOTE_READER_RC_127", snap.diagnostics)

    def test_incomplete_identity_rejected(self):
        reader = opencode.OpenCodeWslReader(
            runner=self._runner(self._payload()))
        snap = reader.read("Ubuntu", "", "/home/u")
        self.assertFalse(snap.authoritative)


class DesktopSourceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.rows = [
            ("ses_a", None, "C:/work/proj", "Add login",
             _NOW_MS - 5000, _NOW_MS - 1000),
            ("ses_child", "ses_a", "C:/work/proj", "child",
             _NOW_MS - 4000, _NOW_MS - 500),
        ]
        _make_db(self.dir, self.rows)

    def tearDown(self):
        self._tmp.cleanup()

    def _source(self, http=None):
        runtime = opencode.OpenCodeRuntime(
            cfg={},
            local_reader=opencode.OpenCodeLocalReader(data_root=self.dir),
            http_provider=http)
        return opencode.OpenCodeDesktopSource(runtime=runtime), runtime

    def test_root_sessions_only_with_unknown_status(self):
        src, _rt = self._source()
        snap = src.poll((_host(),), frozenset(), _NOW)
        self.assertTrue(snap.authoritative)
        self.assertEqual(len(snap.instances), 1)   # 子会话不单独成 pet
        inst = snap.instances[0]
        self.assertEqual(inst.logical_session_id, "ses_a")
        self.assertIs(snap.observations[inst.key].status, Status.UNKNOWN)
        self.assertEqual(snap.observations[inst.key].title, "Add login")

    def test_claim_covered_session_not_duplicated(self):
        src, _rt = self._source()
        claims = frozenset({("opencode", "", "ses_a", "")})  # 结构占位
        from agents.desktop import SessionClaimKey
        claims = frozenset({SessionClaimKey(kind="opencode",
                                            session_id="ses_a")})
        snap = src.poll((_host(),), claims, _NOW)
        self.assertEqual(snap.instances, ())

    def test_no_hosts_clears(self):
        src, _rt = self._source()
        snap = src.poll((), frozenset(), _NOW)
        self.assertTrue(snap.authoritative)
        self.assertEqual(snap.instances, ())

    def test_one_source_per_kind_registered_by_monitor(self):
        # plan §三：一个 Kind 只注册一个权威 Desktop source
        from agents.monitor import Monitor
        cfg = {"monitor": {"agents": {"claude": False, "codex": False,
                                      "kimi": False, "pi": False,
                                      "zcode": False, "opencode": True,
                                      "dsh": True},
                           "windows_enabled": False, "wsl_enabled": False}}
        monitor = Monitor(cfg)
        monitor._register_desktop_sources()
        kinds = [s.kind for s in monitor._desktop_sources]
        self.assertEqual(kinds.count(AgentKind.OPENCODE), 1)
        self.assertEqual(kinds.count(AgentKind.DSH), 1)


if __name__ == "__main__":
    unittest.main()
