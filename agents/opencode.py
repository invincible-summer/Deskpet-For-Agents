"""OpenCode 无插件只读监听 adapter（无插件监听 plan §三/P2）。

上游事实（core/src/session/sql.ts、store.ts 与 schema/src/session-message.ts）：
  * 数据根：``<XDG_DATA_HOME|~/.local/share>/opencode``；DB 名
    ``opencode.db``（或渠道后缀 ``opencode-<channel>.db``）。
  * schema：``session_v2``（id/parent_id/directory/title/time_created/
    time_updated/time_idle/time_suspended/idle_outcome/…）+
    ``session_message``（turn 内持续写入，time_created 建索引）+
    ``session_inbox``（输入 inbox 队列，**不是审批请求**）。
  * ``time_suspended`` 是执行 claim（store.ts：turn 开始置位、终止事件
    提交时清空）——durable turn 证据；崩溃可残留，用有界孤儿 TTL 兜底。
  * 未决审批只存在运行进程内存（permission.ts 的 Map）；官方只读接口：
    GET ``/api/permission/request``、``/api/session/:id/permission``、
    SSE ``/api/event``；``/api/info`` 返回 version/pid/urls。
  * 托管服务注册文件 ``~/.local/state/opencode/service.json`` 携带
    {id, version, url, pid, password}——本模块只用 url/pid/version，
    **绝不读取、保存或使用 password**。

状态语义（plan §六）：
  * SQLite 是会话索引 + durable 生命周期事实：DB 行存在 ≠ WORKING；
    WORKING 只来自 HTTP active 集合、新鲜 session_message 或仍持有的
    执行 claim（晚于 claim 的 time_idle 压住 claim）；否则 UNKNOWN；
  * WAITING 仅来自可信实例的只读 pending 证据（GET），401/403/断链 →
    状态不可用（UNKNOWN），绝不伪造；
  * SQLite 只读合同完全复用 ``agents/sqlite_ro.py``（mode=ro +
    query_only + 40ms busy + 固定 SELECT + 行数上限 + 禁 immutable）；
    WAL 零写入门禁：读连接前后对比 ``-shm`` sidecar，无法证明纯读即
    跳过该数据源并降级（plan §SQLite WAL 零写入补充门槛）。
"""
import json
import ntpath
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import paths
from .base import BaseWatcher, classify_phase
from .desktop import (
    DesktopSessionSource,
    DesktopSourceSnapshot,
    SessionClaimKey,
    bounded_diagnostics,
    claims_cover,
)
from .models import (
    AgentInstance,
    AgentKind,
    AgentSurface,
    Confidence,
    EvidenceSource,
    Observation,
    Phase,
    Status,
)
from .sqlite_ro import (
    DbFingerprint,
    ReadOnlySqlite,
    SchemaCapabilities,
    fingerprints_differ,
    stat_fingerprint,
)
from .summarize import summarize_text, tool_detail

_MAX_SESSIONS = 8
_CATALOG_LIMIT = 32
_SAFETY_REFRESH_SEC = 7.5
_FAILURE_RETRY_SEC = 1.0
_ACTIVITY_WINDOW_SEC = 180.0
_IDLE_LEASE_SEC = 600.0
_HTTP_SNAPSHOT_TTL_SEC = 10.0
_HTTP_AUTH_BACKOFF_SEC = 60.0
_HTTP_ERROR_BACKOFF_SEC = 5.0
_HTTP_TIMEOUT_SEC = 1.0
# 上游 store.ts（anomalyco/opencode dev）：session_v2.time_suspended 是
# turn 执行 claim——执行开始置位、终止事件提交时清空；session_message
# 行在 turn 内持续写入。两者给出不依赖 HTTP active 集合的稳定 WORKING
# 证据（与 ZCode live part / DSH open_turn 同一 durable-turn 合同）。
_LIVE_MESSAGE_FRESH_SEC = 90.0
_CLAIM_ORPHAN_SEC = 600.0

_REQUIRED_SESSION_COLUMNS = ("id", "directory", "time_created", "time_updated")
_OPTIONAL_SESSION_COLUMNS = ("title", "parent_id", "time_idle",
                             "time_suspended", "time_archived",
                             "idle_outcome")

# The same bounded projection is used by local SQLite and the WSL reader.
# Select only the latest displayable assistant block, never tool output/native
# metadata, and keep its leading text as the block grows during streaming.
_MESSAGE_JSON = "CASE WHEN json_valid(m.data) THEN m.data ELSE '{}' END"
_BLOCK_JSON = "CASE WHEN p.type = 'object' THEN p.value ELSE '{}' END"


def _input_sql(keys: tuple[str, ...]) -> str:
    """Fixed schema fields only; empty/scalar-type errors do not hide a fallback."""
    return "COALESCE(" + ", ".join(
        f"NULLIF(CASE WHEN json_type({_BLOCK_JSON}, '$.state.input.{key}') = 'text' "
        f"THEN json_extract({_BLOCK_JSON}, '$.state.input.{key}') END, '')"
        for key in keys) + ")"


_BLOCK_COMMAND = _input_sql(("command", "cmd"))
_BLOCK_QUERY = _input_sql(("query", "pattern", "q", "glob"))
_BLOCK_PATH = _input_sql(("filePath", "file_path", "path", "url"))
# Retain the known field context: a shell group beginning with '{' is a command,
# not a damaged JSON envelope. At most two 256-character values cross the reader.
_BLOCK_DETAIL = (
    f"CASE WHEN {_BLOCK_COMMAND} IS NOT NULL THEN "
    f"json_object('command', substr({_BLOCK_COMMAND}, 1, 256)) ELSE "
    f"json_object('query', substr({_BLOCK_QUERY}, 1, 256), "
    f"'path', substr({_BLOCK_PATH}, 1, 256)) END")
