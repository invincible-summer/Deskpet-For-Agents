"""Codex rollout watcher（V3 被动观察）。

已查证（openai/codex 2026-09 main）：
  * rollout JSONL 记录类型：session_meta / response_item / event_msg /
    turn_context / compacted / token_usage_record / ...
  * TurnStarted 序列化为 "task_started"（别名 turn_started），payload 携带
    collaboration_mode_kind（"plan" / "default"）→ Mode 是结构化数据。
  * TurnComplete 序列化为 "task_complete"。
  * error / exec_command_begin / *_approval_request / request_user_input 等
    都是 transient：不写入 rollout。因此 rollout 静默绝不推断 WAITING
    （plan.md §6/§11）；审批只能来自 Terminal UIA 观察。
  * user_message 的文本字段是 message；agent_message 也是 message。
"""
import json
import time

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
from .summarize import CODEX_MODE, CODEX_SANDBOX, fmt_command, shorten, question_summary

GOAL_MAX = 120
SUMMARY_MAX = 160

# rollout 顶层记录类型（2026-09 查证）
_TOP_RECORD_TYPES = frozenset({
    "session_meta", "turn_context", "event_msg", "response_item",
    "compacted", "token_usage_record", "error", "fatal_error",
    "input", "request_user_input", "?",
})
# event_msg / response_item 的 payload.type
_EVENT_TYPES = frozenset({
    "error", "turn_error", "task_error", "fatal_error", "stream_error",
    "input", "input_required", "request_user_input", "user_input_required",
    "task_started", "turn_started", "task_complete", "turn_complete",
    "turn_finished", "turn_aborted", "task_aborted", "turn_cancelled",
    "task_cancelled", "user_message", "agent_message", "agent_reasoning",
    "agent_reasoning_raw_content", "mcp_tool_call_end", "web_search_end",
    "item_completed", "input_received", "user_input", "token_count",
})
# response_item 的 payload.type
_ITEM_TYPES = frozenset({
    "message", "function_call", "tool_call", "local_shell_call",
    "custom_tool_call", "input", "request_user_input", "error",
    "reasoning",
})


def _event_ts(obj, payload=None) -> float:
    """事件时间；缺失时由当前读取到达时间补齐。"""
    payload = payload if isinstance(payload, dict) else {}
    return (parse_ts(obj.get("timestamp")) or
            parse_ts(payload.get("timestamp")) or time.time())


