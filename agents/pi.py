"""pi watcher：~/.pi/agent/sessions/<编码cwd>/<ts>_<uuid>.jsonl

会话首行 {type:"session", cwd, id}；随后每行 {type, id, parentId, timestamp, ...}。
type=message 时 message.role ∈ user/assistant/toolResult；
assistant 的 content[] 块为 TextContent{text} / ThinkingContent / toolCall{name, arguments}。
已查证：pi 设计上没有审批弹窗（无权限系统），故无 waiting 状态。
"""
import json
import time

from .base import BaseWatcher, FileState, parse_ts
from .models import AgentKind, Status
from .summarize import fmt_command, shorten


class PiFile(FileState):
    def __init__(self, path: str):
        super().__init__(path)
        self.cwd = ""
        self.last_text = ""
        self.last_cmd = ""

    def feed(self, line: str):
        try:
            obj = json.loads(line)
        except ValueError:
            return
        self.last_event_ts = max(self.last_event_ts, parse_ts(obj.get("timestamp")))
        t = obj.get("type")
        if t == "session":
            self.cwd = obj.get("cwd") or self.cwd
        elif t == "message":
            msg = obj.get("message") or {}
            role = msg.get("role")
            content = msg.get("content")
            blocks = content if isinstance(content, list) else (
                [{"type": "text", "text": content}] if isinstance(content, str) else []
            )
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if role == "assistant" and btype == "text" and block.get("text", "").strip():
                    self.last_text = shorten(block["text"], 160)
                elif btype == "toolCall" or btype == "tool_call":
                    name = block.get("name") or "?"
                    args = block.get("arguments")
                    detail = ""
                    if isinstance(args, dict):
                        detail = str(args.get("command") or args.get("path") or "")
                    elif isinstance(args, str):
                        detail = args
                    self.last_cmd = fmt_command(f"{name}: {detail}" if detail else name, 100)

    def status(self, now: float, cfg: dict):
        quiet = now - self.last_event_ts
        if quiet < 10:
            return Status.WORKING, None
        return Status.IDLE, None

    def fill_snapshot(self, snap):
        snap.title = self.cwd and f"cwd {self.cwd}" or ""
        snap.last_line = (self.last_cmd or self.last_text) if snap.status == Status.WORKING \
            else self.last_text


class PiWatcher(BaseWatcher):
    kind = AgentKind.PI

    def make_state(self, path: str) -> FileState:
        return PiFile(path)