_MESSAGE_ACTIVITY_SQL = (
    f"CASE WHEN json_extract({_MESSAGE_JSON}, '$.type') = 'shell' THEN "
    f"json_object('type', 'tool', 'name', 'shell', 'status', "
    f"json_extract({_MESSAGE_JSON}, '$.status'), 'detail', "
    f"json_object('command', substr(json_extract({_MESSAGE_JSON}, '$.command'), 1, 256))) ELSE "
    "(SELECT json_object("
    f"'type', json_extract({_BLOCK_JSON}, '$.type'), "
    f"'text', CASE WHEN json_extract({_BLOCK_JSON}, '$.type') IN ('text', 'reasoning') "
    f"THEN substr(json_extract({_BLOCK_JSON}, '$.text'), 1, 512) END, "
    f"'name', substr(json_extract({_BLOCK_JSON}, '$.name'), 1, 96), "
    f"'status', json_extract({_BLOCK_JSON}, '$.state.status'), "
    f"'detail', {_BLOCK_DETAIL}) "
    f"FROM json_each({_MESSAGE_JSON}, '$.content') p "
    f"WHERE p.type = 'object' AND CAST(p.key AS INTEGER) < 64 "
    f"AND json_extract({_BLOCK_JSON}, '$.type') IN ('tool', 'text', 'reasoning') "
    f"ORDER BY CASE WHEN json_extract({_BLOCK_JSON}, '$.type') = 'tool' "
    f"AND json_extract({_BLOCK_JSON}, '$.state.status') IN ('running', 'streaming') "
    "THEN 1 ELSE 0 END DESC, CAST(p.key AS INTEGER) DESC LIMIT 1) END")
_MESSAGE_FIELDS_SQL = (
    f"COALESCE(json_extract({_MESSAGE_JSON}, '$.type'), "
    f"json_extract({_MESSAGE_JSON}, '$.role')) AS role, "
    f"substr(json_extract({_MESSAGE_JSON}, '$.text'), 1, 512) AS text, "
    f"{_MESSAGE_ACTIVITY_SQL} AS activity")


@dataclass(frozen=True)
class OpenCodeFactsSnapshot:
    """一个数据面（windows / wsl:distro|uid）一轮只读事实。"""
    source: str                       # "windows" 或 "wsl:<distro>"
    plane_key: str
    authoritative: bool
    catalog: tuple = ()               # (id, directory, title, parent_id, time_updated_ms, ...)
    messages: tuple = ()              # (session_id, updated_ms, role, text, activity)
    diagnostics: tuple[str, ...] = ()
    observed_at: float = 0.0

    def latest_message(self, sid: str) -> tuple | None:
        """该 session 最新一条消息事实（None = 无新鲜消息证据）。"""
        best = None
        for row in self.messages:
            if str(row[0]) == sid and (best is None or row[1] > best[1]):
                best = row
        return best

    def sessions_by_directory(self) -> dict[str, tuple]:
        """normalized directory → 最新 root session 行（子会话归并父任务）。"""
        out: dict[str, tuple] = {}
        for row in self.catalog:
            sid, directory = str(row[0] or ""), str(row[1] or "")
            if not sid or not directory:
                continue
            if str(row[3] or ""):
                continue   # parent_id 非空 = 子会话，不单独绑定目录
            key = normalize_opencode_directory(directory)
            prev = out.get(key)
            if prev is None or _row_updated(row) > _row_updated(prev):
                out[key] = row
        if out:
            return out
        # 无 parent_id 列的旧 schema：退化为最新行
        for row in self.catalog:
            sid, directory = str(row[0] or ""), str(row[1] or "")
            if not sid or not directory:
                continue
            key = normalize_opencode_directory(directory)
            prev = out.get(key)
            if prev is None or _row_updated(row) > _row_updated(prev):
                out[key] = row
        return out

    def session_ids(self) -> set[str]:
        return {str(row[0]) for row in self.catalog if row[0]}


def _row_updated(row) -> int:
    try:
        return int(row[4] or 0)
    except (TypeError, ValueError, IndexError):
        return 0


def _row_lifecycle(row) -> tuple[int, int, str]:
    """Return optional idle/archive facts without changing row[4] identity."""
    def _int_at(index: int) -> int:
        try:
            return int(row[index] or 0)
        except (TypeError, ValueError, IndexError):
            return 0
    try:
        outcome = str(row[7] or "")[:40]
    except IndexError:
        outcome = ""
    return _int_at(5), _int_at(6), outcome


def normalize_opencode_directory(directory: str) -> str:
    """DB 里的 directory（Windows 上游存 ``C:/x`` 正斜杠形态）→ 规范键。"""
    text = str(directory or "").strip()
    if not text:
        return ""
    if re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\"):
        # Keep Windows semantics even when fixtures/remote facts are normalized
        # by a POSIX DeskPet test process.
        return ntpath.normcase(ntpath.normpath(text.replace("/", "\\")))
    normalized = os.path.normcase(os.path.normpath(text))
    return normalized.replace("/", os.sep)


class OpenCodeSchemaAdapter:
    """session_v2 capability + 固定白名单 SELECT builder。"""

    def __init__(self, caps: SchemaCapabilities):
        self.caps = caps
        self.columns = frozenset(caps.columns_of("session_v2"))

    def compatible(self) -> bool:
        return (self.caps.has_table("session_v2")
                and self.caps.has_columns("session_v2",
                                          _REQUIRED_SESSION_COLUMNS))

    def catalog_sql(self) -> str | None:
        if not self.compatible():
            return None
        cols = ["id", "directory", "time_updated"]
        for opt in _OPTIONAL_SESSION_COLUMNS:
            if opt in self.columns:
                cols.append(opt)
        where = " WHERE (time_archived IS NULL OR time_archived = 0)" \
            if "time_archived" in self.columns else ""
        return (f"SELECT {', '.join(cols)} FROM session_v2{where} "
                f"ORDER BY time_updated DESC LIMIT {_CATALOG_LIMIT}")

    def messages_sql(self) -> str | None:
        """新鲜 session_message 行（流式活跃证据；上游按 time_created 建索引）。"""
        if not (self.caps.has_table("session_message")
                and self.caps.has_columns(
                    "session_message", ("session_id", "time_created",
                                        "time_updated"))):
            return None
        if self.caps.has_columns("session_message", ("data",)):
            fields = _MESSAGE_FIELDS_SQL
        else:
            fields = "NULL AS role, NULL AS text, NULL AS activity"
        return (f"SELECT session_id, time_updated, {fields} "
                "FROM session_message m "
                "WHERE time_created >= ? "
                "ORDER BY time_created DESC LIMIT 16")


def _normalize_catalog_rows(rows, columns: tuple[str, ...]) -> tuple:
    """行 → (id, directory, title, parent_id, time_updated_ms) 固定形态。"""
    out = []
    for row in rows or ():
        try:
            if hasattr(row, "keys"):
                values = {k: row[k] for k in row.keys()}
            else:
                values = dict(zip(columns, row))
        except Exception:
            continue
        sid = str(values.get("id") or "")
        directory = str(values.get("directory") or "")
        if not sid or not directory:
            continue
        try:
            updated = int(values.get("time_updated") or 0)
        except (TypeError, ValueError):
            updated = 0
        try:
            idle = int(values.get("time_idle") or 0)
        except (TypeError, ValueError):
            idle = 0
        try:
            archived = int(values.get("time_archived") or 0)
        except (TypeError, ValueError):
            archived = 0
        try:
            suspended = int(values.get("time_suspended") or 0)
        except (TypeError, ValueError):
            suspended = 0
        out.append((
            sid,
            directory,
            str(values.get("title") or "")[:120],
            str(values.get("parent_id") or ""),
            updated,
            idle,
            archived,
            str(values.get("idle_outcome") or "")[:40],
            suspended,
        ))
    return tuple(out[:_CATALOG_LIMIT])


