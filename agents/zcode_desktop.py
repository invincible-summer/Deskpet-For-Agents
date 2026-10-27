"""ZCode Desktop 被动 source（plan2 §8/§13/§14）。

数据面（全部只读，零配置、无 hook、无插件）：
  * 主索引 + 主状态源：``%USERPROFILE%\\.zcode\\cli\\db\\db.sqlite``
    只读 SQLite（agents/sqlite_ro 合同）。官方未承诺 schema —— 全部
    列/表 capability 探测，不满足最低要求时非权威 + INCOMPATIBLE_SCHEMA。
  * 状态投影（plan2 §8.3，保守优先）：
      1. tool_usage.approval_status='requested' 且 status='running'
         → WAITING/APPROVAL/EXACT（行级 session 归属，resolved 由行
         状态/消失这一 DB 事实闭合，绝不用"静默 N 秒"推断）；
      2. model_usage.status='running' → WORKING/THINKING；
      3. tool_usage.status='running' → WORKING/EXECUTING(classify)；
      4. 最新 turn completed → DONE（8s 展示窗口）；
      5. 明确 error → ERROR（30s 窗口）；
      6. session 确认存在但无活动 → IDLE；
      7. 关系不足 → UNKNOWN。
    普通用户输入等待（INPUT）当前 schema 无法与已发送输入可靠区分
    （session_input 只有 backgroundNotification/compact/sendText），
    按 plan2 §8.3.2 不投影 INPUT，绝不把输入当审批。
  * subagent 不单独成 pet（plan2 §8.2）：catalog 只取 root session
    （parent_id IS NULL + 已知 subagent task_type 排除）；child session
    的 model/tool 活动通过 parent_id 归入父任务作为 WORKING 证据。
  * Side Conversation：当前版本 DB/进程形态没有可被动归属的稳定
    session 身份（task_type 仅 interactive/subagent_child），按
    AC-ZC-SIDE-01/02 结论不单独支持，不做标题/文本猜测。
  * log tailer（plan2 §8.4）仅在实机捕获白名单 event fixture 后进入
    产品；本版本未固定 fixture，不实现（DB 主路径不受影响）。
"""
import os
import time
from dataclasses import dataclass, field

