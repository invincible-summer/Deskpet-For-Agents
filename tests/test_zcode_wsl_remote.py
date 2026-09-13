"""DeskPet 4.1.0 ZCode Desktop -> WSL Remote Development regression tests."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agents import paths
from agents.discovery import DistroInventory, WslProcessProbe, parse_metadata
from agents.models import (
    AgentKind, AgentSurface, DesktopHost, RemoteRuntimeContext,
    SourceProbeSnapshot, Status,
)
from agents.monitor import ProcessProbeWorker
from agents.zcode_desktop import ZCodeDesktopSource
from agents.zcode_remote import ZCodeRemoteFactsSnapshot, ZCodeRemoteReader
from pet.labels import environment_label


def _ctx(*, distro="Ubuntu", uid=1000, user="dev", home="/home/dev",
         generation=1, pid=220):
    return RemoteRuntimeContext(
        kind=AgentKind.ZCODE, transport="wsl-exec",
        source=f"wsl:{distro}", distro=distro, pid=pid, uid=uid,
        user=user, home=home, runtime_role="agent",
        observed_at=time.time(), generation=generation)


def _host(pid=900, token="host-token"):
    return DesktopHost(kind=AgentKind.ZCODE, source="windows", pid=pid,
                       process_token=token, started_at=10.0)


def _facts(sid="remote-1", *, status="working", now_ms=None):
    now_ms = now_ms or int(time.time() * 1000)
    catalog = [{"id": sid, "time_created": now_ms - 60_000,
                "time_updated": now_ms, "parent_id": None,
                "task_type": "interactive", "title": "remote",
                "directory": "/work"}]
    models, tools, turns = [], [], []
    if status == "working":
        models = [(sid, None, "main_turn", now_ms)]
    elif status == "waiting":
        tools = [(sid, None, "Bash", "running", "requested", now_ms)]
    elif status == "done":
        turns = [(sid, "completed", now_ms - 3000, now_ms - 500)]
    return {"catalog": catalog, "models": models, "tools": tools,
            "turns": turns}


class PathTests(unittest.TestCase):
    def test_exact_home_paths_and_containment(self):
        self.assertEqual(paths.zcode_wsl_db_path("/srv/users/alice"),
                         "/srv/users/alice/.zcode/cli/db/db.sqlite")
        self.assertEqual(paths.zcode_wsl_server_node("/srv/users/alice"),
                         "/srv/users/alice/.zcode/server/node")
        with self.assertRaises(ValueError):
            paths.zcode_wsl_db_path("")
        with self.assertRaises(ValueError):
            paths.zcode_wsl_db_path("relative/home")


class DiscoveryTests(unittest.TestCase):
    def test_runtime_match_is_strict_and_not_terminal_kind(self):
        p = WslProcessProbe()
        self.assertEqual(p._match_zcode_remote_runtime(
            "node", "/home/u/.zcode/server/zcode-server.cjs --stdio"), "server")
        self.assertEqual(p._match_zcode_remote_runtime(
            "node", "/home/u/.zcode/server/agents/glm/zcode.cjs"), "agent")
        self.assertEqual(p._match_zcode_remote_runtime(
            "zcode-agent", "/home/u/.zcode/server/agents/glm/zcode-agent"), "agent")
        self.assertIsNone(p._match_zcode_remote_runtime(
            "node", "/tmp/zcode.cjs"))
        self.assertIsNone(p._match_agent(
            "node", "/home/u/.zcode/server/agents/glm/zcode.cjs"))

    def test_metadata_parser_uses_explicit_getent_username(self):
        parsed = parse_metadata(
            "P\t22\nC\t/work\nT\t123\nH\t1001\t/srv/home/x\nU\talice\n")
        self.assertEqual(parsed[22]["uid"], "1001")
        self.assertEqual(parsed[22]["home"], "/srv/home/x")
        self.assertEqual(parsed[22]["user"], "alice")

    def test_server_and_agent_child_collapse_to_one_remote_plane(self):
        p = WslProcessProbe()
        rows = [
            (100, 1, 100, 100, 0, "?", 1000, 10, "S", "node",
             "/home/dev/.zcode/server/zcode-server.cjs"),
            (101, 100, 100, 100, 0, "?", 1000, 9, "S", "node",
             "/home/dev/.zcode/server/agents/glm/zcode.cjs"),
        ]
        with mock.patch.object(p, "_list_running_distros",
                               return_value=DistroInventory(("Ubuntu",), True)), \
             mock.patch.object(p, "_ps_scan", return_value=rows), \
             mock.patch.object(p, "_metadata", return_value={
                 101: {"cwd": "/work", "ticks": "11", "uid": "1000",
                       "user": "dev", "home": "/home/dev", "env": {}}}):
            snap = p.scan()["wsl:Ubuntu"]
        self.assertTrue(snap.authoritative)
        self.assertEqual(snap.instances, ())
        self.assertEqual(len(snap.remote_runtimes), 1)
        self.assertEqual(snap.remote_runtimes[0].pid, 101)
        self.assertEqual(snap.remote_runtimes[0].runtime_role, "agent")

    def test_non_authoritative_scan_retains_runtime_cache_and_stop_clears(self):
        p = WslProcessProbe()
        rows = [(101, 1, 101, 101, 0, "?", 1000, 9, "S", "node",
                 "/home/dev/.zcode/server/agents/glm/zcode.cjs")]
        meta = {101: {"cwd": "/w", "ticks": "1", "uid": "1000",
                      "user": "dev", "home": "/home/dev", "env": {}}}
        with mock.patch.object(p, "_list_running_distros",
                               return_value=DistroInventory(("Ubuntu",), True)), \
             mock.patch.object(p, "_ps_scan", return_value=rows), \
             mock.patch.object(p, "_metadata", return_value=meta):
            first = p.scan()["wsl:Ubuntu"]
        self.assertEqual(len(first.remote_runtimes), 1)
        with mock.patch.object(p, "_list_running_distros",
                               return_value=DistroInventory(("Ubuntu",), False,
                                                            "list failed")):
            stale = p.scan()["wsl:Ubuntu"]
        self.assertFalse(stale.authoritative)
        self.assertEqual(len(stale.remote_runtimes), 1)
        with mock.patch.object(p, "_list_running_distros",
                               return_value=DistroInventory((), True)):
            stopped = p.scan()["wsl:Ubuntu"]
        self.assertTrue(stopped.authoritative)
        self.assertEqual(stopped.remote_runtimes, ())


class RemoteReaderTests(unittest.TestCase):
    def _schema(self):
        return {"user_version": 0, "columns": {
            "session": ["id", "time_created", "time_updated", "parent_id",
                        "task_type", "title", "directory"],
            "model_usage": ["session_id", "status", "started_at", "query_source"],
            "tool_usage": ["session_id", "status", "started_at", "approval_status",
                           "tool_name"],
            "turn_usage": ["session_id", "status", "started_at", "completed_at"],
        }}

    def test_direct_argv_readonly_transport_and_cache(self):
        calls = []
        responses = [self._schema(), {
            "catalog": [{"id": "s", "time_created": 1, "time_updated": 2}],
            "models": [{"session_id": "s", "parent_id": None,
                        "query_source": "main", "started_at": 2}],
            "tools": [], "turns": [],
        }, {
            "catalog": [{"id": "s", "time_created": 1, "time_updated": 3}],
            "models": [], "tools": [], "turns": [],
        }]
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            payload = responses.pop(0)
            return SimpleNamespace(returncode=0,
                                   stdout=json.dumps(payload).encode(), stderr=b"")
        reader = ZCodeRemoteReader(runner=runner)
        snap = reader.read(_ctx(user="dev;touch /tmp/pwn"))
        self.assertTrue(snap.authoritative)
        self.assertEqual(reader.spawn_count, 2)
        argv = calls[0][0]
        self.assertNotIn("sh", argv)
        self.assertNotIn("-c", argv)
        self.assertEqual(argv[argv.index("-u") + 1], "dev;touch /tmp/pwn")
        self.assertIn("--exec", argv)
        self.assertEqual(argv[argv.index("--exec") + 1],
                         "/home/dev/.zcode/server/node")
        self.assertIn("readOnly: true", argv[argv.index("-e") + 1])
        self.assertIn("allowExtension: false", argv[argv.index("-e") + 1])
        snap2 = reader.read(_ctx(user="dev;touch /tmp/pwn"))
        self.assertTrue(snap2.authoritative)
        self.assertEqual(reader.spawn_count, 3)  # schema is cached

    def test_timeout_and_invalid_json_are_non_authoritative_last_good(self):
        good = [self._schema(), {"catalog": [], "models": [],
                                 "tools": [], "turns": []}]
        mode = {"timeout": False, "bad": False}
        def runner(argv, **kwargs):
            if mode["timeout"]:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))
            if mode["bad"]:
                return SimpleNamespace(returncode=0, stdout=b"not-json", stderr=b"")
            return SimpleNamespace(returncode=0,
                                   stdout=json.dumps(good.pop(0)).encode(), stderr=b"")
        reader = ZCodeRemoteReader(runner=runner)
        self.assertTrue(reader.read(_ctx()).authoritative)
        mode["timeout"] = True
        stale = reader.read(_ctx())
        self.assertFalse(stale.authoritative)
        self.assertIsNotNone(stale.facts)
        self.assertEqual(reader.timeout_count, 1)
        mode["timeout"] = False
        mode["bad"] = True
        bad = reader.read(_ctx())
        self.assertFalse(bad.authoritative)
        self.assertTrue(any("JSON" in d for d in bad.diagnostics))


class DesktopProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.missing = str(Path(self.tmp.name) / "missing.sqlite")
        self.host = _host()
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def test_remote_only_works_when_local_db_missing(self):
        ctx = _ctx()
        snap = ZCodeRemoteFactsSnapshot(ctx, True, _facts("r1"), (), self.now)
        source = ZCodeDesktopSource(zcode_db=self.missing,
                                    remote_provider=lambda: {ctx.plane_key: snap})
        out = source.poll((self.host,), frozenset(), self.now)
        self.assertTrue(out.authoritative)
        self.assertEqual(len(out.instances), 1)
        inst = out.instances[0]
        self.assertIs(inst.surface, AgentSurface.DESKTOP)
        self.assertEqual(inst.source, "wsl:Ubuntu")
        self.assertEqual(inst.host_pid, self.host.pid)
        self.assertEqual(out.observations[inst.key].status, Status.WORKING)
        self.assertEqual(environment_label(inst), "ZCode · Desktop · WSL Ubuntu")
        source.close()

    def test_remote_waiting_is_exactly_session_scoped(self):
        ctx = _ctx()
        snap = ZCodeRemoteFactsSnapshot(ctx, True, _facts("approve", status="waiting"),
                                        (), self.now)
        source = ZCodeDesktopSource(zcode_db=self.missing,
                                    remote_provider=lambda: {ctx.plane_key: snap})
        out = source.poll((self.host,), frozenset(), self.now)
        self.assertEqual(out.observations[out.instances[0].key].status,
                         Status.WAITING)
        source.close()

    def test_plane_disappearance_removes_remote_sessions(self):
        ctx = _ctx()
        state = {ctx.plane_key: ZCodeRemoteFactsSnapshot(
            ctx, True, _facts("r1"), (), self.now)}
        source = ZCodeDesktopSource(zcode_db=self.missing,
                                    remote_provider=lambda: dict(state))
        self.assertEqual(len(source.poll((self.host,), frozenset(), self.now).instances), 1)
        state.clear()
        out = source.poll((self.host,), frozenset(), self.now + 1)
        self.assertEqual(out.instances, ())
        self.assertEqual(source.stats()["zcode_desktop_remote_planes"], 0)
        source.close()

    def test_global_eight_session_cap_across_two_remote_planes(self):
        c1 = _ctx(distro="Ubuntu", uid=1000, user="a", home="/home/a")
        c2 = _ctx(distro="Debian", uid=1001, user="b", home="/home/b")
        def many(prefix, count):
            now_ms = int(self.now * 1000)
            facts = {"catalog": [], "models": [], "tools": [], "turns": []}
            for i in range(count):
                sid = f"{prefix}-{i}"
                facts["catalog"].append({"id": sid, "time_created": now_ms-1,
                                         "time_updated": now_ms, "parent_id": None,
                                         "task_type": "interactive"})
                facts["models"].append((sid, None, "main", now_ms))
            return facts
        remote = {
            c1.plane_key: ZCodeRemoteFactsSnapshot(c1, True, many("a", 5), (), self.now),
            c2.plane_key: ZCodeRemoteFactsSnapshot(c2, True, many("b", 5), (), self.now),
        }
        source = ZCodeDesktopSource(zcode_db=self.missing,
                                    remote_provider=lambda: remote)
        out = source.poll((self.host,), frozenset(), self.now)
        self.assertEqual(len(out.instances), 8)
        self.assertEqual(len({i.key for i in out.instances}), 8)
        source.close()


class WorkerGatingTests(unittest.TestCase):
    class Config(dict):
        pass

    def test_no_windows_zcode_host_means_zero_remote_exec(self):
        worker = ProcessProbeWorker(self.Config(monitor={"agents": {"zcode": True}}))
        worker._snapshot["windows"] = SourceProbeSnapshot(
            source="windows", generation=1, observed_at=time.time(),
            authoritative=True)
        worker._inventory = SimpleNamespace(desktop_hosts=(), authoritative=True)
        ctx = _ctx()
        sp = SourceProbeSnapshot(source=ctx.source, generation=1,
                                 observed_at=time.time(), authoritative=True,
                                 remote_runtimes=(ctx,))
        fake = mock.Mock()
        worker._zcode_remote.read = fake
        worker._refresh_zcode_remote({ctx.source: sp}, time.time())
        fake.assert_not_called()

    def test_non_authoritative_wsl_never_spawns_remote_read(self):
        worker = ProcessProbeWorker(self.Config(monitor={"agents": {"zcode": True}}))
        worker._snapshot["windows"] = SourceProbeSnapshot(
            source="windows", generation=1, observed_at=time.time(),
            authoritative=True)
        worker._inventory = SimpleNamespace(desktop_hosts=(_host(),), authoritative=True)
        ctx = _ctx()
        old = ZCodeRemoteFactsSnapshot(ctx, True, _facts("x"), (), time.time())
        worker._zcode_remote_snapshots[ctx.plane_key] = old
        sp = SourceProbeSnapshot(source=ctx.source, generation=2,
                                 observed_at=time.time(), authoritative=False,
                                 error="ps failed", remote_runtimes=(ctx,))
        fake = mock.Mock()
        worker._zcode_remote.read = fake
        worker._refresh_zcode_remote({ctx.source: sp}, time.time())
        fake.assert_not_called()
        self.assertFalse(worker.zcode_remote_snapshots()[ctx.plane_key].authoritative)


if __name__ == "__main__":
    unittest.main()
