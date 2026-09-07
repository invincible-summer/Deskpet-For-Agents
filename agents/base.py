"""Watcher 基类：增量读取会话文件并建立保守、可解释的实例绑定。

进程发现和会话文件发现是两条不可靠的只读观察线。这里不能按 mtime
把两条排序后硬配，否则多个 Agent 或多个来源时很容易把摘要串到另一
个终端。绑定只使用来源、明确会话 ID、工作目录、启动时间等证据；证据
不足时保留 UNKNOWN，交给上层选择或手动绑定。
"""
import json
import os
import time
from datetime import datetime, timezone

from . import paths
from .models import AgentKind, Snapshot, Status
from .tailer import FileTailer

MAX_TRACKED_FILES = 8
DEFAULT_SCAN_SEC = 5.0


def parse_ts(value) -> float:
    """ISO8601/epoch → epoch；缺失或非法值返回 0，不伪造当前时间。

    回放历史会话时，使用当前时间作为缺失事件时间会把旧的完成事件误判
    成刚刚完成。调用方如果需要“到达时间”，应显式使用自己的 fallback。
    """
    if value is None or value == "":
        return 0.0
    try:
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def trunc(s: str, n: int = 120) -> str:
    """兼容旧 watcher 的确定长度截断工具。"""
    n = max(1, int(n))
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def _as_text(value) -> str:
    return str(value).strip() if value is not None else ""


def _path_session_id(path: str) -> str:
    """从常见会话路径取稳定 ID，避免把 wire/rollout 这类文件名当 ID。"""
    name = os.path.basename(path)
    stem, _ext = os.path.splitext(name)
    if stem.lower() in {"wire", "session", "events"}:
        parent = os.path.basename(os.path.dirname(path))
        if parent:
            return parent
    if stem.startswith("rollout-"):
        return stem[len("rollout-"):]
    return stem


class FileState:
    """每个会话文件的解析状态，由具体 watcher 实现。"""

    def __init__(self, path: str):
        self.path = path
        self.tailer = FileTailer(path)
        self.source = ""
        self.file_id = _path_session_id(path)
        self.session_id = ""
        self.thread_id = ""
        self.turn_id = ""
        self.cwd = ""
        self.goal = ""
        self.phase = ""
        self.started_at = 0.0
        self.last_event_ts = 0.0
        self.last_poll_seen = 0.0
        self.last_mtime = 0.0
        self.last_type = ""
        self.parse_errors = 0
        self.last_parse_error = ""
        self._observed_ts = 0.0

    def feed(self, line: str):
        raise NotImplementedError

    def _observe(self, line: str, arrival_ts: float):
        """读取通用元数据；具体 watcher 仍负责解释自己的事件。"""
        self._observed_ts = arrival_ts
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            return
        if not isinstance(obj, dict):
            return
        containers = [obj]
        for key in ("payload", "params", "thread", "turn", "message", "item"):
            value = obj.get(key)
            if isinstance(value, dict):
                containers.append(value)

        for item in containers:
            event_ts = parse_ts(item.get("timestamp"))
            if event_ts:
                self._observed_ts = event_ts
            self.last_type = _as_text(item.get("type") or self.last_type)
            if not self.session_id:
                self.session_id = _as_text(
                    item.get("session_id") or item.get("sessionId"))
            if not self.thread_id:
                self.thread_id = _as_text(
                    item.get("thread_id") or item.get("threadId"))
            if not self.turn_id:
                self.turn_id = _as_text(
                    item.get("turn_id") or item.get("turnId"))
            if not self.cwd:
                self.cwd = _as_text(item.get("cwd") or item.get("workingDirectory"))
            if not self.goal:
                self.goal = _as_text(
                    item.get("goal") or item.get("task") or item.get("title"))
            if not self.started_at:
                self.started_at = parse_ts(
                    item.get("started_at") or item.get("startTime") or item.get("created_at"))

        self.last_event_ts = max(self.last_event_ts, self._observed_ts)

    def poll_lines(self):
        arrival = time.time()
        for line in self.tailer.poll():
            self._observe(line, arrival)
            try:
                self.feed(line)
            except Exception as exc:
                # 一条坏记录不应让整个会话静默消失；保留有界诊断供日志层使用。
                self.parse_errors += 1
                self.last_parse_error = type(exc).__name__
            event_ts = self._observed_ts or arrival
            self.last_event_ts = max(self.last_event_ts, event_ts)

    def status(self, now: float, cfg: dict) -> tuple[Status, object]:
        raise NotImplementedError

    def fill_snapshot(self, snap: Snapshot):
        """旧 watcher 未实现扩展字段时仍产出合理的保守快照。"""
        snap.session_id = self.session_id or snap.session_id
        snap.turn_id = self.turn_id or snap.turn_id
        snap.cwd = self.cwd or snap.cwd
        snap.goal = self.goal or snap.goal
        snap.phase = self.phase or snap.phase
        snap.summary = snap.last_line or snap.summary