def _normalize_message_rows(rows) -> tuple:
    """Keep one bounded message projection per session, with old-row compatibility."""
    best: dict[str, list] = {}
    for row in rows or ():
        try:
            sid = str(row[0] or "")
            updated = int(row[1] or 0)
        except (TypeError, ValueError, IndexError):
            continue
        if not sid or not updated:
            continue
        try:
            role = str(row[2] or "")[:24]
        except IndexError:
            role = ""
        try:
            text = str(row[3] or "")[:512]
        except IndexError:
            text = ""
        try:
            activity = str(row[4] or "")[:4096]
        except IndexError:
            activity = ""
        cur = best.get(sid)
        if cur is None or updated > cur[1]:
            best[sid] = [sid, updated, role, text, activity]
    return tuple(tuple(v) for v in best.values())


def _message_activity(message: tuple | None) -> tuple[Phase, str]:
    """Interpret only observed assistant content; a user prompt is never activity."""
    if message is None or str(message[2] or "") not in {"assistant", "shell"}:
        return Phase.THINKING, ""
    try:
        block = json.loads(message[4]) if len(message) > 4 and message[4] else None
    except (TypeError, ValueError):
        block = None
    if isinstance(block, dict):
        kind = block.get("type")
        if kind == "tool":
            if block.get("status") not in {"streaming", "running"}:
                return Phase.THINKING, ""
            detail = tool_detail(block.get("detail"), 160)
            phase = classify_phase(str(block.get("name") or ""), detail)
            return (phase if phase is not Phase.NONE else Phase.EXECUTING), detail
        if kind in {"text", "reasoning"}:
            phase = Phase.THINKING if kind == "reasoning" else Phase.ANSWERING
            return phase, summarize_text(block.get("text"), 160)
    # Historical data fixtures/clients used a plain assistant text field.
    text = summarize_text(message[3], 160)
    return (Phase.ANSWERING if text else Phase.THINKING), text


class OpenCodeLocalReader:
    """Windows 本地 opencode.db 只读事实读取（含 WAL 零写入门禁）。

    仅由 Monitor 后台线程调用（与 ZCodeDesktopSource 相同的 bounded
    busy-timeout 合同）；WAL sidecar 门禁失败会停用该 DB 直到文件形态
    变化（plan：无法证明零写入 → 跳过数据源并降级）。
    """

    def __init__(self, cfg: dict | None = None, data_root: str | None = None):
        self._cfg = dict(cfg or {})
        self._data_root = data_root or paths.opencode_data_root()
        self._ro: ReadOnlySqlite | None = None
        self._caps: SchemaCapabilities | None = None
        self._adapter: OpenCodeSchemaAdapter | None = None
        self._fingerprint = DbFingerprint(exists=False)
        self._last_sql = 0.0
        self._retry_at = 0.0
        self._zero_write_blocked = False
        self._last_snapshot: OpenCodeFactsSnapshot | None = None
        self.query_count = 0
        self.busy_count = 0

    def close(self) -> None:
        if self._ro is not None:
            self._ro.close()
            self._ro = None
        self._caps = None
        self._adapter = None
        self._last_snapshot = None

    def _db_candidates(self) -> list[str]:
        return paths.opencode_db_candidates(self._data_root)

    def _sidecars(self, db: str) -> set[str]:
        out = set()
        for suffix in ("-shm", "-wal"):
            try:
                os.stat(db + suffix)
                out.add(suffix)
            except OSError:
                continue
        return out

    def facts(self, now: float) -> OpenCodeFactsSnapshot | None:
        """带指纹节流的事实读取；失败/门禁触发返回 last-good 或 None。"""
        candidates = self._db_candidates()
        db = candidates[0] if candidates else None
        if db is None:
            self.close()
            return None
        fingerprint = stat_fingerprint(db)
        changed = fingerprints_differ(fingerprint, self._fingerprint)
        refresh_due = now - self._last_sql >= _SAFETY_REFRESH_SEC
        retry_due = bool(self._retry_at) and now >= self._retry_at
        if not (changed or refresh_due or retry_due):
            return self._last_snapshot
        snapshot = self._query(db, now)
        self._fingerprint = fingerprint
        if snapshot is None:
            self._retry_at = now + _FAILURE_RETRY_SEC
            return self._last_snapshot
        self._last_sql = now
        self._retry_at = 0.0
        self._last_snapshot = snapshot
        return snapshot

    def _query(self, db: str, now: float) -> OpenCodeFactsSnapshot | None:
        if self._zero_write_blocked:
            # 零写入门禁：一旦发现读连接制造 sidecar，该 DB 停用；
            # 只有文件集合变化（如 Agent 重启清理 sidecar）才重试。
            if "-shm" not in self._sidecars(db):
                self._zero_write_blocked = False
                if self._ro is not None:
                    self._ro.close()
                    self._ro = None
                self._caps = None
                self._adapter = None
            else:
                return None
        before = self._sidecars(db)
        if self._ro is None or self._ro.path != db:
            if self._ro is not None:
                self._ro.close()
            self._ro = ReadOnlySqlite(db)
            self._caps = None
            self._adapter = None
        if not self._ro.open():
            return OpenCodeFactsSnapshot(
                source="windows", plane_key="windows", authoritative=False,
                diagnostics=(self._ro.last_error or "open failed",),
                observed_at=now)
        try:
            if self._caps is None:
                caps = self._ro.read_schema(["session_v2", "session_message"])
                if caps is None:
                    return OpenCodeFactsSnapshot(
                        source="windows", plane_key="windows",
                        authoritative=False,
                        diagnostics=(self._ro.last_error
                                     or "schema probe failed",),
                        observed_at=now)
                self._caps = caps
                self._adapter = OpenCodeSchemaAdapter(caps)
            adapter = self._adapter
            if adapter is None or not adapter.compatible():
                return OpenCodeFactsSnapshot(
                    source="windows", plane_key="windows",
                    authoritative=False,
                    diagnostics=("INCOMPATIBLE_SCHEMA",), observed_at=now)
            sql = adapter.catalog_sql()
            rows = self._ro.query(sql, max_rows=_CATALOG_LIMIT)
            self.query_count = self._ro.query_count
            self.busy_count = self._ro.busy_count
            if rows is None:
                return OpenCodeFactsSnapshot(
                    source="windows", plane_key="windows",
                    authoritative=False,
                    diagnostics=(self._ro.last_error or "query failed",),
                    observed_at=now)
            columns = _catalog_columns(adapter)
            catalog = _normalize_catalog_rows(rows, columns)
            messages = ()
            msg_sql = adapter.messages_sql()
            if msg_sql is not None:
                fresh_ms = int(now * 1000) - int(
                    _LIVE_MESSAGE_FRESH_SEC * 1000)
                msg_rows = self._ro.query(msg_sql, (fresh_ms,), max_rows=16)
                if msg_rows is None:
                    return OpenCodeFactsSnapshot(
                        source="windows", plane_key="windows",
                        authoritative=False,
                        diagnostics=(self._ro.last_error
                                     or "message query failed",),
                        observed_at=now)
                messages = _normalize_message_rows(msg_rows)
        finally:
            # 短连接：读完立即关闭，缩小任何 sidecar 生命周期
            self._ro.close()
            self._ro = None
            self._caps = None
            self._adapter = None
        after = self._sidecars(db)
        if after - before:
            # 读连接制造了新 sidecar（-shm/-wal）：违反零 Agent-owned 写入
            self._zero_write_blocked = True
            return OpenCodeFactsSnapshot(
                source="windows", plane_key="windows", authoritative=False,
                diagnostics=("WAL_SIDECAR_ZERO_WRITE_VIOLATION",),
                observed_at=now)
        return OpenCodeFactsSnapshot(
            source="windows", plane_key="windows", authoritative=True,
            catalog=catalog, messages=messages, diagnostics=(), observed_at=now)


