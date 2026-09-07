"""Kimi Code watcher（V3 被动观察）。

真实 wire.jsonl 格式（2026-09 实机验证，kimi-code CLI）：
  * 顶层事件：metadata / runtime.set_binding / profile.bind /
    prompt.accepted（用户输入，content[].text → Goal）/
    plan_mode.enter / plan_mode.exit / plan_mode.cancel（结构化 Plan，EXACT）/
    permission.set_mode（mode: manual/…）/ turn.ended / turn.cancel
  * 包裹事件：context.append_loop_event{event:{type: step.begin|step.end|
    tool.call|tool.result|content.part{part:{type:think|text}}}}
  * 时间字段 time 为毫秒 epoch。
  * 数据根 $KIMI_CODE_HOME（默认 ~/.kimi-code）+ 根部 session_index.jsonl
    （sessionId/sessionDir/workDir）+ 会话目录 state.json（title/lastPrompt）。
  * SDK 命名（TurnBegin/StatusUpdate/ApprovalRequest…）作为兜底保留；
    未处理的 ApprovalRequest 阻塞 turn → WAITING/EXACT，绝不由静默推断。
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
    Observation,
    Phase,
    Status,
    normalize_mode,
)
from .summarize import fmt_command, shorten

GOAL_MAX = 120
SUMMARY_MAX = 160


def _event_ts(obj: dict, payload: dict) -> float:
    # wire 的 time 字段是毫秒 epoch
    for item in (obj, payload):
        raw = item.get("time") if isinstance(item, dict) else None
        if isinstance(raw, (int, float)) and raw > 0:
            return raw / 1000.0
    ts = (parse_ts(obj.get("timestamp")) or parse_ts(payload.get("timestamp")))
    if ts:
        return ts
    return time.time()


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("think") or value.get("value") or "")
    if isinstance(value, list):
        return " ".join(_text(x) for x in value)
    return ""


def _first_present(mapping: dict, *keys):
    """Read wire fields without dropping a valid numeric zero identifier."""
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return ""


def approval_summary(p: dict) -> str:
    for key in ("title", "command", "summary", "description", "reason"):
        value = p.get(key)
        if isinstance(value, str) and value.strip():
            return shorten(value, 100)
    args = p.get("arguments") or p.get("args")
    if isinstance(args, str) and args.strip():
        return shorten(args, 100)
    if isinstance(args, dict):
        return shorten(str(args.get("command") or args.get("path") or args), 100)
    name = p.get("name") or p.get("tool")
    return shorten(str(name), 100) if name else ""


class KimiFile(FileState):
    kind = AgentKind.KIMI

    def __init__(self, path: str):
        super().__init__(path)
        self.last_text = ""
        self.last_cmd = ""
        self.last_tool = ""
        self.goal = ""
        self.pending = None          # dict payload；仅显式审批请求事件
        self.pending_id = ""
        self.done_ts = 0.0
        self.input_pending = False
        self.input_summary = ""
        self.error_text = ""
        self.error_ts = 0.0
        self.last_assistant_ts = 0.0
        self.last_tool_ts = 0.0
        self.last_activity_kind = ""
        self.permission_mode = ""

    def _touch(self, obj: dict, payload: dict) -> float:
        ts = _event_ts(obj, payload)
        self.last_event_ts = max(self.last_event_ts, ts)
        for item in (obj, payload):
            sid = item.get("session_id") or item.get("sessionId")
            if sid and not self.session_id:
                self.session_id = str(sid)
            turn = item.get("turn_id") or item.get("turnId") or item.get("turnId")
            if turn:
                self.turn_id = str(turn)
            cwd = item.get("cwd") or item.get("workingDirectory") or item.get("workDir")
            if cwd:
                self.cwd = str(cwd)
        return ts

    def _set_error(self, payload: dict, ts: float):
        value = payload.get("message") or payload.get("error") or payload.get("reason")
        self.error_text = shorten(str(value or "Kimi 报告错误"), SUMMARY_MAX)
        self.error_ts = ts
        self.turn_active = False
        self.turn_known_over = True
        self.input_pending = False
        self.done_ts = 0.0
        self.phase = Phase.NONE

    def _set_input(self, payload: dict):
        value = _first_present(payload, "question", "prompt", "message", "text")
        self.input_pending = True
        self.input_summary = shorten(_text(value) or "等待输入", GOAL_MAX)
        self.phase = Phase.USER_INPUT

    def _set_tool(self, name, args, ts: float):
        detail = ""
        if isinstance(args, str):
            try:
                a = json.loads(args)
            except (TypeError, ValueError):
                a = None
            if isinstance(a, dict):
                detail = str(a.get("command") or a.get("path") or a.get("file_path") or "")
            else:
                detail = args
        elif isinstance(args, dict):
            detail = str(args.get("command") or args.get("path") or args.get("file_path") or "")
        self.last_cmd = fmt_command(f"{name}: {detail}" if detail else str(name), 100)
        self.last_tool = shorten(f"{name}: {detail}" if detail else str(name), SUMMARY_MAX)
        self.last_tool_ts = ts
        self.last_activity_kind = "tool"
        phase = classify_phase(str(name), detail)
        self.phase = phase if phase is not Phase.NONE else Phase.EXECUTING

    # ------------------------------------------------------------ 主入口
    def feed(self, line: str):
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            return
        if not isinstance(obj, dict):
            return
        t = str(obj.get("type") or "")
        ts = self._touch(obj, obj)

        # ---- 包裹事件：context.append_loop_event{event:{...}} ----
        if t == "context.append_loop_event":
            event = obj.get("event")
            if isinstance(event, dict):
                self._feed_loop_event(event, _event_ts(obj, event))
            return

        # ---- 真实 CLI 顶层事件 ----
        if t == "prompt.accepted":
            text = _text(obj.get("content"))
            if text:
                self.goal = shorten(text, GOAL_MAX)
                self.turn_active = True
                self.turn_known_over = False
                self.done_ts = 0.0
                self.phase = Phase.THINKING
        elif t == "plan_mode.enter":
            self.mode = normalize_mode("plan")
        elif t in ("plan_mode.exit", "plan_mode.cancel"):
            self.mode = normalize_mode("default")
        elif t == "permission.set_mode":
            # manual/auto 等审批策略；只影响展示，不是 Mode 本身
            mode = str(obj.get("mode") or "")
            if mode:
                self.permission_mode = mode
        elif t in ("turn.begin", "turn.started"):
            self._turn_begin(ts)
        elif t == "turn.ended":
            self.turn_active = False
            self.turn_known_over = True
            self.done_ts = ts if str(obj.get("reason") or "") != "cancelled" else 0.0
            self.input_pending = False
            self.pending = None
            self.pending_id = ""
            self.phase = Phase.NONE
        elif t == "turn.cancel":
            self.turn_active = False
            self.turn_known_over = True
            self.input_pending = False
            self.done_ts = 0.0
            self.phase = Phase.NONE
        elif t in ("error", "fatal"):
            self._set_error(obj, ts)
        elif t in ("approval.request", "ApprovalRequest"):
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
            self.pending = dict(payload)
            self.pending_id = str(_first_present(payload, "request_id", "requestId", "id"))
            self.input_pending = False
            self.phase = Phase.APPROVAL
        elif t in ("approval.response", "approval.resolved",
                   "ApprovalResponse", "ApprovalRequestResolved"):
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
            response_id = str(_first_present(payload, "request_id", "requestId", "id"))
            if not response_id or response_id == self.pending_id:
                self.pending = None
                self.pending_id = ""
        elif t.startswith("permission.") and "request" in t:
            self.pending = dict(obj)
            self.pending_id = str(_first_present(obj, "request_id", "requestId", "id"))
            self.phase = Phase.APPROVAL
        elif t.startswith("permission.") and t.endswith(("resolved", ".response")):
            response_id = str(_first_present(obj, "request_id", "requestId", "id"))
            if not response_id or response_id == self.pending_id:
                self.pending = None
                self.pending_id = ""
        else:
            # ---- legacy SDK 命名兜底（TurnBegin/StatusUpdate/...）----
            self._feed_legacy(obj, t, ts)

    def _turn_begin(self, ts: float):
        self.turn_active = True
        self.turn_known_over = False
        self.input_pending = False
        self.done_ts = 0.0
        self.error_text = ""
        self.error_ts = 0.0
        self.phase = Phase.THINKING

    def _feed_loop_event(self, event: dict, ts: float):
        et = str(event.get("type") or "")
        if et == "step.begin":
            self.turn_active = True
            self.turn_known_over = False
            self.phase = Phase.THINKING
        elif et == "step.end":
            pass   # 活动证据已记录
        elif et == "tool.call":
            self._set_tool(event.get("name") or event.get("tool") or "?",
                           event.get("args") or event.get("arguments"), ts)
        elif et == "tool.result":
            pass
        elif et == "content.part":
            part = event.get("part")
            if isinstance(part, dict):
                ptype = str(part.get("type") or "")
                if ptype == "think":
                    self.phase = Phase.THINKING
                elif ptype == "text":
                    text = _text(part)
                    if text:
                        self.last_text = shorten(text, SUMMARY_MAX)
                        self.last_assistant_ts = ts
                        self.last_activity_kind = "assistant"
                        self.phase = Phase.ANSWERING
        elif et in ("error", "fatal"):
            self._set_error(event, ts)

    def _feed_legacy(self, obj: dict, t: str, ts: float):
        payload = obj.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        pt = str(payload.get("type") or t)

        if pt == "TurnBegin":
            self._turn_begin(ts)
        elif pt == "TurnEnd":
            self.turn_active = False
            self.turn_known_over = True
            self.done_ts = ts
            self.input_pending = False
            self.pending = None
            self.pending_id = ""
            self.phase = Phase.NONE
        elif pt in ("ThinkPart", "ThinkingDelta"):
            self.phase = Phase.THINKING
        elif pt in ("ToolCall", "ToolCallRequest"):
            self._set_tool(payload.get("name") or payload.get("tool") or "?",
                           payload.get("arguments"), ts)
        elif pt == "ApprovalRequest":
            self.pending = dict(payload)
            self.pending_id = str(_first_present(
                payload, "request_id", "requestId", "id"))
            self.input_pending = False
            self.phase = Phase.APPROVAL
        elif pt in ("ApprovalResponse", "ApprovalRequestResolved", "ApprovalResolved"):
            response_id = str(_first_present(
                payload, "request_id", "requestId", "id"))
            if not response_id or response_id == self.pending_id:
                self.pending = None
                self.pending_id = ""
        elif pt in {"Question", "AskUserQuestion", "UserInputRequest", "RequestUserInput",
                    "InputRequest", "InputRequired"}:
            self._set_input(payload)
        elif pt in {"Error", "error", "FatalError"}:
            self._set_error(payload, ts)
        elif pt == "Notification":
            body = payload.get("body") or payload.get("title") or payload.get("text")
            if body:
                self.last_text = shorten(_text(body), SUMMARY_MAX)
                self.last_assistant_ts = ts
                self.last_activity_kind = "assistant"
                self.phase = Phase.ANSWERING
        elif pt == "StatusUpdate":
            if "plan_mode" in payload:
                self.mode = normalize_mode("plan" if payload.get("plan_mode") else "default")
        elif pt in ("UserPrompt", "UserMessage", "PromptSubmitted"):
            text = payload.get("prompt") or payload.get("message") or payload.get("text")
            if text:
                self.goal = shorten(_text(text), GOAL_MAX)
        else:
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                self.last_text = shorten(text, SUMMARY_MAX)
                self.last_assistant_ts = ts
                self.last_activity_kind = "assistant"
                self.phase = Phase.ANSWERING

    # ------------------------------------------------------------ 状态
    def _summary_text(self, status: Status) -> str:
        if status == Status.WAITING:
            return approval_summary(self.pending or {}) or self.last_cmd or "等待批复"
        if status == Status.ERROR:
            return self.error_text or "Kimi 报告错误"
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
        obs.goal = self.goal

        if self.error_ts and 0 <= now - self.error_ts < 30:
            obs.status = Status.ERROR
            obs.phase = Phase.NONE
            obs.confidence = Confidence.HIGH
            obs.summary = shorten(self._summary_text(Status.ERROR), SUMMARY_MAX)
            return obs
        if self.pending is not None:
            obs.status = Status.WAITING
            obs.phase = Phase.APPROVAL
            obs.confidence = Confidence.EXACT
            obs.summary = shorten(approval_summary(self.pending or {}) or "等待批复", SUMMARY_MAX)
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


class KimiWatcher(BaseWatcher):
    kind = AgentKind.KIMI

    def make_state(self, path: str) -> FileState:
        return KimiFile(path)

    def _state_json_hints(self, session_dir: str) -> tuple[str, str]:
        """读取 state.json 的 lastPrompt/title（Goal 初始来源）。"""
        path = os.path.join(session_dir, "state.json")
        if not os.path.isfile(path):
            return "", ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except (OSError, ValueError):
            return "", ""
        if not isinstance(obj, dict):
            return "", ""
        title = shorten(str(obj.get("title") or ""), 100)
        prompt = shorten(str(obj.get("lastPrompt") or ""), GOAL_MAX)
        return title, prompt

    def extra_candidates(self, source: str, instances: list) -> list[tuple[float, str]]:
        """session_index.jsonl → cwd 精确匹配的 wire.jsonl 候选（plan §13）。"""
        out: list[tuple[float, str]] = []
        for inst in instances:
            if str(getattr(inst, "source", "")) != source:
                continue
            for mtime, wire in paths.kimi_wire_candidates(inst):
                if wire not in [p for _m, p in out]:
                    out.append((mtime, wire))
                    self._file_cwd_hint[wire] = str(getattr(inst, "cwd", "") or "")
                    session_dir = os.path.dirname(os.path.dirname(wire))
                    title, prompt = self._state_json_hints(session_dir)
                    st = self.files.get(wire)
                    if st is not None:
                        st.title = st.title or title
                        if prompt and not st.goal:
                            st.goal = prompt
        return out
