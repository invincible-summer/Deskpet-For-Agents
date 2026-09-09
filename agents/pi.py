"""pi watcher：会话 JSONL 的只读状态和本地摘要（V3 观察模型）。

pi 没有通用审批弹窗；这里保留明确的输入/错误生命周期，但不根据静默
生成审批或把 cwd 当作任务标题。
"""
import json
import time

from .base import BaseWatcher, FileState, classify_phase, parse_ts
from .models import (
    AgentKind,
    Confidence,
    Observation,
    Phase,
    Status,
)
from .summarize import fmt_command, shorten

GOAL_MAX = 120
SUMMARY_MAX = 160


def _event_ts(obj: dict) -> float:
    return parse_ts(obj.get("timestamp")) or time.time()


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("value") or "")
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    return ""


class PiFile(FileState):
    kind = AgentKind.PI

    def __init__(self, path: str):
        super().__init__(path)
        self.goal = ""
        self.last_text = ""
        self.last_tool = ""
        self.last_cmd = ""
        self.input_summary = ""
        self.error_text = ""
        self.error_ts = 0.0
        self.done_ts = 0.0
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
        goal = obj.get("goal") or obj.get("task")
        if goal and not self.goal:
            self.goal = shorten(str(goal), GOAL_MAX)
        return ts

    def _set_error(self, obj: dict, ts: float):
        message = obj.get("message") or obj.get("error") or obj.get("reason")
        self.error_text = shorten(str(message or "pi 报告错误"), SUMMARY_MAX)
        self.error_ts = ts
        self.turn_known_over = True
        self.input_pending = False
        self.phase = Phase.NONE

    def _set_input(self, obj: dict):
        value = obj.get("question") or obj.get("prompt") or obj.get("message") or obj.get("text")
        self.input_pending = True
        self.input_summary = shorten(_text(value) or "等待输入", GOAL_MAX)
        self.phase = Phase.USER_INPUT

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
                self.goal = shorten(str(title), GOAL_MAX)
                self.title = self.goal
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
        if role == "user":
            self.input_pending = False
        content = msg.get("content")
        blocks = content if isinstance(content, list) else (
            [{"type": "text", "text": content}] if isinstance(content, str) else [])
        had_assistant_text = False
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            if role == "assistant" and btype in {"text", "output_text"}:
                value = _text(block.get("text") or block.get("content"))
                if value.strip():
                    self.last_text = shorten(value, SUMMARY_MAX)
                    self.last_assistant_ts = ts
                    self.last_activity_kind = "assistant"
                    self.phase = Phase.ANSWERING
                    self.turn_active = True
                    self.turn_known_over = False
                    had_assistant_text = True
            elif role == "user" and btype in {"text", "input_text"}:
                value = _text(block.get("text") or block.get("content"))
                if value.strip():
                    self.goal = shorten(value, GOAL_MAX)
                    self.turn_active = True
                    self.turn_known_over = False
            elif btype in {"thinking", "ThinkingContent", "thinking_content"}:
                self.phase = Phase.THINKING
            elif btype in {"toolCall", "tool_call"}:
                name = block.get("name") or "?"
                args = block.get("arguments")
                detail = ""
                if isinstance(args, dict):
                    detail = str(args.get("command") or args.get("path") or args.get("file_path") or "")
                elif isinstance(args, str):
                    detail = args
                self.last_cmd = fmt_command(f"{name}: {detail}" if detail else str(name), 100)
                self.last_tool = shorten(f"{name}: {detail}" if detail else str(name), SUMMARY_MAX)
                self.last_tool_ts = ts
                self.last_activity_kind = "tool"
                phase = classify_phase(str(name), detail)
                self.phase = phase if phase is not Phase.NONE else Phase.EXECUTING
            elif btype in {"toolResult", "tool_result"}:
                value = _text(block.get("content") or block.get("text") or block.get("output"))
                if value.strip():
                    self.last_text = shorten(value, SUMMARY_MAX)
                    self.last_assistant_ts = ts
                    self.last_activity_kind = "assistant"

        # v4.2.3 §8.1：pi v3 assistant.stopReason 决定 turn 生命周期；
        # 独立 role=toolResult message 只是活动证据。
        if role == "assistant":
            self._apply_stop_reason(msg, ts, had_assistant_text)
        elif role == "toolResult":
            self._apply_tool_result_message(msg, ts)

    def _apply_stop_reason(self, msg: dict, ts: float,
                           had_assistant_text: bool = False):
        """pi v3 stopReason: stop|length|toolUse|error|aborted（§8.1）。

        unknown/empty → 保持保守 activity evidence，不凭空宣布完成。
        """
        reason = str(msg.get("stopReason") or "").strip().lower()
        if reason == "tooluse":
            self.turn_active = True
            self.turn_known_over = False
            if self.phase in (Phase.NONE, Phase.ANSWERING, Phase.THINKING):
                self.phase = Phase.EXECUTING
            return
        if reason == "stop":
            self.turn_active = False
            self.turn_known_over = True
            self.done_ts = ts
            return
        if reason == "length":
            # 同 stop；可标记长度限制，但不是 ERROR
            self.turn_active = False
            self.turn_known_over = True
            self.done_ts = ts
            if not had_assistant_text:
                self.last_text = "输出达到长度限制"
                self.last_activity_kind = "assistant"
                self.last_assistant_ts = ts
            return
        if reason == "error":
            self._set_error({"message": msg.get("errorMessage")
                             or msg.get("error") or msg.get("message")}, ts)
            return
        if reason == "aborted":
            # 不伪造成功庆祝：无 DONE 窗口，直接回 IDLE
            self.turn_active = False
            self.turn_known_over = True
            self.done_ts = 0.0
            self.phase = Phase.NONE
            return
        # unknown/empty：维持 block 处理后的保守状态

    def _apply_tool_result_message(self, msg: dict, ts: float):
        """独立 role=toolResult message（pi v3，§8.1）。

        isError=True 只表示工具结果失败，不能把整个 Agent 置 ERROR——
        pi 可能继续推理并恢复；最终 turn 状态由后续 assistant stopReason
        决定。
        """
        name = str(msg.get("toolName") or msg.get("tool")
                   or msg.get("toolCallId") or "?")
        content = msg.get("content")
        if isinstance(content, list):
            value = " ".join(
                _text(item) if isinstance(item, dict) else str(item)
                for item in content)
        else:
            value = _text(content)
        value = value.strip()
        self.last_tool = shorten(f"{name}: {value}" if value else name,
                                 SUMMARY_MAX)
        self.last_tool_ts = ts
        self.last_activity_kind = "tool"
        if value:
            self.last_text = shorten(value, SUMMARY_MAX)
        phase = classify_phase(name, value)
        self.phase = phase if phase is not Phase.NONE else Phase.EXECUTING

    def _summary_text(self) -> str:
        if self.last_activity_kind == "assistant" and self.last_text:
            return self.last_text
        if self.last_activity_kind == "tool" and self.last_tool:
            return self.last_tool
        if self.last_assistant_ts >= self.last_tool_ts and self.last_text:
            return self.last_text
        return self.last_tool or self.last_text

    def observation(self, now: float, cfg: dict) -> Observation | None:
        obs = self.base_observation()
        obs.goal = self.goal

        if self.error_ts and 0 <= now - self.error_ts < 30:
            obs.status = Status.ERROR
            obs.confidence = Confidence.HIGH
            obs.summary = shorten(self.error_text or "pi 报告错误", SUMMARY_MAX)
            return obs
        if self.input_pending:
            obs.status = Status.INPUT
            obs.phase = Phase.USER_INPUT
            obs.confidence = Confidence.EXACT
            obs.summary = shorten(self.input_summary or "等待输入", SUMMARY_MAX)
            return obs
        if self.done_ts and 0 <= now - self.done_ts < 8:
            # assistant stopReason=stop/length → 短 DONE 展示窗口，之后 IDLE
            obs.status = Status.DONE
            obs.phase = Phase.NONE
            obs.confidence = Confidence.EXACT
            obs.summary = shorten(self._summary_text() or "回合完成", SUMMARY_MAX)
            return obs
        if self.turn_active:
            obs.status = Status.WORKING
            obs.phase = self.phase
            obs.turn_active = True
            obs.confidence = Confidence.HIGH
            obs.summary = shorten(self._summary_text() or "处理中", SUMMARY_MAX)
            return obs
        if self.turn_known_over:
            obs.status = Status.IDLE
            obs.confidence = Confidence.HIGH
            obs.summary = shorten(self._summary_text() or "待命", SUMMARY_MAX)
            return obs
        grace = self.activity_grace(cfg)
        anchor = max(self.last_event_ts, self.last_arrival_ts)
        if anchor and now - anchor < grace:
            obs.status = Status.WORKING
            obs.phase = self.phase
            obs.turn_active = False
            obs.expires_at = anchor + grace
            obs.confidence = Confidence.MEDIUM
            obs.summary = shorten(self._summary_text() or "处理中", SUMMARY_MAX)
            return obs
        return None


class PiWatcher(BaseWatcher):
    kind = AgentKind.PI

    def make_state(self, path: str) -> FileState:
        return PiFile(path)
