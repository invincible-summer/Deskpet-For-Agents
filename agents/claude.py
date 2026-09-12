"""Claude Code watcher（V3 被动观察）。

已查证（code.claude.com 2026-09）：
  * 会话：$CLAUDE_CONFIG_DIR/projects/<改写路径>/<uuid>.jsonl（默认 ~/.claude）。
  * ~/.claude/sessions/<pid>.json 是 PID → sessionId 的强 hint，但 /clear 后
    已知会 stale（issue #53037）：只做 binding hint，不当唯一真值（plan §12）。
  * permissionMode 是正式 mode：default / acceptEdits / plan / auto / dontAsk /
    bypassPermissions（结构化 Plan 证据）。
  * conversation JSONL 不记录 permission prompt 与 allow/deny decision；
    未闭合 tool_use + 静默不能证明 WAITING（plan §6/§12）。
  * 上游存在"活跃 session transcript 不实时写出"的回归 → Claude 是
    Session+UIA 融合的主要案例（终端活动观察在 state.py 融合）。
"""
import json
import os
import time
from dataclasses import dataclass

from . import paths
from .base import BaseWatcher, FileState, _as_text, classify_phase, parse_ts
from .models import (
    AgentKind,
    Confidence,
    EvidenceSource,
    Mode,
    Observation,
    Phase,
    Status,
    parse_mode,
)
from .summarize import fmt_command, shorten, question_summary

GOAL_MAX = 120
SUMMARY_MAX = 160
# PID registry 指向旧 JSONL 后，绑定文件需连续 N 个扫描周期不增长、
# 且同 cwd 候选发生 N 次独立增长才自动脱离（v4.2.3 §6）。
_STALE_CONFIRM_CYCLES = 2
# 持续增长候选的 matcher 加分（< registry hint 4000，> cwd 1000）：
# 只在脱离 stale 绑定后参与评分，让 mutual-unique 确定性选择增长文件，
# 不做"直接强绑第一个增长候选"的 greedy。
_GROWTH_BONUS = 2000
_GROWTH_FRESH_SEC = 600.0
# stale hint 集合上限（运行期防御；实际大小 ≈ /clear 事件数）
_STALE_HINT_MAX = 32

# conversation JSONL 顶层记录类型（2026-09 查证）
_RECORD_TYPES = frozenset({
    "user", "assistant", "system", "result", "ai-title", "summary",
    "permission-mode", "permissionMode", "permission_mode",
    "input", "input_request", "request_user_input", "user_input_required",
    "error", "fatal_error", "turn_complete", "turn_finished", "?",
})


def _feed_ts(obj: dict) -> float:
    return parse_ts(obj.get("timestamp")) or time.time()


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("value") or "")
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    return ""


def _first_present(mapping: dict, *keys):
    """Read protocol fields without treating valid zero values as missing."""
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return ""


def _summarize_tool(block) -> tuple[str, str]:
    name = str(block.get("name", "?"))
    inp = block.get("input") or {}
    detail = ""
    if isinstance(inp, dict):
        for key in ("command", "file_path", "path", "pattern", "url", "description", "prompt"):
            if inp.get(key):
                detail = str(inp[key])
                break
    return name, detail


