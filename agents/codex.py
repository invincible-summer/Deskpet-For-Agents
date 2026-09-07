"""Codex watcher：~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

已查证：每行写后立即 flush（可安全 tail）。记录类型：
  session_meta / turn_context / response_item(message|function_call|...) /
  event_msg(task_started|task_complete|item_completed|token_count|...)
审批事件（exec_approval_request 等）只走内存事件流、不写 rollout（已查证 0 命中）。
降级策略：task_started 后长时间无新事件 → 推测等待批复；
辅助尝试 tail ~/.codex/log/codex-tui.log 中的 approval 痕迹。
"""
import json
import os
import time

from .base import BaseWatcher, FileState, parse_ts
from .models import AgentKind, ApprovalRequest, Status
from .summarize import CODEX_MODE, CODEX_SANDBOX, fmt_command, shorten


class CodexFile(FileState):
    def __init__(self, path: str):
        super().__init__(path)
        self.cwd = ""
        self.last_text = ""
        self.last_cmd = ""
        self.mode = ""
        self.task_active = False
        self.done_ts = 0.0
        self.task_started_ts = 0.0

    def feed(self, line: str):
        try:
            obj = json.loads(line)
        except ValueError:
            return
        self.last_event_ts = max(self.last_event_ts, parse_ts(obj.get("timestamp")))
        rtype = obj.get("type")
        payload = obj.get("payload") or {}
        if rtype == "session_meta":
            self.cwd = payload.get("cwd") or self.cwd
        elif rtype == "turn_context":
            approval = CODEX_MODE.get(str(payload.get("approval_policy") or ""), "")
            sandbox = payload.get("sandbox_policy") or {}
            sandbox_name = CODEX_SANDBOX.get(str(sandbox.get("type") or ""), "")
            self.mode = "·".join(x for x in (approval, sandbox_name) if x)
        elif rtype == "event_msg":
            ptype = payload.get("type")
            if ptype == "task_started":
                self.task_active = True
                self.task_started_ts = time.time()
            elif ptype == "task_complete":
                self.task_active = False
                self.done_ts = time.time()
                msg = payload.get("last_agent_message")
                if msg:
                    self.last_text = trunc(msg, 160)
            elif ptype == "item_completed":
                item = payload.get("item") or {}
                itype = item.get("type")
                if itype == "AgentMessage":
                    texts = item.get("text") or ""
                    if texts:
                        self.last_text = shorten(texts, 160)
                elif itype == "CommandExecution":
                    cmd = fmt_command(item.get("command"), 100)
                    if cmd:
                        self.last_cmd = cmd
            elif ptype == "turn_aborted":
                self.task_active = False
        elif rtype == "response_item":
            ptype = payload.get("type")
            if ptype == "message" and payload.get("role") == "assistant":
                texts = []
                for c in payload.get("content") or []:
                    t = c.get("text")
                    if t:
                        texts.append(t)
                if texts:
                    self.last_text = shorten(" ".join(texts), 160)
            elif ptype == "function_call":
                args = payload.get("arguments") or ""
                try:
                    a = json.loads(args)
                    cmd = a.get("cmd") or a.get("command") or ""
                    cmd = fmt_command(cmd, 100)
                    if cmd:
                        self.last_cmd = cmd
                except ValueError:
                    pass

    def status(self, now: float, cfg: dict):
        quiet = now - self.last_event_ts
        waiting_quiet = cfg.get("waiting_quiet_sec", 15.0)
        if self.task_active and quiet >= waiting_quiet:
            return Status.WAITING, ApprovalRequest(
                summary=self.last_cmd or "（无法读取命令内容）", exact=False,
            )
        if self.done_ts and now - self.done_ts < 8:
            return Status.DONE, None
        if quiet < 10 or (self.task_active and quiet < waiting_quiet):
            return Status.WORKING, None
        return Status.IDLE, None

    def fill_snapshot(self, snap):
        snap.mode = self.mode
        snap.title = f"cwd {self.cwd}" if self.cwd else ""
        if snap.status == Status.WAITING:
            snap.exact_waiting = False
            snap.last_line = f"（推测）等待批复：{self.last_cmd}"
        elif snap.status == Status.DONE:
            snap.last_line = self.last_text or "任务完成"
        elif snap.status == Status.WORKING:
            snap.last_line = self.last_cmd or self.last_text
        else:
            snap.last_line = self.last_text


class CodexWatcher(BaseWatcher):
    kind = AgentKind.CODEX

    def make_state(self, path: str) -> FileState:
        return CodexFile(path)
