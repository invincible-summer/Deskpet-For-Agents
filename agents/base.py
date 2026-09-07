"""Watcher 基类：增量读取会话文件并建立保守、可解释的实例绑定。

进程发现和会话文件发现是两条不可靠的只读观察线。绑定只使用来源、
明确会话 ID、工作目录（V3 起来自 /proc/<pid>/cwd 的真实值）、启动时间
等证据；证据不足时保留 UNKNOWN，不乱绑（plan.md §10/§34/§35）。

V3 变化：
  * watcher 产出 Observation（带证据/置信度/TTL），由 StateReducer 融合。
  * 会话目录扫描：有未绑定 Agent 时 1~3s；全部绑定后 10~15s。
  * late-start fallback：DeskPet 晚于 Agent 启动时，允许一次无时间窗的
    最近 N 候选扫描 + cwd/session 评分，不无限递归全 HOME。
  * 手工绑定 JSONL 已取消；高级诊断的运行期临时 override 不落盘。
"""
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import paths
from .matching import mutual_unique_matches
from .models import (
    AgentKind,
    Confidence,
    EvidenceSource,
    Mode,
    Observation,
    Phase,
    Status,
)
from .tailer import FileTailer

MAX_TRACKED_FILES = 8
DEFAULT_SCAN_SEC = 3.0        # 有未绑定实例时的解析周期
BOUND_RESAN_SEC = 15.0        # 全部绑定后的目录重扫周期
FALLBACK_RETRY_SEC = 15.0     # late-start fallback 的重试周期
GOAL_MAX = 120
SUMMARY_MAX = 160


def parse_ts(value) -> float:
    """ISO8601/epoch → epoch；缺失或非法值返回 0，不伪造当前时间。"""
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


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


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

    kind: AgentKind = None  # type: ignore[assignment]

    # 解析器兼容性诊断：子类可声明 RECORD_TYPES（已知记录类型集合），
    # 未声明的 watcher 一律视为全部可识别（health=OK）。
    RECORD_TYPES: frozenset | None = None

    def __init__(self, path: str):
        self.path = path
        self.tailer = FileTailer(path)
        self.source = ""
        self.file_id = _path_session_id(path)
        self.session_id = ""
        self.thread_id = ""
        self.turn_id = ""
        self.cwd = ""
        self.title = ""
        self.phase = Phase.NONE
        self.mode = Mode.NONE
        self.mode_raw = ""
        self.goal = ""
        self.started_at = 0.0
        self.last_event_ts = 0.0
        self.last_arrival_ts = 0.0
        self.last_poll_seen = 0.0
        self.last_mtime = 0.0
        self.last_type = ""
        self.parse_errors = 0
        self.last_parse_error = ""
        # 兼容性健康计数（只记类型名与计数，绝不保存事件内容）
        self.records_seen = 0
        self.recognized_records = 0
        self.unknown_types: dict[str, int] = {}
        self._observed_ts = 0.0

    def feed(self, line: str):
        raise NotImplementedError

    # ---- 解析器兼容性 ----
    def _record_type(self, obj: dict) -> str:
        """记录的规范类型名（子类可覆盖以展开 payload/event 内层类型）。"""
        return str(obj.get("type") or "?")

    def _count_record(self, obj: dict):
        self.records_seen += 1
        known = self.RECORD_TYPES
        if known is None:
            self.recognized_records += 1
            return
        rtype = self._record_type(obj)
        if rtype in known:
            self.recognized_records += 1
        elif len(self.unknown_types) < 8:
            self.unknown_types[rtype] = self.unknown_types.get(rtype, 0) + 1

    def _observe(self, obj, arrival_ts: float):
        """读取通用元数据；具体 watcher 仍负责解释自己的事件。"""
        self._observed_ts = arrival_ts
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
            if not self.started_at:
                self.started_at = parse_ts(
                    item.get("started_at") or item.get("startTime") or item.get("created_at"))

        self.last_event_ts = max(self.last_event_ts, self._observed_ts)

    def poll_lines(self):
        arrival = time.time()
        got = False
        for line in self.tailer.poll():
            got = True
            self.last_arrival_ts = arrival
            try:
                obj = json.loads(line)
            except (TypeError, ValueError):
                obj = None
            if isinstance(obj, dict):
                self._count_record(obj)
            self._observe(obj, arrival)
            try:
                self.feed(line)
            except Exception:
                # 一条坏记录不应让整个会话静默消失；保留有界诊断。
                self.parse_errors += 1
            event_ts = self._observed_ts or arrival
            self.last_event_ts = max(self.last_event_ts, event_ts)
        if got:
            self.last_arrival_ts = arrival

    # ------------------------------------------------------------------
    # 子类需要的生命周期标记（由具体 watcher 维护）：
    #   turn_active      已知 active turn（WORKING 不过期）
    #   turn_known_over  明确见过 turn 结束（IDLE 的前提）
    #   error_ts/input_pending/pending 等按 agent 而定
    turn_active = False
    turn_known_over = False
    error_ts = 0.0
    input_pending = False

    def activity_grace(self, cfg: dict) -> float:
        try:
            return max(1.0, float(cfg.get("activity_grace_sec", 10.0)))
        except (TypeError, ValueError):
            return 10.0

    def observation(self, now: float, cfg: dict) -> Observation | None:
        """把文件状态转成证据观察；无法建立状态时返回 None。"""
        raise NotImplementedError

    def base_observation(self) -> Observation:
        return Observation(
            source=EvidenceSource.SESSION,
            timestamp=self.last_event_ts or self.last_arrival_ts,
            session_id=self.session_id,
            session_file=self.path,
            cwd=self.cwd,
            title=self.title,
        )