class ClaudeFile(FileState):
    kind = AgentKind.CLAUDE
    RECORD_TYPES = _RECORD_TYPES

    def __init__(self, path: str):
        super().__init__(path)
        self.title = ""
        self.goal = ""
        self.last_text = ""
        self.last_tool = ""
        self.open_tools: dict[str, str] = {}
        self.done_ts = 0.0
        self.error_text = ""
        self.error_ts = 0.0
        self.input_pending = False
        self.input_summary = ""
        self.last_assistant_ts = 0.0
        self.last_tool_ts = 0.0
        self.last_activity_kind = ""

    def _touch(self, obj: dict) -> float:
        ts = _feed_ts(obj)
        self.last_event_ts = max(self.last_event_ts, ts)
        sid = obj.get("session_id") or obj.get("sessionId")
        if sid and not self.session_id:
            self.session_id = str(sid)
        turn = obj.get("turn_id") or obj.get("turnId")
        if turn:
            self.turn_id = str(turn)
        cwd = obj.get("cwd") or obj.get("workingDirectory")
        if cwd:
            self.cwd = str(cwd)
        return ts

    def _set_error(self, obj: dict, ts: float):
        message = obj.get("message") or obj.get("error") or obj.get("reason")
        self.error_text = shorten(str(message or "Claude 报告错误"), SUMMARY_MAX)
        self.error_ts = ts
        self.turn_active = False
        self.turn_known_over = True
        self.input_pending = False
        self.done_ts = 0.0
        self.phase = Phase.NONE

    def _clear_error_transient(self):
        """比旧 error 更新的明确 session 活动使旧 ERROR 展示瞬态失效（plan1 R03）。"""
        self.error_text = ""
        self.error_ts = 0.0

    def _set_input(self, obj: dict):
        value = _first_present(obj, "question", "prompt", "message")
        self.input_pending = True
        self.input_summary = shorten(_text(value) or "等待输入", GOAL_MAX)
        self.phase = Phase.USER_INPUT

    def feed(self, line: str):
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            return
        ts = self._touch(obj)
        t = str(obj.get("type") or "")

        if t == "assistant":
            self.turn_active = True
            self.turn_known_over = False
            self.done_ts = 0.0
            self._clear_error_transient()
            msg = obj.get("message") or {}
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and _text(block.get("text")).strip():
                    self.last_text = shorten(_text(block.get("text")), SUMMARY_MAX)
                    self.last_assistant_ts = ts
                    self.last_activity_kind = "assistant"
                    self.phase = Phase.ANSWERING
                elif btype == "tool_use":
                    name, detail = _summarize_tool(block)
                    summary = shorten(f"{name}: {detail}" if detail else name, SUMMARY_MAX)
                    self.last_tool = summary
                    self.last_tool_ts = ts
                    self.last_activity_kind = "tool"
                    phase = classify_phase(name, detail)
                    self.phase = phase if phase is not Phase.NONE else Phase.EXECUTING
                    self.open_tools[str(block.get("id") or "")] = summary
                    if block.get("name") == "AskUserQuestion":
                        self.input_pending = True
                        self.input_summary = question_summary(block.get("input"), GOAL_MAX)
                        self.phase = Phase.USER_INPUT
            return

        if t == "user":
            self.input_pending = False
            msg = obj.get("message") or {}
            content = msg.get("content")
            blocks = content if isinstance(content, list) else []
            plain = [content] if isinstance(content, str) else []
            for block in blocks:
                if isinstance(block, dict):
                    if block.get("type") == "tool_result":
                        self.open_tools.pop(str(block.get("tool_use_id") or ""), None)
                    elif block.get("type") == "text":
                        plain.append(_text(block.get("text")))
                    elif "text" in block and not block.get("type"):
                        plain.append(_text(block.get("text")))
                elif isinstance(block, str):
                    plain.append(block)
            # 普通用户文本 → Goal（最新一次真实输入；plan §12）。
            # 新 turn 开始时同时失效旧 ERROR 瞬态（plan1 R03）；
            # 纯 tool_result block 不构成"新用户 Goal"，不清瞬态。
            text = shorten(" ".join(x for x in plain if x.strip()), GOAL_MAX)
            if text:
                self.goal = text
                self.turn_active = True
                self.turn_known_over = False
                self.done_ts = 0.0
                self._clear_error_transient()
            return

        if t == "ai-title":
            self.title = shorten(str(obj.get("aiTitle") or ""), 100)
        elif t == "summary":
            title = shorten(str(obj.get("summary") or ""), 100)
            self.title = self.title or title
        elif t in {"permission-mode", "permissionMode", "permission_mode"}:
            mode = obj.get("mode") or obj.get("permissionMode") or obj.get("permission_mode")
            self.mode, self.mode_raw = parse_mode(mode)
        elif t in {"input", "input_request", "request_user_input", "user_input_required"}:
            self._set_input(obj)
        elif t in {"error", "fatal_error"} or (t == "system" and obj.get("subtype") == "error"):
            self._set_error(obj, ts)
        elif t == "system" and obj.get("subtype") in {"turn_duration", "turn_complete", "turn_finished"}:
            # 使用记录里的时间，历史回放不能伪造“刚完成”。
            self.done_ts = ts
            self.turn_active = False
            self.turn_known_over = True
            self.input_pending = False
            self.open_tools.clear()
            self.phase = Phase.NONE
        elif t in {"result", "turn_complete", "turn_finished"}:
            self.done_ts = ts
            self.turn_active = False
            self.turn_known_over = True
            result = obj.get("result") or obj.get("message")
            if result:
                self.last_text = shorten(_text(result), SUMMARY_MAX)
                self.last_assistant_ts = ts
                self.last_activity_kind = "assistant"
            self.phase = Phase.NONE

    def _summary_text(self, status: Status) -> str:
        if status == Status.ERROR:
            return self.error_text or "Claude 报告错误"
        if status == Status.INPUT:
            return self.input_summary or "等待输入"
        if status == Status.DONE:
            return self.last_text or "回合完成"
        if self.last_activity_kind == "assistant" and self.last_text:
            return self.last_text
        if self.last_activity_kind == "tool" and self.last_tool:
            return self.last_tool
        if self.last_assistant_ts >= self.last_tool_ts and self.last_text:
            return self.last_text
        return self.last_tool or self.last_text

    def observation(self, now: float, cfg: dict) -> Observation | None:
        obs = self.base_observation()
        obs.mode = self.mode
        obs.mode_raw = self.mode_raw
        obs.goal = self.goal or self.title

        if self.error_ts and 0 <= now - self.error_ts < 30:
            obs.status = Status.ERROR
            obs.phase = Phase.NONE
            obs.confidence = Confidence.HIGH
            obs.summary = shorten(self._summary_text(Status.ERROR), SUMMARY_MAX)
            return obs
        if self.input_pending:
            obs.status = Status.INPUT
            obs.phase = Phase.USER_INPUT
            obs.confidence = Confidence.EXACT
            obs.summary = shorten(self._summary_text(Status.INPUT), SUMMARY_MAX)
            return obs
        if self.done_ts and 0 <= now - self.done_ts < 8:
            obs.status = Status.DONE
            obs.phase = Phase.NONE
            obs.confidence = Confidence.EXACT
            obs.summary = shorten(self._summary_text(Status.DONE), SUMMARY_MAX)
            return obs
        # open_tools + 静默只说明没有新的磁盘记录，不能生成审批请求。
        if self.turn_active:
            obs.status = Status.WORKING
            obs.phase = self.phase
            obs.turn_active = True
            obs.confidence = Confidence.HIGH
            obs.summary = shorten(self._summary_text(Status.WORKING) or "处理中", SUMMARY_MAX)
            return obs
        if self.turn_known_over:
            obs.status = Status.IDLE
            obs.phase = Phase.NONE
            obs.confidence = Confidence.HIGH
            obs.summary = shorten(self._summary_text(Status.WORKING) or "待命", SUMMARY_MAX)
            return obs
        grace = self.activity_grace(cfg)
        anchor = max(self.last_event_ts, self.last_arrival_ts)
        if anchor and now - anchor < grace:
            obs.status = Status.WORKING
            obs.phase = self.phase
            obs.turn_active = False
            obs.expires_at = anchor + grace
            obs.confidence = Confidence.MEDIUM
            obs.summary = shorten(self._summary_text(Status.WORKING) or "处理中", SUMMARY_MAX)
            return obs
        return None


