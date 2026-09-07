"""Kimi CLI watcher（只读 wire.jsonl）。

只有 wire 中明确出现的 ApprovalRequest 才会形成 WAITING；文件静默永远
不会伪造审批。普通状态和文本都在本地做有界摘要。
"""
import json
import time

from .base import BaseWatcher, FileState, parse_ts
from .models import AgentKind, ApprovalRequest, Status
from .summarize import classify_tool, fmt_command, shorten


def _event_ts(obj: dict, payload: dict) -> float:
    return (parse_ts(obj.get("timestamp")) or parse_ts(payload.get("timestamp"))
            or time.time())


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("value") or "")
    if isinstance(value, list):
        return " ".join(_text(x) for x in value)
    return ""


def _first_present(mapping: dict, *keys):
    """Read wire fields without dropping a valid numeric zero identifier."""
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return ""


class KimiFile(FileState):
    def __init__(self, path: str):
        super().__init__(path)
        self.last_text = ""
        self.last_cmd = ""
        self.last_tool = ""
        self.mode = ""
        self.phase = ""
        self.goal = ""
        self.pending = None          # dict payload；仅显式 ApprovalRequest
        self.pending_id = ""
        self.done_ts = 0.0
        self.active = False
        self.input_pending = False
        self.input_summary = ""
        self.error_text = ""
        self.error_ts = 0.0
        self.last_assistant_ts = 0.0
        self.last_tool_ts = 0.0
        self.last_activity_kind = ""

    def _touch(self, obj: dict, payload: dict) -> float:
        ts = _event_ts(obj, payload)
        self.last_event_ts = max(self.last_event_ts, ts)
        for item in (obj, payload):
            sid = item.get("session_id") or item.get("sessionId")
            if sid and not self.session_id:
                self.session_id = str(sid)
            turn = item.get("turn_id") or item.get("turnId")
            if turn:
                self.turn_id = str(turn)
            cwd = item.get("cwd") or item.get("workingDirectory")
            if cwd:
                self.cwd = str(cwd)
            goal = item.get("goal") or item.get("task") or item.get("title")
            if goal and not self.goal:
                self.goal = shorten(goal, 100)
        return ts

    def _set_error(self, payload: dict, ts: float):
        value = payload.get("message") or payload.get("error") or payload.get("reason")
        self.error_text = shorten(value or "Kimi 报告错误", 160)
        self.error_ts = ts
        self.active = False
        self.input_pending = False
        self.done_ts = 0.0
        self.phase = "异常"

    def _set_input(self, payload: dict):
        value = _first_present(payload, "question", "prompt", "message", "text")
        self.input_pending = True
        self.input_summary = shorten(_text(value) or "等待输入", 120)
        self.phase = "等待输入"

    def feed(self, line: str):
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            return
        t = str(obj.get("type") or "")
        payload = obj.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        pt = str(payload.get("type") or t)
        ts = self._touch(obj, payload)

        if pt == "TurnBegin":
            self.active = True
            self.input_pending = False
            self.done_ts = 0.0
            self.error_text = ""
            self.error_ts = 0.0
            self.phase = "处理中"
        elif pt == "TurnEnd":
            self.active = False
            self.done_ts = ts
            self.input_pending = False
            self.pending = None
            self.pending_id = ""
            self.phase = "完成"
        elif pt in ("ToolCall", "ToolCallRequest"):
            name = payload.get("name") or "?"
            args = payload.get("arguments")
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
            self.last_cmd = fmt_command(f"{name}: {detail}" if detail else name, 100)
            self.last_tool = classify_tool(name, detail, 120)
            self.last_tool_ts = ts
            self.last_activity_kind = "tool"
            self.phase = self.last_tool.split("：", 1)[0] if self.last_tool else "执行"
        elif pt == "ApprovalRequest":
            self.pending = dict(payload)
            self.pending_id = str(_first_present(
                payload, "request_id", "requestId", "id"))
            self.input_pending = False
            self.phase = "等待批复"
        elif pt == "ApprovalResponse":
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
                self.last_text = shorten(_text(body), 160)
                self.last_assistant_ts = ts
                self.last_activity_kind = "assistant"
                self.phase = "回答"
        elif pt == "StatusUpdate":
            flags = []
            if payload.get("plan_mode"):
                flags.append("计划")
                self.phase = "计划"
            usage = payload.get("context_usage")
            if isinstance(usage, (int, float)) and usage > 0:
                flags.append(f"上下文{int(usage)}%")
            goal = payload.get("goal") or payload.get("task") or payload.get("title")
            if goal:
                self.goal = shorten(goal, 100)
            if flags:
                self.mode = "·".join(flags)
        else:
            # 兜底只采纳明确文本，不根据静默推断审批。
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                self.last_text = shorten(text, 160)
                self.last_assistant_ts = ts
                self.last_activity_kind = "assistant"
                self.phase = "回答"

    def status(self, now: float, cfg: dict):
        if self.error_ts and 0 <= now - self.error_ts < 30:
            return Status.ERROR, None
        if self.pending is not None:
            summary = _approval_summary(self.pending) or self.last_cmd or "等待批复"
            request_id = _first_present(
                self.pending, "request_id", "requestId", "id")
            request = ApprovalRequest(
                summary=shorten(summary, 100), exact=True,
                request_id=request_id,
                protocol="kimi-wire",
                thread_id=str(self.pending.get("thread_id") or self.pending.get("threadId") or
                               self.thread_id or ""),
                turn_id=str(self.pending.get("turn_id") or self.pending.get("turnId") or
                            self.turn_id or ""),
                item_id=str(self.pending.get("item_id") or self.pending.get("itemId") or ""),
                state="pending",
            )
            return Status.WAITING, request
        if self.input_pending:
            return Status.INPUT, None
        if self.done_ts and 0 <= now - self.done_ts < 8:
            return Status.DONE, None
        if self.active or now - self.last_event_ts < 10:
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
        snap.goal = self.goal
        snap.title = ""
        snap.cwd = self.cwd
        snap.session_id = self.session_id or snap.session_id
        snap.turn_id = self.turn_id
        snap.phase = self.phase
        if snap.status == Status.WAITING:
            summary = _approval_summary(self.pending or {}) or self.last_cmd or "等待批复"
        elif snap.status == Status.ERROR:
            summary = self.error_text or "Kimi 报告错误"
        elif snap.status == Status.INPUT:
            summary = self.input_summary or "等待输入"
        elif snap.status == Status.DONE:
            summary = self.last_text or "回合完成"
        else:
            summary = self._summary() or ("处理中" if snap.status == Status.WORKING else "待命")
        snap.summary = shorten(summary, 120)
        snap.last_line = snap.summary
        snap.exact_waiting = snap.status == Status.WAITING
        # wire.jsonl 可精确识别请求，但没有通用安全的后台响应通道；让 UI
        # 通过兼容终端路径处理，受控协议审批仍由 managed manager 提供。
        snap.can_approve = False


def _approval_summary(p: dict) -> str:
    for key in ("title", "command", "summary", "description", "reason"):
        value = p.get(key)
        if isinstance(value, str) and value.strip():
            return shorten(value, 100)
    args = p.get("arguments")
    if isinstance(args, str) and args.strip():
        return shorten(args, 100)
    if isinstance(args, dict):
        return shorten(str(args.get("command") or args.get("path") or args), 100)
    name = p.get("name")
    return shorten(name, 100) if name else ""


class KimiWatcher(BaseWatcher):
    kind = AgentKind.KIMI

    def make_state(self, path: str) -> FileState:
        return KimiFile(path)