def _catalog_columns(adapter: OpenCodeSchemaAdapter) -> tuple[str, ...]:
    cols = ["id", "directory", "time_updated"]
    for opt in _OPTIONAL_SESSION_COLUMNS:
        if opt in adapter.columns:
            cols.append(opt)
    return tuple(cols)


# WSL 读取：发行版内 python3 只读脚本（只由 ProcessProbeWorker 执行）。
# 直接 argv、无 shell；stdout 64KB 上限；python3 缺失即降级（plan：若
# WSL 无读取器则降级，不安装 Python/SQLite 到 WSL）。

_WSL_SCRIPT = r"""
import json, os, sqlite3, sys, time, urllib.parse
db = sys.argv[1]
out = {"error": "", "sidecar_created": False, "rows": [], "messages": []}
def sidecar_exists():
    return os.path.exists(db + "-shm")
before = sidecar_exists()
uri = "file:" + urllib.parse.quote(db) + "?mode=ro"
con = None
try:
    con = sqlite3.connect(uri, uri=True, timeout=0.04)
    con.execute("PRAGMA query_only=ON")
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "session_v2" not in tables:
        out["error"] = "NO_SESSION_V2"
    else:
        cols = [r[1] for r in con.execute("PRAGMA table_info(session_v2)")]
        need = {"id", "directory", "time_created", "time_updated"}
        if not need <= set(cols):
            out["error"] = "SESSION_V2_COLUMNS"
        else:
            sel = ["id", "directory"] + [
                c for c in ("title", "parent_id", "time_idle",
                            "time_suspended", "idle_outcome") if c in cols]
            sel.append("time_updated")
            sql = ("SELECT " + ", ".join(sel) +
                   " FROM session_v2 ORDER BY time_updated DESC LIMIT 32")
            def idx(name):
                return sel.index(name) if name in sel else -1
            at = {name: idx(name) for name in
                  ("title", "parent_id", "time_idle", "time_suspended",
                   "idle_outcome", "time_updated")}
            for row in con.execute(sql):
                def text(name):
                    i = at[name]
                    if i < 0 or i >= len(row) or row[i] is None:
                        return ""
                    return str(row[i])
                def num(name):
                    try:
                        return int(text(name) or 0)
                    except ValueError:
                        return 0
                out["rows"].append([
                    text("id"), text("directory"),
                    text("title")[:120], text("parent_id"),
                    num("time_updated"), num("time_idle"),
                    0, text("idle_outcome")[:40], num("time_suspended")])
        if "session_message" in tables and not out["error"]:
            mcols = {r[1] for r in con.execute(
                "PRAGMA table_info(session_message)")}
            if {"session_id", "time_created", "time_updated"} <= mcols:
                if "data" in mcols:
                    extras = ", " + __MESSAGE_FIELDS_SQL__
                else:
                    extras = ", NULL, NULL, NULL"
                msql = ("SELECT session_id, time_updated" + extras +
                        " FROM session_message m WHERE time_created >= ?"
                        " ORDER BY time_created DESC LIMIT 16")
                fresh = int(time.time() * 1000) - 90000
                best = {}
                for row in con.execute(msql, (fresh,)):
                    sid, updated = str(row[0] or ""), int(row[1] or 0)
                    if not sid or not updated:
                        continue
                    cur = best.get(sid)
                    if cur is None or updated > cur[1]:
                        best[sid] = [sid, updated,
                                     str(row[2] or "")[:24],
                                     str(row[3] or "")[:512],
                                     str(row[4] or "")[:4096]]
                out["messages"] = list(best.values())[:16]
except Exception as exc:
    out["error"] = type(exc).__name__
finally:
    if con is not None:
        con.close()
out["sidecar_created"] = (not before) and sidecar_exists()
sys.stdout.write(json.dumps(out, ensure_ascii=True))
""".strip().replace("__MESSAGE_FIELDS_SQL__", repr(_MESSAGE_FIELDS_SQL))


