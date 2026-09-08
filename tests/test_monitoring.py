"""V3 纯监听测试：无 Tk 窗口、无子进程、无真实 Agent（plan.md §56-§61）。"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.base import parse_ts
from agents.claude import ClaudeFile, ClaudeWatcher
from agents.codex import CodexFile, CodexWatcher
from agents.kimi import KimiFile, KimiWatcher
from agents.models import (
    AgentInstance,
    AgentKind,
    Confidence,
    EvidenceSource,
    Mode,
    Observation,
    Phase,
    Snapshot,
    SourceProbeSnapshot,
    Status,
)


def probe(sources: dict, gen: int = 1, t: float = 1000.0):
    """构造 dict[str, SourceProbeSnapshot] 的便捷 helper。"""
    out = {}
    for src, (auth, insts) in sources.items():
        out[src] = SourceProbeSnapshot(
            source=src, generation=gen, observed_at=t,
            authoritative=auth, instances=tuple(insts),
            error="" if auth else "probe failed")
    return out
from agents.discovery import scan_windows
from agents.monitor import Monitor
from agents.terminal_service import WindowsTerminalService
from agents.terminal_uia import TerminalEvent


class _FakeExitWatcher:
    def __init__(self, events):
        self._events = events

    def drain(self):
        events, self._events = self._events, []
        return events

    def register(self, inst):
        return True

    def unregister(self, key):
        pass

    def watched_count(self):
        return 0
from agents.summarize import classify_tool, fmt_command, shorten
from agents.tailer import FileTailer


class MemoryConfig:
    """Small dotted config replacement for monitor-only tests."""

    def __init__(self, data=None):
        self.data = data or {
            "monitor": {
                "agents": {kind.value: True for kind in AgentKind},
                "windows_enabled": True,
                "wsl_enabled": False,
                "windows_scan_sec": 3600,
                "wsl_scan_sec": 3600,
                "file_poll_sec": 0.5,
                "session_scan_sec": 3600,
                "gone_grace_sec": 30,
                "activity_grace_sec": 10,
            },
            "privacy": {"goal_max_chars": 120, "summary_max_chars": 160},
        }

    def get(self, path, default=None):
        node = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path, value):
        node = self.data
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def save(self):
        pass


def _line(path: Path, obj: dict):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(obj, ensure_ascii=False) + "\n")


class SummaryAndTailerTests(unittest.TestCase):
    def test_summary_is_bounded_and_classifies_without_copying_full_output(self):
        self.assertEqual(shorten("\x1b[31m# **标题**\x1b[0m\n第二行", 30), "标题")
        self.assertLessEqual(len(shorten("abc。" * 100, 20)), 20)
        self.assertEqual(fmt_command(["bash", "-lc", "pytest -q"]), "bash -lc pytest -q")
        self.assertIn("测试", classify_tool("Bash", "pytest -q", 30))

    def test_tailer_handles_partial_lines_rotation_and_oversized_records(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            path.write_bytes(b'{"a":1')
            tailer = FileTailer(str(path))
            self.assertEqual(list(tailer.poll()), [])
            with path.open("ab") as stream:
                stream.write(b'}\nsmall\n')
            self.assertEqual(list(tailer.poll()), ['{"a":1}', "small"])

            # On filesystems with a zero inode, ordinary append must not look
            # like replacement and replay the already consumed first record.
            with path.open("ab") as stream:
                stream.write(b"later\n")
            self.assertEqual(list(tailer.poll()), ["later"])

            replacement = Path(temp) / "replacement.jsonl"
            replacement.write_text("new-file\n", encoding="utf-8")
            os.replace(replacement, path)
            self.assertEqual(list(tailer.poll()), ["new-file"])

            path.write_bytes(b"x" * (FileTailer.MAX_LINE + 10) + b"\nvalid\n")
            self.assertEqual(list(tailer.poll()), ["valid"])
            tailer.close()
            self.assertEqual(tailer.pos, 0)

    def test_parse_ts_does_not_turn_missing_or_bad_time_into_now(self):
        self.assertEqual(parse_ts(None), 0.0)
        self.assertEqual(parse_ts("not-a-time"), 0.0)
        self.assertEqual(parse_ts("2020-01-01T00:00:00Z"), 1577836800.0)


# ============================================================ Codex fixtures

class CodexWatcherTests(unittest.TestCase):
    def test_goal_from_latest_user_message(self):
        state = CodexFile("rollout-x.jsonl")
        state.feed(json.dumps({
            "type": "event_msg",
            "timestamp": "2020-01-01T00:00:00Z",
            "payload": {"type": "user_message", "message": "帮我把登录系统迁移成 JWT 并跑测试"},
        }))
        obs = state.observation(time.time(), {})
        self.assertEqual(obs.status, Status.WORKING)
        self.assertEqual(obs.goal, "帮我把登录系统迁移成 JWT 并跑测试")
        self.assertTrue(obs.turn_active)
        # 新的 user message 更新 Goal（不是只保留第一条）
        state.feed(json.dumps({
            "type": "event_msg",
            "timestamp": "2020-01-01T00:01:00Z",
            "payload": {"type": "user_message", "message": "再补充单元测试"},
        }))
        self.assertEqual(state.observation(time.time(), {}).goal, "再补充单元测试")

    def test_task_started_carries_collaboration_mode_kind(self):
        state = CodexFile("rollout-x.jsonl")
        state.feed(json.dumps({
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "t1",
                        "collaboration_mode_kind": "plan"},
        }))
        obs = state.observation(time.time(), {})
        self.assertEqual(obs.status, Status.WORKING)
        self.assertEqual(obs.mode, Mode.PLAN)
        self.assertEqual(obs.phase, Phase.THINKING)
        # turn_context 的 approval/sandbox 不应覆盖 Plan mode
        state.feed(json.dumps({
            "type": "turn_context",
            "payload": {"cwd": "/w", "approval_policy": "on-request",
                        "sandbox_policy": {"type": "workspace-write"}},
        }))
        obs = state.observation(time.time(), {})
        self.assertEqual(obs.mode, Mode.PLAN)
        self.assertEqual(state.policy, "按需审批·工作区写入")

    def test_phase_mapping_read_code_test_exec_answer(self):
        state = CodexFile("rollout-x.jsonl")
        state.feed(json.dumps({"type": "event_msg", "payload": {
            "type": "task_started", "collaboration_mode_kind": "default"}}))
        base = time.time()
        state.feed(json.dumps({"type": "response_item", "timestamp": base + 1, "payload": {
            "type": "function_call", "name": "read", "arguments": '{"path": "/a.py"}'}}))
        self.assertEqual(state.phase, Phase.READING)
        state.feed(json.dumps({"type": "response_item", "timestamp": base + 2, "payload": {
            "type": "function_call", "name": "apply_patch", "arguments": '{"path": "/a.py"}'}}))
        self.assertEqual(state.phase, Phase.CODING)
        state.feed(json.dumps({"type": "response_item", "timestamp": base + 3, "payload": {
            "type": "function_call", "name": "shell", "arguments": '{"cmd": ["pytest", "-q"]}'}}))
        self.assertEqual(state.phase, Phase.TESTING)
        state.feed(json.dumps({"type": "response_item", "timestamp": base + 4, "payload": {
            "type": "function_call", "name": "shell", "arguments": '{"cmd": ["cargo", "build"]}'}}))
        self.assertEqual(state.phase, Phase.EXECUTING)
        state.feed(json.dumps({"type": "event_msg", "timestamp": base + 5, "payload": {
            "type": "agent_message", "message": "已完成迁移"}}))
        self.assertEqual(state.phase, Phase.ANSWERING)
        state.feed(json.dumps({"type": "event_msg", "timestamp": base + 6, "payload": {
            "type": "task_complete", "last_agent_message": "完成", "turn_id": "t1"}}))
        obs = state.observation(base + 6.5, {})
        self.assertEqual(obs.status, Status.DONE)

    def test_paginated_item_completed_command_execution(self):
        state = CodexFile("rollout-x.jsonl")
        state.feed(json.dumps({"type": "event_msg", "payload": {
            "type": "item_completed",
            "item": {"type": "command_execution", "command": ["pytest", "-q"],
                     "status": "completed"}}}))
        self.assertEqual(state.phase, Phase.TESTING)

    def test_error_and_abort_lifecycle(self):
        state = CodexFile("rollout-x.jsonl")
        base = time.time()
        state.feed(json.dumps({"type": "event_msg", "timestamp": base, "payload": {
            "type": "error", "message": "boom"}}))
        self.assertEqual(state.observation(base + 1, {}).status, Status.ERROR)
        # 新 turn 开始立刻清除 ERROR
        state.feed(json.dumps({"type": "event_msg", "timestamp": base + 2, "payload": {
            "type": "task_started", "collaboration_mode_kind": "default"}}))
        self.assertEqual(state.observation(base + 3, {}).status, Status.WORKING)
        state.feed(json.dumps({"type": "event_msg", "timestamp": base + 4, "payload": {
            "type": "turn_aborted", "reason": "interrupted"}}))
        self.assertEqual(state.observation(base + 5, {}).status, Status.IDLE)

    def test_silence_never_creates_a_guessed_approval(self):
        state = CodexFile("codex.jsonl")
        state.feed(json.dumps({
            "type": "event_msg",
            "timestamp": "2020-01-01T00:00:00Z",
            "payload": {"type": "task_started", "task": "old task",
                        "collaboration_mode_kind": "default"},
        }))
        obs = state.observation(time.time() + 120, {})
        self.assertEqual(obs.status, Status.WORKING)   # active turn 已知 → 无限保持
        state.feed(json.dumps({
            "type": "event_msg",
            "timestamp": "2020-01-01T00:00:05Z",
            "payload": {"type": "task_complete", "last_agent_message": "finished"},
        }))
        self.assertEqual(state.observation(time.time(), {}).status, Status.IDLE)

    def test_activity_only_evidence_degrades_to_unknown_not_idle(self):
        state = CodexFile("codex.jsonl")
        # 只有零散活动事件，从未见过 turn 生命周期
        recent = time.time() - 5
        state.feed(json.dumps({
            "type": "event_msg",
            "timestamp": recent,
            "payload": {"type": "token_count", "info": None},
        }))
        obs = state.observation(time.time(), {"activity_grace_sec": 10})
        self.assertEqual(obs.status, Status.WORKING)
        self.assertFalse(obs.turn_active)
        self.assertEqual(obs.confidence, Confidence.MEDIUM)
        # 过了宽限期 → 无观察（UNKNOWN），不伪造 IDLE
        self.assertIsNone(state.observation(time.time() + 60, {}))


# ============================================================ Claude fixtures

class ClaudeWatcherTests(unittest.TestCase):
    def test_user_prompt_becomes_goal_and_tool_result_is_not_goal(self):
        state = ClaudeFile("claude.jsonl")
        base = time.time()
        state.feed(json.dumps({
            "type": "user", "timestamp": base,
            "message": {"content": [{"type": "text", "text": "优化 WSL Agent 监听"}]},
        }))
        state.feed(json.dumps({
            "type": "user", "timestamp": base + 1,
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                     "content": "noise"}]},
        }))
        obs = state.observation(base + 2, {})
        self.assertEqual(obs.goal, "优化 WSL Agent 监听")
        self.assertNotIn("noise", obs.goal)

    def test_permission_mode_normalized(self):
        state = ClaudeFile("claude.jsonl")
        state.feed(json.dumps({"type": "permission-mode", "mode": "plan"}))
        self.assertEqual(state.mode, Mode.PLAN)
        state.feed(json.dumps({"type": "permission-mode", "mode": "acceptEdits"}))
        self.assertEqual(state.mode, Mode.ACCEPT_EDITS)
        state.feed(json.dumps({"type": "permission-mode", "mode": "default"}))
        self.assertEqual(state.mode, Mode.DEFAULT)

    def test_tool_use_phase_and_turn_complete(self):
        state = ClaudeFile("claude.jsonl")
        base = time.time()
        state.feed(json.dumps({
            "type": "assistant", "timestamp": base,
            "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/a.py"}},
            ]},
        }))
        obs = state.observation(base + 1, {})
        self.assertEqual(obs.status, Status.WORKING)
        self.assertEqual(obs.phase, Phase.READING)
        state.feed(json.dumps({
            "type": "result", "timestamp": base + 2, "result": "done",
        }))
        self.assertEqual(state.observation(base + 2.5, {}).status, Status.DONE)
        self.assertEqual(state.observation(base + 20, {}).status, Status.IDLE)

    def test_old_completion_is_not_new_done_or_waiting(self):
        state = ClaudeFile("claude.jsonl")
        state.feed(json.dumps({
            "type": "assistant",
            "timestamp": "2020-01-01T00:00:00Z",
            "message": {"content": [{
                "type": "tool_use", "id": "tool-1", "name": "Bash",
                "input": {"command": "pytest -q"},
            }]},
        }))
        state.feed(json.dumps({
            "type": "system", "subtype": "turn_duration",
            "timestamp": "2020-01-01T00:00:01Z",
        }))
        obs = state.observation(time.time(), {})
        self.assertEqual(obs.status, Status.IDLE)

    def test_open_tool_plus_silence_never_waits(self):
        state = ClaudeFile("claude.jsonl")
        state.feed(json.dumps({
            "type": "assistant", "timestamp": "2020-01-01T00:00:00Z",
            "message": {"content": [{"type": "tool_use", "id": "t", "name": "Bash",
                                     "input": {"command": "ls"}}]},
        }))
        obs = state.observation(time.time() + 999, {})
        # turn 已知 active（assistant 活动后没有完成记录）→ 仍 WORKING，
        # 但绝不因静默变成 WAITING
        self.assertIn(obs.status, (Status.WORKING, Status.UNKNOWN))
        self.assertIsNot(obs.status, Status.WAITING)


# ============================================================ Kimi fixtures

class KimiWatcherTests(unittest.TestCase):
    def _wire(self, path: Path):
        return str(path / "agents" / "main" / "wire.jsonl")

    def test_real_cli_wire_format(self):
        """实机验证过的真实 wire 格式：顶层点分事件 + append_loop_event 包裹。"""
        state = KimiFile("wire.jsonl")
        base_ms = int(time.time() * 1000) - 5000
        state.feed(json.dumps({"type": "metadata", "protocol_version": "1.5",
                               "created_at": base_ms}))
        state.feed(json.dumps({"type": "prompt.accepted", "agentId": "main",
                               "content": [{"type": "text", "text": "帮我重构 WSL 监听"}],
                               "time": base_ms + 10}))
        state.feed(json.dumps({"type": "plan_mode.enter", "agentId": "main",
                               "time": base_ms + 20}))
        state.feed(json.dumps({"type": "permission.set_mode", "mode": "manual",
                               "time": base_ms + 30}))
        state.feed(json.dumps({"type": "context.append_loop_event",
                               "event": {"type": "step.begin", "turnId": "0", "step": 1},
                               "time": base_ms + 100}))
        state.feed(json.dumps({"type": "context.append_loop_event",
                               "event": {"type": "tool.call", "toolCallId": "Edit_0",
                                         "name": "Edit", "args": {"file_path": "/a.py"}},
                               "time": base_ms + 200}))
        obs = state.observation(time.time(), {})
        self.assertEqual(obs.status, Status.WORKING)
        self.assertEqual(obs.mode, Mode.PLAN)          # plan_mode.enter → EXACT
        self.assertEqual(obs.goal, "帮我重构 WSL 监听")   # prompt.accepted → Goal
        self.assertEqual(obs.phase, Phase.CODING)
        self.assertEqual(state.permission_mode, "manual")
        state.feed(json.dumps({"type": "context.append_loop_event",
                               "event": {"type": "content.part",
                                         "part": {"type": "text", "text": "已完成重构"}},
                               "time": base_ms + 300}))
        self.assertEqual(state.phase, Phase.ANSWERING)
        state.feed(json.dumps({"type": "turn.ended", "agentId": "main",
                               "turnId": 0, "reason": "complete",
                               "durationMs": 8000, "time": base_ms + 400}))
        self.assertEqual(state.observation(time.time(), {}).status, Status.DONE)

    def test_wire_ms_time_is_used_not_wall_clock(self):
        state = KimiFile("wire.jsonl")
        old_ms = 1788000000000   # 很旧的毫秒时间
        state.feed(json.dumps({"type": "context.append_loop_event",
                               "event": {"type": "step.end"}, "time": old_ms}))
        # 旧事件不应制造“近期活动”（无 turn 生命周期 → None/UNKNOWN）
        self.assertIsNone(state.observation(time.time(), {"activity_grace_sec": 10}))

    def test_new_layout_state_and_wire(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session_dir = root / "sessions" / "key1" / "s1"
            (session_dir / "agents" / "main").mkdir(parents=True)
            (session_dir / "state.json").write_text(json.dumps({
                "title": "重构监控", "lastPrompt": "重构 WSL 监听"}), encoding="utf-8")
            wire = session_dir / "agents" / "main" / "wire.jsonl"
            base = time.time()
            _line(wire, {"type": "TurnBegin", "timestamp": base})
            _line(wire, {"type": "StatusUpdate", "timestamp": base + 1,
                         "payload": {"type": "StatusUpdate", "plan_mode": True}})
            _line(wire, {"type": "ToolCall", "timestamp": base + 2,
                         "payload": {"type": "ToolCall", "name": "edit",
                                     "arguments": {"file_path": "/a.py"}}})
            state = KimiFile(str(wire))
            with wire.open(encoding="utf-8") as fh:
                for line in fh:
                    state.feed(line)
            obs = state.observation(base + 3, {})
            self.assertEqual(obs.status, Status.WORKING)
            self.assertEqual(obs.mode, Mode.PLAN)
            self.assertEqual(obs.phase, Phase.CODING)

    def test_wire_user_prompt_updates_goal(self):
        state = KimiFile("wire.jsonl")
        state.feed(json.dumps({"type": "UserPrompt", "timestamp": time.time(),
                               "payload": {"type": "UserPrompt", "prompt": "重构 WSL 监听"}}))
        self.assertEqual(state.goal, "重构 WSL 监听")
        state.feed(json.dumps({"type": "UserPrompt", "timestamp": time.time(),
                               "payload": {"type": "UserPrompt", "prompt": "再跑一次测试"}}))
        self.assertEqual(state.goal, "再跑一次测试")

    def test_state_json_last_prompt_loaded_by_watcher(self):
        with tempfile.TemporaryDirectory() as temp:
            session_dir = Path(temp) / "s1"
            (session_dir / "agents" / "main").mkdir(parents=True)
            (session_dir / "state.json").write_text(json.dumps({
                "title": "重构监控", "lastPrompt": "重构 WSL 监听"}), encoding="utf-8")
            wire = session_dir / "agents" / "main" / "wire.jsonl"
            wire.write_text("", encoding="utf-8")
            watcher = KimiWatcher({})
            inst = AgentInstance(AgentKind.KIMI, 1, "wsl:Ubuntu", cwd="/w",
                                 home="/home/u", process_token="7")
            from agents import paths as paths_mod
            index_text = json.dumps({"sessionId": "s1", "sessionDir": str(session_dir),
                                     "workDir": "/w"})
            with patch.object(paths_mod, "read_kimi_index_tail", return_value=[
                    {"sessionId": "s1", "sessionDir": str(session_dir), "workDir": "/w"}]):
                candidates = watcher.extra_candidates("wsl:Ubuntu", [inst])
            self.assertEqual(len(candidates), 1)
            self.assertTrue(candidates[0][1].endswith("wire.jsonl"))

    def test_approval_request_exact_and_response_clears(self):
        state = KimiFile("wire.jsonl")
        state.feed(json.dumps({
            "type": "ApprovalRequest",
            "timestamp": "2020-01-01T00:00:00Z",
            "payload": {"type": "ApprovalRequest", "id": 0,
                        "thread_id": "thread-1", "turn_id": "turn-1", "item_id": "item-1",
                        "command": "pytest -q"},
        }))
        obs = state.observation(time.time(), {})
        self.assertEqual(obs.status, Status.WAITING)
        self.assertEqual(obs.phase, Phase.APPROVAL)
        self.assertEqual(obs.confidence, Confidence.EXACT)
        self.assertIn("pytest", obs.summary)
        # 未匹配 id 的 response 不清除；匹配的清除
        state.feed(json.dumps({"type": "ApprovalResponse", "payload": {"type": "ApprovalResponse", "id": 1}}))
        self.assertEqual(state.observation(time.time(), {}).status, Status.WAITING)
        state.feed(json.dumps({"type": "ApprovalResponse", "payload": {"type": "ApprovalResponse", "id": 0}}))
        obs = state.observation(time.time(), {})
        self.assertIsNot(obs.status, Status.WAITING)

    def test_turn_end_done_then_idle(self):
        state = KimiFile("wire.jsonl")
        base = time.time()
        state.feed(json.dumps({"type": "TurnBegin", "timestamp": base}))
        state.feed(json.dumps({"type": "TurnEnd", "timestamp": base + 1}))
        self.assertEqual(state.observation(base + 2, {}).status, Status.DONE)
        self.assertEqual(state.observation(base + 20, {}).status, Status.IDLE)


# ============================================================ 绑定

class BindingTests(unittest.TestCase):
    def test_base_binds_by_session_identity_and_leaves_ambiguous_files_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / "one.jsonl", Path(temp) / "two.jsonl"
            first.write_text(json.dumps({
                "type": "session_meta", "payload": {"session_id": "s1", "cwd": "/one"}
            }) + "\n", encoding="utf-8")
            second.write_text(json.dumps({
                "type": "session_meta", "payload": {"session_id": "s2", "cwd": "/two"}
            }) + "\n", encoding="utf-8")
            watcher = CodexWatcher({"active_file_window_sec": 3600, "session_scan_sec": 1})
            one = AgentInstance(AgentKind.CODEX, 1, "windows", session_id="s1")
            two = AgentInstance(AgentKind.CODEX, 2, "windows", session_id="s2")
            active = [(first.stat().st_mtime, str(first)), (second.stat().st_mtime, str(second))]
            with patch("agents.base.paths.session_files", return_value=active):
                observations = watcher.poll([one, two])
            self.assertEqual(observations[one.key].session_file, str(first))
            self.assertEqual(observations[two.key].session_file, str(second))

            ambiguous = CodexWatcher({"active_file_window_sec": 3600})
            no_id = Path(temp) / "three.jsonl"
            no_id.write_text('{"type":"event_msg","payload":{"type":"token_count"}}\n', encoding="utf-8")
            candidates = [(no_id.stat().st_mtime, str(no_id)), (second.stat().st_mtime, str(second))]
            a = AgentInstance(AgentKind.CODEX, 3, "windows")
            b = AgentInstance(AgentKind.CODEX, 4, "windows")
            with patch("agents.base.paths.session_files", return_value=candidates):
                observations = ambiguous.poll([a, b])
            self.assertTrue(all(not observations[k].session_bound for k in (a.key, b.key)))

    def test_instance_cwd_from_proc_wins_binding(self):
        """V3：/proc cwd 让两个同目录外的实例按项目区分。"""
        with tempfile.TemporaryDirectory() as temp:
            fa = Path(temp) / "a.jsonl"
            fb = Path(temp) / "b.jsonl"
            fa.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": "/proj/a"}}) + "\n", encoding="utf-8")
            fb.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": "/proj/b"}}) + "\n", encoding="utf-8")
            watcher = CodexWatcher({"active_file_window_sec": 3600, "session_scan_sec": 1})
            ia = AgentInstance(AgentKind.CODEX, 1, "wsl:Ubuntu", cwd="/proj/a")
            ib = AgentInstance(AgentKind.CODEX, 2, "wsl:Ubuntu", cwd="/proj/b")
            with patch("agents.base.paths.session_files",
                       return_value=[(fa.stat().st_mtime, str(fa)), (fb.stat().st_mtime, str(fb))]), \
                 patch.object(CodexWatcher, "_roots_for_source", return_value=[temp]):
                observations = watcher.poll([ia, ib])
            self.assertEqual(observations[ia.key].session_file, str(fa))
            self.assertEqual(observations[ib.key].session_file, str(fb))

    def test_runtime_binding_survives_directory_scan_exclusion(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manual.jsonl"
            path.write_text('{"type":"event_msg","payload":{"type":"token_count"}}\n', encoding="utf-8")
            instance = AgentInstance(AgentKind.CODEX, 10, "windows")
            watcher = CodexWatcher({"active_file_window_sec": 1})
            watcher.set_runtime_binding(instance.key, str(path))
            with patch("agents.base.paths.session_files", return_value=[]):
                observations = watcher.poll([instance])
            self.assertEqual(observations[instance.key].session_file, str(path))
            # 运行期 override 不写配置
            self.assertNotIn("session_bindings", watcher.cfg)


# ============================================================ Monitor

class MonitorPassiveTests(unittest.TestCase):
    def test_monitor_fuses_session_and_terminal_observation(self):
        config = MemoryConfig()
        monitor = Monitor(config)
        monitor._terminal_service = WindowsTerminalService(None)   # 无 UIA 环境
        inst = AgentInstance(AgentKind.CODEX, 101, "wsl:Ubuntu", cwd="/w",
                             process_token="999")
        watcher = monitor._watchers[AgentKind.CODEX]
        with patch.object(watcher, "poll") as wpoll, \
             patch.object(monitor._probe, "snapshot",
                          return_value=probe({"windows": (True, [inst])})):
            def fake_poll(instances):
                obs = Observation(
                    source=EvidenceSource.SESSION, timestamp=time.time(),
                    status=Status.WORKING, phase=Phase.CODING,
                    mode=Mode.PLAN, confidence=Confidence.HIGH,
                    turn_active=True, goal="迁移 JWT", summary="修改 codex.py",
                    session_bound=True, session_id="s1", session_file="/x.jsonl")
                return {i.key: obs for i in instances}
            wpoll.side_effect = fake_poll
            monitor._tick()
        target = monitor.get_target(inst.key)
        self.assertIsNotNone(target)
        self.assertEqual(target.snapshot.status, Status.WORKING)
        self.assertEqual(target.snapshot.phase, Phase.CODING)
        self.assertEqual(target.snapshot.mode, Mode.PLAN)

    def test_terminal_waiting_preempts_session_working(self):
        """终端可见审批（HIGH 绑定）覆盖会话 WORKING（plan §26 优先级）。"""
        from agents.terminal_uia import PaneInfo, TerminalObserver, TerminalBackend
        config = MemoryConfig()
        monitor = Monitor(config)
        backend = TerminalBackend()
        observer = TerminalObserver(backend, cfg={"terminal_observer": True})
        observer._started = True
        pane_id = (11, (1, 2))
        observer.panes[pane_id] = PaneInfo(pane_id=pane_id, hwnd=11,
                                           window_pid=50, title="codex")
        observer.observations[pane_id] = Observation(
            source=EvidenceSource.TERMINAL, timestamp=time.time(),
            status=Status.WAITING, phase=Phase.APPROVAL,
            confidence=Confidence.HIGH, summary="命令执行需要确认",
            expires_at=time.time() + 1.5)
        observer._last_discover = time.time()   # 阻止空发现清掉预置 pane
        monitor._terminal_service = WindowsTerminalService(observer)
        inst = AgentInstance(AgentKind.CODEX, 101, "wsl:Ubuntu", cwd="/w", process_token="9")
        from agents.models import BindingConfidence, TerminalBinding
        fake_binding = TerminalBinding(
            hwnd=11, pane_id=pane_id, confidence=BindingConfidence.HIGH,
            observable=True, last_seen=time.time())
        watcher = monitor._watchers[AgentKind.CODEX]
        with patch.object(watcher, "poll") as wpoll, \
             patch.object(monitor._terminal_service, "resolve",
                          return_value={inst.key: fake_binding}), \
             patch.object(monitor._probe, "snapshot",
                          return_value=probe({"windows": (True, [inst])})):
            wpoll.return_value = {inst.key: Observation(
                source=EvidenceSource.SESSION, timestamp=time.time(),
                status=Status.WORKING, phase=Phase.CODING,
                turn_active=True, confidence=Confidence.HIGH,
                session_bound=True, summary="修改中")}
            monitor._tick()
        target = monitor.get_target(inst.key)
        self.assertEqual(target.snapshot.status, Status.WAITING)
        self.assertEqual(target.snapshot.phase, Phase.APPROVAL)
        self.assertIn("终端", target.snapshot.summary)

    def test_ambiguous_binding_does_not_fuse_terminal_observation(self):
        from agents.terminal_uia import PaneInfo, TerminalObserver, TerminalBackend
        config = MemoryConfig()
        monitor = Monitor(config)
        observer = TerminalObserver(TerminalBackend(), cfg={})
        observer._started = True
        pane_id = (11, (1, 2))
        observer.panes[pane_id] = PaneInfo(pane_id=pane_id, hwnd=11,
                                           window_pid=50, title="codex")
        observer.observations[pane_id] = Observation(
            source=EvidenceSource.TERMINAL, timestamp=time.time(),
            status=Status.WAITING, phase=Phase.APPROVAL,
            confidence=Confidence.HIGH, expires_at=time.time() + 1.5)
        observer._last_discover = time.time()
        monitor._terminal_service = WindowsTerminalService(observer)
        inst = AgentInstance(AgentKind.CODEX, 101, "wsl:Ubuntu", cwd="/w", process_token="9")
        from agents.models import BindingConfidence, TerminalBinding
        fake_binding = TerminalBinding(
            hwnd=11, pane_id=pane_id, confidence=BindingConfidence.AMBIGUOUS,
            observable=True)
        watcher = monitor._watchers[AgentKind.CODEX]
        with patch.object(watcher, "poll") as wpoll, \
             patch.object(monitor._terminal_service, "resolve",
                          return_value={inst.key: fake_binding}), \
             patch.object(monitor._probe, "snapshot",
                          return_value=probe({"windows": (True, [inst])})):
            wpoll.return_value = {inst.key: Observation(
                source=EvidenceSource.SESSION, timestamp=time.time(),
                status=Status.WORKING, turn_active=True,
                confidence=Confidence.HIGH, session_bound=True, summary="修改中")}
            monitor._tick()
        target = monitor.get_target(inst.key)
        # AMBIGUOUS → 终端审批不归属（plan §25）
        self.assertEqual(target.snapshot.status, Status.WORKING)

    def test_terminal_waiting_kind_mismatch_not_attributed(self):
        """Codex 绑定的 pane 上命中 Claude 审批文案 → 不能归属给 Codex。"""
        from agents.terminal_uia import PaneInfo, TerminalObserver, TerminalBackend
        config = MemoryConfig()
        monitor = Monitor(config)
        observer = TerminalObserver(TerminalBackend(), cfg={})
        observer._started = True
        pane_id = (11, (1, 2))
        observer.panes[pane_id] = PaneInfo(pane_id=pane_id, hwnd=11,
                                           window_pid=50, title="claude")
        observer._last_discover = time.time()
        observer.observations[pane_id] = Observation(
            source=EvidenceSource.TERMINAL, timestamp=time.time(),
            status=Status.WAITING, phase=Phase.APPROVAL,
            agent_kind=AgentKind.CLAUDE,
            confidence=Confidence.HIGH, summary="Bash 命令需要确认",
            expires_at=time.time() + 1.5)
        monitor._terminal_service = WindowsTerminalService(observer)
        inst = AgentInstance(AgentKind.CODEX, 101, "wsl:Ubuntu", cwd="/w", process_token="9")
        from agents.models import BindingConfidence, TerminalBinding
        fake_binding = TerminalBinding(
            hwnd=11, pane_id=pane_id, confidence=BindingConfidence.HIGH,
            observable=True, last_seen=time.time())
        watcher = monitor._watchers[AgentKind.CODEX]
        with patch.object(watcher, "poll") as wpoll, \
             patch.object(monitor._terminal_service, "resolve",
                          return_value={inst.key: fake_binding}), \
             patch.object(monitor._probe, "snapshot",
                          return_value=probe({"wsl:Ubuntu": (True, [inst])})):
            wpoll.return_value = {inst.key: Observation(
                source=EvidenceSource.SESSION, timestamp=time.time(),
                status=Status.WORKING, turn_active=True,
                confidence=Confidence.HIGH, session_bound=True, summary="修改中")}
            monitor._tick()
        target = monitor.get_target(inst.key)
        self.assertEqual(target.snapshot.status, Status.WORKING)   # 不串 WAITING

    def test_session_done_survives_terminal_activity(self):
        """任务完成后终端 prompt 绘制（泛化活动）不得吞掉 DONE 动画。"""
        from agents.terminal_uia import PaneInfo, TerminalObserver, TerminalBackend
        config = MemoryConfig()
        monitor = Monitor(config)
        observer = TerminalObserver(TerminalBackend(), cfg={})
        observer._started = True
        pane_id = (11, (1, 2))
        observer.panes[pane_id] = PaneInfo(pane_id=pane_id, hwnd=11,
                                           window_pid=50, title="codex")
        observer._last_discover = time.time()
        observer.activity[pane_id] = time.time()   # 泛化终端活动（无 agent_kind）
        monitor._terminal_service = WindowsTerminalService(observer)
        inst = AgentInstance(AgentKind.CODEX, 101, "wsl:Ubuntu", cwd="/w", process_token="9")
        from agents.models import BindingConfidence, TerminalBinding
        fake_binding = TerminalBinding(
            hwnd=11, pane_id=pane_id, confidence=BindingConfidence.HIGH,
            observable=True, last_seen=time.time())
        watcher = monitor._watchers[AgentKind.CODEX]
        with patch.object(watcher, "poll") as wpoll, \
             patch.object(monitor._terminal_service, "resolve",
                          return_value={inst.key: fake_binding}), \
             patch.object(monitor._probe, "snapshot",
                          return_value=probe({"wsl:Ubuntu": (True, [inst])})):
            wpoll.return_value = {inst.key: Observation(
                source=EvidenceSource.SESSION, timestamp=time.time(),
                status=Status.DONE, confidence=Confidence.EXACT,
                session_bound=True, summary="完成")}
            monitor._tick()
        target = monitor.get_target(inst.key)
        self.assertEqual(target.snapshot.status, Status.DONE)

    def test_source_health_isolation_for_stale_flag(self):
        """Ubuntu 扫描失败只影响 Ubuntu 实例的 stale 标记。"""
        config = MemoryConfig()
        monitor = Monitor(config)
        monitor._terminal_service = WindowsTerminalService(None)
        ubuntu = AgentInstance(AgentKind.CODEX, 1, "wsl:Ubuntu",
                               process_token="1")
        win = AgentInstance(AgentKind.CODEX, 2, "windows", process_token="2")
        watcher = monitor._watchers[AgentKind.CODEX]
        with patch.object(watcher, "poll") as wpoll, \
             patch.object(monitor._probe, "snapshot",
                          return_value=probe({"windows": (True, [win]),
                                          "wsl:Ubuntu": (False, [ubuntu])})):
            wpoll.return_value = {}
            monitor._tick()
        self.assertTrue(monitor.get_target(ubuntu.key).snapshot.stale)
        self.assertFalse(monitor.get_target(win.key).snapshot.stale)

    def test_authoritative_absence_commits_exit_immediately(self):
        """权威空 source：exact key 本轮即退出（v4plan §4.2，无 grace）。"""
        config = MemoryConfig()
        monitor = Monitor(config)
        monitor._terminal_service = WindowsTerminalService(None)
        debian = AgentInstance(AgentKind.CODEX, 1, "wsl:Debian",
                               process_token="1")
        monitor.instances = {debian.key: debian}
        monitor.snapshots = {debian.key: Snapshot(
            debian.key, debian.kind, debian.source, debian.pid)}
        with patch.object(monitor._probe, "snapshot",
                          return_value=probe({"wsl:Debian": (True, [])})):
            monitor._merge_instances(1000.0)
        self.assertNotIn(debian.key, monitor.instances)
        self.assertNotIn(debian.key, monitor.snapshots)

    def test_windows_global_scan_failure_is_not_authoritative_empty(self):
        """Windows 全局枚举失败：authoritative=False，旧实例保留（v4plan §4.1）。"""
        config = MemoryConfig()
        monitor = Monitor(config)
        monitor._terminal_service = WindowsTerminalService(None)
        win = AgentInstance(AgentKind.CODEX, 5, "windows", process_token="5")
        monitor.instances = {win.key: win}
        with patch.object(monitor._probe, "snapshot",
                          return_value=probe({"windows": (False, [])})):
            monitor._merge_instances(1000.0)
        self.assertIn(win.key, monitor.instances)   # 保留，绝不判死
        # 再来一轮仍然失败：依旧保留
        with patch.object(monitor._probe, "snapshot",
                          return_value=probe({"windows": (False, [])})):
            monitor._merge_instances(1100.0)
        self.assertIn(win.key, monitor.instances)

    def test_scan_windows_global_failure_raises_probe_unavailable(self):
        """process_iter 整体失败必须抛 ProbeUnavailable，绝不返回空列表。"""
        import psutil
        from agents.discovery import ProbeUnavailable
        with patch.object(psutil, "process_iter",
                          side_effect=RuntimeError("enumeration broken")):
            with self.assertRaises(ProbeUnavailable):
                scan_windows()

    def test_one_distro_stopped_does_not_affect_other(self):
        config = MemoryConfig()
        monitor = Monitor(config)
        monitor._terminal_service = WindowsTerminalService(None)
        ubuntu = AgentInstance(AgentKind.CODEX, 1, "wsl:Ubuntu",
                               process_token="1")
        debian = AgentInstance(AgentKind.CLAUDE, 2, "wsl:Debian",
                               process_token="2")
        monitor.instances = {ubuntu.key: ubuntu, debian.key: debian}
        result = probe({"wsl:Ubuntu": (True, [ubuntu]), "wsl:Debian": (True, [])})
        for t in (1000.0, 1010.0):
            with patch.object(monitor._probe, "snapshot", return_value=result):
                monitor._merge_instances(t)
        self.assertIn(ubuntu.key, monitor.instances)   # Ubuntu 不受影响
        self.assertNotIn(debian.key, monitor.instances)  # Debian 权威空 → 立即退出

    def test_one_distro_failed_does_not_affect_other(self):
        config = MemoryConfig()
        monitor = Monitor(config)
        monitor._terminal_service = WindowsTerminalService(None)
        ubuntu = AgentInstance(AgentKind.CODEX, 1, "wsl:Ubuntu",
                               process_token="1")
        debian = AgentInstance(AgentKind.CLAUDE, 2, "wsl:Debian",
                               process_token="2")
        monitor.instances = {ubuntu.key: ubuntu, debian.key: debian}
        result = probe({"wsl:Ubuntu": (True, [ubuntu]),
                        "wsl:Debian": (False, [debian])})
        for t in (1000.0, 1100.0):
            with patch.object(monitor._probe, "snapshot", return_value=result):
                monitor._merge_instances(t)
        self.assertIn(ubuntu.key, monitor.instances)
        self.assertIn(debian.key, monitor.instances)   # 失败源不判死

    def test_exit_event_for_replaced_incarnation_is_ignored(self):
        """exit 事件晚到且 key 已被新 incarnation 替换：不删新实例。"""
        from agents.process_watch import ProcessExitEvent
        config = MemoryConfig()
        monitor = Monitor(config)
        monitor._terminal_service = WindowsTerminalService(None)
        new = AgentInstance(AgentKind.CODEX, 7, "windows",
                            process_token="new-token")
        monitor.instances = {new.key: new}
        monitor._exit_watcher = _FakeExitWatcher([
            ProcessExitEvent(key=new.key, pid=7,
                             process_token="old-token",
                             timestamp=time.time())])
        monitor._drain_exit_events(time.time())
        self.assertIn(new.key, monitor.instances)   # token 不匹配 → 忽略

    def test_exit_event_commits_exit_for_exact_incarnation(self):
        from agents.process_watch import ProcessExitEvent
        config = MemoryConfig()
        monitor = Monitor(config)
        monitor._terminal_service = WindowsTerminalService(None)
        inst = AgentInstance(AgentKind.CODEX, 7, "windows",
                             process_token="tok")
        monitor.instances = {inst.key: inst}
        monitor.snapshots = {inst.key: Snapshot(
            inst.key, inst.kind, inst.source, inst.pid)}
        monitor._exit_watcher = _FakeExitWatcher([
            ProcessExitEvent(key=inst.key, pid=7, process_token="tok",
                             timestamp=time.time())])
        monitor._drain_exit_events(time.time())
        self.assertNotIn(inst.key, monitor.instances)
        self.assertNotIn(inst.key, monitor.snapshots)

    def test_terminal_activity_after_exit_does_not_revive_agent(self):
        """Agent 退出后 terminal shell 继续输出：不得复活 AgentTarget。"""
        from agents.terminal_uia import PaneInfo, TerminalObserver, TerminalBackend
        config = MemoryConfig()
        monitor = Monitor(config)
        observer = TerminalObserver(TerminalBackend(), cfg={})
        observer._started = True
        pane_id = (11, (1,))
        observer.panes[pane_id] = PaneInfo(pane_id=pane_id, hwnd=11,
                                           window_pid=50, title="shell")
        observer._last_discover = time.time()
        monitor._terminal_service = WindowsTerminalService(observer)
        gone = AgentInstance(AgentKind.CODEX, 101, "wsl:Ubuntu",
                             cwd="/w", process_token="9")
        live = AgentInstance(AgentKind.CLAUDE, 202, "wsl:Ubuntu",
                             cwd="/w", process_token="10")
        watcher = monitor._watchers[AgentKind.CODEX]
        with patch.object(watcher, "poll") as wpoll, patch.object(
                monitor._probe, "snapshot",
                return_value=probe({"wsl:Ubuntu": (True, [live])})):
            wpoll.return_value = {}
            monitor._tick()
            # gone 实例 authoritative absence → commit exit
            self.assertNotIn(gone.key, monitor.instances)
        # terminal shell 继续产生活动事件
        observer._on_event(TerminalEvent(pane_id=pane_id, kind="activity",
                                         ts=time.time()))
        observer.poll(time.time())
        self.assertNotIn(gone.key, monitor.instances)

    def test_last_instance_exit_triggers_zero_kind_poll_cleanup(self):
        """最后一个某 kind Agent 退出后 poll([]) 仍被调用（v4plan §4.6）。"""
        config = MemoryConfig()
        monitor = Monitor(config)
        monitor._terminal_service = WindowsTerminalService(None)
        inst = AgentInstance(AgentKind.CODEX, 1, "windows", process_token="1")
        calls = []
        watcher = monitor._watchers[AgentKind.CODEX]
        with patch.object(watcher, "poll",
                          side_effect=lambda insts: (calls.append(list(insts)), {})[1]), patch.object(
                monitor._probe, "snapshot",
                return_value=probe({"windows": (True, [])})):
            monitor.instances = {inst.key: inst}
            monitor._tick()
        self.assertEqual(calls, [[]])   # kind 为 0 也必须 poll([])
        self.assertNotIn(inst.key, monitor.instances)

if __name__ == "__main__":
    unittest.main()