class BaseWatcher:
    kind: AgentKind = None  # type: ignore

    def __init__(self, monitor_cfg: dict):
        self.cfg = monitor_cfg or {}
        self.files: dict[str, FileState] = {}
        self._file_source: dict[str, str] = {}
        self._source_files: dict[str, list[tuple[float, str]]] = {}
        self._source_last_scan: dict[str, float] = {}
        self._source_scan_errors: dict[str, int] = {}
        self._instance_files: dict[str, str] = {}

    # ---- 文件发现与绑定 ----
    def _scan_interval(self) -> float:
        value = self.cfg.get("file_scan_sec", self.cfg.get("directory_scan_sec", DEFAULT_SCAN_SEC))
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            return DEFAULT_SCAN_SEC

    def _roots_for_source(self, source: str) -> list[str]:
        if source == "windows":
            return paths.windows_roots(self.kind)
        if source.startswith("wsl:"):
            return paths.wsl_roots(self.kind, source.split(":", 1)[1])
        return []

    def _bindings(self) -> dict:
        value = self.cfg.get("session_bindings", {})
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _instance_aliases(inst) -> list[str]:
        aliases = []
        key = _as_text(getattr(inst, "key", ""))
        if key:
            aliases.append(key)
        source = _as_text(getattr(inst, "source", ""))
        kind = getattr(getattr(inst, "kind", None), "value", getattr(inst, "kind", ""))
        pid = getattr(inst, "pid", "")
        if source and kind and pid not in (None, ""):
            aliases.append(f"{source}|{kind}|{pid}")
        sid = _as_text(getattr(inst, "session_id", ""))
        if sid:
            aliases.append(sid)
        return aliases

    def _binding_value(self, inst):
        bindings = self._bindings()
        source = _as_text(getattr(inst, "source", ""))
        for alias in self._instance_aliases(inst):
            value = bindings.get(alias)
            if value is not None:
                return value
        # 也接受按 source 分组的配置：{source: {instance_key: binding}}。
        grouped = bindings.get(source)
        if isinstance(grouped, dict):
            for alias in self._instance_aliases(inst):
                if alias in grouped:
                    return grouped[alias]
        return None

    @staticmethod
    def _binding_parts(value) -> dict[str, str]:
        if isinstance(value, str):
            return {"path": value}
        if not isinstance(value, dict):
            return {}
        out = {}
        for dest, keys in {
            "path": ("path", "file", "session_file"),
            "session_id": ("session_id", "sessionId", "id", "thread_id", "threadId"),
            "cwd": ("cwd", "workdir", "working_directory"),
            "started_at": ("started_at", "start_time", "created_at"),
        }.items():
            for key in keys:
                if value.get(key) not in (None, ""):
                    out[dest] = _as_text(value[key])
                    break
        return out

    def refresh_files(self, sources: list[str], instances: list | None = None,
                      force: bool = False):
        """低频刷新目录候选；已绑定文件仍由 poll() 每轮增量读取。"""
        now = time.time()
        sources = sorted(set(sources))
        interval = self._scan_interval()
        for source in sources:
            due = (force or source not in self._source_files or
                   now - self._source_last_scan.get(source, 0.0) >= interval)
            if not due:
                continue
            try:
                active = paths.session_files(
                    self.kind, self._roots_for_source(source),
                    self.cfg.get("active_file_window_sec", 180))
            except Exception:
                # 失败时保留该来源的旧候选，不能用空列表覆盖它。
                self._source_scan_errors[source] = self._source_scan_errors.get(source, 0) + 1
                continue
            self._source_files[source] = list(active or [])
            self._source_last_scan[source] = now
            self._source_scan_errors.pop(source, None)

        current_keys = {_as_text(getattr(i, "key", "")) for i in (instances or [])}
        bound_paths = {
            p for key, p in self._instance_files.items() if key in current_keys and p in self.files
        }
        # 手动绑定的路径即使长时间没有 mtime 变化，也必须保留。
        manual_paths = set()
        for inst in instances or []:
            parts = self._binding_parts(self._binding_value(inst))
            path = parts.get("path", "")
            if path and os.path.isfile(path):
                manual_paths.add(path)

        wanted: dict[str, str] = {}
        for source in sources:
            candidates = self._source_files.get(source, [])
            for _mtime, path in candidates[:MAX_TRACKED_FILES]:
                wanted[path] = source
        for path in bound_paths | manual_paths:
            source = self._file_source.get(path, "")
            if not source:
                for inst in instances or []:
                    if self._binding_parts(self._binding_value(inst)).get("path") == path:
                        source = _as_text(getattr(inst, "source", ""))
                        break
            wanted[path] = source

        # 仅移除不再活跃且没有任何绑定证据的文件状态。
        for path in list(self.files):
            if path not in wanted:
                self.files[path].tailer.close()
                self.files.pop(path, None)
                self._file_source.pop(path, None)

        for path, source in wanted.items():
            if path in self.files:
                continue
            st = self.make_state(path)
            st.source = source
            st.file_id = st.file_id or _path_session_id(path)
            try:
                size = os.path.getsize(path)
                st.tailer.pos = 0 if size < 512 * 1024 else _near_end(path)
                st.last_mtime = os.stat(path).st_mtime
            except OSError:
                st.tailer.pos = 0
            self.files[path] = st
            self._file_source[path] = source

    def _candidate_score(self, inst, st: FileState) -> int:
        source = _as_text(getattr(inst, "source", ""))
        if source and st.source and source != st.source:
            return -1
        score = 0
        binding = self._binding_parts(self._binding_value(inst))
        if binding.get("path"):
            if os.path.normcase(os.path.normpath(binding["path"])) == os.path.normcase(os.path.normpath(st.path)):
                score += 10000
            elif binding["path"] in (st.file_id, st.session_id):
                score += 9000
        sid = _as_text(getattr(inst, "session_id", ""))
        if sid and sid in {st.session_id, st.file_id}:
            score += 5000
        bind_sid = binding.get("session_id", "")
        if bind_sid and bind_sid in {st.session_id, st.file_id}:
            score += 4000
        inst_cwd = _norm_cwd(getattr(inst, "cwd", ""))
        st_cwd = _norm_cwd(getattr(st, "cwd", ""))
        bind_cwd = _norm_cwd(binding.get("cwd", ""))
        if inst_cwd and st_cwd and inst_cwd == st_cwd:
            score += 1000
        if bind_cwd and st_cwd and bind_cwd == st_cwd:
            score += 900
        inst_start = _number(getattr(inst, "started_at", 0.0))
        st_start = _number(getattr(st, "started_at", 0.0))
        bind_start = _number(binding.get("started_at", 0.0))
        if inst_start and st_start and abs(inst_start - st_start) <= 180:
            score += 500
        if bind_start and st_start and abs(bind_start - st_start) <= 180:
            score += 450
        return score

    def _assign_files(self, instances: list):
        """在同一来源内建立稳定一对一绑定，证据不足时明确保持未绑定。"""
        live = {_as_text(getattr(i, "key", "")): i for i in instances}
        used: set[str] = set()
        mapping: dict[str, str] = {}

        # 先保留旧绑定，避免每次目录刷新重新配对。
        for key, path in self._instance_files.items():
            inst = live.get(key)
            st = self.files.get(path)
            if inst is not None and st is not None and st.source == getattr(inst, "source", ""):
                mapping[key] = path
                used.add(path)

        pending = [i for key, i in live.items() if key not in mapping]
        candidates = [st for st in self.files.values() if st.path not in used]
        for inst in pending:
            scored = [(self._candidate_score(inst, st), st) for st in candidates
                      if self._candidate_score(inst, st) >= 0]
            scored.sort(key=lambda pair: pair[0], reverse=True)
            if not scored:
                continue
            best_score = scored[0][0]
            tied = [st for score, st in scored if score == best_score]
            # 明确证据必须唯一；无证据时只允许唯一实例/唯一候选的安全退化。
            if len(tied) != 1:
                continue
            source = _as_text(getattr(inst, "source", ""))
            same_source_pending = [item for item in pending
                                   if _as_text(getattr(item, "source", "")) == source]
            same_source_candidates = [item for item in candidates
                                      if not item.source or item.source == source]
            if best_score <= 0 and not (
                    len(same_source_pending) == 1 and len(same_source_candidates) == 1):
                continue
            chosen = tied[0]
            key = _as_text(getattr(inst, "key", ""))
            mapping[key] = chosen.path
            used.add(chosen.path)
            candidates = [st for st in candidates if st.path != chosen.path]

        self._instance_files = mapping

    def make_state(self, path: str) -> FileState:
        raise NotImplementedError

    def poll(self, instances: list) -> list[Snapshot]:
        if not instances:
            # Once discovery has confirmed that no process is alive, release
            # the tailers and their bounded parser state.  Monitor keeps
            # disappeared processes during its grace window, so a transient
            # scan miss does not discard a live binding here.
            for state in self.files.values():
                state.tailer.close()
            self.files.clear()
            self._file_source.clear()
            self._instance_files = {}
            return []
        self.refresh_files(sorted({i.source for i in instances}), instances)
        now = time.time()
        for st in self.files.values():
            st.poll_lines()
            st.last_poll_seen = now
            mtime = _file_mtime(st.path)
            if mtime:
                # mtime is a useful freshness hint for the UI, but it is not
                # an event timestamp.  Mixing it into last_event_ts makes a
                # file containing a historical completion look active merely
                # because it was copied or restored today.
                st.last_mtime = max(st.last_mtime, mtime)
        self._assign_files(instances)

        snaps: list[Snapshot] = []
        for inst in sorted(instances, key=lambda item: _as_text(getattr(item, "key", ""))):
            key = _as_text(getattr(inst, "key", ""))
            path = self._instance_files.get(key, "")
            st = self.files.get(path) if path else None
            snap = Snapshot(
                key=key, kind=inst.kind, source=inst.source, pid=inst.pid,
                session_file=path,
                session_id=_as_text(getattr(inst, "session_id", "")),
                cwd=_as_text(getattr(inst, "cwd", "")),
                connection="readonly",
                # Keep completion detection tied to the record's timestamp;
                # Snapshot's dataclass default is wall clock time and would
                # make every historical DONE replay look newly completed.
                ts=0.0,
                freshness=0.0,
            )
            if st is None:
                snap.status = Status.UNKNOWN
                snap.last_line = "进程存活，但未找到可唯一绑定的会话文件"
                snap.summary = snap.last_line
            else:
                if not st.last_event_ts:
                    # Empty files or files whose first write is still a
                    # partial line cannot establish an Agent state.
                    snap.status = Status.UNKNOWN
                    snap.last_line = "会话文件尚无完整事件"
                    snap.summary = snap.last_line
                else:
                    status, extra = st.status(now, self.cfg)
                    snap.status = status
                    st.fill_snapshot(snap)
                    if extra is not None:
                        snap.approval = extra
                        snap.exact_waiting = bool(getattr(extra, "exact", False))
                snap.freshness = st.last_event_ts or st.last_mtime
                snap.ts = snap.freshness
                # 只读 tail 没有可以安全写回的协议审批通道。
                snap.can_approve = False
            snaps.append(snap)
        return snaps


def _norm_cwd(value) -> str:
    text = _as_text(value)
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(text.rstrip("/\\")))


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _file_mtime(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _near_end(path: str) -> int:
    """大文件从尾部约 256KB 处开始读，避免回放全部历史。"""
    try:
        size = os.path.getsize(path)
        return max(0, size - 256 * 1024)
    except OSError:
        return 0


def jdump(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)