@dataclass
class ParserDiagnostics:
    """会话解析器的兼容性健康（只含类型名与计数，无事件内容）。"""
    bound: bool
    parse_errors: int
    records_seen: int
    recognized_records: int
    unknown_types: tuple[str, ...]
    health: str   # OK / PARTIAL / UNKNOWN


class BaseWatcher:
    kind: AgentKind = None  # type: ignore[assignment]

    def __init__(self, monitor_cfg: dict):
        self.cfg = monitor_cfg or {}
        self.files: dict[str, FileState] = {}
        self._file_source: dict[str, str] = {}
        self._file_cwd_hint: dict[str, str] = {}      # path → 已知 cwd（如 Kimi 索引）
        self._source_files: dict[str, list[tuple[float, str]]] = {}
        self._source_last_scan: dict[str, float] = {}
        self._source_last_fallback: dict[str, float] = {}
        self._source_scan_errors: dict[str, int] = {}
        self._instance_files: dict[str, str] = {}
        self._runtime_bindings: dict[str, str] = {}   # 运行期临时 override（不落盘）

    # ------------------------------------------------------- 子类 hook
    def instance_hints(self, inst) -> dict[str, str]:
        """Agent 特定的强 hint（如 Claude PID registry 的 sessionId）。"""
        return {}

    def extra_candidates(self, source: str, instances: list) -> list[tuple[float, str]]:
        """Agent 特定的候选注入（如 Kimi session_index）。"""
        return []

    def revalidate_bindings(self, instances: list, now: float):
        """绑定后校验 hook（Claude 用于 /clear 后切换新 transcript）。"""

    # ------------------------------------------------------- 文件发现
    def _scan_interval(self, unbound: bool) -> float:
        if unbound:
            value = self.cfg.get("session_scan_sec",
                                 self.cfg.get("file_scan_sec", DEFAULT_SCAN_SEC))
        else:
            value = self.cfg.get("directory_rescan_sec", BOUND_RESAN_SEC)
        try:
            return max(1.0, float(value))
        except (TypeError, ValueError):
            return DEFAULT_SCAN_SEC if unbound else BOUND_RESAN_SEC

    def _roots_for_source(self, source: str, instances: list) -> list[str]:
        if source == "windows":
            return paths.windows_roots(self.kind)
        if source.startswith("wsl:"):
            roots: list[str] = []
            seen = set()
            for inst in instances:
                if _as_text(getattr(inst, "source", "")) != source:
                    continue
                for root in paths.instance_roots(inst, self.kind):
                    if root.lower() not in seen:
                        seen.add(root.lower())
                        roots.append(root)
            if roots:
                return roots
            # 元数据缺失时退回默认 /home/<default-user> 布局不可靠，
            # 保持为空——诚实呈现"未解析"胜过扫全 HOME。
            return []
        return []

    def _session_candidates(self, source: str, instances: list,
                            window_sec: float | None) -> list[tuple[float, str]]:
        roots = self._roots_for_source(source, instances)
        try:
            found = list(paths.session_files(self.kind, roots, window_sec))
        except Exception:
            self._source_scan_errors[source] = self._source_scan_errors.get(source, 0) + 1
            return []
        extra = []
        try:
            extra = [c for c in self.extra_candidates(source, instances)
                     if c[1] not in {p for _m, p in found}]
        except Exception:
            extra = []
        return found + extra

    def refresh_files(self, sources: list[str], instances: list | None = None,
                      force: bool = False):
        """低频刷新目录候选；已绑定文件仍由 poll() 每轮增量读取。"""
        now = time.time()
        sources = sorted(set(sources))
        instances = list(instances or [])
        current_keys = {_as_text(getattr(i, "key", "")) for i in instances}
        unbound = [k for k in current_keys if k not in self._instance_files]
        has_unbound = bool(unbound)
        interval = self._scan_interval(has_unbound)
        window = self.cfg.get("active_file_window_sec", 180)

        for source in sources:
            due = (force or source not in self._source_files or
                   now - self._source_last_scan.get(source, 0.0) >= interval)
            if not due:
                continue
            try:
                active = self._session_candidates(source, instances, window)
            except Exception:
                self._source_scan_errors[source] = self._source_scan_errors.get(source, 0) + 1
                continue
            # late-start fallback（plan §34）：仍有未绑定实例时，周期性合并
            # 无时间窗的最近 N 候选（DeskPet 可能晚于 Agent 启动很久）。
            if has_unbound and now - self._source_last_fallback.get(source, 0.0) >= FALLBACK_RETRY_SEC:
                self._source_last_fallback[source] = now
                try:
                    extra = self._session_candidates(source, instances, None)
                except Exception:
                    extra = []
                known = {p for _m, p in active}
                active = active + [c for c in extra if c[1] not in known]
            self._source_files[source] = list(active or [])
            self._source_last_scan[source] = now
            self._source_scan_errors.pop(source, None)

        bound_paths = {
            p for key, p in self._instance_files.items()
            if key in current_keys and p in self.files
        }
        manual_paths = set()
        for inst in instances:
            path = self._runtime_bindings.get(_as_text(getattr(inst, "key", "")))
            if path and os.path.isfile(path):
                manual_paths.add(path)

        wanted: dict[str, str] = {}
        for source in sources:
            candidates = self._source_files.get(source, [])
            for _mtime, path in candidates[:MAX_TRACKED_FILES]:
                wanted[path] = source
        for path in bound_paths | manual_paths:
            wanted[path] = self._file_source.get(path, "")

        for path in list(self.files):
            if path not in wanted:
                self.files[path].tailer.close()
                self.files.pop(path, None)
                self._file_source.pop(path, None)
                self._file_cwd_hint.pop(path, None)

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

    # ------------------------------------------------------- 绑定评分
    def _candidate_score(self, inst, st: FileState) -> int:
        source = _as_text(getattr(inst, "source", ""))
        if source and st.source and source != st.source:
            return -1
        score = 0
        runtime = self._runtime_bindings.get(_as_text(getattr(inst, "key", "")))
        if runtime:
            if _same_path(runtime, st.path):
                score += 10000
        sid = _as_text(getattr(inst, "session_id", ""))
        if sid and sid in {st.session_id, st.file_id}:
            score += 5000
        hint = self.instance_hints(inst)
        hint_sid = _as_text(hint.get("session_id"))
        if hint_sid and hint_sid in {st.session_id, st.file_id}:
            # PID registry 是强 hint 但非真值：要求 cwd 兼容（plan §12）。
            inst_cwd = _norm_cwd(getattr(inst, "cwd", ""))
            st_cwd = _norm_cwd(st.cwd or self._file_cwd_hint.get(st.path, ""))
            if not inst_cwd or not st_cwd or inst_cwd == st_cwd:
                score += 4000
        inst_cwd = _norm_cwd(getattr(inst, "cwd", ""))
        st_cwd = _norm_cwd(st.cwd or self._file_cwd_hint.get(st.path, ""))
        if inst_cwd and st_cwd and inst_cwd == st_cwd:
            score += 1000
        inst_start = _number(getattr(inst, "started_at", 0.0))
        st_start = _number(getattr(st, "started_at", 0.0))
        if inst_start and st_start and abs(inst_start - st_start) <= 180:
            score += 500
        return score

    def _assign_files(self, instances: list):
        """在同一来源内建立稳定一对一绑定，证据不足时明确保持未绑定。

        V3.1：用互相唯一匹配（matching.mutual_unique_matches）替代逐实例
        greedy——绑定结果不依赖实例遍历顺序；0 分退化（同 source 恰好
        1 实例 + 1 候选）仍单独处理。
        """
        live = {_as_text(getattr(i, "key", "")): i for i in instances}
        used: set[str] = set()
        mapping: dict[str, str] = {}

        for key, path in self._instance_files.items():
            inst = live.get(key)
            st = self.files.get(path)
            if inst is not None and st is not None and st.source == getattr(inst, "source", ""):
                mapping[key] = path
                used.add(path)

        pending = [i for key, i in live.items() if key not in mapping]
        candidates = [st for st in self.files.values() if st.path not in used]
        matrix = {
            (id(inst), st.path): self._candidate_score(inst, st)
            for inst in pending for st in candidates
        }
        decisions = mutual_unique_matches(
            [id(i) for i in pending], [st.path for st in candidates],
            lambda iid, path: matrix.get((iid, path), -1),
            min_score=1, min_margin=1)
        inst_by_id = {id(i): i for i in pending}
        for iid, dec in decisions.items():
            inst = inst_by_id[iid]
            key = _as_text(getattr(inst, "key", ""))
            mapping[key] = dec.right
            used.add(dec.right)

        # 0 分 fallback：同 source 恰好剩余 1 个未绑定实例 + 1 个未用候选
        # 才允许无证据绑定（plan §10 的安全退化）。
        remaining_pending = [i for i in pending
                             if _as_text(getattr(i, "key", "")) not in mapping]
        remaining_candidates = [st for st in candidates if st.path not in used]
        grouped: dict[str, list] = {}
        for inst in remaining_pending:
            grouped.setdefault(_as_text(getattr(inst, "source", "")), []).append(inst)
        for source, items in grouped.items():
            compat = [st for st in remaining_candidates
                      if not st.source or st.source == source]
            if len(items) == 1 and len(compat) == 1:
                key = _as_text(getattr(items[0], "key", ""))
                mapping[key] = compat[0].path
                used.add(compat[0].path)

        self._instance_files = mapping

    def set_runtime_binding(self, key: str, path: str):
        """高级诊断的运行期临时 override（plan §39）：不写永久配置。"""
        if path and os.path.isfile(path):
            self._runtime_bindings[key] = path
            self._instance_files.pop(key, None)
        else:
            self._runtime_bindings.pop(key, None)

    def diagnostics_for(self, key: str) -> ParserDiagnostics:
        """该实例的会话解析器健康（UI 只看 Snapshot 上的投影，不碰 watcher）。"""
        path = self._instance_files.get(key, "")
        st = self.files.get(path) if path else None
        if st is None:
            return ParserDiagnostics(bound=False, parse_errors=0,
                                     records_seen=0, recognized_records=0,
                                     unknown_types=(), health="UNKNOWN")
        unknown = tuple(sorted(st.unknown_types))
        if st.unknown_types or st.parse_errors > 3:
            health = "PARTIAL"
        else:
            health = "OK"
        return ParserDiagnostics(bound=True, parse_errors=st.parse_errors,
                                 records_seen=st.records_seen,
                                 recognized_records=st.recognized_records,
                                 unknown_types=unknown, health=health)

    def reset_scan_cache(self):
        """清空目录扫描缓存，让下一次 poll 立即重扫（重新扫描入口）。"""
        self._source_files.clear()
        self._source_last_scan.clear()
        self._source_last_fallback.clear()

    def make_state(self, path: str) -> FileState:
        raise NotImplementedError

    # ------------------------------------------------------- 主入口
    def poll(self, instances: list) -> dict[str, Observation]:
        """产出每个实例的会话观察（None 值以 status=None 的占位观察表示）。"""
        if not instances:
            for state in self.files.values():
                state.tailer.close()
            self.files.clear()
            self._file_source.clear()
            self._file_cwd_hint.clear()
            self._instance_files = {}
            self._runtime_bindings.clear()
            return {}
        # 运行期手动绑定也要有生命周期：实例退出/文件消失后清理
        live_keys = {_as_text(getattr(i, "key", "")) for i in instances}
        for key in list(self._runtime_bindings):
            if key not in live_keys:
                self._runtime_bindings.pop(key, None)
        self.refresh_files(sorted({i.source for i in instances}), instances)
        now = time.time()
        for st in self.files.values():
            st.poll_lines()
            st.last_poll_seen = now
            mtime = _file_mtime(st.path)
            if mtime:
                # mtime 是 UI 的新鲜度提示，不是事件时间；不能混入
                # last_event_ts（否则恢复的旧完成文件会被当成活跃）。
                st.last_mtime = max(st.last_mtime, mtime)
        self._assign_files(instances)
        try:
            self.revalidate_bindings(instances, now)
        except Exception:
            pass

        out: dict[str, Observation] = {}
        for inst in sorted(instances, key=lambda item: _as_text(getattr(item, "key", ""))):
            key = _as_text(getattr(inst, "key", ""))
            path = self._instance_files.get(key, "")
            st = self.files.get(path) if path else None
            placeholder = Observation(
                source=EvidenceSource.SESSION, timestamp=now,
                status=None, session_bound=False, cwd=inst.cwd,
                session_id=_as_text(getattr(inst, "session_id", "")),
            )
            if st is None:
                placeholder.summary = "Session：未解析"
                out[key] = placeholder
                continue
            if not st.last_event_ts and not st.last_arrival_ts:
                placeholder.summary = "会话文件尚无完整事件"
                out[key] = placeholder
                continue
            obs = st.observation(now, self.cfg)
            if obs is None:
                placeholder.summary = "状态暂不可读"
                out[key] = placeholder
            else:
                obs.session_id = obs.session_id or st.session_id
                obs.session_file = obs.session_file or st.path
                obs.cwd = obs.cwd or st.cwd
                obs.title = obs.title or st.title
                obs.session_bound = True
                out[key] = obs
                # 回填实例 session_id，供下轮绑定评分使用。
                if st.session_id and not getattr(inst, "session_id", ""):
                    try:
                        inst.session_id = st.session_id
                    except Exception:
                        pass
        return out

    def release(self):
        for state in self.files.values():
            state.tailer.close()
        self.files.clear()
        self._file_source.clear()
        self._file_cwd_hint.clear()
        self._instance_files = {}


