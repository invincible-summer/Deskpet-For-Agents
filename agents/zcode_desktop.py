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

_MAX_SESSIONS = 8            # plan2 §6.2：每 Desktop kind active-track ≤8
_CATALOG_LIMIT = 32
_IDLE_LEASE_SEC = 600.0
_SAFETY_REFRESH_SEC = 7.5
_FAILURE_RETRY_SEC = 1.0
_DONE_WINDOW_SEC = 8.0       # 与 CLI watcher 的 DONE 展示窗口一致
_ERROR_WINDOW_SEC = 30.0
_ACTIVITY_WINDOW_SEC = 180.0  # cold-start admit 窗口
_TURN_LOOKBACK_SEC = 600.0   # turn 完成历史的回看窗口
_DIAG_MAX = 8

# 明确的 subagent task_type（实机枚举；未知新值按 plan 只排除已知值）
_SUBAGENT_TASK_TYPES = frozenset({"subagent_child"})

_REQUIRED_SESSION_COLUMNS = ("id", "time_created", "time_updated")
_PREFERRED_SESSION_COLUMNS = ("parent_id", "task_type", "title", "directory")


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


@dataclass
class _ZCodePlaneState:
    plane_key: str
    source: str
    sessions: dict[str, _ZcodeSession] = field(default_factory=dict)
    last_facts: dict | None = None


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
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return (f"SELECT {', '.join(cols)} FROM session{clause} "
                f"ORDER BY time_updated DESC LIMIT {_CATALOG_LIMIT}")

    def running_models_sql(self) -> str | None:
        if not (self.caps.has_table("model_usage")
                and self.caps.has_columns(
                    "model_usage", ("session_id", "status", "started_at"))):
            return None
        return ("SELECT m.session_id, s.parent_id, m.query_source, "
                "m.started_at FROM model_usage m "
                "JOIN session s ON s.id = m.session_id "
                "WHERE m.status = 'running' "
                "ORDER BY m.started_at DESC LIMIT 32")

    def running_tools_sql(self) -> str | None:
        if not (self.caps.has_table("tool_usage")
                and self.caps.has_columns(
                    "tool_usage", ("session_id", "status", "started_at",
                                   "approval_status"))):
            return None
        return ("SELECT t.session_id, s.parent_id, t.tool_name, t.status, "
                "t.approval_status, t.started_at "
                "FROM tool_usage t JOIN session s ON s.id = t.session_id "
                "WHERE t.status = 'running' "
                "ORDER BY t.started_at DESC LIMIT 32")

    def recent_turns_sql(self, since_ms: int) -> str | None:
        if not (self.caps.has_table("turn_usage")
                and self.caps.has_columns(
                    "turn_usage", ("session_id", "status", "started_at",
                                   "completed_at"))):
            return None
        return ("SELECT session_id, status, started_at, completed_at "
                "FROM turn_usage "
                "WHERE COALESCE(completed_at, started_at) > ? "
                "ORDER BY COALESCE(completed_at, started_at) DESC LIMIT 64")