class OpenCodeWslReader:
    """bounded WSL 数据面读取器；无常驻线程/服务（仅 subprocess）。"""

    def __init__(self, runner=None, timeout_sec: float = 1.5):
        self._runner = runner or __import__("subprocess").run
        self.timeout_sec = min(2.0, max(0.2, float(timeout_sec)))
        self._last_good: dict[str, OpenCodeFactsSnapshot] = {}
        self.spawn_count = 0
        self.timeout_count = 0
        self.error_count = 0

    def drop_except(self, active: set[str]) -> None:
        for key in list(self._last_good):
            if key not in active:
                self._last_good.pop(key, None)

    def close(self) -> None:
        self._last_good.clear()

    def stale(self, snap: OpenCodeFactsSnapshot, reason: str,
              now: float) -> OpenCodeFactsSnapshot:
        diag = tuple((list(snap.diagnostics) + [reason])[-6:])
        import dataclasses as _dc
        return _dc.replace(snap, authoritative=False, diagnostics=diag,
                           observed_at=now)

    def read(self, distro: str, user: str, home: str,
             db_override: str = "") -> OpenCodeFactsSnapshot:
        now = time.time()
        plane = f"wsl:{distro}|uid-home:{home}"
        if not distro or not user or not home:
            return self._failure(plane, "REMOTE_IDENTITY_INCOMPLETE", now)
        try:
            if db_override and db_override.startswith("/"):
                db_path = db_override
            else:
                db_path = paths.opencode_wsl_data_root(home)
                db_path = os.path.join(db_path, db_override or "opencode.db")
        except ValueError as exc:
            return self._failure(plane, f"REMOTE_PATH_REJECTED:{exc}", now)
        argv = ["wsl.exe", "-d", distro, "-u", user,
                "--exec", "python3", "-c", _WSL_SCRIPT, db_path]
        self.spawn_count += 1
        try:
            r = self._runner(
                argv, capture_output=True, timeout=self.timeout_sec,
                creationflags=getattr(
                    __import__("subprocess"), "CREATE_NO_WINDOW", 0))
        except __import__("subprocess").TimeoutExpired:
            self.timeout_count += 1
            return self._failure(plane, "REMOTE_QUERY_TIMEOUT", now)
        except Exception as exc:
            self.error_count += 1
            return self._failure(
                plane, f"REMOTE_EXEC_FAILED:{type(exc).__name__}", now)
        rc = int(getattr(r, "returncode", 0) or 0)
        if rc != 0:
            self.error_count += 1
            return self._failure(plane, f"REMOTE_READER_RC_{rc}", now)
        raw = bytes(getattr(r, "stdout", b"") or b"")
        if len(raw) > 64 * 1024:
            return self._failure(plane, "REMOTE_OUTPUT_TOO_LARGE", now)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._failure(plane, "REMOTE_JSON_INVALID", now)
        if not isinstance(payload, dict):
            return self._failure(plane, "REMOTE_JSON_NOT_OBJECT", now)
        if payload.get("sidecar_created"):
            return self._failure(plane, "WAL_SIDECAR_ZERO_WRITE_VIOLATION", now)
        error = str(payload.get("error") or "")
        if error:
            return self._failure(plane, f"REMOTE_{error}", now)
        rows = payload.get("rows")
        if not isinstance(rows, list) or len(rows) > _CATALOG_LIMIT:
            return self._failure(plane, "REMOTE_ROWS_INVALID", now)
        catalog = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 3:
                continue
            try:
                catalog.append((
                    str(row[0]), str(row[1]),
                    str(row[2] if len(row) > 2 else "")[:120],
                    str(row[3] if len(row) > 3 else ""),
                    int(row[4] if len(row) > 4 else 0) or 0,
                    int(row[5] if len(row) > 5 else 0) or 0,
                    int(row[6] if len(row) > 6 else 0) or 0,
                    str(row[7] if len(row) > 7 else "")[:40],
                    int(row[8] if len(row) > 8 else 0) or 0,
                ))
            except (TypeError, ValueError):
                continue
        messages = _normalize_message_rows(payload.get("messages") or ())
        snap = OpenCodeFactsSnapshot(
            source=f"wsl:{distro}", plane_key=plane, authoritative=True,
            catalog=tuple(catalog[:_CATALOG_LIMIT]), messages=messages,
            diagnostics=(), observed_at=now)
        self._last_good[plane] = snap
        return snap

    def _failure(self, plane: str, reason: str,
                 now: float) -> OpenCodeFactsSnapshot:
        self.error_count += 1
        snap = self._last_good.get(plane)
        if snap is not None:
            import dataclasses as _dc
            snap = _dc.replace(snap, authoritative=False,
                               diagnostics=(reason[:200],), observed_at=now)
            return snap
        return OpenCodeFactsSnapshot(
            source=plane.split("|")[0], plane_key=plane, authoritative=False,
            diagnostics=(reason[:200],), observed_at=now)


@dataclass(frozen=True)
class OpenCodeHttpSnapshot:
    """可信实例只读 pending 事实（GET；绝不使用密码）。"""
    available: bool = False
    authoritative: bool = False
    reason: str = ""
    pending: dict = field(default_factory=dict)   # sessionID -> 有界描述
    active: frozenset = frozenset()                # V2 /api/session/active ids
    server_pid: int = 0
    url: str = ""
    observed_at: float = 0.0


