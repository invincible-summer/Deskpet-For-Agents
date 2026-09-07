"""Kimi CLI watcher：~/.kimi/sessions/<md5(cwd)>/<uuid>/wire.jsonl

wire 记录为 {"type": <名称>, "payload": {...}}（已查证 wire/types.py）。
关键事件：TurnBegin / ToolCall / ToolCallRequest / ApprovalRequest / ApprovalResponse /
TurnEnd / StatusUpdate / Notification。
待批复检测（精确）：wire.jsonl 里 ApprovalRequest 出现且尚无对应 ApprovalResponse。
"""
import json
import time

from .base import BaseWatcher, FileState
from .models import AgentKind, ApprovalRequest, Status
from .summarize import fmt_command, shorten


class KimiFile(FileState):
    def __init__(self, path: str):
        super().__init__(path)
        self.last_text = ""
        self.last_cmd = ""
        self.mode = ""
        self.pending = None          # dict payload
        self.done_ts = 0.0
        self.active = False

    def feed(self, line: str):
        try:
            obj = json.loads(line)
        except ValueError:
            return
        t = obj.get("type") or ""
        payload = obj.get("payload") or {}
        pt = payload.get("type") or t
        self.last_event_ts = time.time()  # wire 记录无独立时间字段时以到达时间为准
        if pt == "TurnBegin":
            self.active = True
        elif pt == "TurnEnd":
            self.active = False
            self.done_ts = time.time()
            self.pending = None
        elif pt in ("ToolCall", "ToolCallRequest"):
            name = payload.get("name") or "?"
            args = payload.get("arguments")
            detail = ""
            if isinstance(args, str):
                try:
                    a = json.loads(args)
                    detail = str(a.get("command") or a.get("path") or a.get("file_path") or args)
                except ValueError:
                    detail = args
            elif isinstance(args, dict):
                detail = str(args.get("command") or args.get("path") or args.get("file_path") or "")
            self.last_cmd = fmt_command(f"{name}: {detail}" if detail else name, 100)
        elif pt == "ApprovalRequest":
            self.pending = payload
        elif pt == "ApprovalResponse":
            self.pending = None
        elif pt == "Notification":
            body = payload.get("body") or payload.get("title")
            if body:
                self.last_text = shorten(body, 160)
        elif pt == "StatusUpdate":
            flags = []
            if payload.get("plan_mode"):
                flags.append("Plan模式")
            usage = payload.get("context_usage")
            if isinstance(usage, (int, float)) and usage > 0:
                flags.append(f"上下文{int(usage)}%")
            if flags:
                self.mode = "·".join(flags)
        else:
            # 兜底：尽力找文本内容（TextPart / Message 等，格式随版本可能变化）
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                self.last_text = trunc(text, 160)

    def status(self, now: float, cfg: dict):
        quiet = now - self.last_event_ts
        if self.pending is not None:
            summary = _approval_summary(self.pending) or self.last_cmd
            return Status.WAITING, ApprovalRequest(summary=summary, exact=True,
                                                   request_id=str(self.pending.get("id") or ""))
        if self.done_ts and now - self.done_ts < 8:
            return Status.DONE, None
        if self.active or quiet < 10:
            return Status.WORKING, None
        return Status.IDLE, None

    def fill_snapshot(self, snap):
        snap.mode = self.mode
        if snap.status == Status.WAITING:
            snap.exact_waiting = True
            snap.last_line = f"等待批复：{_approval_summary(self.pending or {}) or self.last_cmd}"
        elif snap.status == Status.DONE:
            snap.last_line = self.last_text or "回合完成"
        elif snap.status == Status.WORKING:
            snap.last_line = self.last_cmd or self.last_text
        else:
            snap.last_line = self.last_text


def _approval_summary(p: dict) -> str:
    for key in ("title", "command", "summary", "description"):
        v = p.get(key)
        if isinstance(v, str) and v.strip():
            return shorten(v, 100)
    args = p.get("arguments")
    if isinstance(args, str) and args.strip():
        return shorten(args, 100)
    name = p.get("name")
    if name:
        return str(name)
    return ""


class KimiWatcher(BaseWatcher):
    kind = AgentKind.KIMI

    def make_state(self, path: str) -> FileState:
        return KimiFile(path)
