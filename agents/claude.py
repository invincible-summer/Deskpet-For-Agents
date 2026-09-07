"""Claude Code watcher：~/.claude/projects/**.jsonl

事件格式（已查证 v2.1.x）：
  type=assistant → message.content[] 里有 text / tool_use 块
  type=user      → message.content[] 里 tool_result 块（配对 tool_use_id）
  type=ai-title / summary / last-prompt
  type=system, subtype=turn_duration → 回合结束
待批复检测：磁盘上没有"pending permission"记录（已查证），
用启发式：存在未闭合 tool_use + 文件静默 > waiting_quiet_sec → 推测等待批复。
"""
import time

from .base import BaseWatcher, FileState
from .models import AgentKind, ApprovalRequest, Status
from .summarize import CLAUDE_MODE, shorten, tool_line


def _summarize_tool(block) -> str:
    name = block.get("name", "?")
    inp = block.get("input") or {}
    detail = ""
    for key in ("command", "file_path", "path", "pattern", "url", "description", "prompt"):
        if isinstance(inp, dict) and inp.get(key):
            detail = str(inp[key])
            break
    return tool_line(name, detail, 90)


class ClaudeFile(FileState):
    def __init__(self, path: str):
        super().__init__(path)
        self.title = ""
        self.last_text = ""
        self.last_tool = ""
        self.mode = ""
        self.open_tools: dict[str, str] = {}
        self.done_ts = 0.0

    def feed(self, line: str):
        import json
        try:
            obj = json.loads(line)
        except ValueError:
            return
        self.last_event_ts = max(self.last_event_ts, _feed_ts(obj))
        t = obj.get("type")
        if t == "assistant":
            msg = obj.get("message") or {}
            for block in msg.get("content") or []:
                btype = block.get("type")
                if btype == "text" and block.get("text", "").strip():
                    self.last_text = shorten(block["text"], 160)
                elif btype == "tool_use":
                    summary = _summarize_tool(block)
                    self.last_tool = summary
                    self.open_tools[str(block.get("id"))] = summary
        elif t == "user":
            msg = obj.get("message") or {}
            content = msg.get("content")
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.open_tools.pop(str(block.get("tool_use_id")), None)
        elif t == "ai-title":
            self.title = shorten(obj.get("aiTitle") or "", 60)
        elif t == "summary":
            self.title = self.title or shorten(obj.get("summary") or "", 60)
        elif t == "permission-mode":
            mode = obj.get("mode") or obj.get("permissionMode") or ""
            self.mode = CLAUDE_MODE.get(str(mode), str(mode))
        elif t == "system" and obj.get("subtype") == "turn_duration":
            self.done_ts = time.time()
            self.open_tools.clear()

    def status(self, now: float, cfg: dict):
        quiet = now - self.last_event_ts
        waiting_quiet = cfg.get("waiting_quiet_sec", 15.0)
        approval = None
        if self.open_tools and quiet >= waiting_quiet:
            approval = ApprovalRequest(
                summary=next(iter(self.open_tools.values())), exact=False,
            )
            return Status.WAITING, approval
        if self.done_ts and now - self.done_ts < 8:
            return Status.DONE, None
        if self.open_tools or quiet < 10:
            return Status.WORKING, None
        return Status.IDLE, None

    def fill_snapshot(self, snap):
        snap.mode = self.mode
        snap.title = self.title
        if snap.status == Status.WAITING:
            snap.exact_waiting = False
            snap.last_line = f"（推测）等待批复：{next(iter(self.open_tools.values()), '')}"
        elif snap.status == Status.DONE:
            snap.last_line = self.last_text or "回合完成"
        elif snap.status == Status.WORKING:
            snap.last_line = self.last_tool or self.last_text
        else:
            snap.last_line = self.last_text


def _feed_ts(obj) -> float:
    from .base import parse_ts
    return parse_ts(obj.get("timestamp"))


class ClaudeWatcher(BaseWatcher):
    kind = AgentKind.CLAUDE

    def make_state(self, path: str) -> FileState:
        return ClaudeFile(path)
