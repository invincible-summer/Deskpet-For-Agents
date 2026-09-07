"""pi watcher：会话 JSONL 的只读状态和本地摘要。

pi 没有通用审批弹窗；这里保留明确的输入/错误生命周期，但不根据静默
生成审批或把 cwd 当作任务标题。
"""
import json
import time

from .base import BaseWatcher, FileState, parse_ts
from .models import AgentKind, Status
from .summarize import classify_tool, fmt_command, shorten


def _event_ts(obj: dict) -> float:
    return parse_ts(obj.get("timestamp")) or time.time()


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("value") or "")
    if isinstance(value, list):
        return " ".join(_text(x) for x in value)
    return ""


class PiFile(FileState):
    def __init__(self, path: str):
        super().__init__(path)
        self.cwd = ""
        self.goal = ""
        self.last_text = ""
        self.last_tool = ""
        self.last_cmd = ""
        self.phase = ""
        self.input_pending = False
        self.input_summary = ""
        self.error_text = ""
        self.error_ts = 0.0
        self.last_assistant_ts = 0.0
        self.last_tool_ts = 0.0
        self.last_activity_kind = ""

    def _touch(self, obj: dict) -> float:
        ts = _event_ts(obj)
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
        goal = obj.get("goal") or obj.get("task") or obj.get("title")
        if goal and not self.goal:
            self.goal = shorten(goal, 100)
        return ts

    def _set_error(self, obj: dict, ts: float):
        message = obj.get("message") or obj.get("error") or obj.get("reason")
        self.error_text = shorten(message or "pi 报告错误", 160)
        self.error_ts = ts
        self.input_pending = False
        self.phase = "异常"

    def _set_input(self, obj: dict):
        value = obj.get("question") or obj.get("prompt") or obj.get("message") or obj.get("text")
        self.input_pending = True
        self.input_summary = shorten(_text(value) or "等待输入", 120)
        self.phase = "等待输入"

    def feed(self, line: str):
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            return
        ts = self._touch(obj)
        t = str(obj.get("type") or "")
        if t == "session":
            self.cwd = str(obj.get("cwd") or self.cwd)
            sid = obj.get("session_id") or obj.get("sessionId") or obj.get("id")
            if sid:
                self.session_id = str(sid)
            title = obj.get("title") or obj.get("name") or obj.get("goal")
            if title:
                self.goal = shorten(title, 100)
            return

        if t in {"error", "fatal_error"}:
            self._set_error(obj, ts)
            return
        if t in {"input", "input_request", "request_user_input", "user_input_required"}:
            self._set_input(obj)
            return
        if t in {"input_received", "user_input"}:
            self.input_pending = False
            return
        if t != "message":
            return

        msg = obj.get("message") or {}
        role = msg.get("role")
        self.input_pending = False if role == "user" else self.input_pending
        content = msg.get("content")
        blocks = content if isinstance(content, list) else (
            [{"type": "text", "text": content}] if isinstance(content, str) else [])
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if role == "assistant" and btype in {"text", "output_text"}:
                value = _text(block.get("text") or block.get("content"))
                if value.strip():
                    self.last_text = shorten(value, 160)
                    self.last_assistant_ts = ts
                    self.last_activity_kind = "assistant"
                    self.phase = "回答"
            elif btype in {"thinking", "ThinkingContent", "thinking_content"}:
                self.phase = "思考"
            elif btype in {"toolCall", "tool_call"}:
                name = block.get("name") or "?"
                args = block.get("arguments")
                detail = ""
                if isinstance(args, dict):
                    detail = str(args.get("command") or args.get("path") or args.get("file_path") or "")
                elif isinstance(args, str):
                    detail = args
                self.last_cmd = fmt_command(f"{name}: {detail}" if detail else name, 100)
                self.last_tool = classify_tool(name, detail, 120)
                self.last_tool_ts = ts
                self.last_activity_kind = "tool"
                self.phase = self.last_tool.split("：", 1)[0] if self.last_tool else "执行"
            elif btype in {"toolResult", "tool_result"}:
                value = _text(block.get("content") or block.get("text") or block.get("output"))
                if value.strip():
                    self.last_text = shorten(value, 160)
                    self.last_assistant_ts = ts
                    self.last_activity_kind = "assistant"

    def status(self, now: float, cfg: dict):
        if self.error_ts and 0 <= now - self.error_ts < 30:
            return Status.ERROR, None
        if self.input_pending:
            return Status.INPUT, None
        if now - self.last_event_ts < 10:
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
        snap.title = ""
        snap.goal = self.goal
        snap.cwd = self.cwd
        snap.session_id = self.session_id or snap.session_id
        snap.turn_id = self.turn_id
        snap.phase = self.phase
        if snap.status == Status.ERROR:
            summary = self.error_text or "pi 报告错误"
        elif snap.status == Status.INPUT:
            summary = self.input_summary or "等待输入"
        else:
            summary = self._summary() or ("处理中" if snap.status == Status.WORKING else "待命")
        snap.summary = shorten(summary, 120)
        snap.last_line = snap.summary
        snap.exact_waiting = False
        snap.can_approve = False


class PiWatcher(BaseWatcher):
    kind = AgentKind.PI

    def make_state(self, path: str) -> FileState:
        return PiFile(path)
