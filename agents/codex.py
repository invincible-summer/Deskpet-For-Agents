"""Codex rollout watcher（V3 被动观察）。

已查证（openai/codex 2026-09 main）：
  * rollout JSONL 记录类型：session_meta / response_item / event_msg /
    turn_context / compacted / token_usage_record / ...
  * TurnStarted 序列化为 "task_started"（别名 turn_started），payload 携带
    collaboration_mode_kind（"plan" / "default"）→ Mode 是结构化数据。
  * TurnComplete 序列化为 "task_complete"。
  * error / exec_command_begin / *_approval_request / request_user_input 等
    都是 transient：不写入 rollout。因此 rollout 静默绝不推断 WAITING
    审批只能来自 Terminal UIA 观察。
  * user_message 的文本字段是 message；agent_message 也是 message。
"""
import json
import os
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
from .summarize import (CODEX_MODE, CODEX_SANDBOX, fmt_command, shorten,
                        question_summary, wrapped_command, summarize_text,
                        summarize_tool, tool_detail)

GOAL_MAX = 120
SUMMARY_MAX = 160
_CODEX_ZSTD_MAX_COMPRESSED = 32 * 1024 * 1024
_CODEX_ZSTD_MAX_DECOMPRESSED = 128 * 1024 * 1024

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
    # 上游 rollout policy.rs 的 durable 事件（protocol.rs serde snake_case
    # 命名，2026-10 对照 main 复核）：缺任何一个都会让含该记录的 rollout
    # 解析健康误判 PARTIAL。
    "thread_goal_updated", "thread_rolled_back", "thread_settings_applied",
    "context_compacted", "entered_review_mode", "exited_review_mode",
    "patch_apply_end", "image_generation_end", "sub_agent_activity",
    # Current protocol.rs EventMsg names. Most are transient, but retaining
    # them avoids labelling a healthy rollout PARTIAL when a newer client
    # persists a lifecycle marker.
    "warning", "auth_recovery_started", "auth_recovery_completed",
    "guardian_warning", "realtime_conversation_started",
    "realtime_conversation_realtime", "realtime_conversation_closed",
    "realtime_conversation_sdp", "model_reroute", "model_verification",
    "turn_moderation_metadata", "safety_buffering", "thread_queue_changed",
    "session_configured", "environment_connected", "environment_disconnected",
    "mcp_startup_update", "mcp_startup_complete", "mcp_tool_call_begin",
    "web_search_begin", "image_generation_begin", "exec_command_begin",
    "exec_command_output_delta", "terminal_interaction", "exec_command_end",
    "exec_approval_request", "request_permissions", "request_user_input",
    "dynamic_tool_call_request", "dynamic_tool_call_response",
    "elicitation_request", "elicitation_abandoned",
    "apply_patch_approval_request", "guardian_assessment", "deprecation_notice",
    "patch_apply_begin", "patch_apply_updated", "turn_diff", "plan_update",
    "shutdown_complete", "raw_response_item", "raw_response_completed",
    "item_started", "hook_started", "hook_completed",
    "agent_message_content_delta", "plan_delta", "reasoning_content_delta",
    "reasoning_raw_content_delta", "agent_reasoning_section_break",
    "collab_agent_spawn_begin", "collab_agent_spawn_end",
    "collab_agent_interaction_begin", "collab_agent_interaction_end",
    "collab_waiting_begin", "collab_waiting_end", "collab_close_begin",
    "collab_close_end", "collab_resume_begin", "collab_resume_end",
})
# response_item 的 payload.type
_ITEM_TYPES = frozenset({
    "message", "function_call", "tool_call", "local_shell_call",
    "custom_tool_call", "input", "request_user_input", "error",
    "reasoning",
    "function_call_output", "custom_tool_call_output", "tool_result",
    "agent_message", "user_message", "command_execution", "plan",
    "dynamic_tool_call", "collab_agent_tool_call", "sub_agent_activity",
    "web_search", "image_view", "extension", "image_generation",
    "entered_review_mode", "exited_review_mode", "file_change",
    "mcp_tool_call", "context_compaction", "tool_search_call",
    "tool_search_output", "web_search_call", "image_generation_call",
    "configuration_update", "compaction", "compaction_trigger",
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
        nested = (value.get("text") or value.get("value")
                  or value.get("content") or value.get("message"))
        if nested is not None and nested is not value:
            return _text_content(nested)
        summary = value.get("summary_text")
        if isinstance(summary, list):
            return " ".join(_text_content(x) for x in summary)
        return ""
    return ""


def _lifecycle_reason(payload: dict) -> tuple[str, str]:
    """Normalize current and older Codex completion payloads.

    The Rust enum normally serializes a scalar event tag, but bridge/client
    adapters have emitted ``reason``/``outcome`` objects as well.  Returning
    both a normalized kind and a short message keeps abnormal endings from
    being mistaken for successful ``task_complete`` events.
    """
    raw = (payload.get("reason") or payload.get("status")
           or payload.get("outcome") or payload.get("subtype") or "")
    detail = ""
    if isinstance(raw, dict):
        detail = str(raw.get("message") or raw.get("error")
                     or raw.get("detail") or "")
        raw = (raw.get("kind") or raw.get("status")
               or raw.get("outcome") or raw.get("type") or "")
    kind = str(raw or "").casefold().replace("_", "-")
    if payload.get("is_error") or payload.get("isError"):
        kind = kind or "error"
    return kind, detail


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
        self._init_codex_state()
        self._compressed = str(path).lower().endswith(".jsonl.zst")
        self._compressed_sig = None
        self._compressed_error = ""

    def _init_codex_state(self):
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
        # Monotonic runtime fact used by desktop admission. Metadata/header
        # records may refresh a file without representing a user/assistant/
        # tool turn; those records must not create a target by themselves.
        self.meaningful_revision = 0
        self.last_meaningful_ts = 0.0
        self._feed_ts = 0.0

    def _reset_for_replacement(self):
        """Reset parser state when a rollout representation is atomically replaced."""
        source = self.source
        FileState.__init__(self, self.path)
        self.source = source
        self._init_codex_state()

    @staticmethod
    def _decode_zstd_lines(path: str):
        """Yield decoded JSONL lines from one immutable Codex ``.jsonl.zst``.

        Codex compresses archived rollouts as one zstd stream (``.zst``), while
        DSH uses concatenated ``.zstd`` frames.  Keep this reader separate so a
        Codex archive is never mistaken for a DSH frame stream.  The output and
        input caps keep a corrupt archive from consuming unbounded memory.
        """
        try:
            import zstandard  # type: ignore
        except ImportError as exc:
            raise ValueError("zstandard library unavailable") from exc
        size = os.path.getsize(path)
        if size > _CODEX_ZSTD_MAX_COMPRESSED:
            raise ValueError("compressed rollout exceeds input cap")
        pending = b""
        produced = 0
        with open(path, "rb") as raw:
            with zstandard.ZstdDecompressor().stream_reader(raw) as reader:
                while True:
                    chunk = reader.read(65536)
                    if not chunk:
                        break
                    produced += len(chunk)
                    if produced > _CODEX_ZSTD_MAX_DECOMPRESSED:
                        raise ValueError("decompressed rollout exceeds output cap")
                    pending += chunk
                    while True:
                        idx = pending.find(b"\n")
                        if idx < 0:
                            break
                        yield pending[:idx]
                        pending = pending[idx + 1:]
        # A torn final JSONL record is intentionally ignored, matching the
        # plain FileTailer contract; complete records have a trailing newline.

    def _consume_decoded_line(self, raw: bytes, arrival: float):
        if len(raw) > self.tailer.MAX_LINE:
            self.parse_errors += 1
            return
        line = raw.decode("utf-8", errors="replace").strip("\r")
        if not line:
            return
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
            self.parse_errors += 1
        self.last_event_ts = max(self.last_event_ts, self._observed_ts or arrival)

    def _poll_compressed_lines(self):
        arrival = time.time()
        try:
            st = os.stat(self.path)
        except OSError:
            return
        sig = (st.st_size, getattr(st, "st_mtime_ns", 0),
               getattr(st, "st_dev", 0), getattr(st, "st_ino", 0))
        if sig == self._compressed_sig:
            return
        if self._compressed_sig is not None:
            self._reset_for_replacement()
        self._compressed_sig = sig
        before = self.last_event_ts
        try:
            for raw in self._decode_zstd_lines(self.path):
                self._consume_decoded_line(raw, arrival)
        except Exception as exc:
            self._compressed_error = str(exc)[:200]
            self.parse_errors += 1
            self.last_parse_error = self._compressed_error
            return
        self._compressed_error = ""
        self.last_arrival_ts = arrival
        self.last_event_ts = max(self.last_event_ts, before)

    def poll_lines(self):
        if self._compressed:
            self._poll_compressed_lines()
            return
        super().poll_lines()

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
        text = summarize_text(value, GOAL_MAX)
        if text:
            self.goal = text
            return True
        return False

    def _mark_meaningful(self) -> None:
        self.meaningful_revision += 1
        self.last_meaningful_ts = max(self.last_meaningful_ts, self._feed_ts)

    def _set_error(self, payload: dict, ts: float):
        self._mark_meaningful()
        value = (payload.get("message") or payload.get("error") or
                 payload.get("reason") or payload.get("detail") or "Codex 报告错误")
        self.error_text = shorten(str(value), SUMMARY_MAX)
        self.error_ts = ts
        self.task_active = False
        self.turn_active = False
        self.turn_known_over = True
        self.input_pending = False
        self.done_ts = 0.0
        self.phase = Phase.NONE

    def _set_input(self, payload: dict):
        self.input_pending = True
        self.input_summary = question_summary(payload, GOAL_MAX)
        self.phase = Phase.USER_INPUT

    def _begin_turn(self):
        self._mark_meaningful()
        self.task_active = self.turn_active = True
        self.turn_known_over = False
        self.done_ts = self.error_ts = 0.0
        self.error_text = ""
        self.input_pending = False
        self.input_call_id = ""
        self.last_text = self.last_tool = self.last_cmd = ""
        self.last_activity_kind = ""
        self.phase = Phase.THINKING

    def _set_assistant(self, text: str, ts: float):
        text = summarize_text(text, SUMMARY_MAX)
        if text:
            self._mark_meaningful()
            self.last_text = text
            self.last_assistant_ts = ts
            self.last_activity_kind = "assistant"
            self.phase = Phase.ANSWERING

    def _set_tool(self, name: str, detail, ts: float):
        self._mark_meaningful()
        self.turn_active = True
        self.turn_known_over = False
        self.done_ts = 0.0
        self.error_ts = 0.0
        detail_s = tool_detail(detail, SUMMARY_MAX)
        self.last_cmd = detail_s
        self.last_text = ""
        self.last_tool = summarize_tool(name, detail_s, SUMMARY_MAX)
        self.last_tool_ts = ts
        self.last_activity_kind = "tool"
        phase = classify_phase(name, detail_s)
        self.phase = phase if phase is not Phase.NONE else Phase.EXECUTING

    def _set_reasoning(self, value, ts: float):
        """Project only reasoning text actually present in the session log."""
        self._mark_meaningful()
        self.last_text = summarize_text(value, SUMMARY_MAX)
        self.last_tool = self.last_cmd = ""
        self.last_assistant_ts = ts
        self.last_activity_kind = "reasoning"
        self.phase = Phase.THINKING

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
        self._feed_ts = parse_ts(obj.get("timestamp")) or parse_ts(payload.get("timestamp"))

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
            self._begin_turn()
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
        elif ptype in {"task_complete", "turn_complete", "turn_finished",
                       "turn_ended", "task_ended", "turn_failed",
                       "task_failed"}:
            reason, nested_detail = _lifecycle_reason(payload)
            if (ptype in {"turn_failed", "task_failed"}
                    or reason in {"error", "failed", "failure", "aborted",
                                   "cancelled", "canceled", "interrupted",
                                   "blocked"}):
                self._set_error({
                    "message": payload.get("message")
                    or payload.get("error") or nested_detail
                    or payload.get("reason")
                    or "任务异常中断"}, ts)
                return
            self.task_active = False
            self.turn_active = False
            self.turn_known_over = True
            self.input_pending = False
            self.done_ts = ts
            msg = payload.get("last_agent_message") or payload.get("message") or payload.get("result")
            if msg:
                self._set_assistant(_text_content(msg), ts)
            self.phase = Phase.NONE
        elif ptype in {"turn_aborted", "task_aborted", "turn_cancelled",
                       "task_cancelled", "turn_interrupted",
                       "task_interrupted"}:
            self._set_error({
                "message": payload.get("message") or payload.get("reason")
                or "任务异常中断"}, ts)
        elif ptype == "user_message":
            # 普通用户输入 → Goal 第一来源；用户提交即开始新 turn。
            self._begin_turn()
            self._set_goal(payload.get("message"))
            self.task_active = True
            self.turn_active = True
            self.turn_known_over = False
            self.done_ts = 0.0
            self.input_pending = False
            self.phase = Phase.THINKING
        elif ptype in {"agent_message"}:
            self._mark_meaningful()
            self._set_assistant(_text_content(payload.get("message")), ts)
        elif ptype in {"agent_reasoning", "agent_reasoning_raw_content"}:
            self._set_reasoning(payload.get("text") or payload.get("summary")
                                or payload.get("summary_text"), ts)
        elif ptype in {"reasoning_content_delta", "reasoning_raw_content_delta"}:
            self._set_reasoning(payload.get("delta") or payload.get("text"), ts)
        elif ptype == "agent_message_content_delta":
            self._set_assistant(_text_content(payload.get("delta")
                                              or payload.get("text")), ts)
        elif ptype in {"mcp_tool_call_begin", "mcp_tool_call_end",
                       "web_search_begin", "web_search_end",
                       "image_generation_begin", "image_generation_end",
                       "exec_command_begin", "exec_command_output_delta",
                       "patch_apply_begin", "patch_apply_updated",
                       "sub_agent_activity",
                       "collab_agent_spawn_begin", "collab_agent_interaction_begin"}:
            invocation = payload.get("invocation") or {}
            detail = (payload.get("command") or payload.get("cmd")
                      or payload.get("arguments") or invocation)
            name = (invocation.get("tool") if isinstance(invocation, dict) else "") or \
                payload.get("tool") or payload.get("name") or ptype
            self._set_tool(str(name), detail, ts)
        elif ptype == "item_completed":
            item = payload.get("item") or {}
            if isinstance(item, dict):
                self._feed_turn_item(item, ts)
        elif ptype in {"input_received", "user_input"}:
            self.input_pending = False
        elif ptype in {"thread_goal_updated", "plan_update", "plan_delta"}:
            value = (payload.get("goal") or payload.get("text")
                     or payload.get("title") or payload.get("plan"))
            if isinstance(value, dict):
                value = value.get("text") or value.get("goal") or value.get("title")
            if value:
                self._set_goal(value)
                if ptype != "thread_goal_updated":
                    self.phase = Phase.PLANNING
        elif ptype == "shutdown_complete":
            self.task_active = False
            self.turn_active = False
            self.turn_known_over = True
            self.input_pending = False
            self.done_ts = 0.0
            self.phase = Phase.NONE
        elif ptype == "token_count":
            pass   # 活动证据已由 last_event_ts 记录
        else:
            message = payload.get("message") or payload.get("text")
            if isinstance(message, str) and message.strip():
                self._set_assistant(message, ts)

    def _feed_turn_item(self, item: dict, ts: float):
        """paginated history 的 item_completed TurnItem（command_execution 等）。"""
        itype = str(item.get("type") or "").lower()
        item_ts = parse_ts(item.get("timestamp")) or ts
        if itype in {"agentmessage", "agent_message", "assistantmessage",
                     "assistant_message", "usermessage", "user_message", "message"}:
            self._mark_meaningful()
            role = str(item.get("role") or "")
            if role == "user" or itype in {"usermessage", "user_message"}:
                self._begin_turn()
                self._set_goal(item.get("text") or item.get("content")
                               or item.get("message"))
                self.turn_active = True
                self.turn_known_over = False
            else:
                self._set_assistant(
                    _text_content(item.get("text") or item.get("content")
                                  or item.get("message")), item_ts)
        elif itype in {"commandexecution", "command_execution", "functioncall",
                       "function_call", "localshellcall", "customtoolcall"}:
            command = item.get("command") or item.get("cmd") or item.get("arguments")
            if isinstance(command, dict):
                command = command.get("command") or command.get("cmd") or str(command)
            self._set_tool(itype, command, item_ts)
        elif itype in {"reasoning", "agentreasoning", "agent_reasoning"}:
            self._set_reasoning(item.get("summary") or item.get("summary_text")
                                or item.get("text") or item.get("content"), item_ts)
        elif itype == "plan":
            self.phase = Phase.PLANNING
        elif itype in {"mcp tool call", "mcptoolcall", "mcp_tool_call",
                       "dynamic_tool_call", "web_search", "image_generation",
                       "collab_agent_tool_call", "sub_agent_activity"}:
            self._set_tool(str(item.get("tool") or "mcp"), item.get("arguments"), item_ts)

    def _feed_response_item(self, payload: dict, ts: float):
        ptype = str(payload.get("type") or "")
        if ptype == "message":
            role = str(payload.get("role") or "")
            if role in {"user", "assistant"}:
                self._mark_meaningful()
            text = _text_content(payload.get("content"))
            if role == "user":
                self._begin_turn()
                if self._set_goal(text):
                    self.turn_active = True
                    self.turn_known_over = False
                    self.done_ts = 0.0
            elif role == "assistant" and text:
                self._set_assistant(text, ts)
        elif ptype in {"function_call", "tool_call", "local_shell_call",
                       "custom_tool_call", "tool_search_call", "web_search_call",
                       "image_generation_call", "dynamic_tool_call",
                       "collab_agent_tool_call"}:
            args = (payload.get("arguments") or payload.get("input")
                    or payload.get("action") or payload.get("query") or "")
            parsed = args if isinstance(args, dict) else {}
            command = args
            name = payload.get("name") or payload.get("tool") or ptype
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, dict):
                    command = (parsed.get("cmd") or parsed.get("command")
                               or parsed.get("path") or parsed.get("tool") or args)
            elif isinstance(args, dict):
                command = (args.get("cmd") or args.get("command")
                           or args.get("path") or args.get("tool") or str(args))
            if str(name).split(".")[-1] == "exec":
                command = wrapped_command(command)
            self._set_tool(str(name), parsed if isinstance(parsed, dict) else command, ts)
            tool = str(name).split(".")[-1]
            if tool in {"request_user_input", "request_user_input_async", "AskUserQuestion"}:
                self._set_input(parsed if isinstance(parsed, dict) else {})
                self.input_call_id = str(
                    payload.get("call_id") or payload.get("callId") or "") \
                    if tool != "request_user_input_async" else ""
            elif tool == "create_goal":
                self.goal_mode = True
                if isinstance(parsed, dict):
                    self._set_goal(parsed.get("objective"))
            elif tool == "update_goal" and isinstance(parsed, dict) and parsed.get("status") in {"complete", "blocked"}:
                self.goal_mode = False
        elif ptype in {"function_call_output", "custom_tool_call_output",
                       "tool_result", "tool_search_output"}:
            call_id = payload.get("call_id") or payload.get("callId")
            if self.input_call_id and call_id == self.input_call_id:
                self.input_pending = False
                self.input_call_id = ""
                self.phase = Phase.THINKING
            elif not self.input_pending:
                # A result does not mean that the previous assistant answer
                # or tool command is the current activity.
                self.last_text = self.last_tool = self.last_cmd = ""
                self.last_activity_kind = ""
                self.phase = Phase.THINKING
        elif ptype in {"input", "request_user_input"}:
            self._set_input(payload)
        elif ptype == "error":
            self._set_error(payload, ts)
        elif ptype == "reasoning":
            self._set_reasoning(payload.get("summary") or payload.get("summary_text")
                                or payload.get("content") or payload.get("text"), ts)
        elif ptype in {"context_compaction", "compaction",
                       "configuration_update"}:
            self.last_text = self.last_tool = self.last_cmd = ""
            self.last_activity_kind = ""
            self.phase = Phase.THINKING

    # ------------------------------------------------------------- status
    def _summary_text(self, status: Status) -> str:
        if status == Status.ERROR:
            return self.error_text or "Codex 报告错误"
        if status == Status.INPUT:
            return self.input_summary or "等待输入"
        if status == Status.DONE:
            return self.last_text or "任务完成"
        if self.last_activity_kind in {"assistant", "reasoning"}:
            return self.last_text
        if self.last_activity_kind == "tool":
            return self.last_tool
        return ""

    def observation(self, now: float, cfg: dict) -> Observation | None:
        if self._compressed_error:
            return None
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
            obs.summary = fmt_command(self._summary_text(Status.WORKING) or "处理中", SUMMARY_MAX)
            # v4.4.1：上游 rollout policy 把 ExecApprovalRequest /
            # RequestUserInput 列为 transient 不落盘，rollout 读者永远看不到
            # 当前是否有未决审批。不伪造 WAITING，但要诚实告知"审批状态
            # 不可见"，避免用户看到一个没有上下文的 WORKING 空壳。
            if not self.turn_known_over:
                obs.approval_visibility = "unavailable"
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
            obs.summary = fmt_command(self._summary_text(Status.WORKING) or "处理中", SUMMARY_MAX)
            return obs
        # 没有任何 turn 生命周期证据 → 不伪造状态。
        return None


class CodexWatcher(BaseWatcher):
    kind = AgentKind.CODEX

    def make_state(self, path: str) -> FileState:
        return CodexFile(path)
