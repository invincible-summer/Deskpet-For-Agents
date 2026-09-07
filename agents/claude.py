"""Claude Code watcher（只读兼容模式）。

Claude 的权限暂停不会可靠地写入会话 JSONL。未闭合 tool_use 加静默只
能说明会话暂时没有新记录，不能证明有一个可安全批准的请求，因此本
watcher 不由静默生成 WAITING/ApprovalRequest。
"""
import json
import time

from .base import BaseWatcher, FileState, parse_ts
from .models import AgentKind, Status
from .summarize import CLAUDE_MODE, classify_tool, shorten


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


def _summarize_tool(block) -> str:
    name = block.get("name", "?")
    inp = block.get("input") or {}
    detail = ""
    if isinstance(inp, dict):
        for key in ("command", "file_path", "path", "pattern", "url", "description", "prompt"):
            if inp.get(key):
                detail = str(inp[key])
                break
    return classify_tool(name, detail, 120)


class ClaudeFile(FileState):
    def __init__(self, path: str):
        super().__init__(path)
        self.title = ""
        self.goal = ""
        self.last_text = ""
        self.last_tool = ""
        self.mode = ""
        self.open_tools: dict[str, str] = {}
        self.done_ts = 0.0
        self.error_text = ""
        self.error_ts = 0.0
        self.input_pending = False
        self.input_summary = ""
        self.turn_active = False
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
        self.error_text = shorten(message or "Claude 报告错误", 160)
        self.error_ts = ts
        self.turn_active = False
        self.input_pending = False
        self.done_ts = 0.0
        self.phase = "异常"

    def _set_input(self, obj: dict):
        value = _first_present(obj, "question", "prompt", "message")
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

        if t == "assistant":
            self.turn_active = True
            self.done_ts = 0.0
            msg = obj.get("message") or {}
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and _text(block.get("text")).strip():
                    self.last_text = shorten(_text(block.get("text")), 160)
                    self.last_assistant_ts = ts
                    self.last_activity_kind = "assistant"
                    self.phase = "回答"
                elif btype == "tool_use":
                    summary = _summarize_tool(block)
                    self.last_tool = summary
                    self.last_tool_ts = ts
                    self.last_activity_kind = "tool"
                    self.phase = summary.split("：", 1)[0] if summary else "执行"
                    self.open_tools[str(block.get("id") or "")] = summary
            return

        if t == "user":
            self.input_pending = False
            msg = obj.get("message") or {}
            content = msg.get("content")
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.open_tools.pop(str(block.get("tool_use_id") or ""), None)
            # 普通 user 记录是下一回合输入，不是 permission approval。
            if any(not (isinstance(block, dict) and block.get("type") == "tool_result")
                   for block in blocks):
                self.turn_active = True
                self.done_ts = 0.0
            return

        if t == "ai-title":
            self.title = shorten(obj.get("aiTitle") or "", 100)
            self.goal = self.goal or self.title
        elif t == "summary":
            title = shorten(obj.get("summary") or "", 100)
            self.title = self.title or title
            self.goal = self.goal or title
        elif t == "permission-mode":
            mode = obj.get("mode") or obj.get("permissionMode") or ""
            self.mode = CLAUDE_MODE.get(str(mode), str(mode))
        elif t in {"input", "input_request", "request_user_input", "user_input_required"}:
            self._set_input(obj)
        elif t in {"error", "fatal_error"} or (t == "system" and obj.get("subtype") == "error"):
            self._set_error(obj, ts)
        elif t == "system" and obj.get("subtype") in {"turn_duration", "turn_complete", "turn_finished"}:
            # 使用记录里的时间，历史回放不能伪造“刚完成”。
            self.done_ts = ts
            self.turn_active = False
            self.input_pending = False
            self.open_tools.clear()
            self.phase = "完成"
        elif t in {"result", "turn_complete", "turn_finished"}:
            self.done_ts = ts
            self.turn_active = False
            result = obj.get("result") or obj.get("message")
            if result:
                self.last_text = shorten(_text(result), 160)
                self.last_assistant_ts = ts
                self.last_activity_kind = "assistant"
            self.phase = "完成"

    def status(self, now: float, cfg: dict):
        if self.error_ts and 0 <= now - self.error_ts < 30:
            return Status.ERROR, None
        if self.input_pending:
            return Status.INPUT, None
        if self.done_ts and 0 <= now - self.done_ts < 8:
            return Status.DONE, None
        # open_tools + 静默只说明没有新的磁盘记录，不能生成审批请求。
        if self.turn_active or now - self.last_event_ts < 10:
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
            summary = self.error_text or "Claude 报告错误"
        elif snap.status == Status.INPUT:
            summary = self.input_summary or "等待输入"
        elif snap.status == Status.DONE:
            summary = self.last_text or "回合完成"
        else:
            summary = self._summary() or ("处理中" if snap.status == Status.WORKING else "待命")
        snap.summary = shorten(summary, 120)
        snap.last_line = snap.summary
        snap.exact_waiting = False
        snap.can_approve = False


class ClaudeWatcher(BaseWatcher):
    kind = AgentKind.CLAUDE

    def make_state(self, path: str) -> FileState:
        return ClaudeFile(path)