def _text_content(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_text_content(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("value") or "")
    return ""


class CodexFile(FileState):
    kind = AgentKind.CODEX
    RECORD_TYPES = _TOP_RECORD_TYPES | {f"event_msg:{t}" for t in _EVENT_TYPES} \
        | {f"response_item:{t}" for t in _ITEM_TYPES}

    def _record_type(self, obj: dict) -> str:
        t = str(obj.get("type") or "?")
        if t in ("event_msg", "response_item"):
            payload = obj.get("payload")
            inner = str(payload.get("type") or "") if isinstance(payload, dict) else ""
            if inner:
                t = f"{t}:{inner}"
        return t

    def __init__(self, path: str):
        super().__init__(path)
        self.title = ""
        self.goal = ""
        self.last_text = ""
        self.last_cmd = ""
        self.last_tool = ""
        self.policy = ""
        self.task_active = False
        self.input_pending = False
        self.input_summary = ""
        self.input_call_id = ""
        self.goal_mode = False
        self.error_text = ""
        self.error_ts = 0.0
        self.done_ts = 0.0
        self.task_started_ts = 0.0
        self.last_assistant_ts = 0.0
        self.last_tool_ts = 0.0
        self.last_activity_kind = ""

    # ------------------------------------------------------------- helpers
    def _touch(self, obj: dict, payload: dict) -> float:
        ts = _event_ts(obj, payload)
        self.last_event_ts = max(self.last_event_ts, ts)
        for item in (obj, payload):
            if not isinstance(item, dict):
                continue
            sid = item.get("session_id") or item.get("sessionId")
            if sid and not self.session_id:
                self.session_id = str(sid)
            turn = item.get("turn_id") or item.get("turnId")
            if turn:
                self.turn_id = str(turn)
            cwd = item.get("cwd") or item.get("working_directory")
            if cwd:
                self.cwd = str(cwd)
        return ts

    def _set_goal(self, value) -> bool:
        """Goal 第一来源：当前 turn 最新普通 user message（plan §11）。"""
        text = shorten(str(value or ""), GOAL_MAX)
        if text:
            self.goal = text
            return True
        return False

    def _set_error(self, payload: dict, ts: float):
        value = (payload.get("message") or payload.get("error") or
                 payload.get("reason") or payload.get("detail") or "Codex 报告错误")
        self.error_text = shorten(str(value), SUMMARY_MAX)
        self.error_ts = ts
        self.task_active = False
        self.turn_known_over = True
        self.input_pending = False
        self.done_ts = 0.0
        self.phase = Phase.NONE

    def _set_input(self, payload: dict):
        self.input_pending = True
        value = payload.get("question") or payload.get("prompt") or payload.get("message")
        self.input_summary = question_summary(payload, GOAL_MAX)
        self.phase = Phase.USER_INPUT

    def _set_assistant(self, text: str, ts: float):
        text = shorten(text, SUMMARY_MAX)
        if text:
            self.last_text = text
            self.last_assistant_ts = ts
            self.last_activity_kind = "assistant"
            self.phase = Phase.ANSWERING

    def _set_tool(self, name: str, detail, ts: float):
        self.turn_active = True
        self.turn_known_over = False
        self.done_ts = 0.0
        self.error_ts = 0.0
        detail_s = fmt_command(detail, 100) if detail else ""
        self.last_cmd = detail_s or self.last_cmd
        self.last_tool = shorten(f"{name}: {detail_s}" if detail_s else str(name), SUMMARY_MAX)
        self.last_tool_ts = ts
        self.last_activity_kind = "tool"
        phase = classify_phase(name, detail_s)
        self.phase = phase if phase is not Phase.NONE else Phase.EXECUTING

    # ------------------------------------------------------------- feed
    def feed(self, line: str):
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            return
        rtype = obj.get("type") or ""
        payload = obj.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        ts = self._touch(obj, payload)

        if rtype == "session_meta":
            self.cwd = str(payload.get("cwd") or self.cwd)
            sid = payload.get("id") or payload.get("session_id") or payload.get("sessionId")
            if sid:
                self.session_id = str(sid)
            title = payload.get("title") or payload.get("name")
            if title:
                self.title = shorten(str(title), 100)
            return

        if rtype == "turn_context":
            approval = CODEX_MODE.get(
                str(payload.get("approval_policy") or payload.get("approvalPolicy") or ""), "")
            sandbox = payload.get("sandbox_policy") or payload.get("sandboxPolicy") or {}
            if not isinstance(sandbox, dict):
                sandbox = {}
            sandbox_name = CODEX_SANDBOX.get(
                str(sandbox.get("type") or sandbox.get("mode") or ""), "")
            self.policy = "·".join(x for x in (approval, sandbox_name) if x)
            self.turn_id = str(payload.get("turn_id") or payload.get("turnId") or self.turn_id)
            collab = payload.get("collaboration_mode") or payload.get("collaborationMode")
            if collab:
                self.mode, self.mode_raw = parse_mode(collab)
            self.cwd = str(payload.get("cwd") or self.cwd)
            return

        if rtype == "event_msg":
            self._feed_event(payload, ts)
            return

        if rtype == "response_item":
            self._feed_response_item(payload, ts)
            return

        # 某些旧版本使用顶层 error/input 记录；保守支持，不把静默当审批。
        low = str(rtype).lower()
        if low in {"error", "fatal_error"}:
            self._set_error(payload or obj, ts)
        elif low in {"input", "request_user_input", "input_required"}:
            self._set_input(payload or obj)

    def _feed_event(self, payload: dict, ts: float):
        ptype = str(payload.get("type") or "")
        if ptype in {"error", "turn_error", "task_error", "fatal_error", "stream_error"}:
            self._set_error(payload, ts)
        elif ptype in {"input", "input_required", "request_user_input", "user_input_required"}:
            self._set_input(payload)
        elif ptype in {"task_started", "turn_started"}:
            # TurnStartedEvent：collaboration_mode_kind 是结构化 Plan 证据。
            self.task_active = True
            self.turn_active = True
            self.turn_known_over = False
            self.input_pending = False
            self.done_ts = 0.0
            self.error_text = ""
            self.error_ts = 0.0
            self.task_started_ts = ts
            self.turn_id = str(payload.get("turn_id") or payload.get("turnId") or self.turn_id)
            mode, mode_raw = parse_mode(payload.get("collaboration_mode_kind")
                                        or payload.get("collaborationModeKind"))
            if mode is not Mode.NONE:
                self.mode = mode
                self.mode_raw = mode_raw
            self.phase = Phase.THINKING
        elif ptype in {"task_complete", "turn_complete", "turn_finished"}:
            self.task_active = False
            self.turn_active = False
            self.turn_known_over = True
            self.input_pending = False
            self.done_ts = ts
            msg = payload.get("last_agent_message") or payload.get("message") or payload.get("result")
            if msg:
                self._set_assistant(_text_content(msg), ts)
            self.phase = Phase.NONE
        elif ptype in {"turn_aborted", "task_aborted", "turn_cancelled", "task_cancelled"}:
            self.task_active = False
            self.turn_active = False
            self.turn_known_over = True
            self.input_pending = False
            self.done_ts = 0.0
            self.phase = Phase.NONE
        elif ptype == "user_message":
            # 普通用户输入 → Goal 第一来源；用户提交即开始新 turn。
            self._set_goal(payload.get("message"))
            self.task_active = True
            self.turn_active = True
            self.turn_known_over = False
            self.done_ts = 0.0
            self.input_pending = False
            self.phase = Phase.THINKING
        elif ptype in {"agent_message"}:
            self._set_assistant(_text_content(payload.get("message")), ts)
        elif ptype in {"agent_reasoning", "agent_reasoning_raw_content"}:
            text = _text_content(payload.get("text"))
            if text:
                self.phase = Phase.THINKING
        elif ptype in {"mcp_tool_call_end", "web_search_end"}:
            invocation = payload.get("invocation") or {}
            name = (invocation.get("tool") if isinstance(invocation, dict) else "") or ptype
            self._set_tool(str(name), invocation if isinstance(invocation, dict) else "", ts)
        elif ptype == "item_completed":
            item = payload.get("item") or {}
            if isinstance(item, dict):
                self._feed_turn_item(item, ts)
        elif ptype in {"input_received", "user_input"}:
            self.input_pending = False
        elif ptype == "token_count":
            pass   # 活动证据已由 last_event_ts 记录
        else:
            message = payload.get("message") or payload.get("text")
            if isinstance(message, str) and message.strip():
                self._set_assistant(message, ts)

    def _feed_turn_item(self, item: dict, ts: float):
        """paginated history 的 item_completed TurnItem（command_execution 等）。"""
        itype = str(item.get("type") or "").lower()
        item_ts = _event_ts(item) or ts
        if itype in {"agentmessage", "assistantmessage", "message"}:
            role = str(item.get("role") or "")
            if role == "user":
                self._set_goal(item.get("text") or item.get("content"))
                self.turn_active = True
                self.turn_known_over = False
            else:
                self._set_assistant(_text_content(item.get("text") or item.get("content")), item_ts)
        elif itype in {"commandexecution", "command_execution", "functioncall",
                       "function_call", "localshellcall", "customtoolcall"}:
            command = item.get("command") or item.get("cmd") or item.get("arguments")
            if isinstance(command, dict):
                command = command.get("command") or command.get("cmd") or str(command)
            self._set_tool(itype, command, item_ts)
        elif itype in {"reasoning", "agentreasoning"}:
            self.phase = Phase.THINKING
        elif itype == "plan":
            self.phase = Phase.PLANNING
        elif itype in {"mcp tool call", "mcptoolcall"}:
            self._set_tool(str(item.get("tool") or "mcp"), item.get("arguments"), item_ts)

    def _feed_response_item(self, payload: dict, ts: float):
        ptype = str(payload.get("type") or "")
        if ptype == "message":
            role = str(payload.get("role") or "")
            texts = []
            for content in payload.get("content") or []:
                if isinstance(content, dict):
                    texts.append(_text_content(content.get("text") or content.get("content")))
                else:
                    texts.append(_text_content(content))
            text = " ".join(x for x in texts if x)
            if role == "user":
                if self._set_goal(text):
                    self.turn_active = True
                    self.turn_known_over = False
                    self.done_ts = 0.0
            elif role == "assistant" and text:
                self._set_assistant(text, ts)
        elif ptype in {"function_call", "tool_call", "local_shell_call", "custom_tool_call"}:
            args = payload.get("arguments") or payload.get("input") or payload.get("action") or ""
            parsed = args if isinstance(args, dict) else {}
            command = args
            name = payload.get("name") or ptype
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, dict):
                    command = parsed.get("cmd") or parsed.get("command") or parsed.get("path") or args
            elif isinstance(args, dict):
                command = args.get("cmd") or args.get("command") or args.get("path") or str(args)
            self._set_tool(str(name), command, ts)
            tool = str(name).split(".")[-1]
            if tool in {"request_user_input", "request_user_input_async", "AskUserQuestion"}:
                self._set_input(parsed if isinstance(parsed, dict) else {})
                self.input_call_id = str(payload.get("call_id") or "")
            elif tool == "create_goal":
                self.goal_mode = True
            elif tool == "update_goal" and isinstance(parsed, dict) and parsed.get("status") in {"complete", "blocked"}:
                self.goal_mode = False
        elif ptype in {"function_call_output", "custom_tool_call_output", "tool_result"}:
            if self.input_call_id and payload.get("call_id") == self.input_call_id:
                self.input_pending = False
                self.input_call_id = ""
                self.phase = Phase.THINKING
        elif ptype in {"input", "request_user_input"}:
            self._set_input(payload)
        elif ptype == "error":
            self._set_error(payload, ts)

    # ------------------------------------------------------------- status
    def _summary_text(self, status: Status) -> str:
        if status == Status.ERROR:
            return self.error_text or "Codex 报告错误"
        if status == Status.INPUT:
            return self.input_summary or "等待输入"
        if status == Status.DONE:
            return self.last_text or "任务完成"
        if self.last_activity_kind == "assistant" and self.last_text:
            return self.last_text
        if self.last_activity_kind == "tool" and self.last_tool:
            return self.last_tool
        if self.last_assistant_ts >= self.last_tool_ts and self.last_text:
            return self.last_text
        return self.last_tool or self.last_text

    def observation(self, now: float, cfg: dict) -> Observation | None:
        obs = self.base_observation()
        obs.mode = Mode.GOAL if self.goal_mode else self.mode
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
        # rollout 不记录实时审批请求（transient），静默只代表 turn 仍在
        # 负责，绝不能生成 WAITING（plan §6）。
        if self.turn_active:
            obs.status = Status.WORKING
            obs.phase = self.phase
            obs.turn_active = True
            obs.confidence = Confidence.HIGH
            obs.summary = shorten(self._summary_text(Status.WORKING) or "处理中", SUMMARY_MAX)
            return obs
        if self.turn_known_over:
            # 已知 turn 结束：立即 IDLE（plan §27），不被活动宽限拖回 WORKING。
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
        # 没有任何 turn 生命周期证据 → 不伪造状态。
        return None


class CodexWatcher(BaseWatcher):
    kind = AgentKind.CODEX

    def make_state(self, path: str) -> FileState:
        return CodexFile(path)