class OpenCodeHttpProbe:
    """service.json 归属映射 + 只读 GET；仅由 ProcessProbeWorker 调用。

    严格门禁（plan §一/§二）：只有注册文件的 pid 与本轮 census 中存活的
    OpenCode 进程吻合，且 ``/api/info`` 自报 pid 一致时才读 pending；
    绝不读取/使用注册文件中的 password，绝不扫端口、不起 serve。
    401/403 → AUTH（合法不可用），不重试风暴。
    """

    def __init__(self, fetch=None, state_root: str | None = None):
        self._fetch = fetch or self._default_fetch
        self._state_root = state_root or paths.opencode_state_root()
        self._last_attempt = 0.0
        self._backoff_until = 0.0
        self._last: OpenCodeHttpSnapshot = OpenCodeHttpSnapshot()
        self.attempt_count = 0

    def snapshot(self) -> OpenCodeHttpSnapshot:
        return self._last

    def reset(self) -> None:
        """kind 关闭时清空全部缓存事实（O(1)、无 IO）。"""
        self._backoff_until = 0.0
        self._last = OpenCodeHttpSnapshot()

    def refresh(self, live_pids: set[int], now: float) -> OpenCodeHttpSnapshot:
        if now < self._backoff_until:
            return self._last
        reg = self._registration()
        if reg is None:
            self._last = OpenCodeHttpSnapshot(
                reason="NO_REGISTRATION", observed_at=now)
            return self._last
        url, pid = reg
        if pid not in live_pids:
            self._last = OpenCodeHttpSnapshot(
                reason="REGISTRATION_PID_NOT_LIVE", url=url, server_pid=pid,
                observed_at=now)
            return self._last
        self._last_attempt = now
        self.attempt_count += 1
        info = self._get_json(url, "/api/info", now)
        if info is None:
            return self._last
        if not isinstance(info, dict) or "pid" not in info:
            self._last = OpenCodeHttpSnapshot(
                reason="INFO_INVALID", url=url, server_pid=pid,
                observed_at=now)
            return self._last
        try:
            info_pid = int(info.get("pid"))
        except (TypeError, ValueError):
            info_pid = -1
        if info_pid != pid:
            self._last = OpenCodeHttpSnapshot(
                reason="INFO_PID_MISMATCH", url=url, server_pid=pid,
                observed_at=now)
            return self._last
        # V2 exposes active execution sessions separately from the SQLite
        # catalog.  The endpoint is optional: 404/older servers simply yield
        # an empty set, while a trusted 200 response prevents stale
        # time_updated rows from being treated as idle.
        active_raw = self._get_json(url, "/api/session/active", now)
        active_ids: set[str] = set()
        active_items = (active_raw.get("data") if isinstance(active_raw, dict)
                        else active_raw)
        if isinstance(active_raw, dict) and not isinstance(active_items, list):
            active_items = active_raw.get("sessions")
        if isinstance(active_items, list):
            for item in active_items[:64]:
                if isinstance(item, dict):
                    sid = str(item.get("sessionID") or item.get("sessionId")
                              or item.get("id") or "")
                else:
                    sid = str(item or "")
                if sid:
                    active_ids.add(sid)
        pending = self._get_json(url, "/api/permission/request", now)
        if pending is None:
            if self._last.reason not in ("AUTH_REQUIRED", "FORBIDDEN"):
                self._last = OpenCodeHttpSnapshot(
                    reason="PENDING_UNAVAILABLE", url=url, server_pid=pid,
                    active=frozenset(active_ids), observed_at=now)
            return self._last
        data = pending.get("data") if isinstance(pending, dict) else None
        items = data if isinstance(data, list) else (
            pending if isinstance(pending, list) else [])
        out: dict[str, str] = {}
        for item in items[:16]:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("sessionID") or "")
            if not sid:
                continue
            action = str(item.get("action") or "")[:80]
            out[sid] = action or "等待批复"
        self._last = OpenCodeHttpSnapshot(
            available=True, authoritative=True, pending=out,
            active=frozenset(active_ids), server_pid=pid, url=url,
            observed_at=now)
        return self._last

    # ------------------------------------------------------------ 内部
    def _registration(self) -> tuple[str, int] | None:
        for path in paths.opencode_service_files(self._state_root):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    obj = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            url = str(obj.get("url") or "")
            pid = obj.get("pid")
            if not (isinstance(pid, int) and pid > 0):
                continue
            if not url.startswith("http://127.0.0.1:"):
                continue   # 只认 loopback（plan：不探测非本机地址）
            # password 字段在此被显式丢弃：绝不进入内存快照
            return url, int(pid)
        return None

    def _get_json(self, base: str, path: str, now: float):
        try:
            status, body = self._fetch(base + path)
        except Exception:
            self._backoff_until = now + _HTTP_ERROR_BACKOFF_SEC
            self._last = OpenCodeHttpSnapshot(
                reason="NETWORK_UNAVAILABLE", url=base, observed_at=now)
            return None
        if status == 401:
            # 服务器总是要求认证（上游总是设置随机密码）：合法不可用，
            # 绝不读取/猜测密码；长退避避免请求风暴
            self._backoff_until = now + _HTTP_AUTH_BACKOFF_SEC
            self._last = OpenCodeHttpSnapshot(
                reason="AUTH_REQUIRED", url=base, observed_at=now)
            return None
        if status == 403:
            self._backoff_until = now + _HTTP_AUTH_BACKOFF_SEC
            self._last = OpenCodeHttpSnapshot(
                reason="FORBIDDEN", url=base, observed_at=now)
            return None
        if status != 200:
            return None
        try:
            return json.loads(body)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _default_fetch(url: str) -> tuple[int, object]:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as r:
                return int(r.status), r.read(64 * 1024)
        except urllib.error.HTTPError as exc:
            return int(exc.code), b""


# ---------------------------------------------------------- 共享 runtime

