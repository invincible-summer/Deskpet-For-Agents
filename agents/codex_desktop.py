"""Codex Desktop 被动 source（plan2 §7/§14）。

数据面（全部只读）：
  * 主索引：``CODEX_HOME/state_N.sqlite`` 的 threads 表——只读 SQLite
    （agents/sqlite_ro 合同），schema capability 探测，绝不硬编码版本；
  * 状态真值：每个 admitted thread 的 exact rollout JSONL——复用
    agents/codex.CodexFile 的 durable turn 语义，不复制 parser；
  * 审批 WAITING：上游 rollout policy 把 ExecApprovalRequest /
    RequestPermissions / RequestUserInput / ApplyPatchApprovalRequest
    列为 transient 不落盘——rollout 无法重建审批。本 source 绝不从
    静默/文件不增长合成 WAITING；无 exact UIA 证据时诚实降级
    WORKING/UNKNOWN（plan2 §7.6）。4.4.0 不实现 Desktop UIA 增强；
  * 绝不连接 App Server 控制面：initialize 会改进程级 client metadata
    （上游 initialize_processor.rs），不是无副作用观察路径。

admission / lease（plan2 §7.4）：
  * admit 条件（任一）：rollout 当前 active turn；DeskPet 启动后出现
    新增长；cold start 时 DB updated_at 在 active_file_window_sec 内且
    解析显示进行中；
  * idle lease 10 分钟（内部常量，非 config）；有 WORKING/WAITING/INPUT
    观察时绝不因 TTL 移除；
  * archived=0；thread_source 排除 generated（subagent/guardian/…）；
    originator 列存在时只接受已知 desktop 族，未知值进有界诊断；
  * catalog LIMIT 32；最多同时 track 8 个 session（与 Presentation
    1..8 产品上限对应）。

身份：thread 逻辑会话的 AgentInstance 由 Monitor 侧 host 绑定生成；
runtime key 含宿主 incarnation（plan2 §3.3），同 thread 在 app 重启后
绝不继承旧 target 身份。runtime 身份绝不持久化。
"""
import os
import time
from dataclasses import dataclass, field

from . import paths
from .codex import CodexFile
from .desktop import (
    DesktopSessionSource,
    DesktopSourceSnapshot,
    SessionClaimKey,
    bounded_diagnostics,
    canonical_session_path,
    claims_cover,
)
from .models import (
    AgentKind,
    AgentSurface,
    AgentInstance,
    EvidenceSource,
    Observation,
    Status,
)
from .sqlite_ro import (
    DbFingerprint,
    ReadOnlySqlite,
    SchemaCapabilities,
    fingerprints_differ,
    stat_fingerprint,
)

# 上游 otel/tags.rs + login/default_client.rs 已知的 desktop 族 originator；
# 未知名不自动当 Desktop（写诊断，plan2 §7.1）。
_DESKTOP_ORIGINATORS = frozenset({
    "codex_desktop", "codex_chatgpt_desktop", "codex_work_desktop",
    "codex_atlas",
})
# thread_source（上游 migration 0030 / protocol thread_data.rs）中明确
# 非 root/user 的 generated 值：绝不形成用户 pet。
_GENERATED_THREAD_SOURCES = frozenset({
    "subagent", "guardian", "guardian_review", "memory_consolidation",
})

_MAX_SESSIONS = 8            # 与 Presentation 1..8 上限对应（plan2 §6.2）
_CATALOG_LIMIT = 32          # plan2 §6.2：catalog LIMIT <= 32
_IDLE_LEASE_SEC = 600.0      # plan2 §7.4：默认 idle lease 10 分钟
_SAFETY_REFRESH_SEC = 7.5    # plan2 §6.2：空闲 safety refresh 5~10s
_FAILURE_RETRY_SEC = 1.0     # 失败后的最短重试间隔（防 busy SQL storm）
_DIAG_MAX = 8

_REQUIRED_THREAD_COLUMNS = ("id", "rollout_path", "updated_at", "archived")
_PREFERRED_COLUMNS = ("title", "cwd", "has_user_event", "thread_source",
                      "originator", "source", "updated_at_ms")


