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

from . import paths
from .base import BaseWatcher, FileState, classify_phase, parse_ts
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
from .summarize import fmt_command, shorten

GOAL_MAX = 120
SUMMARY_MAX = 160
# PID registry 指向旧 JSONL 后，同 cwd 新 JSONL 需要连续 N 个扫描周期
# 确认增长才自动切换（plan §12）。
_STALE_CONFIRM_CYCLES = 2

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
            return

        if t == "user":
            self.input_pending = False
            msg = obj.get("message") or {}
            content = msg.get("content")
            blocks = content if isinstance(content, list) else []
            plain = []
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
            text = shorten(" ".join(x for x in plain if x.strip()), GOAL_MAX)
            if text:
                self.goal = text
                self.turn_active = True
                self.turn_known_over = False
                self.done_ts = 0.0
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


class ClaudeWatcher(BaseWatcher):
    kind = AgentKind.CLAUDE

    def __init__(self, monitor_cfg: dict):
        super().__init__(monitor_cfg)
        self._pid_hint_cache: dict[int, tuple[float, dict]] = {}
        self._growth: dict[str, list[tuple[float, float]]] = {}  # path → [(ts, mtime)]

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
                self._pid_hint_cache[pid] = (now, hint)
                return hint
        self._pid_hint_cache[pid] = (now, {})
        return {}

    # ---- /clear 后的 transcript 切换（plan §12） ----
    def revalidate_bindings(self, instances: list, now: float):
        if not self._instance_files:
            return
        for key, path in list(self._instance_files.items()):
            st = self.files.get(path)
            if st is None:
                continue
            history = self._growth.setdefault(path, [])
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            if history and history[-1][1] != mtime:
                history.append((now, mtime))
            elif not history:
                history.append((now, mtime))
            del history[:-6]
            if len(history) < _STALE_CONFIRM_CYCLES + 1:
                continue
            # 绑定文件连续 N 个扫描周期未增长，而同 cwd 的其他候选持续增长
            # → registry 指向旧 JSONL，自动切换（plan §12）。
            growing = self._growing_same_cwd_candidates(st, now)
            if growing:
                self._instance_files.pop(key, None)
                self._growth.pop(path, None)

    def _growing_same_cwd_candidates(self, st: FileState, now: float) -> bool:
        cwd = (st.cwd or "").rstrip("/")
        if not cwd:
            return False
        source = st.source or ""
        for other_path, other in self.files.items():
            if other_path == st.path:
                continue
            if source and other.source and other.source != source:
                continue
            ocwd = (other.cwd or "").rstrip("/")
            if not ocwd or ocwd != cwd:
                continue
            history = self._growth.get(other_path) or []
            if len(history) >= _STALE_CONFIRM_CYCLES and history[-1][1] != history[0][1]:
                return True
        return False