class OpenCodeRuntime:
    """Watcher 与 DesktopSource 共享的数据面编排。

    Windows：本地 SQLite（Monitor 线程，bounded busy timeout）；
    WSL：只消费 ProcessProbeWorker 产出的缓存快照（本对象绝不 wsl.exe）；
    pending：只消费 worker 的 OpenCodeHttpSnapshot。
    """

    def __init__(self, cfg: dict | None = None,
                 local_reader: OpenCodeLocalReader | None = None,
                 remote_provider=None, http_provider=None):
        self._cfg = dict(cfg or {})
        self.local = local_reader or OpenCodeLocalReader(self._cfg)
        self.remote_provider = remote_provider
        self.http_provider = http_provider

    def windows_facts(self, now: float) -> OpenCodeFactsSnapshot | None:
        return self.local.facts(now)

    def remote_facts(self) -> dict[str, OpenCodeFactsSnapshot]:
        if self.remote_provider is None:
            return {}
        try:
            return dict(self.remote_provider() or {})
        except Exception:
            return {}

    def http_snapshot(self) -> OpenCodeHttpSnapshot:
        if self.http_provider is None:
            return OpenCodeHttpSnapshot(reason="NO_PROVIDER")
        try:
            return self.http_provider()
        except Exception:
            return OpenCodeHttpSnapshot(reason="PROVIDER_FAILED")

    def close(self) -> None:
        self.local.close()

    def _http_pending(self, now: float) -> tuple[dict[str, str], bool]:
        snap = self.http_snapshot()
        fresh = (snap.available and snap.authoritative
                 and now - snap.observed_at <= _HTTP_SNAPSHOT_TTL_SEC)
        if not fresh:
            return {}, False
        return snap.pending, True

    def _http_active(self, now: float) -> tuple[frozenset, bool]:
        snap = self.http_snapshot()
        fresh = (snap.available and snap.authoritative
                 and now - snap.observed_at <= _HTTP_SNAPSHOT_TTL_SEC)
        if not fresh:
            return frozenset(), False
        return snap.active, True

    def observation_for_session(self, session_row, now: float,
                                want_title: bool = True,
                                message: tuple | None = None) -> Observation:
        """session 行 → 有界元数据观察。

        状态阶梯（上游 store.ts/sql.ts 对齐）：可信 pending → WAITING；
        HTTP active / 新鲜 session_message → WORKING；仍持有的执行 claim
        （time_suspended）→ WORKING（有界孤儿 TTL）；idle_outcome →
        DONE/ERROR 展示窗口；否则 UNKNOWN。DB 行存在本身不是 WORKING。
        """
        _sid = session_row[0]
        directory = session_row[1]
        title = session_row[2]
        obs = Observation(
            source=EvidenceSource.SESSION, timestamp=now,
            status=Status.UNKNOWN, confidence=Confidence.UNKNOWN,
            session_bound=True)
        obs.cwd = str(directory or "")
        if want_title and title:
            obs.title = str(title)
            obs.goal = str(title)
        pending, pending_ok = self._http_pending(now)
        active, active_ok = self._http_active(now)
        sid = str(session_row[0])
        idle_ms, _archived_ms, outcome = _row_lifecycle(session_row)
        try:
            claim_ms = int(session_row[8] or 0)
        except (TypeError, ValueError, IndexError):
            claim_ms = 0
        msg_ms = int(message[1] or 0) if message else 0
        now_ms = int(now * 1000)
        msg_fresh = bool(msg_ms) and 0 <= now_ms - msg_ms <= int(
            _LIVE_MESSAGE_FRESH_SEC * 1000)
        # time_idle 是 turn 结束事实：晚于 claim / 最新消息的 idle 压住
        # 活跃证据（崩溃残留的 claim 不能越过其后的 idle）。
        if idle_ms and idle_ms >= max(claim_ms, msg_ms):
            claim_ms = 0
            msg_fresh = False
        activity_phase, activity = (_message_activity(message) if msg_fresh
                                    else (Phase.THINKING, ""))
        if pending_ok and sid in pending:
            # 可信实例的只读 pending 证据：唯一允许 WAITING 的来源
            obs.status = Status.WAITING
            obs.phase = Phase.APPROVAL
            obs.confidence = Confidence.EXACT
            obs.turn_active = True
            obs.expires_at = 0.0
            obs.waiting_detail = pending[sid][:160]
        elif active_ok and sid in active:
            obs.status = Status.WORKING
            obs.phase = activity_phase
            obs.turn_active = True
            obs.confidence = Confidence.HIGH
            obs.summary = activity
        elif msg_fresh:
            obs.status = Status.WORKING
            obs.phase = activity_phase
            obs.turn_active = True
            obs.confidence = Confidence.HIGH
            obs.summary = activity
        elif (claim_ms and 0 <= now_ms - claim_ms
              <= int(_CLAIM_ORPHAN_SEC * 1000)):
            obs.status = Status.WORKING
            obs.phase = Phase.THINKING
            obs.turn_active = True
            obs.confidence = Confidence.MEDIUM
            obs.summary = ""
        if obs.status in (Status.WAITING, Status.WORKING):
            return obs
        # The V2 catalog carries a bounded lifecycle outcome alongside
        # the session row.  Preserve explicit completion/failure
        # reminders even when the active endpoint has already removed
        # the session.  A stale catalog row alone still remains UNKNOWN.
        outcome = str(outcome or "").casefold().replace("_", "-")
        anchor_ms = idle_ms or _row_updated(session_row)
        age = (now * 1000 - anchor_ms) / 1000.0 if anchor_ms else -1
        if outcome in {"completed", "complete", "success", "succeeded",
                        "done", "finished"} and 0 <= age < 8:
            obs.status = Status.DONE
            obs.confidence = Confidence.EXACT
            obs.summary = "回合完成"
        elif outcome in {"error", "failed", "failure", "aborted",
                          "cancelled", "canceled", "interrupted",
                          "blocked"} and 0 <= age < 30:
            obs.status = Status.ERROR
            obs.confidence = Confidence.EXACT
            labels = {"aborted": "任务已中断",
                      "cancelled": "任务已取消",
                      "canceled": "任务已取消",
                      "interrupted": "任务被中断",
                      "blocked": "任务被阻塞"}
            obs.summary = labels.get(outcome, "任务异常结束")
        return obs


# ---------------------------------------------------------- CLI watcher

class OpenCodeWatcher(BaseWatcher):
    """OpenCode CLI 终端实例 watcher：DB 目录匹配 + 可信 pending 补充。

    绝不做文件 tail（DB 是索引不是事件流）；状态证据只有可信 pending，
    否则 UNKNOWN + 会话元数据（plan §六明确：DB 行存在 ≠ WORKING）。
    同一 cwd 多个 live OpenCode 进程 → 无法归属，全部不绑定。
    """
    kind = AgentKind.OPENCODE

    def __init__(self, monitor_cfg: dict, runtime: OpenCodeRuntime):
        super().__init__(monitor_cfg)
        self.runtime = runtime
        self._bindings: dict[str, str] = {}   # instance key → session id

    # BaseWatcher 的文件发现/绑定接口不适用于 DB 索引数据面：显式空实现
    def make_state(self, path: str):  # pragma: no cover
        raise NotImplementedError

    def refresh_files(self, sources, instances=None, force=False):
        return

    def poll(self, instances: list) -> dict[str, Observation]:
        now = time.time()
        live_keys = {getattr(i, "key", "") for i in instances}
        for key in list(self._bindings):
            if key not in live_keys:
                self._bindings.pop(key, None)
        if not instances:
            return {}
        by_source: dict[str, list] = {}
        for inst in instances:
            by_source.setdefault(_source_of(inst), []).append(inst)
        out: dict[str, Observation] = {}
        remote = self.runtime.remote_facts()
        for source, group in sorted(by_source.items()):
            snapshot = None
            if source == "windows":
                snapshot = self.runtime.windows_facts(now)
            else:
                for snap in remote.values():
                    if snap.source == source and snap.authoritative:
                        snapshot = snap
                        break
                if snapshot is None:
                    for snap in remote.values():
                        if snap.source == source:
                            snapshot = snap
                            break
            self._bind_group(source, group, snapshot, now, out)
        return out

    def terminal_claims(self) -> frozenset:
        """已绑定 CLI 实例的会话 claim（desktop source 去重用）。"""
        return frozenset(
            SessionClaimKey(kind="opencode", session_id=sid)
            for sid in self._bindings.values())

    # ------------------------------------------------------------ 内部
    def _bind_group(self, source: str, group: list, snapshot, now: float,
                    out: dict[str, Observation]):
        cwd_counts: dict[str, int] = {}
        for inst in group:
            key = normalize_opencode_directory(getattr(inst, "cwd", ""))
            if key:
                cwd_counts[key] = cwd_counts.get(key, 0) + 1
        by_directory = snapshot.sessions_by_directory() if snapshot else {}
        for inst in sorted(group, key=lambda i: str(getattr(i, "key", ""))):
            key = str(getattr(inst, "key", ""))
            cwd_key = normalize_opencode_directory(getattr(inst, "cwd", ""))
            row = None
            if cwd_key and cwd_counts.get(cwd_key, 0) == 1:
                row = by_directory.get(cwd_key)
            if row is not None:
                self._bindings[key] = str(row[0])
                out[key] = self.runtime.observation_for_session(
                    row, now, message=snapshot.latest_message(str(row[0]))
                    if snapshot is not None else None)
                out[key].session_id = str(row[0])
            else:
                self._bindings.pop(key, None)
                out[key] = Observation(
                    source=EvidenceSource.SESSION, timestamp=now,
                    status=None, session_bound=False,
                    cwd=getattr(inst, "cwd", ""),
                    summary="Session：未解析（目录无法唯一归属）")