@dataclass
class FileGrowthState:
    """单文件 (size, mtime_ns) 增长采样状态（v4.2.3 §6）。

    固定大小状态而非 history list：JSONL 增长优先依赖 size（避免
    mtime 分辨率差异）；stable_cycles 统计绑定文件的静默周期；
    change_count 统计自采样基线后的独立增长次数。
    """
    size: int = -1
    mtime_ns: int = -1
    stable_cycles: int = 0
    change_count: int = 0
    last_change_at: float = 0.0
    last_sample_at: float = 0.0


class ClaudeWatcher(BaseWatcher):
    kind = AgentKind.CLAUDE

    def __init__(self, monitor_cfg: dict):
        super().__init__(monitor_cfg)
        self._pid_hint_cache: dict[int, tuple[float, dict]] = {}
        # path → FileGrowthState；上限 = MAX_TRACKED_FILES（files 清理时同步 prune）
        self._growth: dict[str, FileGrowthState] = {}
        # 已被 growth 证据证伪的 PID registry sessionId（runtime-only）：
        # 防止 stale registry hint 把实例反复拉回冻结旧 transcript
        self._stale_hint_sids: set[str] = set()

    def make_state(self, path: str) -> FileState:
        return ClaudeFile(path)

    # ---- PID registry 强 hint（/clear 后可能 stale，仅作绑定线索） ----
    def instance_hints(self, inst) -> dict[str, str]:
        pid = int(getattr(inst, "pid", 0) or 0)
        if pid <= 0:
            return {}
        now = time.time()
        cached = self._pid_hint_cache.get(pid)
        if cached and now - cached[0] < 3.0:
            return cached[1]
        for path in paths.claude_pid_registry_files(inst):
            record = paths.read_claude_pid_registry(path)
            sid = str(record.get("sessionId") or record.get("session_id") or "")
            if sid:
                hint = {"session_id": sid}
                # 记录里的 cwd 与实例 cwd 冲突时降级（stale 防护第一层）。
                rcwd = str(record.get("cwd") or "")
                icwd = str(getattr(inst, "cwd", "") or "")
                if rcwd and icwd and rcwd.rstrip("/") != icwd.rstrip("/"):
                    hint = {}
                # growth 证据已证伪的 sessionId 不再作为 hint（第二层：
                # /clear 后 registry 长期指旧 transcript，issue #53037）。
                if hint and sid in self._stale_hint_sids:
                    hint = {}
                self._pid_hint_cache[pid] = (now, hint)
                return hint
        self._pid_hint_cache[pid] = (now, {})
        return {}

    def _candidate_score(self, inst, st: FileState) -> int:
        score = super()._candidate_score(inst, st)
        growth = self._growth.get(st.path)
        if growth is not None and growth.change_count >= _STALE_CONFIRM_CYCLES:
            now = time.time()
            if growth.last_change_at and now - growth.last_change_at < _GROWTH_FRESH_SEC:
                score += _GROWTH_BONUS
        return score

    # ---- /clear 后的 transcript 切换（v4.2.3 §6） ----
    def revalidate_bindings(self, instances: list, now: float):
        if not self._instance_files:
            return
        # 1) 对当前 bound file 以及同 source、same normalized cwd 的
        #    所有已跟踪候选各做一次 stat，更新 (size, mtime_ns) 增长状态。
        bound_paths = {p for p in self._instance_files.values()
                       if p in self.files}
        sample_paths = set(bound_paths)
        for path in bound_paths:
            st = self.files.get(path)
            if st is None:
                continue
            cwd = (st.cwd or "").rstrip("/")
            source = st.source or ""
            if not cwd:
                continue
            for other_path, other in self.files.items():
                if other_path in sample_paths:
                    continue
                ocwd = (other.cwd or "").rstrip("/")
                if not ocwd or ocwd != cwd:
                    continue
                if source and other.source and other.source != source:
                    continue
                sample_paths.add(other_path)
        for path in sample_paths:
            self._sample_growth(path, now)
        # 2) 绑定 A 静默 ≥2 周期，且存在满足全部切换条件的增长候选 B
        #    → 只 pop 绑定与旧 growth 引用，不直接强绑 B；下一轮由
        #    mutual-unique matcher（growth bonus 让 B 确定性胜出）决定。
        inst_by_key = {_as_text(getattr(i, "key", "")): i for i in instances}
        for key, path in list(self._instance_files.items()):
            if path not in self.files:
                continue
            growth = self._growth.get(path)
            if growth is None or growth.stable_cycles < _STALE_CONFIRM_CYCLES:
                continue
            if self._growing_same_cwd_candidate(path, growth, now):
                st = self.files.get(path)
                self._instance_files.pop(key, None)
                self._growth.pop(path, None)
                # stale registry hint 失效 + 清掉从 A 回填的 session_id，
                # 否则 +4000/+5000 会把实例立刻拉回冻结的 A。
                if st is not None:
                    for sid in {st.session_id, st.file_id}:
                        if sid:
                            self._stale_hint_sids.add(sid)
                    inst = inst_by_key.get(key)
                    if inst is not None and getattr(inst, "session_id", ""):
                        if inst.session_id in {st.session_id, st.file_id}:
                            try:
                                inst.session_id = ""
                            except Exception:
                                pass
                while len(self._stale_hint_sids) > _STALE_HINT_MAX:
                    self._stale_hint_sids.pop()

    def _sample_growth(self, path: str, now: float) -> None:
        """一次 stat 采样：用 (st_size, st_mtime_ns) 判定增长。"""
        try:
            s = os.stat(path)
            size, mtime_ns = int(s.st_size), int(s.st_mtime_ns)
        except OSError:
            return
        g = self._growth.get(path)
        if g is None:
            self._growth[path] = FileGrowthState(
                size=size, mtime_ns=mtime_ns, last_sample_at=now)
            return
        g.last_sample_at = now
        if size != g.size or mtime_ns != g.mtime_ns:
            if size > g.size:
                # JSONL append 增长优先看 size 增大
                g.change_count += 1
                g.last_change_at = now
                g.stable_cycles = 0
            g.size, g.mtime_ns = size, mtime_ns
        else:
            g.stable_cycles += 1

    def _growing_same_cwd_candidate(self, bound_path: str,
                                    bound_growth: FileGrowthState,
                                    now: float) -> bool:
        """切换条件（v4.2.3 §6）：B 与 A source/cwd 一致、≥2 次独立
        增长、比 A 更新、仍存在于 self.files。"""
        st = self.files.get(bound_path)
        if st is None:
            return False
        cwd = (st.cwd or "").rstrip("/")
        source = st.source or ""
        if not cwd:
            return False
        for other_path, other in self.files.items():
            if other_path == bound_path:
                continue
            ocwd = (other.cwd or "").rstrip("/")
            if not ocwd or ocwd != cwd:
                continue
            if source and other.source and other.source != source:
                continue
            growth = self._growth.get(other_path)
            if growth is None:
                continue
            if (growth.change_count >= _STALE_CONFIRM_CYCLES
                    and growth.last_change_at > bound_growth.last_change_at):
                return True
        return False

    def prune_growth(self):
        """files 清理后同步 prune _growth（状态上限 = MAX_TRACKED_FILES）。"""
        for path in list(self._growth):
            if path not in self.files:
                self._growth.pop(path, None)

    def _after_files_pruned(self):
        self.prune_growth()