@dataclass
class _DesktopSession:
    """一个 Codex Desktop thread 的 source 内部状态（runtime-only）。"""
    thread_id: str
    rollout_path: str           # 已通过 containment 校验的真实路径
    bound_host_key: str = ""
    first_seen_size: int = -1   # 首次见到的 rollout 大小（增长判定基线）
    admitted: bool = False
    admitted_reason: str = ""
    db_updated_at: float = 0.0
    db_title: str = ""
    db_cwd: str = ""
    archived_seen: bool = False
    file: CodexFile | None = None
    last_status: Status | None = None
    last_activity_ts: float = 0.0


class CodexDesktopSource(DesktopSessionSource):
    """threads catalog（只读 SQLite）+ exact rollout（CodexFile）。

    poll() 在 Monitor 后台线程执行；所有 DB/文件操作有界，失败保守
    （locked/corrupt → non-authoritative 保留 last good；missing rollout
    不凭 DB row 推状态）。
    """

    def __init__(self, cfg: dict | None = None, codex_home: str | None = None,
                 db_path: str | None = None):
        self.kind = AgentKind.CODEX
        self._cfg = dict(cfg or {})
        self._codex_home = codex_home or paths.codex_home_root()
        self._db_path = db_path or ""
        self._ro: ReadOnlySqlite | None = None
        self._caps: SchemaCapabilities | None = None
        self._fingerprint = DbFingerprint(exists=False)
        self._last_sql = 0.0
        self._retry_at = 0.0
        self._sessions: dict[str, _DesktopSession] = {}
        self._failures = 0
        self.db_query_count = 0
        self.db_busy_count = 0
        self.active_window_sec = float(self._cfg.get("active_file_window_sec", 180.0) or 180.0)

    # ------------------------------------------------------------ 公共
    def close(self) -> None:
        """释放 DB 连接与全部 session 文件句柄（shutdown/测试用）。"""
        self._release_all()
        self._close_db()

    def drop_host(self, host_key: str) -> None:
        """host 退出：释放绑定该 host 的全部 session 状态（plan2 §9）。"""
        for thread_id in list(self._sessions):
            sess = self._sessions[thread_id]
            if not host_key or sess.bound_host_key == host_key:
                self._release(thread_id)

    def stats(self) -> dict:
        return {
            "codex_desktop_sessions": len(self._sessions),
            "codex_desktop_db_queries": self.db_query_count,
            "codex_desktop_db_busy": self.db_busy_count,
        }

    # ------------------------------------------------------------ 主路径
    def poll(self, hosts, claimed_sessions, now) -> DesktopSourceSnapshot:
        codex_hosts = [h for h in hosts if h.kind is AgentKind.CODEX]
        diag: list[str] = []
        if not codex_hosts:
            self._release_all()
            return self._finish((), {}, codex_hosts, diag, now)
        # host 找到、DB 找不到：不把 host 冒充 agent（plan2 §13）
        db = self._resolve_db(diag)
        if not db:
            self._release_all()
            diag.append("NO_STATE_DB")
            return self._finish((), {}, codex_hosts, diag, now)

        fingerprint = stat_fingerprint(db)
        changed = fingerprints_differ(fingerprint, self._fingerprint)
        refresh_due = now - self._last_sql >= _SAFETY_REFRESH_SEC
        retry_due = bool(self._retry_at) and now >= self._retry_at
        rows = None
        if changed or refresh_due or retry_due:
            rows = self._query_catalog(db, diag)
            if rows is None:
                # locked/corrupt：保留 last good 且 non-authoritative；
                # 1s 节流重试（不做 SQL storm，也不吞掉恢复）
                self._retry_at = now + _FAILURE_RETRY_SEC
                self._fingerprint = fingerprint
                return DesktopSourceSnapshot(
                    authoritative=False,
                    host_keys=frozenset(h.host_key for h in codex_hosts),
                    diagnostics=bounded_diagnostics(diag))
            self._fingerprint = fingerprint
            self._last_sql = now
            self._retry_at = 0.0
            self._apply_catalog(rows, codex_hosts)
        instances, observations, claims = self._collect(codex_hosts,
                                                        claimed_sessions, now)
        return self._finish(instances, observations, codex_hosts, diag, now,
                            claims=claims)

    # ------------------------------------------------------------ DB
    def _resolve_db(self, diag: list[str]) -> str:
        if self._db_path and os.path.isfile(self._db_path):
            return self._db_path
        self._close_db()
        for cand in paths.codex_state_db_candidates(self._codex_home):
            self._db_path = cand
            if os.path.isfile(cand):
                return cand
        self._db_path = ""
        return ""

    def _close_db(self):
        if self._ro is not None:
            self._ro.close()
            self._ro = None
        self._caps = None

    def _ensure_connection(self, db: str, diag: list[str]) -> bool:
        if self._ro is None or self._ro.path != db:
            self._close_db()
            self._ro = ReadOnlySqlite(db)
        if self._ro.open():
            return True
        diag.append(self._ro.last_error or "open failed")
        self._failures += 1
        if self._failures >= 3:
            self._close_db()
            self._failures = 0
        return False

    def _query_catalog(self, db: str, diag: list[str]) -> list | None:
        """threads catalog 白名单查询；schema 不满足 → INCOMPATIBLE_SCHEMA。"""
        if not self._ensure_connection(db, diag):
            return None
        if self._caps is None:
            caps = self._ro.read_schema(["threads"])
            if caps is None:
                diag.append(self._ro.last_error or "schema probe failed")
                self.db_busy_count = self._ro.busy_count
                return None
            self._caps = caps
        caps = self._caps
        if (not caps.has_table("threads")
                or not caps.has_columns("threads", _REQUIRED_THREAD_COLUMNS)):
            diag.append("INCOMPATIBLE_SCHEMA")
            return None
        cols = set(caps.columns_of("threads"))
        select = list(_REQUIRED_THREAD_COLUMNS)
        select += [c for c in _PREFERRED_COLUMNS if c in cols]
        where = ["archived = 0"]
        if "has_user_event" in cols:
            where.append("has_user_event = 1")
        if "thread_source" in cols:
            quoted = ",".join(
                "'" + v + "'" for v in sorted(_GENERATED_THREAD_SOURCES))
            where.append(
                f"(thread_source IS NULL OR thread_source NOT IN ({quoted}))")
        sql = (f"SELECT {', '.join(select)} FROM threads "
               f"WHERE {' AND '.join(where)} "
               f"ORDER BY updated_at DESC LIMIT {_CATALOG_LIMIT}")
        rows = self._ro.query(sql, max_rows=_CATALOG_LIMIT)
        self.db_query_count = self._ro.query_count
        self.db_busy_count = self._ro.busy_count
        if rows is None:
            diag.append(self._ro.last_error or "catalog query failed")
            return None
        return rows

    @staticmethod
    def _row_updated_at(row) -> float:
        """updated_at（秒优先；毫秒值自动换算），缺失 → 0。"""
        try:
            value = float(row["updated_at"] or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            value = 0.0
        if 0 < value < 1e11 and "updated_at_ms" in row.keys():
            try:
                ms = float(row["updated_at_ms"] or 0)
                if ms > 1e12:
                    return ms / 1000.0
            except (TypeError, ValueError):
                pass
        if value > 1e12:      # 毫秒 epoch
            return value / 1000.0
        return value

    def _apply_catalog(self, rows, codex_hosts):
        """行 → 内部 session 候选；不触碰 rollout 文件（poll 统一读）。"""
        host_key = codex_hosts[0].host_key
        seen = set()
        for row in rows:
            try:
                thread_id = str(row["id"] or "")
            except (KeyError, IndexError, TypeError):
                continue
            if not thread_id:
                continue
            # originator 列存在时要求 desktop 族（plan2 §7.1）；未知新值
            # 只写诊断，不自动当 Desktop
            keys = row.keys()
            if "originator" in keys:
                originator = str(row["originator"] or "").strip()
                if originator and originator not in _DESKTOP_ORIGINATORS:
                    continue
            rollout = self._validate_rollout_path(row["rollout_path"])
            if rollout is None:
                continue   # 越界/无法解析：拒绝该 row（plan2 §7.2）
            updated_at = self._row_updated_at(row)
            seen.add(thread_id)
            sess = self._sessions.get(thread_id)
            if sess is None:
                size = self._safe_size(rollout)
                sess = _DesktopSession(
                    thread_id=thread_id, rollout_path=rollout,
                    first_seen_size=size)
                self._sessions[thread_id] = sess
            else:
                # rollout path 变化（revert/重写）→ 重新校准基线
                if canonical_session_path(sess.rollout_path) != \
                        canonical_session_path(rollout):
                    self._release_file(sess)
                    sess.rollout_path = rollout
                    sess.admitted = False
                    sess.file = None
                    sess.first_seen_size = self._safe_size(rollout)
            sess.bound_host_key = host_key
            sess.db_updated_at = max(sess.db_updated_at, updated_at)
            if "title" in row.keys():
                sess.db_title = str(row["title"] or "")[:120]
            if "cwd" in row.keys():
                sess.db_cwd = str(row["cwd"] or "")
        # catalog 缺席（archived=1 / DELETE / 滚出 LIMIT 32）→ 标记；
        # 真正退场走 idle lease（active 观察优先，plan2 §7.4）
        for thread_id, sess in self._sessions.items():
            if thread_id not in seen:
                sess.archived_seen = True

    def _validate_rollout_path(self, raw) -> str | None:
        """DB rollout_path 是 untrusted local metadata（plan2 §7.2）。"""
        text = str(raw or "").strip()
        if not text:
            return None
        if text.startswith("\\\\?\\"):
            text = text[4:]
        try:
            real = os.path.realpath(text)
        except Exception:
            return None
        norm = os.path.normcase(real)
        for root in paths.codex_rollout_roots(self._codex_home):
            try:
                root_real = os.path.realpath(root)
            except Exception:
                continue
            root_norm = os.path.normcase(root_real)
            if norm == root_norm or norm.startswith(root_norm + os.sep):
                return real
        return None

    @staticmethod
    def _safe_size(path: str) -> int:
        try:
            return os.stat(path).st_size
        except OSError:
            return -1

    # ------------------------------------------------------------ 会话收集
    def _collect(self, codex_hosts, claimed_sessions, now):
        instances: list[AgentInstance] = []
        observations: dict[str, Observation] = {}
        claims: set[SessionClaimKey] = set()
        host_by_key = {h.host_key: h for h in codex_hosts}
        for thread_id in list(self._sessions):
            sess = self._sessions[thread_id]
            host = host_by_key.get(sess.bound_host_key)
            if host is None:
                # host 已不在本轮 inventory：等 Monitor 的 census/exit
                # 级联；本轮不发 target
                continue
            claimed = claims_cover(
                claimed_sessions, "codex", session_id=thread_id,
                canonical_path=sess.rollout_path)
            obs = self._read_rollout(sess, now)
            if obs is not None:
                sess.last_status = obs.status
                anchor = max(sess.file.last_event_ts if sess.file else 0.0,
                             sess.last_activity_ts, sess.db_updated_at)
                if anchor:
                    sess.last_activity_ts = max(sess.last_activity_ts, anchor)
            # idle lease（plan2 §7.4）：active/WAITING/INPUT 绝不因 TTL
            # 移除；其余超时释放（admitted 与候选一致处理）
            if self._lease_expired(sess, now):
                self._release(thread_id)
                continue
            if not sess.admitted and not self._admit(sess, now):
                continue
            if claimed:
                # terminal exact binding 优先（plan2 §3.4）：不发 target；
                # 保留状态，CLI 消失后下一轮 reclaim
                continue
            inst = AgentInstance(
                kind=AgentKind.CODEX, pid=0, source="windows",
                surface=AgentSurface.DESKTOP,
                host_pid=host.pid, host_process_token=host.process_token,
                host_key=host.host_key,
                logical_session_id=thread_id,
                session_file_hint=sess.rollout_path)
            if obs is not None:
                obs.session_file = sess.rollout_path
                obs.session_id = thread_id
                if not obs.goal and sess.db_title:
                    obs.goal = sess.db_title
                if not obs.title and sess.db_title:
                    obs.title = sess.db_title
                if not obs.cwd and sess.db_cwd:
                    obs.cwd = sess.db_cwd
                observations[inst.key] = obs
            claims.add(SessionClaimKey(
                kind="codex", session_id=thread_id,
                canonical_session_path=canonical_session_path(sess.rollout_path)))
            instances.append(inst)
        return instances, observations, claims

    def _read_rollout(self, sess: _DesktopSession, now: float) -> Observation | None:
        """增量读取 exact rollout（复用 CodexFile durable 语义）。"""
        size = self._safe_size(sess.rollout_path)
        if size < 0:
            # rollout 暂时不存在：不凭 DB row 推 WORKING（plan2 §13）
            return None
        if sess.file is None:
            if not self._admission_probe_wanted(sess, size):
                return None
            sess.file = CodexFile(sess.rollout_path)
        sess.file.poll_lines()
        obs = sess.file.observation(now, self._cfg)
        if obs is None:
            # 无任何 turn 生命周期证据：保守给 UNKNOWN 载体（reduce 兜底）
            obs = Observation(
                source=EvidenceSource.SESSION, timestamp=now, status=None,
                session_bound=True)
        return obs

    def _admission_probe_wanted(self, sess: _DesktopSession, size: int) -> bool:
        """是否需要打开 rollout 做 admission 探测（控制 fd/读放）。"""
        if size != sess.first_seen_size:
            return True     # 新增长（plan2 §7.4）
        if (sess.db_updated_at
                and 0 <= time.time() - sess.db_updated_at <= self.active_window_sec):
            return True     # cold start 活跃窗口
        return False

    def _admit(self, sess: _DesktopSession, now: float) -> bool:
        if sess.admitted:
            return True
        if sess.archived_seen:
            return False   # catalog 已缺席（archived/滚出）：不再 admit
        # 已满：不再 admit 新会话（保留现有，plan2 §6.2 active-track ≤8）
        admitted_count = sum(1 for s in self._sessions.values() if s.admitted)
        if admitted_count >= _MAX_SESSIONS:
            return False
        reason = ""
        obs_status = sess.last_status
        if obs_status in (Status.WORKING, Status.WAITING, Status.INPUT):
            reason = "active-turn"          # rollout 当前解析为 active turn
        elif (sess.file is not None and sess.first_seen_size >= 0
                and self._safe_size(sess.rollout_path) > sess.first_seen_size):
            reason = "growth"               # DeskPet 启动后出现新增长
        if not reason and sess.db_updated_at:
            # cold start：DB updated_at 在窗口内且解析显示正在进行；
            # 刚进入 DONE/ERROR 展示窗口的会话同样 admit（否则用户
            # 看不到"刚完成"的桌面线程；老 DONE 仍被窗口拒绝）
            if 0 <= now - sess.db_updated_at <= self.active_window_sec:
                if obs_status is Status.WORKING:
                    reason = "cold-start-active"
                elif obs_status in (Status.DONE, Status.ERROR):
                    reason = "cold-start-recent-terminal"
        if not reason:
            return False
        sess.admitted = True
        sess.admitted_reason = reason
        return True

    def _lease_expired(self, sess: _DesktopSession, now: float) -> bool:
        """idle lease（plan2 §7.4）：非 WORKING/WAITING/INPUT 且超过
        10 分钟无活动/无 DB 更新 → 释放。TTL 只决定 target 是否继续
        显示，绝不把状态改成 WAITING/DONE。"""
        if sess.last_status in (Status.WORKING, Status.WAITING, Status.INPUT):
            return False
        anchor = max(sess.last_activity_ts, sess.db_updated_at)
        return bool(anchor) and now - anchor > _IDLE_LEASE_SEC

    def _release_file(self, sess: _DesktopSession) -> None:
        if sess.file is not None:
            try:
                sess.file.tailer.close()
            except Exception:
                pass
            sess.file = None

    def _release(self, thread_id: str) -> None:
        sess = self._sessions.pop(thread_id, None)
        if sess is not None:
            self._release_file(sess)

    def _release_all(self) -> None:
        for thread_id in list(self._sessions):
            self._release(thread_id)

    # ------------------------------------------------------------ 快照
    def _finish(self, instances, observations, codex_hosts, diag, now,
                claims=None) -> DesktopSourceSnapshot:
        snap = DesktopSourceSnapshot(
            instances=tuple(instances),
            observations=dict(observations),
            claims=frozenset(claims or ()),
            host_keys=frozenset(h.host_key for h in codex_hosts),
            authoritative=True,
            diagnostics=bounded_diagnostics(diag))
        return snap
