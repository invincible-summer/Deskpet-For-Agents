"""Watcher 基类：把"发现到的进程实例"与"活跃会话文件"按新旧顺序配对并产出快照。"""
import json
import time
from datetime import datetime, timezone

from . import paths
from .models import AgentKind, Snapshot, Status
from .tailer import FileTailer

MAX_TRACKED_FILES = 4


def parse_ts(value) -> float:
    """ISO8601 → epoch；失败返回当前时间。"""
    if not value:
        return time.time()
    try:
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return time.time()


def trunc(s: str, n: int = 120) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


class FileState:
    """每个会话文件的解析状态，由具体 watcher 实现。"""

    def __init__(self, path: str):
        self.path = path
        self.tailer = FileTailer(path)
        self.last_event_ts = time.time()
        self.last_poll_seen = 0.0

    def feed(self, line: str):
        raise NotImplementedError

    def poll_lines(self):
        for line in self.tailer.poll():
            try:
                self.feed(line)
            except Exception:
                continue

    def status(self, now: float, cfg: dict) -> tuple[Status, object]:
        raise NotImplementedError


class BaseWatcher:
    kind: AgentKind = None  # type: ignore

    def __init__(self, monitor_cfg: dict):
        self.cfg = monitor_cfg
        self.files: dict[str, FileState] = {}

    def refresh_files(self, sources: list[str]):
        roots = []
        if "windows" in sources:
            roots += paths.windows_roots(self.kind)
        wsl = sorted({s.split(":", 1)[1] for s in sources if s.startswith("wsl:")})
        for d in wsl:
            roots += paths.wsl_roots(self.kind, d)
        active = paths.session_files(self.kind, roots, self.cfg.get("active_file_window_sec", 180))
        keep = {p for _, p in active[:MAX_TRACKED_FILES]}
        for p in list(self.files):
            if p not in keep:
                del self.files[p]
        import os
        for _mtime, p in active[:MAX_TRACKED_FILES]:
            if p not in self.files:
                st = self.make_state(p)
                # 小文件从头读全量，大文件只从尾部 256KB 开始，避免回放历史
                try:
                    st.tailer.pos = 0 if os.path.getsize(p) < 512 * 1024 else _near_end(p)
                except OSError:
                    st.tailer.pos = 0
                self.files[p] = st

    def make_state(self, path: str) -> FileState:
        raise NotImplementedError

    def poll(self, instances: list) -> list[Snapshot]:
        if not instances:
            return []
        self.refresh_files(sorted({i.source for i in instances}))
        now = time.time()
        for st in self.files.values():
            st.poll_lines()
            st.last_poll_seen = now
        # 实例按启动时间新→旧，文件按 mtime 新→旧，按序配对
        insts = sorted(instances, key=lambda i: i.started_at, reverse=True)
        file_states = sorted(self.files.values(), key=lambda s: s.last_event_ts, reverse=True)
        snaps: list[Snapshot] = []
        for i, inst in enumerate(insts):
            snap = Snapshot(
                key=inst.key, kind=inst.kind, source=inst.source, pid=inst.pid,
                session_file=file_states[i].path if i < len(file_states) else "",
            )
            if i < len(file_states):
                st = file_states[i]
                st.last_event_ts = max(st.last_event_ts, _file_mtime(st.path) or now)
                status, extra = st.status(now, self.cfg)
                snap.status = status
                st.fill_snapshot(snap)
                if extra is not None:
                    snap.approval = extra
            else:
                snap.status = Status.UNKNOWN
                snap.last_line = "进程存活，但未找到活跃会话文件"
            snaps.append(snap)
        return snaps


def _file_mtime(path: str) -> float | None:
    try:
        import os
        return os.stat(path).st_mtime
    except OSError:
        return None


def _near_end(path: str) -> int:
    """大文件从尾部约 256KB 处开始读，避免首尾全量回放。"""
    import os
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