from . import paths
from .base import classify_phase
from .desktop import (
    DesktopSessionSource,
    DesktopSourceSnapshot,
    SessionClaimKey,
    bounded_diagnostics,
    claims_cover,
)
from .models import (
    AgentKind,
    AgentSurface,
    AgentInstance,
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

_MAX_SESSIONS = 8            # plan2 §6.2：每 Desktop kind active-track ≤8
_CATALOG_LIMIT = 32
_IDLE_LEASE_SEC = 600.0
_ORPHAN_ACTIVE_TTL_SEC = 180.0
_SAFETY_REFRESH_SEC = 7.5
_FAILURE_RETRY_SEC = 1.0
_DONE_WINDOW_SEC = 8.0       # 与 CLI watcher 的 DONE 展示窗口一致
_ERROR_WINDOW_SEC = 30.0
_ACTIVITY_WINDOW_SEC = 180.0  # cold-start admit 窗口
_DIAG_MAX = 8
_REMOTE_FACT_TTL_SEC = 15.0
# 实机 schema 取证（2026-10-27）：turn_usage / model_usage 是回合结束后
# 才落盘的统计表，流式生成期间没有任何 status='running' 行；part.time_updated
# 是唯一实时前进的活跃信号。该窗口内的 part 更新按显式 WORKING 证据消费。
_LIVE_PART_FRESH_SEC = 90.0

# 明确的 subagent task_type（实机枚举；未知新值按 plan 只排除已知值）
_SUBAGENT_TASK_TYPES = frozenset({"subagent_child"})

_REQUIRED_SESSION_COLUMNS = ("id", "time_created", "time_updated")
_PREFERRED_SESSION_COLUMNS = ("parent_id", "task_type", "title", "directory",
                              "last_outcome")
_COMPLETED = frozenset({"completed", "complete", "success", "succeeded",
                        "done", "finished"})
_INTERRUPTED = frozenset({"error", "failed", "failure", "aborted", "cancelled",
                          "canceled", "interrupted", "blocked", "stopped"})
_TERMINAL = _COMPLETED | _INTERRUPTED


@dataclass
class _ZcodeSession:
    """一个 ZCode root task 的 source 内部状态（runtime-only）。"""
    session_id: str
    bound_host_key: str = ""
    first_seen_updated_ms: int = 0   # time_updated 基线（增长判定）
    admitted: bool = False
    admitted_reason: str = ""
    db_title: str = ""
    db_directory: str = ""
    db_updated_ms: int = 0
    last_status: Status | None = None
    last_activity_ts: float = 0.0
    db_outcome: str = ""


@dataclass
class _ZCodePlaneState:
    plane_key: str
    source: str
    sessions: dict[str, _ZcodeSession] = field(default_factory=dict)
    emitted_keys: set[str] = field(default_factory=set)
    runtime_identity: tuple = ()


class ZCodeSchemaAdapter:
    """table/column capability + 固定白名单 SQL builder（plan2 §8.2）。"""

    def __init__(self, caps: SchemaCapabilities):
        self.caps = caps
        self.session_columns = frozenset(caps.columns_of("session"))

    def compatible(self) -> bool:
        return (self.caps.has_table("session")
                and self.caps.has_columns("session",
                                          _REQUIRED_SESSION_COLUMNS))

    def catalog_sql(self) -> str:
        """最近 root sessions（只取固定白名单列）。"""
        cols = ["id", "time_created", "time_updated"]
        for opt in _PREFERRED_SESSION_COLUMNS:
            if opt in self.session_columns:
                cols.append(opt)
        where = []
        if "parent_id" in self.session_columns:
            where.append("parent_id IS NULL")
        if "task_type" in self.session_columns:
            quoted = ",".join("'" + v + "'"
                              for v in sorted(_SUBAGENT_TASK_TYPES))
            where.append(
                f"(task_type IS NULL OR task_type NOT IN ({quoted}))")
        if "time_archived" in self.session_columns:
            where.append("(time_archived IS NULL OR time_archived = 0)")
        if "time_deleted" in self.session_columns:
            where.append("(time_deleted IS NULL OR time_deleted = 0)")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return (f"SELECT {', '.join(cols)} FROM session{clause} "
                f"ORDER BY time_updated DESC LIMIT {_CATALOG_LIMIT}")

    def running_models_sql(self) -> str | None:
        if not (self.caps.has_table("model_usage")
                and self.caps.has_columns(
                    "model_usage", ("session_id", "status", "started_at"))):
            return None
        parent_expr = "s.parent_id" if "parent_id" in self.session_columns else "NULL"
        query_expr = "m.query_source" if "query_source" in self.caps.columns_of("model_usage") else "NULL"
        turn_expr = self._column("model_usage", "m", "turn_id")
        message_expr = self._column("model_usage", "m", "assistant_message_id")
        return (f"SELECT m.session_id, {parent_expr} AS parent_id, "
                f"{query_expr} AS query_source, m.started_at, "
                f"{turn_expr} AS turn_id, {message_expr} AS assistant_message_id "
                "FROM model_usage m "
                "JOIN session s ON s.id = m.session_id "
                f"WHERE m.status = 'running' {self._live_usage('model_usage', 'm')} "
                "ORDER BY m.started_at DESC LIMIT 32")

    def running_tools_sql(self) -> str | None:
        if not (self.caps.has_table("tool_usage")
                and self.caps.has_columns(
                    "tool_usage", ("session_id", "status", "started_at",
                                   "approval_status"))):
            return None
        parent_expr = "s.parent_id" if "parent_id" in self.session_columns else "NULL"
        tool_expr = "t.tool_name" if "tool_name" in self.caps.columns_of("tool_usage") else "NULL"
        turn_expr = self._column("tool_usage", "t", "turn_id")
        call_expr = self._column("tool_usage", "t", "tool_call_id")
        message_expr = "NULL"
        if (turn_expr != "NULL" and self.caps.has_columns(
                "model_usage", ("session_id", "turn_id", "started_at",
                                "assistant_message_id"))):
            message_expr = ("(SELECT mu.assistant_message_id FROM model_usage mu "
                            "WHERE mu.session_id = t.session_id "
                            "AND mu.turn_id = t.turn_id AND mu.started_at <= t.started_at "
                            "ORDER BY mu.started_at DESC LIMIT 1)")
        return (f"SELECT t.session_id, {parent_expr} AS parent_id, "
                f"{tool_expr} AS tool_name, t.status, t.approval_status, t.started_at, "
                f"{turn_expr} AS turn_id, {call_expr} AS tool_call_id, "
                f"{message_expr} AS assistant_message_id "
                "FROM tool_usage t JOIN session s ON s.id = t.session_id "
                f"WHERE t.status = 'running' {self._live_usage('tool_usage', 't')} "
                "ORDER BY t.started_at DESC LIMIT 32")

    def _column(self, table: str, alias: str, column: str) -> str:
        return f"{alias}.{column}" if column in self.caps.columns_of(table) else "NULL"

    def _live_usage(self, table: str, alias: str) -> str:
        conditions = []
        if "cancelled_by_user" in self.caps.columns_of(table):
            conditions.append(f"COALESCE({alias}.cancelled_by_user, 0) = 0")
        if ("turn_id" in self.caps.columns_of(table)
                and self.caps.has_columns("turn_usage", ("session_id", "turn_id", "status"))):
            terminal = ",".join("'" + value + "'" for value in sorted(_TERMINAL))
            cancelled = (" OR COALESCE(ended.cancelled_by_user, 0) != 0"
                         if "cancelled_by_user" in self.caps.columns_of("turn_usage") else "")
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM turn_usage ended "
                f"WHERE ended.session_id = {alias}.session_id "
                f"AND ended.turn_id = {alias}.turn_id "
                f"AND (lower(ended.status) IN ({terminal}){cancelled}))")
        return "".join(" AND " + condition for condition in conditions)

    def latest_turns_sql(self) -> str | None:
        if not (self.caps.has_table("turn_usage")
                and self.caps.has_columns(
                    "turn_usage", ("session_id", "status", "started_at",
                                   "completed_at"))):
            return None
        turn_expr = self._column("turn_usage", "t", "turn_id")
        cancelled = self._column("turn_usage", "t", "cancelled_by_user")
        # Each of the bounded catalog roots uses the existing session/turn index.
        # Historical terminal facts remain necessary to close orphan running rows.
        return ("SELECT t.session_id, t.status, t.started_at, t.completed_at, "
                f"{turn_expr} AS turn_id, {cancelled} AS cancelled_by_user "
                f"FROM ({self.catalog_sql()}) roots JOIN turn_usage t "
                "ON t.session_id = roots.id WHERE t.started_at = "
                "(SELECT MAX(last.started_at) FROM turn_usage last "
                "WHERE last.session_id = roots.id) "
                "ORDER BY t.started_at DESC LIMIT 32")

    def live_parts_sql(self) -> str | None:
        """新鲜 part 行（流式活跃证据 + 活动/思考正文）。"""
        if not self.caps.has_columns(
                "part", ("session_id", "time_updated", "data")):
            return None
        parent_expr = "s.parent_id" if "parent_id" in self.session_columns else "NULL"
        return (f"SELECT p.session_id, {parent_expr} AS parent_id, "
                "p.time_updated, "
                "substr(json_extract(p.data, '$.type'), 1, 24) AS part_type, "
                "substr(json_extract(p.data, '$.tool'), 1, 96) AS tool_name, "
                "substr(json_extract(p.data, '$.state.status'), 1, 24) AS tool_status, "
                "substr(json_extract(p.data, '$.text'), 1, 256) AS text "
                "FROM part p JOIN session s ON s.id = p.session_id "
                "WHERE p.time_updated >= ? AND json_valid(p.data) "
                "ORDER BY p.time_updated DESC LIMIT 16")

    def activity_sql(self) -> str | None:
        if not self.caps.has_columns("part", ("session_id", "message_id", "data",
                                               "time_updated")):
            return None
        owners = []
        for query in (self.running_models_sql(), self.running_tools_sql()):
            if query:
                owners.append("SELECT session_id, parent_id, assistant_message_id "
                              f"FROM ({query}) WHERE assistant_message_id IS NOT NULL")
        if not owners:
            return None
        details = ",".join(
            f"'{key}', substr(json_extract(p.data, '$.state.input.{key}'), 1, 256)"
            for key in ("command", "cmd", "file_path", "path", "query", "pattern", "url"))
        return (f"WITH owners AS ({' UNION '.join(owners)}) "
                "SELECT p.session_id, owners.parent_id, p.message_id, p.time_updated, "
                "json_extract(p.data, '$.type') AS part_type, "
                "substr(json_extract(p.data, '$.tool'), 1, 96) AS tool_name, "
                "substr(json_extract(p.data, '$.callID'), 1, 128) AS call_id, "
                "json_extract(p.data, '$.state.status') AS tool_status, "
                "substr(json_extract(p.data, '$.text'), 1, 512) AS text, "
                f"json_object({details}) AS detail "
                "FROM owners JOIN part p ON p.session_id = owners.session_id "
                "AND p.message_id = owners.assistant_message_id "
                "WHERE json_valid(p.data) AND json_extract(p.data, '$.type') "
                "IN ('tool', 'reasoning', 'text') "
                "ORDER BY p.time_updated DESC LIMIT 16")


