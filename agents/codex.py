"""Codex rollout watcher（只读兼容模式）。

rollout JSONL 不承载 app-server 的实时审批请求，因此这里绝不根据静默
时间生成 WAITING/ApprovalRequest。精确审批由受控 app-server 会话提供；
本 watcher 只报告文件中实际出现的生命周期、活动和错误事件。
"""
import json
import time

from .base import BaseWatcher, FileState, parse_ts
from .models import AgentKind, Status
from .summarize import CODEX_MODE, CODEX_SANDBOX, classify_tool, fmt_command, shorten


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
    def __init__(self, path: str):
        super().__init__(path)
        self.cwd = ""
        self.title = ""
        self.goal = ""
        self.last_text = ""
        self.last_cmd = ""
        self.last_tool = ""
        self.mode = ""
        self.phase = ""
        self.task_active = False
        self.input_pending = False
        self.input_summary = ""
        self.error_text = ""
        self.error_ts = 0.0
        self.done_ts = 0.0
        self.task_started_ts = 0.0
        self.last_assistant_ts = 0.0
        self.last_tool_ts = 0.0
        self.last_activity_kind = ""

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
            goal = item.get("goal") or item.get("task")
            if goal and not self.goal:
                self.goal = shorten(goal, 100)
        return ts

    def _set_error(self, payload: dict, ts: float):
        value = (payload.get("message") or payload.get("error") or
                 payload.get("reason") or payload.get("detail") or "Codex 报告错误")
        self.error_text = shorten(value, 160)
        self.error_ts = ts
        self.task_active = False
        self.input_pending = False
        self.done_ts = 0.0
        self.phase = "异常"

    def _set_input(self, payload: dict):
        self.input_pending = True
        value = payload.get("question") or payload.get("prompt") or payload.get("message")
        self.input_summary = shorten(value or "等待输入", 120)
        self.phase = "等待输入"

    def _set_assistant(self, text: str, ts: float):
        text = shorten(text, 160)
        if text:
            self.last_text = text
            self.last_assistant_ts = ts
            self.last_activity_kind = "assistant"
            self.phase = "回答"

    def _set_tool(self, name: str, detail, ts: float):
        detail_s = fmt_command(detail, 100) if detail else ""
        self.last_cmd = detail_s or self.last_cmd
        self.last_tool = classify_tool(name, detail_s, 120)
        self.last_tool_ts = ts
        self.last_activity_kind = "tool"
        self.phase = self.last_tool.split("：", 1)[0] if self.last_tool else "执行"

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
                self.title = shorten(title, 100)
            goal = payload.get("goal") or payload.get("task")
            if goal:
                self.goal = shorten(goal, 100)
            return

        if rtype == "turn_context":
            approval = CODEX_MODE.get(
                str(payload.get("approval_policy") or payload.get("approvalPolicy") or ""), "")
            sandbox = payload.get("sandbox_policy") or payload.get("sandboxPolicy") or {}
            if not isinstance(sandbox, dict):
                sandbox = {}
            sandbox_name = CODEX_SANDBOX.get(
                str(sandbox.get("type") or sandbox.get("mode") or ""), "")
            self.mode = "·".join(x for x in (approval, sandbox_name) if x)
            self.turn_id = str(payload.get("turn_id") or payload.get("turnId") or self.turn_id)
            self.goal = shorten(payload.get("goal") or payload.get("task") or self.goal, 100)
            return

        if rtype == "event_msg":
            ptype = str(payload.get("type") or "")
            if ptype in {"error", "turn_error", "task_error", "fatal_error"}:
                self._set_error(payload, ts)
            elif ptype in {"input", "input_required", "request_user_input", "user_input_required"}:
                self._set_input(payload)
            elif ptype in {"task_started", "turn_started"}:
                self.task_active = True
                self.input_pending = False
                self.done_ts = 0.0
                self.error_text = ""
                self.error_ts = 0.0
                self.task_started_ts = ts
                self.turn_id = str(payload.get("turn_id") or payload.get("turnId") or self.turn_id)
                self.goal = shorten(payload.get("goal") or payload.get("task") or self.goal, 100)
                self.phase = "处理中"
            elif ptype in {"task_complete", "turn_complete", "turn_finished"}:
                self.task_active = False
                self.input_pending = False
                self.done_ts = ts
                msg = payload.get("last_agent_message") or payload.get("message") or payload.get("result")
                if msg:
                    self._set_assistant(_text_content(msg), ts)
                self.phase = "完成"
            elif ptype in {"turn_aborted", "task_aborted", "turn_cancelled", "task_cancelled"}:
                self.task_active = False
                self.input_pending = False
                self.done_ts = 0.0
                self.phase = "已停止"
            elif ptype in {"item_completed", "item_started"}:
                item = payload.get("item") or {}
                if not isinstance(item, dict):
                    return
                item_ts = _event_ts(item, payload)
                self.turn_id = str(item.get("turn_id") or item.get("turnId") or self.turn_id)
                itype = str(item.get("type") or "")
                if itype.lower() in {"agentmessage", "assistantmessage", "message"}:
                    self._set_assistant(_text_content(item.get("text") or item.get("content")), item_ts)
                elif itype.lower() in {"commandexecution", "command_execution", "functioncall", "function_call"}:
                    command = item.get("command") or item.get("cmd") or item.get("arguments")
                    if isinstance(command, dict):
                        command = command.get("command") or command.get("cmd") or str(command)
                    self._set_tool(itype, command, item_ts)
            elif ptype in {"user_message", "input_received"}:
                self.input_pending = False
            else:
                message = payload.get("message") or payload.get("text")
                if isinstance(message, str) and message.strip():
                    self._set_assistant(message, ts)
            return

        if rtype == "response_item":
            ptype = str(payload.get("type") or "")
            if ptype == "message" and payload.get("role") == "assistant":
                texts = []
                for content in payload.get("content") or []:
                    if isinstance(content, dict):
                        texts.append(_text_content(content.get("text") or content.get("content")))
                    else:
                        texts.append(_text_content(content))
                self._set_assistant(" ".join(x for x in texts if x), ts)
            elif ptype in {"function_call", "tool_call"}:
                args = payload.get("arguments") or ""
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
                self._set_tool(name, command, ts)
            elif ptype in {"input", "request_user_input"}:
                self._set_input(payload)
            elif ptype == "error":
                self._set_error(payload, ts)
            return

        # 某些版本使用顶层 error/input 记录，保守支持但不把静默当审批。
        if str(rtype).lower() in {"error", "fatal_error"}:
            self._set_error(payload or obj, ts)
        elif str(rtype).lower() in {"input", "request_user_input", "input_required"}:
            self._set_input(payload or obj)

    def status(self, now: float, cfg: dict):
        if self.error_ts and 0 <= now - self.error_ts < 30:
            return Status.ERROR, None
        if self.input_pending:
            return Status.INPUT, None
        if self.done_ts and 0 <= now - self.done_ts < 8:
            return Status.DONE, None
        # rollout 不记录实时审批请求，静默只代表“仍由任务负责”，不能生成 WAITING。
        if self.task_active or now - self.last_event_ts < 10:
            return Status.WORKING, None
        return Status.IDLE, None

    def _summary(self) -> str:
        if self.last_activity_kind == "assistant" and self.last_text:
            return self.last_text
        if self.last_activity_kind == "tool" and self.last_tool:
            return self.last_tool
        if self.last_assistant_ts >= self.last_tool_ts and self.last_text:
            return self.last_text
        return self.last_tool or self.last_text

    def fill_snapshot(self, snap):
        snap.mode = self.mode
        snap.title = self.title
        snap.goal = self.goal or self.title
        snap.cwd = self.cwd
        snap.session_id = self.session_id or snap.session_id
        snap.turn_id = self.turn_id
        snap.phase = self.phase
        if snap.status == Status.ERROR:
            summary = self.error_text or "Codex 报告错误"
        elif snap.status == Status.INPUT:
            summary = self.input_summary or "等待输入"
        elif snap.status == Status.DONE:
            summary = self.last_text or "任务完成"
        else:
            summary = self._summary() or ("处理中" if snap.status == Status.WORKING else "待命")
        snap.summary = shorten(summary, 120)
        snap.last_line = snap.summary
        snap.exact_waiting = False
        snap.can_approve = False


class CodexWatcher(BaseWatcher):
    kind = AgentKind.CODEX

    def make_state(self, path: str) -> FileState:
        return CodexFile(path)