def _same_path(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))
    except (TypeError, ValueError):
        return False


def _norm_cwd(value) -> str:
    text = _as_text(value)
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(text.rstrip("/\\")))


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


# ------------------------------------------------- 工具 → Phase 映射

import re as _re

_TEST_WORDS = _re.compile(r"\b(tests?|pytest|unittest|vitest|jest)\b")
_READ_WORDS = _re.compile(r"\b(grep|rg|ripgrep|search|find|glob|query|read|reads|cat|head|tail|list|ls|stat|inspect)\b")
_CODE_WORDS = _re.compile(r"\b(edit|edits|patch|replace|apply_?patch|modify|write|writes|create|save|delete)\b")
_EXEC_WORDS = _re.compile(r"\b(bash|shell|command|commands|exec|execute|run|terminal|make|build|task|agent|delegate|spawn)\b")


def classify_phase(name: str, detail: str = "") -> Phase:
    """工具名/命令 → V3 Phase（plan.md §11；UI 再本地化）。"""
    raw = " ".join(str(name or "").replace("_", " ").replace("-", " ").split()).lower()
    detail_low = " ".join(str(detail or "").split()).lower()
    joined = f"{raw} {detail_low}"
    if _TEST_WORDS.search(joined):
        return Phase.TESTING
    if _READ_WORDS.search(joined):
        return Phase.READING
    if _CODE_WORDS.search(joined):
        return Phase.CODING
    if _EXEC_WORDS.search(joined):
        return Phase.EXECUTING
    if raw:
        return Phase.EXECUTING
    return Phase.NONE