class ZCodeStateProjector:
    """DB facts → Observation（plan2 §8.3 第三层）。

    只消费明确状态行；任何"最近没有新行"的静默都不产生 WORKING/
    WAITING（保守漏报优先，plan2 §13）。
    """

    def __init__(self, now_ms: int):
        self.now_ms = now_ms

    @staticmethod
    def _ms_to_s(ms) -> float:
        try:
            return float(int(ms or 0)) / 1000.0
        except (TypeError, ValueError):
            return 0.0

    def project(self, running_model, running_tool, latest_turn,
                live_part=None) -> Observation | None:
        """单 session 的证据行 → Observation；无证据 → None（UNKNOWN）。

        live_part 是该 root 最新 part 行（time_updated 在流式期间实时前进），
        用于 turn/model usage 统计行尚未落盘的生成阶段保持 WORKING。
        """
        obs = Observation(source=EvidenceSource.SESSION,
                          timestamp=self.now_ms / 1000.0,
                          status=None, session_bound=True)
        if latest_turn is not None:
            terminal = str(latest_turn[1] or "").casefold().replace("_", "-")
            if terminal in {"completed", "complete", "success", "succeeded",
                            "done", "finished", "error", "failed", "failure",
                            "aborted", "cancelled", "canceled", "interrupted",
                            "blocked"}:
                ended = self._ms_to_s(latest_turn[3])
                if ended:
                    if (running_model is not None
                            and self._ms_to_s(running_model[3]) <= ended):
                        running_model = None
                    if (running_tool is not None
                            and self._ms_to_s(running_tool[5]) <= ended):
                        running_tool = None
                    if (live_part is not None
                            and self._ms_to_s(live_part[0]) <= ended):
                        live_part = None
        # 1) unresolved permission approval → WAITING（exact 行级归属）
        if running_tool is not None and str(running_tool[4] or "") == "requested":
            obs.status = Status.WAITING
            obs.phase = Phase.APPROVAL
            obs.confidence = Confidence.EXACT
            obs.turn_active = True
            obs.expires_at = 0.0
            summary = str(running_tool[2] or "等待批复")
            obs.summary = summary[:160]
            return obs
        # 2/3) 明确 running 活动 → WORKING（subagent 行已归并入父）
        if running_model is not None:
            obs.status = Status.WORKING
            obs.phase = Phase.THINKING
            obs.confidence = Confidence.HIGH
            obs.turn_active = True
            return obs
        if running_tool is not None:
            name = str(running_tool[2] or "")
            phase = classify_phase(name, "")
            obs.status = Status.WORKING
            obs.phase = phase if phase is not Phase.NONE else Phase.EXECUTING
            obs.confidence = Confidence.HIGH
            obs.turn_active = True
            obs.summary = name[:160]
            return obs
        # 2.5) 流式 part 更新：usage 统计行未落盘的生成阶段保持 WORKING，
        # 携带思考/正文摘要，消除 turn 间隙的 执行中↔待命中 抖动。
        if live_part is not None:
            updated_s = self._ms_to_s(live_part[0])
            if 0 <= self.now_ms / 1000.0 - updated_s <= _LIVE_PART_FRESH_SEC:
                part_type = str(live_part[1] or "")
                tool = str(live_part[2] or "")
                phase = (classify_phase(tool, "")
                         if part_type == "tool" and tool else Phase.THINKING)
                obs.status = Status.WORKING
                obs.phase = (phase if phase is not Phase.NONE
                             else (Phase.EXECUTING if part_type == "tool"
                                   else Phase.THINKING))
                obs.confidence = Confidence.HIGH
                obs.turn_active = True
                text = str(live_part[4] or "")
                if text:
                    obs.summary = summarize_text(text, 160)
                elif tool:
                    obs.summary = tool[:160]
                return obs
        # 4/5) 明确 completion / error 的展示窗口
        if latest_turn is not None:
            status = str(latest_turn[1] or "")
            completed_s = self._ms_to_s(latest_turn[3])
            started_s = self._ms_to_s(latest_turn[2])
            status = status.casefold().replace("_", "-")
            if status in {"completed", "complete", "success", "succeeded",
                          "done", "finished"} and completed_s:
                if 0 <= self.now_ms / 1000.0 - completed_s < _DONE_WINDOW_SEC:
                    obs.status = Status.DONE
                    obs.phase = Phase.NONE
                    obs.confidence = Confidence.EXACT
                    return obs
            elif status in {"error", "failed", "failure", "aborted",
                            "cancelled", "canceled", "interrupted",
                            "blocked"}:
                anchor = completed_s or started_s
                if anchor and 0 <= self.now_ms / 1000.0 - anchor < _ERROR_WINDOW_SEC:
                    obs.status = Status.ERROR
                    obs.phase = Phase.NONE
                    obs.confidence = Confidence.EXACT
                    return obs
            # Unknown lifecycle values remain UNKNOWN/IDLE; no guessing.
        return None


