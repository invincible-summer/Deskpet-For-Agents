"""Read-only ZCode Remote Development facts transport for WSL.

The Windows ZCode process remains the DesktopHost/control surface.  This module reads
session-state facts where the Agent actually runs: inside a freshly-observed WSL
runtime and under that runtime's exact uid/user/HOME.  It never scans /home/*, never
opens a live WSL SQLite database through a Windows UNC path, never installs helpers,
and never writes/checkpoints/repairs the Agent database.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, replace

from .models import AgentKind, RemoteRuntimeContext
from .paths import zcode_wsl_db_path, zcode_wsl_server_node
from .sqlite_ro import SchemaCapabilities
from .zcode_desktop import ZCodeSchemaAdapter, _LIVE_PART_FRESH_SEC

_TIMEOUT_SEC = 1.5
_MAX_STDOUT = 64 * 1024
_MAX_DIAGNOSTICS = 6
_TABLES = ("session", "model_usage", "tool_usage", "turn_usage", "part")
_RUNTIME_FRESH_SEC = 15.0

_IDENTITY_SCRIPT = r"""
const fs = require('node:fs');
const os = require('node:os');
const identity = JSON.parse(process.argv[process.argv.length - 1]);
if (process.getuid() !== identity.uid || os.homedir() !== identity.home)
  throw new Error('REMOTE_USER_CHANGED');
const stat = fs.readFileSync(`/proc/${identity.pid}/stat`, 'utf8');
const fields = stat.slice(stat.lastIndexOf(')') + 2).trim().split(/\s+/);
const status = fs.readFileSync(`/proc/${identity.pid}/status`, 'utf8');
const uid = /^Uid:\s+(\d+)/m.exec(status);
if (fields[19] !== identity.token || !uid || Number(uid[1]) !== identity.uid)
  throw new Error('REMOTE_RUNTIME_CHANGED');
const dbPath = process.argv[1];
const containedPath = fs.realpathSync(identity.home) + '/.zcode/cli/db/db.sqlite';
if (fs.realpathSync(dbPath) !== containedPath)
  throw new Error('REMOTE_DB_PATH_REJECTED');
if (fs.existsSync(dbPath + '-wal') && !fs.existsSync(dbPath + '-shm'))
  throw new Error('REMOTE_WAL_SIDECAR_UNAVAILABLE');