class ZCodeCatalog:
    """一次 catalog 读取（plan2 §8.2 第二层）。只返回白名单行。"""

    @staticmethod
    def read(con: ReadOnlySqlite, sql: str) -> list | None:
        return con.query(sql, max_rows=_CATALOG_LIMIT)


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

    def project(self, running_model, running_tool, latest_turn
                ) -> Observation | None:
        """单 session 的证据行 → Observation；无证据 → None（UNKNOWN）。"""
        obs = Observation(source=EvidenceSource.SESSION,
                          timestamp=self.now_ms / 1000.0,
                          status=None, session_bound=True)
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
        # 4/5) 明确 completion / error 的展示窗口
        if latest_turn is not None:
            status = str(latest_turn[1] or "")
            completed_s = self._ms_to_s(latest_turn[3])
            started_s = self._ms_to_s(latest_turn[2])
            if status == "completed" and completed_s:
                if 0 <= self.now_ms / 1000.0 - completed_s < _DONE_WINDOW_SEC:
                    obs.status = Status.DONE
                    obs.phase = Phase.NONE
                    obs.confidence = Confidence.EXACT
                    return obs
            elif status == "error":
                anchor = completed_s or started_s
                if anchor and 0 <= self.now_ms / 1000.0 - anchor < _ERROR_WINDOW_SEC:
                    obs.status = Status.ERROR
                    obs.phase = Phase.NONE
                    obs.confidence = Confidence.EXACT
                    return obs
            # cancelled：无成功庆祝，落回 IDLE 语义（返回 None→UNKNOWN/IDLE）
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
        self.db_query_count = 0
        self.db_busy_count = 0
        self.refresh_count = 0

    # ------------------------------------------------------------ 公共
    def close(self) -> None:
        self._sessions.clear()
        self._remote_planes.clear()
        self._last_facts = None
        if self._ro is not None:
            self._ro.close()
            self._ro = None
        self._caps = None
        self._adapter = None

    def drop_host(self, host_key: str) -> None:
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
            self._sessions.clear()
            self._remote_planes.clear()
            return self._finish((), {}, zcode_hosts, diag)

        all_instances: list[AgentInstance] = []
        all_observations: dict[str, Observation] = {}
        all_claims: set[SessionClaimKey] = set()
        authoritative = True

        # Local Windows plane: retain the existing ReadOnlySqlite/fingerprint path.
        if os.path.isfile(self._db_path):
            fingerprint = stat_fingerprint(self._db_path)
            changed = fingerprints_differ(fingerprint, self._fingerprint)
            refresh_due = now - self._last_sql >= _SAFETY_REFRESH_SEC
            retry_due = bool(self._retry_at) and now >= self._retry_at
            if changed or refresh_due or retry_due:
                facts = self._query_facts(diag)
                if facts is None:
                    self._retry_at = now + _FAILURE_RETRY_SEC
                    self._fingerprint = fingerprint
                    authoritative = False
                    facts = self._last_facts
                else:
                    self._fingerprint = fingerprint
                    self._last_sql = now
                    self._retry_at = 0.0
                    self.refresh_count += 1
                    self._last_facts = facts
                    self._local_plane.last_facts = facts
                    self._apply_catalog(facts["catalog"], zcode_hosts,
                                        sessions=self._sessions)
            else:
                facts = self._last_facts
            if facts:
                items, observations, claims = self._collect(
                    facts, zcode_hosts, claimed_sessions, now,
                    sessions=self._sessions, source="windows")
                all_instances.extend(items)
                all_observations.update(observations)
                all_claims.update(claims)
        else:
            # Local DB absence does not suppress a valid remote WSL data plane.
            diag.append("NO_STATE_DB")
            self._sessions.clear()
            self._last_facts = None
            self._local_plane.last_facts = None

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
                if getattr(snap, "authoritative", False) and facts is not None:
                    plane.last_facts = facts
                    self._apply_catalog(facts.get("catalog", ()), zcode_hosts,
                                        sessions=plane.sessions)
                else:
                    authoritative = False
                    facts = facts or plane.last_facts
                if facts:
                    items, observations, claims = self._collect(
                        facts, zcode_hosts, claimed_sessions, now,
                        sessions=plane.sessions, source=ctx.source)
                    all_instances.extend(items)
                    all_observations.update(observations)
                    all_claims.update(claims)
            # Provider omission is authoritative runtime disappearance; degraded
            # sources remain present in the provider as non-authoritative snapshots.
            for key in list(self._remote_planes):
                if key not in active_keys:
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
                                         "tool_usage", "turn_usage"])
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
        facts: dict = {"catalog": [], "models": [], "tools": [], "turns": []}
        now_ms = int(time.time() * 1000)
        queries = (
            ("catalog", adapter.catalog_sql(), ()),
            ("models", adapter.running_models_sql(), ()),
            ("tools", adapter.running_tools_sql(), ()),
            ("turns", adapter.recent_turns_sql(
                now_ms - int(_TURN_LOOKBACK_SEC * 1000)),
             (now_ms - int(_TURN_LOOKBACK_SEC * 1000),)),
        )
        for key, sql, params in queries:
            if sql is None:
                continue   # 可选表缺失：capability 降级（plan2 §13）
            rows = self._ro.query(sql, params, max_rows=64)
            self.db_query_count = self._ro.query_count
            self.db_busy_count = self._ro.busy_count
            if rows is None:
                diag.append(self._ro.last_error or f"{key} query failed")
                return None
            facts[key] = rows
        return facts

    def _apply_catalog(self, rows, zcode_hosts, sessions=None):
        sessions = self._sessions if sessions is None else sessions
        host = self._select_primary_host(zcode_hosts)
        if host is None:
            return
        for row in rows:
            try:
                sid = str(row["id"] or "")
            except (KeyError, IndexError, TypeError):
                continue
            if not sid:
                continue
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
                sess.db_title = str(row["title"] or "")[:120]
            if "directory" in keys:
                sess.db_directory = str(row["directory"] or "")[:120]

    # ------------------------------------------------------------ 会话收集
    def _collect(self, facts, zcode_hosts, claimed_sessions, now,
                 sessions=None, source: str = "windows"):
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

        for sid in list(sessions):
            sess = sessions[sid]
            host = host_by_key.get(sess.bound_host_key)
            if host is None:
                continue
            if self._db_updated_changed(sess):
                sess.last_activity_ts = max(
                    sess.last_activity_ts, sess.db_updated_ms / 1000.0)
            obs = projector.project(
                model_by_root.get(sid), tool_by_root.get(sid),
                turn_by_root.get(sid))
            if obs is not None:
                sess.last_status = obs.status
                if obs.turn_active or obs.status in (Status.DONE, Status.ERROR):
                    sess.last_activity_ts = max(sess.last_activity_ts, now)
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
            if claims_cover(claimed_sessions, "zcode", session_id=sid):
                continue
            inst = AgentInstance(
                kind=AgentKind.ZCODE, pid=0, source=source,
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
            claims.add(SessionClaimKey(kind="zcode", session_id=sid))
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
        if not zcode_hosts:
            return None
        return min(zcode_hosts, key=lambda h: (
            float(getattr(h, "started_at", 0.0) or 0.0),
            int(getattr(h, "pid", 0) or 0), str(h.host_key)))

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
            return False
        anchor = max(sess.last_activity_ts, sess.db_updated_ms / 1000.0)
        return bool(anchor) and now - anchor > _IDLE_LEASE_SEC

    # ------------------------------------------------------------ 快照
    def _finish(self, instances, observations, zcode_hosts, diag,
                claims=None) -> DesktopSourceSnapshot:
        return DesktopSourceSnapshot(
            instances=tuple(instances),
            observations=dict(observations),
            claims=frozenset(claims or ()),
            host_keys=frozenset(h.host_key for h in zcode_hosts),
            authoritative=True,
            diagnostics=bounded_diagnostics(diag))