class ZCodeDesktopSource(DesktopSessionSource):
    """ZCode root tasks 的被动 source（catalog + running/turn 投影）。"""

    def __init__(self, cfg: dict | None = None, zcode_db: str | None = None,
                 remote_provider=None):
        self.kind = AgentKind.ZCODE
        self._cfg = dict(cfg or {})
        self._db_path = zcode_db or paths.zcode_db_path()
        self._ro: ReadOnlySqlite | None = None
        self._caps: SchemaCapabilities | None = None
        self._adapter: ZCodeSchemaAdapter | None = None
        self._fingerprint = DbFingerprint(exists=False)
        self._last_sql = 0.0
        self._retry_at = 0.0
        self._last_facts: dict | None = None
        self._sessions: dict[str, _ZcodeSession] = {}
        self._local_plane = _ZCodePlaneState(
            plane_key="windows", source="windows", sessions=self._sessions)
        self._remote_provider = remote_provider
        self._remote_planes: dict[str, _ZCodePlaneState] = {}
        self._retired_keys: set[str] = set()
        self.db_query_count = 0
        self.db_busy_count = 0
        self.refresh_count = 0

    # ------------------------------------------------------------ 公共
    def close(self) -> None:
        self._sessions.clear()
        self._local_plane.emitted_keys.clear()
        self._remote_planes.clear()
        self._retired_keys.clear()
        self._last_facts = None
        self._last_sql = 0.0
        self._retry_at = 0.0
        self._fingerprint = DbFingerprint(exists=False)
        if self._ro is not None:
            self._ro.close()
            self._ro = None
        self._caps = None
        self._adapter = None

    def drop_host(self, host_key: str) -> None:
        for plane in [self._local_plane, *self._remote_planes.values()]:
            self._retired_keys.update(plane.emitted_keys)
            plane.emitted_keys.clear()
        for sessions in self._session_maps():
            for sid in list(sessions):
                sess = sessions[sid]
                if not host_key or sess.bound_host_key == host_key:
                    sessions.pop(sid, None)

    def stats(self) -> dict:
        return {
            "zcode_desktop_sessions": sum(len(s) for s in self._session_maps()),
            "zcode_desktop_remote_planes": len(self._remote_planes),
            "zcode_desktop_db_queries": self.db_query_count,
            "zcode_desktop_db_busy": self.db_busy_count,
            "zcode_desktop_refreshes": self.refresh_count,
        }

    # ------------------------------------------------------------ 主路径
    def poll(self, hosts, claimed_sessions, now) -> DesktopSourceSnapshot:
        zcode_hosts = [h for h in hosts if h.kind is AgentKind.ZCODE]
        diag: list[str] = []
        if not zcode_hosts:
            retired = set(self._local_plane.emitted_keys) | self._retired_keys
            for plane in self._remote_planes.values():
                retired.update(plane.emitted_keys)
            self.close()
            return DesktopSourceSnapshot(retired_keys=frozenset(retired))
        if len(zcode_hosts) > 1:
            return DesktopSourceSnapshot(
                authoritative=False, diagnostics=("AMBIGUOUS_ZCODE_HOST",))

        all_instances: list[AgentInstance] = []
        all_observations: dict[str, Observation] = {}
        all_claims: set[SessionClaimKey] = set()
        retired = set(self._retired_keys)
        self._retired_keys.clear()
        authoritative = True

        # Local Windows plane: retain the existing ReadOnlySqlite/fingerprint path.
        local_authoritative = True
        if os.path.isfile(self._db_path):
            fingerprint = stat_fingerprint(self._db_path)
            changed = fingerprints_differ(fingerprint, self._fingerprint)
            if changed and self._adapter is not None and not self._adapter.compatible():
                self._caps = None
                self._adapter = None
            refresh_due = now - self._last_sql >= _SAFETY_REFRESH_SEC
            retry_due = bool(self._retry_at) and now >= self._retry_at
            if self._retry_at and not retry_due and not changed:
                authoritative = False
                local_authoritative = False
                facts = self._last_facts
            elif changed or refresh_due or retry_due:
                facts = self._query_facts(diag)
                if facts is None:
                    self._retry_at = now + _FAILURE_RETRY_SEC
                    self._fingerprint = fingerprint
                    authoritative = False
                    local_authoritative = False
                    facts = self._last_facts
                else:
                    self._fingerprint = fingerprint
                    self._last_sql = now
                    self._retry_at = 0.0
                    self.refresh_count += 1
                    self._last_facts = facts
                    self._apply_catalog(facts["catalog"], zcode_hosts,
                                        sessions=self._sessions)
            else:
                facts = self._last_facts
            if facts and local_authoritative:
                items, observations, claims = self._collect(
                    facts, zcode_hosts, claimed_sessions, now,
                    sessions=self._sessions, source="windows",
                    facts_observed_at=self._last_sql)
                all_instances.extend(items)
                all_observations.update(observations)
                all_claims.update(claims)
                current = {inst.key for inst in items}
                retired.update(self._local_plane.emitted_keys - current)
                self._local_plane.emitted_keys = current
        else:
            # Local DB absence does not suppress a valid remote WSL data plane.
            diag.append("NO_STATE_DB")
            self._sessions.clear()
            retired.update(self._local_plane.emitted_keys)
            self._local_plane.emitted_keys.clear()
            self._last_facts = None
            if self._ro is not None:
                self._ro.close()
                self._ro = None
            self._caps = None
            self._adapter = None

        # Remote WSL planes are already queried by ProcessProbeWorker. poll() only
        # consumes the in-memory bounded snapshots and never executes wsl.exe.
        remote_map = None
        if self._remote_provider is not None:
            try:
                remote_map = dict(self._remote_provider() or {})
            except Exception as exc:
                authoritative = False
                diag.append(f"REMOTE_PROVIDER_FAILED:{type(exc).__name__}")
        if remote_map is not None:
            active_keys: set[str] = set()
            for plane_key in sorted(remote_map):
                snap = remote_map[plane_key]
                ctx = getattr(snap, "context", None)
                if ctx is None or not str(getattr(ctx, "source", "")).startswith("wsl:"):
                    continue
                active_keys.add(plane_key)
                plane = self._remote_planes.get(plane_key)
                if plane is None:
                    plane = _ZCodePlaneState(
                        plane_key=plane_key, source=ctx.source)
                    self._remote_planes[plane_key] = plane
                for item in getattr(snap, "diagnostics", ()):
                    diag.append(f"{ctx.source}:{item}")
                facts = getattr(snap, "facts", None)
                facts_at = float(getattr(snap, "observed_at", 0.0) or 0.0)
                if (not getattr(snap, "authoritative", False) or facts is None
                        or not 0 <= now - facts_at <= _REMOTE_FACT_TTL_SEC):
                    authoritative = False
                    continue
                runtime_identity = (ctx.uid, ctx.user, ctx.home, ctx.pid,
                                    ctx.process_token, ctx.runtime_role)
                if (plane.runtime_identity
                        and plane.runtime_identity != runtime_identity):
                    retired.update(plane.emitted_keys)
                    plane.emitted_keys.clear()
                    plane.sessions.clear()
                plane.runtime_identity = runtime_identity
                self._apply_catalog(facts.get("catalog", ()), zcode_hosts,
                                    sessions=plane.sessions)
                if facts:
                    items, observations, claims = self._collect(
                        facts, zcode_hosts, claimed_sessions, now,
                        sessions=plane.sessions, source=ctx.source,
                        facts_observed_at=facts_at, plane_key=plane_key,
                        runtime_token=ctx.process_token,
                        runtime_active=ctx.runtime_role == "agent",
                        uid=ctx.uid, user=ctx.user, home=ctx.home)
                    all_instances.extend(items)
                    all_observations.update(observations)
                    all_claims.update(claims)
                    current = {inst.key for inst in items}
                    retired.update(plane.emitted_keys - current)
                    plane.emitted_keys = current
            # Provider omission is authoritative runtime disappearance; degraded
            # sources remain present in the provider as non-authoritative snapshots.
            for key in list(self._remote_planes):
                if key not in active_keys:
                    retired.update(self._remote_planes[key].emitted_keys)
                    self._remote_planes.pop(key, None)
        elif self._remote_provider is not None:
            # Provider failure: retain plane state; Monitor will retain last-good
            # source targets because this DesktopSourceSnapshot is non-authoritative.
            authoritative = False

        return DesktopSourceSnapshot(
            instances=tuple(all_instances),
            observations=all_observations,
            claims=frozenset(all_claims),
            host_keys=frozenset(h.host_key for h in zcode_hosts),
            authoritative=authoritative,
            authoritative_keys=frozenset(inst.key for inst in all_instances),
            retired_keys=frozenset(retired),
            diagnostics=bounded_diagnostics(diag))

    # ------------------------------------------------------------ DB
    def _query_facts(self, diag: list[str]) -> dict | None:
        if self._ro is None:
            self._ro = ReadOnlySqlite(self._db_path)
        if not self._ro.open():
            diag.append(self._ro.last_error or "open failed")
            return None
        if self._caps is None:
            caps = self._ro.read_schema(["session", "model_usage",
                                         "tool_usage", "turn_usage", "part"])
            if caps is None:
                diag.append(self._ro.last_error or "schema probe failed")
                self.db_busy_count = self._ro.busy_count
                return None
            self._caps = caps
            self._adapter = ZCodeSchemaAdapter(caps)
        adapter = self._adapter
        if not adapter.compatible():
            diag.append("INCOMPATIBLE_SCHEMA")
            return None
        facts: dict = {"catalog": [], "models": [], "tools": [],
                       "turns": [], "parts": []}
        now_ms = int(time.time() * 1000)
        fresh_ms = now_ms - int(_LIVE_PART_FRESH_SEC * 1000)
        queries = (
            ("catalog", adapter.catalog_sql(), ()),
            ("models", adapter.running_models_sql(), ()),
            ("tools", adapter.running_tools_sql(), ()),
            ("turns", adapter.latest_turns_sql(), ()),
            ("parts", adapter.live_parts_sql(), (fresh_ms,)),
        )
        for key, sql, params in queries:
            if sql is None:
                continue   # 可选表缺失：capability 降级（plan2 §13）
            rows = self._ro.query(sql, params, max_rows=64)
            self.db_query_count = self._ro.query_count
            self.db_busy_count = self._ro.busy_count
            if rows is None:
                error = self._ro.last_error or f"{key} query failed"
                diag.append(error)
                if "no such table" in error or "no such column" in error:
                    self._caps = None
                    self._adapter = None
                return None
            facts[key] = rows
        return facts

    def _apply_catalog(self, rows, zcode_hosts, sessions=None):
        sessions = self._sessions if sessions is None else sessions
        host = self._select_primary_host(zcode_hosts)
        if host is None:
            return
        present: set[str] = set()
        for row in rows:
            try:
                sid = str(row["id"] or "")
            except (KeyError, IndexError, TypeError):
                continue
            if not sid:
                continue
            present.add(sid)
            keys = row.keys()
            sess = sessions.get(sid)
            if sess is None:
                sess = _ZcodeSession(session_id=sid)
                sessions[sid] = sess
            sess.bound_host_key = host.host_key
            try:
                sess.db_updated_ms = int(row["time_updated"] or 0)
            except (KeyError, TypeError, ValueError):
                pass
            if sess.first_seen_updated_ms == 0:
                sess.first_seen_updated_ms = sess.db_updated_ms
            if "title" in keys:
                sess.db_title = summarize_text(row["title"] or "", 120)
            if "directory" in keys:
                sess.db_directory = str(row["directory"] or "")[:120]
        for sid in set(sessions) - present:
            sessions.pop(sid, None)

    # ------------------------------------------------------------ 会话收集
    def _collect(self, facts, zcode_hosts, claimed_sessions, now,
                 sessions=None, source: str = "windows", *,
                 facts_observed_at: float = 0.0, plane_key: str = "windows",
                 runtime_token: str = "", runtime_active: bool = True,
                 uid: int | None = None, user: str = "", home: str = ""):
        sessions = self._sessions if sessions is None else sessions
        instances: list[AgentInstance] = []
        observations: dict[str, Observation] = {}
        claims: set[SessionClaimKey] = set()
        host_by_key = {h.host_key: h for h in zcode_hosts}
        if not facts:
            return instances, observations, claims
        now_ms = int(now * 1000)
        projector = ZCodeStateProjector(now_ms)

        model_by_root: dict[str, object] = {}
        for row in facts.get("models", ()):
            root = self._root_of(row[0], row[1])
            if root and root not in model_by_root:
                model_by_root[root] = row
        tool_by_root: dict[str, object] = {}
        for row in facts.get("tools", ()):
            root = self._root_of(row[0], row[1])
            if root and root not in tool_by_root:
                tool_by_root[root] = row
        turn_by_root: dict[str, object] = {}
        for row in facts.get("turns", ()):
            root = str(row[0] or "")
            if root and root not in turn_by_root:
                turn_by_root[root] = row
        live_by_root: dict[str, object] = {}
        for row in facts.get("parts", ()):
            if isinstance(row, dict):
                root = self._root_of(row.get("session_id"),
                                     row.get("parent_id"))
                item = (row.get("time_updated"), row.get("part_type"),
                        row.get("tool_name"), row.get("tool_status"),
                        row.get("text"))
            else:
                root = self._root_of(row[0], row[1])
                item = tuple(row[2:])
            if root and root not in live_by_root:
                live_by_root[root] = item

        for sid in list(sessions):
            sess = sessions[sid]
            host = host_by_key.get(sess.bound_host_key)
            if host is None:
                continue
            db_changed = self._db_updated_changed(sess)
            if db_changed:
                sess.last_activity_ts = max(
                    sess.last_activity_ts, sess.db_updated_ms / 1000.0)
            obs = projector.project(
                model_by_root.get(sid), tool_by_root.get(sid),
                turn_by_root.get(sid), live_by_root.get(sid))
            if obs is not None and obs.turn_active and not runtime_active:
                # A surviving remote-development server does not prove that its
                # former Agent child is still running a persisted model/tool row.
                obs = None
            if obs is not None:
                sess.last_status = obs.status
                # Only a successful data-plane read advances this lease. Reusing
                # a snapshot between worker revisions must never renew it at now.
                if (obs.turn_active
                        or obs.status in (Status.DONE, Status.ERROR)):
                    sess.last_activity_ts = max(sess.last_activity_ts,
                                                facts_observed_at)
            if self._lease_expired(sess, now):
                sessions.pop(sid, None)
                continue
            if not sess.admitted and not self._admit(
                    sess, now, obs, sid in turn_by_root):
                continue
            if obs is None:
                obs = Observation(source=EvidenceSource.SESSION,
                                  timestamp=now, status=Status.IDLE,
                                  phase=Phase.NONE,
                                  confidence=Confidence.HIGH,
                                  session_bound=True)
                sess.last_status = Status.IDLE
            if claims_cover(claimed_sessions, "zcode", session_id=sid,
                            canonical_data_root=plane_key):
                continue
            key = ""
            if source.startswith("wsl:"):
                key = (f"{source}|zcode|desktop|{host.process_token}|"
                       f"uid:{uid}|{runtime_token}|{sid}")
            inst = AgentInstance(
                kind=AgentKind.ZCODE, pid=0, source=source,
                key=key, uid=uid, user=user, home=home,
                surface=AgentSurface.DESKTOP,
                host_pid=host.pid, host_process_token=host.process_token,
                host_key=host.host_key,
                logical_session_id=sid)
            if obs is not None and obs.status is not None:
                obs.session_id = sid
                if not obs.goal and sess.db_title:
                    obs.goal = sess.db_title
                if not obs.title and sess.db_title:
                    obs.title = sess.db_title
                if not obs.cwd and sess.db_directory:
                    obs.cwd = sess.db_directory
                observations[inst.key] = obs
            claims.add(SessionClaimKey(kind="zcode", session_id=sid,
                                       canonical_data_root=plane_key))
            instances.append(inst)
        return instances, observations, claims

    @staticmethod
    def _root_of(session_id, parent_id) -> str | None:
        """child 行归并到 root（plan2 §8.2：subagent 是父任务内部工作）。"""
        sid = str(session_id or "")
        if not sid:
            return None
        return str(parent_id) if parent_id else sid

    def _db_updated_changed(self, sess: _ZcodeSession) -> bool:
        return sess.db_updated_ms > sess.first_seen_updated_ms

    @staticmethod
    def _select_primary_host(zcode_hosts):
        return zcode_hosts[0] if len(zcode_hosts) == 1 else None

    def _session_maps(self):
        return [self._sessions, *(p.sessions for p in self._remote_planes.values())]

    def _admitted_count(self) -> int:
        return sum(1 for sessions in self._session_maps()
                   for sess in sessions.values() if sess.admitted)

    def _admit(self, sess: _ZcodeSession, now: float,
               obs: Observation | None,
               has_recent_turn: bool = False) -> bool:
        if sess.admitted:
            return True
        admitted = self._admitted_count()
        if admitted >= _MAX_SESSIONS:
            return False
        reason = ""
        if obs is not None and obs.status in (
                Status.WORKING, Status.WAITING, Status.INPUT):
            reason = "active-activity"
        elif self._db_updated_changed(sess):
            reason = "updated-since-start"
        elif (sess.db_updated_ms
              and 0 <= now - sess.db_updated_ms / 1000.0 <= _ACTIVITY_WINDOW_SEC
              and obs is not None
              and obs.status in (Status.DONE, Status.ERROR)):
            reason = "cold-start-recent-terminal"
        elif (has_recent_turn and sess.db_updated_ms
              and 0 <= now - sess.db_updated_ms / 1000.0
              <= _ACTIVITY_WINDOW_SEC):
            # 明确 turn 行（含 cancelled）+ 新鲜 time_updated：
            # 刚交互过的任务以 IDLE 显示，不伪造 WORKING/WAITING
            reason = "recent-turn-evidence"
        if not reason:
            return False
        sess.admitted = True
        sess.admitted_reason = reason
        return True

    def _lease_expired(self, sess: _ZcodeSession, now: float) -> bool:
        if sess.last_status in (Status.WORKING, Status.WAITING, Status.INPUT):
            anchor = max(sess.last_activity_ts, sess.db_updated_ms / 1000.0)
            return bool(anchor) and now - anchor > _ORPHAN_ACTIVE_TTL_SEC
        anchor = max(sess.last_activity_ts, sess.db_updated_ms / 1000.0)
        return bool(anchor) and now - anchor > _IDLE_LEASE_SEC