""".strip()

_SCHEMA_SCRIPT = r"""
const { DatabaseSync } = require('node:sqlite');
const path = process.argv[1];
const wanted = ['session', 'model_usage', 'tool_usage', 'turn_usage', 'part'];
let db;
try {
  db = new DatabaseSync(path, {readOnly: true, timeout: 40, allowExtension: false});
  db.exec('PRAGMA query_only=ON');
  const names = new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map(r => String(r.name)));
  const columns = {};
  for (const table of wanted) {
    if (!names.has(table)) continue;
    columns[table] = db.prepare(`PRAGMA table_info("${table}")`).all().map(r => String(r.name));
  }
  const uv = db.prepare('PRAGMA user_version').get();
  process.stdout.write(JSON.stringify({user_version: Number((uv && uv.user_version) || 0), columns}));
} finally {
  if (db) db.close();
}
""".strip()

_FACTS_SCRIPT = r"""
const { DatabaseSync } = require('node:sqlite');
const path = process.argv[1];
const plan = JSON.parse(process.argv[2]);
let db;
try {
  db = new DatabaseSync(path, {readOnly: true, timeout: 40, allowExtension: false});
  db.exec('PRAGMA query_only=ON');
  const out = {};
  for (const q of plan) out[q.key] = db.prepare(q.sql).all(...q.params);
  process.stdout.write(JSON.stringify(out));
} finally {
  if (db) db.close();
}
""".strip()


@dataclass(frozen=True)
class ZCodeRemoteFactsSnapshot:
    context: RemoteRuntimeContext
    authoritative: bool
    facts: dict | None = None
    diagnostics: tuple[str, ...] = ()
    observed_at: float = 0.0

    @property
    def plane_key(self) -> str:
        return self.context.plane_key


class ZCodeRemoteReader:
    """Bounded, subprocess-only WSL reader; no resident helper/thread is created."""

    def __init__(self, runner=None, timeout_sec: float = _TIMEOUT_SEC):
        self._runner = runner or subprocess.run
        self.timeout_sec = min(2.0, max(0.2, float(timeout_sec)))
        self._adapters: dict[str, ZCodeSchemaAdapter] = {}
        self._last_good: dict[str, dict] = {}
        self._last_good_at: dict[str, float] = {}
        self._identities: dict[str, tuple] = {}
        self.spawn_count = 0
        self.timeout_count = 0
        self.error_count = 0
        self.schema_probe_count = 0
        self.facts_query_count = 0

    def drop_except(self, active: set[str]) -> None:
        for key in list(self._identities):
            if key not in active:
                self._adapters.pop(key, None)
                self._last_good.pop(key, None)
                self._last_good_at.pop(key, None)
                self._identities.pop(key, None)

    def close(self) -> None:
        self._adapters.clear()
        self._last_good.clear()
        self._last_good_at.clear()
        self._identities.clear()

    def stale(self, snap: ZCodeRemoteFactsSnapshot, reason: str,
              now: float | None = None) -> ZCodeRemoteFactsSnapshot:
        diag = tuple((list(snap.diagnostics) + [reason])[-_MAX_DIAGNOSTICS:])
        return replace(snap, authoritative=False, diagnostics=diag)

    def read(self, ctx: RemoteRuntimeContext) -> ZCodeRemoteFactsSnapshot:
        now = time.time()
        key = ctx.plane_key
        if (ctx.kind is not AgentKind.ZCODE or ctx.transport != "wsl-exec"
                or not ctx.distro or ctx.source != f"wsl:{ctx.distro}"
                or not ctx.user or not ctx.home or ctx.pid <= 0 or ctx.uid < 0
                or not ctx.process_token
                or ctx.runtime_role not in {"agent", "server"}):
            return self._failure(ctx, "REMOTE_RUNTIME_IDENTITY_INCOMPLETE", now)
        if not 0 <= now - ctx.observed_at <= _RUNTIME_FRESH_SEC:
            return self._failure(ctx, "REMOTE_RUNTIME_STALE", now)
        identity = (ctx.uid, ctx.user, ctx.home, ctx.pid, ctx.process_token,
                    ctx.runtime_role)
        if self._identities.get(key) != identity:
            self._adapters.pop(key, None)
            self._last_good.pop(key, None)
            self._last_good_at.pop(key, None)
            self._identities[key] = identity
        try:
            db_path = zcode_wsl_db_path(ctx.home)
            node_path = zcode_wsl_server_node(ctx.home)
        except ValueError as exc:
            return self._failure(ctx, f"REMOTE_PATH_REJECTED:{exc}", now)

        adapter = self._adapters.get(key)
        if adapter is None:
            payload, error = self._exec_json(ctx, node_path, _SCHEMA_SCRIPT,
                                             [db_path])
            self.schema_probe_count += 1
            if payload is None:
                return self._failure(ctx, error or "REMOTE_SCHEMA_UNAVAILABLE", now)
            caps = self._caps(payload)
            adapter = ZCodeSchemaAdapter(caps)
            if not adapter.compatible():
                return self._failure(ctx, "INCOMPATIBLE_SCHEMA", now)
            self._adapters[key] = adapter

        plan = self._query_plan(adapter, int(now * 1000))
        payload, error = self._exec_json(
            ctx, node_path, _FACTS_SCRIPT,
            [db_path, json.dumps(plan, ensure_ascii=True, separators=(",", ":"))])
        self.facts_query_count += 1
        if payload is None:
            low = (error or "").lower()
            if "no such" in low or "schema" in low or "sqlite_schema" in low:
                self._adapters.pop(key, None)
            return self._failure(ctx, error or "REMOTE_QUERY_UNAVAILABLE", now)
        try:
            facts = self._normalize_facts(payload)
        except (TypeError, ValueError, KeyError) as exc:
            return self._failure(ctx, f"REMOTE_PAYLOAD_INVALID:{exc}", now)
        now = time.time()
        self._last_good[key] = facts
        self._last_good_at[key] = now
        return ZCodeRemoteFactsSnapshot(
            context=ctx, authoritative=True, facts=facts,
            diagnostics=(), observed_at=now)

    def _failure(self, ctx: RemoteRuntimeContext, reason: str,
                 now: float) -> ZCodeRemoteFactsSnapshot:
        self.error_count += 1
        identity = (ctx.uid, ctx.user, ctx.home, ctx.pid, ctx.process_token,
                    ctx.runtime_role)
        same_runtime = self._identities.get(ctx.plane_key) == identity
        return ZCodeRemoteFactsSnapshot(
            context=ctx, authoritative=False,
            facts=self._last_good.get(ctx.plane_key) if same_runtime else None,
            diagnostics=(str(reason)[:200],),
            observed_at=self._last_good_at.get(ctx.plane_key, 0.0)
            if same_runtime else 0.0)

    def _exec_json(self, ctx: RemoteRuntimeContext, node_path: str,
                   script: str, args: list[str]) -> tuple[dict | None, str]:
        # Direct argv only. No shell/sh -c, so distro/user/path values cannot become
        # commands even if upstream metadata is malformed.
        identity = json.dumps({"pid": ctx.pid, "uid": ctx.uid,
                               "home": ctx.home, "token": ctx.process_token},
                              ensure_ascii=True, separators=(",", ":"))
        argv = ["wsl.exe", "-d", ctx.distro, "-u", ctx.user,
                "--exec", node_path, "-e", _IDENTITY_SCRIPT + "\n" + script,
                *args, identity]
        self.spawn_count += 1
        try:
            r = self._runner(
                argv, capture_output=True, timeout=self.timeout_sec,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            self.timeout_count += 1
            return None, "REMOTE_QUERY_TIMEOUT"
        except Exception as exc:
            return None, f"REMOTE_EXEC_FAILED:{type(exc).__name__}"
        rc = int(getattr(r, "returncode", 0) or 0)
        stderr = bytes(getattr(r, "stderr", b"") or b"")[:4096].decode(
            "utf-8", "replace")
        if rc != 0:
            for code in ("REMOTE_USER_CHANGED", "REMOTE_RUNTIME_CHANGED",
                         "REMOTE_DB_PATH_REJECTED", "REMOTE_WAL_SIDECAR_UNAVAILABLE"):
                if code in stderr:
                    return None, code
            if "no such table" in stderr or "no such column" in stderr:
                return None, "REMOTE_SCHEMA_CHANGED"
            return None, f"REMOTE_EXEC_RC_{rc}"
        raw = bytes(getattr(r, "stdout", b"") or b"")
        if len(raw) > _MAX_STDOUT:
            return None, "REMOTE_OUTPUT_TOO_LARGE"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return None, "REMOTE_JSON_INVALID"
        if not isinstance(payload, dict):
            return None, "REMOTE_JSON_NOT_OBJECT"
        return payload, ""

    @staticmethod
    def _caps(payload: dict) -> SchemaCapabilities:
        columns_raw = payload.get("columns") or {}
        columns = {}
        for table in _TABLES:
            vals = columns_raw.get(table)
            if isinstance(vals, list):
                columns[table] = tuple(str(v) for v in vals[:64])
        try:
            uv = int(payload.get("user_version") or 0)
        except (TypeError, ValueError):
            uv = 0
        return SchemaCapabilities(
            user_version=uv,
            tables=frozenset(columns),
            columns=columns,
        )

    @staticmethod
    def _query_plan(adapter: ZCodeSchemaAdapter, now_ms: int) -> list[dict]:
        fresh_ms = now_ms - int(_LIVE_PART_FRESH_SEC * 1000)
        candidates = (
            ("catalog", adapter.catalog_sql(), []),
            ("models", adapter.running_models_sql(), []),
            ("tools", adapter.running_tools_sql(), []),
            ("turns", adapter.latest_turns_sql(), []),
            ("parts", adapter.live_parts_sql(), [fresh_ms]),
            ("activity", adapter.activity_sql(), []),
        )
        plan = []
        for key, sql, params in candidates:
            if sql is None:
                continue
            prefix = sql.lstrip().lower()
            if not prefix.startswith(("select", "with")):
                raise ValueError("non-read-only SQL rejected")
            plan.append({"key": key, "sql": sql, "params": params})
        return plan

    @staticmethod
    def _normalize_facts(payload: dict) -> dict:
        def rows(key: str) -> list[dict]:
            value = payload.get(key, [])
            if not isinstance(value, list) or len(value) > 64:
                raise ValueError(f"{key} rows invalid")
            if not all(isinstance(row, dict) for row in value):
                raise ValueError(f"{key} row is not an object")
            return value

        catalog = rows("catalog")[:32]
        models = [
            (r.get("session_id"), r.get("parent_id"), r.get("query_source"),
             r.get("started_at"), r.get("turn_id"),
             r.get("assistant_message_id")) for r in rows("models")]
        tools = [
            (r.get("session_id"), r.get("parent_id"), r.get("tool_name"),
             r.get("status"), r.get("approval_status"), r.get("started_at"),
             r.get("turn_id"), r.get("tool_call_id"), r.get("assistant_message_id"))
            for r in rows("tools")]
        turns = [
            (r.get("session_id"), r.get("status"), r.get("started_at"),
             r.get("completed_at"), r.get("turn_id"),
             r.get("cancelled_by_user")) for r in rows("turns")]
        parts = [
            (r.get("session_id"), r.get("parent_id"), r.get("time_updated"),
             r.get("part_type"), r.get("tool_name"), r.get("tool_status"),
             r.get("text"), r.get("detail")) for r in rows("parts")[:16]]
        activity = [
            (r.get("session_id"), r.get("parent_id"), r.get("message_id"),
             r.get("time_updated"), r.get("part_type"), r.get("tool_name"),
             r.get("call_id"), r.get("tool_status"), r.get("text"),
             r.get("detail")) for r in rows("activity")[:16]]
        return {"catalog": catalog, "models": models,
                "tools": tools, "turns": turns, "parts": parts,
                "activity": activity}