def _source_of(inst) -> str:
    return str(getattr(inst, "source", "") or "")


# ---------------------------------------------------------- Desktop source

@dataclass
class _OpenCodeDesktopSession:
    session_id: str
    bound_host_key: str = ""
    admitted: bool = False
    last_activity_ts: float = 0.0
    last_revision_ts: float = 0.0
    last_pending_ts: float = 0.0


class OpenCodeDesktopSource(DesktopSessionSource):
    """OpenCode Desktop 宿主的逻辑会话 source（每个 kind 唯一实例）。

    只读同一数据面的 root session；被 CLI claim 覆盖的会话不发 target
    （plan §三：一个 Kind 一个权威 Desktop source）。
    """
    kind = AgentKind.OPENCODE

    def __init__(self, cfg: dict | None = None,
                 runtime: OpenCodeRuntime | None = None):
        self._cfg = dict(cfg or {})
        self.runtime = runtime or OpenCodeRuntime(self._cfg)
        self._sessions: dict[str, _OpenCodeDesktopSession] = {}

    def close(self) -> None:
        self._sessions.clear()

    def drop_host(self, host_key: str) -> None:
        for sid in list(self._sessions):
            sess = self._sessions[sid]
            if not host_key or sess.bound_host_key == host_key:
                self._sessions.pop(sid, None)

    def stats(self) -> dict:
        return {"opencode_desktop_sessions": len(self._sessions)}

    def poll(self, hosts, claimed_sessions, now) -> DesktopSourceSnapshot:
        opencode_hosts = [h for h in hosts if h.kind is AgentKind.OPENCODE]
        if not opencode_hosts:
            self._sessions.clear()
            return DesktopSourceSnapshot(
                authoritative=True, diagnostics=())
        host = _select_primary_host(opencode_hosts)
        snapshot = self.runtime.windows_facts(now)
        if snapshot is None or not snapshot.authoritative:
            return DesktopSourceSnapshot(
                authoritative=False,
                diagnostics=bounded_diagnostics(
                    (snapshot.diagnostics if snapshot else ("NO_FACTS",))))
        window = float(self._cfg.get("active_file_window_sec", 180))
        pending, _ok = self.runtime._http_pending(now)
        active_ids, _active_ok = self.runtime._http_active(now)
        now_ms = int(now * 1000)
        catalog = _root_rows(snapshot.catalog)
        live: set[str] = set()
        instances: list[AgentInstance] = []
        observations: dict[str, Observation] = {}
        claims: set[SessionClaimKey] = set()
        for row in catalog[:_MAX_SESSIONS * 2]:
            sid = str(row[0])
            updated_ms = _row_updated(row)
            idle_ms, archived_ms, idle_outcome = _row_lifecycle(row)
            if archived_ms:
                continue
            live.add(sid)
            try:
                claim_ms = int(row[8] or 0)
            except (TypeError, ValueError, IndexError):
                claim_ms = 0
            message = snapshot.latest_message(sid)
            msg_ms = int(message[1] or 0) if message else 0
            sess = self._sessions.get(sid)
            if sess is None:
                sess = _OpenCodeDesktopSession(session_id=sid)
                self._sessions[sid] = sess
            active = (updated_ms and 0 <= now_ms - updated_ms
                      <= int(window * 1000))
            # time_idle/idle_outcome are lifecycle facts; a stale DB row is
            # not evidence of a currently running turn.
            if idle_ms and idle_ms >= max(updated_ms, claim_ms, msg_ms):
                active = False
            # 执行 claim（time_suspended）与新鲜 session_message 是
            # durable-turn 证据：admission 不依赖 HTTP active 集合。
            if claim_ms and 0 <= now_ms - claim_ms <= int(
                    _CLAIM_ORPHAN_SEC * 1000):
                active = True
            if msg_ms and 0 <= now_ms - msg_ms <= int(
                    _LIVE_MESSAGE_FRESH_SEC * 1000):
                active = True
            if sid in pending:
                active = True
                sess.last_pending_ts = now
            if sid in active_ids:
                active = True
            if not sess.admitted and not active:
                continue
            sess.last_revision_ts = max(sess.last_revision_ts,
                                        max(updated_ms, msg_ms) / 1000.0)
            sess.admitted = True
            sess.bound_host_key = host.host_key
            sess.last_activity_ts = max(
                sess.last_activity_ts,
                max(updated_ms, msg_ms, claim_ms) / 1000.0)
            if (sess.admitted and not active and sid not in pending
                    and sess.last_activity_ts
                    and now - max(sess.last_activity_ts,
                                   sess.last_pending_ts) > _IDLE_LEASE_SEC):
                self._sessions.pop(sid, None)
                continue
            if claims_cover(claimed_sessions, "opencode", session_id=sid):
                continue
            inst = AgentInstance(
                kind=AgentKind.OPENCODE, pid=0, source="windows",
                surface=AgentSurface.DESKTOP,
                host_pid=host.pid, host_process_token=host.process_token,
                host_key=host.host_key, logical_session_id=sid)
            obs = self.runtime.observation_for_session(
                row, now, message=message)
            obs.session_id = sid
            observations[inst.key] = obs
            claims.add(SessionClaimKey(kind="opencode", session_id=sid))
            instances.append(inst)
        for sid in list(self._sessions):
            if sid not in live:
                self._sessions.pop(sid, None)
        return DesktopSourceSnapshot(
            instances=tuple(instances),
            observations=observations,
            claims=frozenset(claims),
            host_keys=frozenset(h.host_key for h in opencode_hosts),
            authoritative=True,
            diagnostics=bounded_diagnostics(snapshot.diagnostics))


def _root_rows(catalog) -> tuple:
    """root session（parent_id 为空）优先；无 parent_id 列时按原序。"""
    roots = [row for row in catalog if not str(row[3] or "")]
    if roots:
        return tuple(roots)
    return tuple(catalog)


def _select_primary_host(hosts):
    return min(hosts, key=lambda h: (
        float(getattr(h, "started_at", 0.0) or 0.0),
        int(getattr(h, "pid", 0) or 0), str(h.host_key)))
