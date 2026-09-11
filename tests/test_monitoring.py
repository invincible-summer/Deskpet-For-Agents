"""V3 纯监听测试：无 Tk 窗口、无子进程、无真实 Agent（plan.md §56-§61）。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

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


class AsyncRescanTests(unittest.TestCase):
    """Dashboard rescan must never carry UIA work into the Tk caller."""

    def test_rescan_is_o1_coalesced_and_worker_owned(self):
        monitor = Monitor(MemoryConfig())
        monitor._probe.rescan = Mock()
        for watcher in monitor._watchers.values():
            watcher.reset_scan_cache = Mock()
        entered = threading.Event()
        release = threading.Event()
        worker_ids = []

        def slow_refresh(force=False):
            worker_ids.append(threading.get_ident())
            entered.set()
            release.wait(2.0)

        monitor._terminal_service.refresh_observed_controls = slow_refresh
        caller = threading.get_ident()
        started = time.perf_counter()
        self.assertTrue(monitor.rescan())
        for _ in range(19):
            self.assertFalse(monitor.rescan())
        self.assertLess(time.perf_counter() - started, 0.05)
        monitor._probe.rescan.assert_called_once_with()
        self.assertEqual(monitor._rescan_request_count, 1)
        self.assertEqual(monitor._rescan_complete_count, 0)

        worker = threading.Thread(target=monitor._consume_rescan_request)
        worker.start()
        self.assertTrue(entered.wait(1.0))
        self.assertTrue(worker.is_alive())
        release.set()
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual(monitor._rescan_complete_count, 1)
        self.assertEqual(worker_ids, [worker.ident])
        self.assertNotEqual(worker_ids[0], caller)

    def test_shutdown_seals_rescan_requests(self):
        monitor = Monitor(MemoryConfig())
        monitor._probe.stop = Mock()
        monitor.request_stop()
        self.assertFalse(monitor.rescan())
        self.assertFalse(monitor._rescan_requested.is_set())

    def test_rescan_survives_refresh_exception(self):
        monitor = Monitor(MemoryConfig())
        monitor._probe.rescan = Mock()
        for watcher in monitor._watchers.values():
            watcher.reset_scan_cache = Mock()
        monitor._terminal_service.refresh_observed_controls = Mock(
            side_effect=RuntimeError("uia boom"))
        self.assertTrue(monitor.rescan())
        monitor._consume_rescan_request()
        self.assertEqual(monitor._rescan_complete_count, 1)
        # 异常不杀 worker、不留下 pending 请求：后续新请求照常接受消费
        self.assertTrue(monitor.rescan())
        monitor._consume_rescan_request()
        self.assertEqual(monitor._rescan_complete_count, 2)
        self.assertFalse(monitor._rescan_requested.is_set())


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


class ClaudeClearRebindTests(unittest.TestCase):
    """v4.2.3 §6：/clear 后 stale PID registry 的 growth 采样切换。

    fixture：同 PID、registry 始终指 A；A 冻结，B same cwd 连续增长，
    C same cwd 但不增长 → 必须脱离 A 并最终只绑定 B。
    只有 A 静默而没有增长候选时不得切换。
    """

    def _watcher_with_files(self, temp, cwd="/home/u/proj"):
        watcher = ClaudeWatcher({})
        files = {}
        for name in ("A", "B", "C"):
            path = str(Path(temp) / f"{name}.jsonl")
            Path(path).write_text(
                json.dumps({"type": "user", "cwd": cwd,
                            "message": {"content": [{"type": "text",
                                                     "text": f"goal {name}"}]},
                            "timestamp": "2026-01-01T00:00:00Z"}) + "\n",
                encoding="utf-8")
            st = ClaudeFile(path)
            st.source = "windows"
            st.cwd = cwd
            files[path] = st
        watcher.files = files
        return watcher, files

    def test_stale_registry_switches_from_frozen_a_to_growing_b(self):
        with tempfile.TemporaryDirectory() as temp:
            watcher, files = self._watcher_with_files(temp)
            a, b, c = (files[p].path for p in sorted(files))
            inst = AgentInstance(AgentKind.CLAUDE, 42, "windows",
                                 process_token="42", cwd="/home/u/proj")
            watcher._instance_files = {inst.key: a}
            now = time.time()
            # cycle 1: 基线采样；B 第一次增长
            time.sleep(0.01)
            with open(b, "a", encoding="utf-8") as s:
                s.write(json.dumps({"type": "assistant"}) + "\n")
            watcher.revalidate_bindings([inst], now + 1)
            self.assertIn(inst.key, watcher._instance_files)
            # cycle 2: A 静默 1；B 第二次增长
            time.sleep(0.01)
            with open(b, "a", encoding="utf-8") as s:
                s.write(json.dumps({"type": "assistant"}) + "\n")
            watcher.revalidate_bindings([inst], now + 2)
            self.assertIn(inst.key, watcher._instance_files)  # A stable=1 还不够
            # cycle 3: B 第三次增长（change_count=2）+ A stable=2 → 脱离 A
            time.sleep(0.01)
            with open(b, "a", encoding="utf-8") as s:
                s.write(json.dumps({"type": "assistant"}) + "\n")
            watcher.revalidate_bindings([inst], now + 3)
            self.assertNotIn(inst.key, watcher._instance_files)
            # 下一轮 mutual-unique matcher：同 cwd 增长的 B 胜出（C 静默）
            for path in (a, b, c):
                files[path].poll_lines()
            watcher._assign_files([inst])
            self.assertEqual(watcher._instance_files.get(inst.key), b)

    def test_silent_a_without_growing_candidate_never_switches(self):
        with tempfile.TemporaryDirectory() as temp:
            watcher, files = self._watcher_with_files(temp)
            a, _b, _c = (files[p].path for p in sorted(files))
            inst = AgentInstance(AgentKind.CLAUDE, 42, "windows",
                                 process_token="42", cwd="/home/u/proj")
            watcher._instance_files = {inst.key: a}
            for cycle in range(6):
                watcher.revalidate_bindings([inst], time.time() + cycle)
                self.assertEqual(watcher._instance_files.get(inst.key), a)

    def test_growth_state_pruned_with_files(self):
        with tempfile.TemporaryDirectory() as temp:
            watcher, files = self._watcher_with_files(temp)
            a, b, _c = (files[p].path for p in sorted(files))
            watcher._sample_growth(a, time.time())
            watcher._sample_growth(b, time.time())
            self.assertEqual(len(watcher._growth), 2)
            watcher.files.pop(b, None)
            watcher.prune_growth()
            self.assertEqual(set(watcher._growth), {a})


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


class PiSessionV3Tests(unittest.TestCase):
    """v4.2.3 §8：pi v3 stopReason / 独立 toolResult / WSL canonical root。"""

    def _state(self):
        from agents.pi import PiFile
        return PiFile("pi-session.jsonl")

    def test_user_then_assistant_stop_done_then_idle(self):
        # AC-PI-01：DONE 8s 后 IDLE，不能永久 WORKING
        state = self._state()
        base = time.time()
        state.feed(json.dumps({"type": "message",
                               "message": {"role": "user",
                                           "content": "重构解析器"}}))
        state.feed(json.dumps({"type": "message",
                               "message": {"role": "assistant",
                                           "content": [{"type": "text",
                                                        "text": "完成"}],
                                           "stopReason": "stop"}}))
        self.assertEqual(state.observation(base + 1, {}).status, Status.DONE)
        self.assertEqual(state.observation(base + 9, {}).status, Status.IDLE)

    def test_tooluse_toolresult_stop_lifecycle(self):
        # AC-PI-01/02：toolUse 保持 EXECUTING；独立 toolResult message
        # 是活动证据；最终 stop → DONE
        state = self._state()
        base = time.time()
        state.feed(json.dumps({"type": "message",
                               "message": {"role": "user",
                                           "content": "跑测试"}}))
        state.feed(json.dumps({"type": "message",
                               "message": {"role": "assistant",
                                           "content": [],
                                           "stopReason": "toolUse"}}))
        obs = state.observation(base + 0.1, {})
        self.assertEqual(obs.status, Status.WORKING)
        self.assertEqual(obs.phase, Phase.EXECUTING)
        state.feed(json.dumps({"type": "message", "message": {
            "role": "toolResult", "toolCallId": "call-1",
            "toolName": "Bash", "isError": False,
            "content": [{"type": "text", "text": "3 passed"}]}}))
        obs = state.observation(base + 0.2, {})
        self.assertEqual(obs.status, Status.WORKING)
        state.feed(json.dumps({"type": "message",
                               "message": {"role": "assistant",
                                           "content": [{"type": "text",
                                                        "text": "全部通过"}],
                                           "stopReason": "stop"}}))
        self.assertEqual(state.observation(base + 0.3, {}).status, Status.DONE)

    def test_assistant_error_is_error(self):
        state = self._state()
        base = time.time()
        state.feed(json.dumps({"type": "message", "message": {
            "role": "assistant", "content": [],
            "stopReason": "error", "errorMessage": "rate limited"}}))
        obs = state.observation(base + 0.1, {})
        self.assertEqual(obs.status, Status.ERROR)
        self.assertIn("rate limited", obs.summary)

    def test_assistant_aborted_no_success_done(self):
        state = self._state()
        base = time.time()
        state.feed(json.dumps({"type": "message",
                               "message": {"role": "user",
                                           "content": "长任务"}}))
        state.feed(json.dumps({"type": "message", "message": {
            "role": "assistant", "content": [],
            "stopReason": "aborted"}}))
        obs = state.observation(base + 0.2, {})
        self.assertNotEqual(obs.status, Status.DONE)
        self.assertEqual(obs.status, Status.IDLE)

    def test_length_stopreason_marks_limit_not_error(self):
        state = self._state()
        base = time.time()
        state.feed(json.dumps({"type": "message", "message": {
            "role": "assistant", "content": [],
            "stopReason": "length"}}))
        obs = state.observation(base + 0.1, {})
        self.assertEqual(obs.status, Status.DONE)
        self.assertIn("长度限制", obs.summary)

    def test_tool_result_error_does_not_set_agent_error(self):
        # AC-PI-02：isError=True 只是工具结果失败，不是整个 Agent ERROR
        state = self._state()
        base = time.time()
        state.feed(json.dumps({"type": "message", "message": {
            "role": "toolResult", "toolCallId": "c2", "toolName": "Bash",
            "isError": True,
            "content": [{"type": "text", "text": "exit 1"}]}}))
        obs = state.observation(base + 0.1, {})
        self.assertNotEqual(obs.status, Status.ERROR)

    def test_unknown_stopreason_stays_conservative(self):
        state = self._state()
        base = time.time()
        state.feed(json.dumps({"type": "message", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "正在处理"}],
            "stopReason": ""}}))
        obs = state.observation(base + 0.1, {})
        self.assertEqual(obs.status, Status.WORKING)   # 不凭空宣布完成

    def test_pi_session_roots_windows_and_wsl(self):
        # AC-PI-03：Windows/WSL 默认 pi session path 都指向
        # <home>/.pi/agent/sessions；WSL 不从 ~/.pi 整根递归
        from agents.paths import instance_roots, session_files, windows_roots
        win_roots = windows_roots(AgentKind.PI)
        self.assertEqual(len(win_roots), 1)
        self.assertIn("agent", win_roots[0])
        self.assertIn("sessions", win_roots[0])
        wsl = AgentInstance(AgentKind.PI, 2, "wsl:Ubuntu", process_token="2",
                            home="/home/u")
        wsl_roots = instance_roots(wsl)
        self.assertEqual(
            wsl_roots, ["\\\\wsl.localhost\\Ubuntu\\home\\u\\.pi\\agent\\sessions"])
        # env override 优先
        wsl.pi_session_dir = "/custom/pi-sessions"
        self.assertEqual(
            instance_roots(wsl),
            ["\\\\wsl.localhost\\Ubuntu\\custom\\pi-sessions",
             "\\\\wsl.localhost\\Ubuntu\\home\\u\\.pi\\agent\\sessions"])
        # depth=2 仍适用：<sessions>/<encoded-cwd>/<file>.jsonl
        found = session_files(AgentKind.PI, [win_roots[0]], 180.0)
        self.assertIsInstance(found, list)


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
            # Windows 实例：session_index root 即临时目录，sessionDir 为
            # 受控 relative 路径（v4.2.3 §7.3 containment 合法路径）
            inst = AgentInstance(AgentKind.KIMI, 1, "windows", cwd="/w",
                                 process_token="7")
            from agents import paths as paths_mod
            with patch.object(paths_mod, "kimi_index_roots",
                              return_value=[str(temp)]), \
                 patch.object(paths_mod, "read_kimi_index_tail", return_value=[
                    {"sessionId": "s1", "sessionDir": "s1", "workDir": "/w"}]):
                candidates = watcher.extra_candidates("windows", [inst])
            self.assertEqual(len(candidates), 1)
            self.assertTrue(candidates[0][1].endswith("wire.jsonl"))
            st = watcher.files.get(candidates[0][1])
            self.assertIsNone(st)   # extra_candidates 只登记 hint，不建 state

    def test_state_json_read_from_session_root_not_agents_dir(self):
        # AC-KIMI-03：wire=<session>/agents/main/wire.jsonl → state.json
        # 必须从 <session>/state.json（parents[2]）读取，不是 agents/ 目录
        with tempfile.TemporaryDirectory() as temp:
            session_dir = Path(temp) / "s1"
            (session_dir / "agents" / "main").mkdir(parents=True)
            (session_dir / "state.json").write_text(json.dumps({
                "title": "T", "lastPrompt": "P"}), encoding="utf-8")
            # 旧 off-by-one 会错误地找 <session>/agents/state.json
            wrong = session_dir / "agents" / "state.json"
            wrong.write_text(json.dumps({"title": "WRONG"}), encoding="utf-8")
            watcher = KimiWatcher({})
            title, prompt = watcher._state_json_hints(str(session_dir))
            self.assertEqual(title, "T")
            self.assertEqual(prompt, "P")

    def test_session_dir_traversal_rejected(self):
        # AC-KIMI-03：恶意 sessionDir 不产生候选
        from agents.paths import resolve_kimi_session_dir
        with tempfile.TemporaryDirectory() as temp:
            root = str(Path(temp) / "kimi")
            (Path(root)).mkdir(parents=True)
            self.assertIsNone(resolve_kimi_session_dir(root, "../../other"))
            self.assertIsNone(resolve_kimi_session_dir(root, "..\\..\\other"))
            self.assertIsNone(resolve_kimi_session_dir(root, "D:\\evil"))
            self.assertIsNone(resolve_kimi_session_dir(root, "\\\\wsl.localhost\\Ubuntu\\home"))
            ok = resolve_kimi_session_dir(root, "sessions/abc")
            self.assertIsNotNone(ok)
            self.assertTrue(ok.startswith(os.path.normpath(root)))
            # WSL：absolute Linux 路径必须位于授权 root 内
            unc_root = "\\\\wsl.localhost\\Ubuntu\\home\\u\\.kimi-code"
            self.assertEqual(
                resolve_kimi_session_dir(unc_root, "/home/u/.kimi-code/sessions/x",
                                         "Ubuntu"),
                "\\\\wsl.localhost\\Ubuntu\\home\\u\\.kimi-code\\sessions\\x")
            self.assertIsNone(
                resolve_kimi_session_dir(unc_root, "/home/other/sessions/x",
                                         "Ubuntu"))
            self.assertIsNone(
                resolve_kimi_session_dir(unc_root, "/home/u/.kimi-code/../../etc",
                                         "Ubuntu"))
            # WSL relative：在对应 root 内规范化
            self.assertEqual(
                resolve_kimi_session_dir(unc_root, "sessions/y", "Ubuntu"),
                "\\\\wsl.localhost\\Ubuntu\\home\\u\\.kimi-code\\sessions\\y")
            self.assertIsNone(
                resolve_kimi_session_dir(unc_root, "../../home/other", "Ubuntu"))

    def test_interaction_request_approval_and_resolved(self):
        # AC-KIMI-01：current durable interaction.request(approval) →
        # WAITING/EXACT；interaction.resolved 清除
        state = KimiFile("wire.jsonl")
        base = time.time()
        state.feed(json.dumps({
            "type": "prompt.accepted", "time": int(base * 1000),
            "content": [{"type": "text", "text": "跑测试"}]}))
        obs = state.observation(base + 0.1, {})
        self.assertEqual(obs.status, Status.WORKING)
        state.feed(json.dumps({
            "type": "interaction.request", "time": int((base + 1) * 1000),
            "agentId": "main", "id": "it-1", "kind": "approval",
            "toolCallId": "tc-9",
            "request": {"title": "Bash: rm -rf build", "command": "rm -rf build"}}))
        obs = state.observation(base + 1.1, {})
        self.assertEqual(obs.status, Status.WAITING)
        self.assertEqual(obs.confidence, Confidence.EXACT)
        self.assertEqual(obs.phase, Phase.APPROVAL)
        state.feed(json.dumps({
            "type": "interaction.resolved", "time": int((base + 2) * 1000),
            "agentId": "main", "id": "it-1", "response": {"decision": "approved"}}))
        obs = state.observation(base + 2.1, {})
        self.assertEqual(obs.status, Status.WORKING)
        state.feed(json.dumps({
            "type": "turn.ended", "time": int((base + 3) * 1000)}))
        obs = state.observation(base + 3.1, {})
        self.assertEqual(obs.status, Status.DONE)

    def test_interaction_question_is_input_not_waiting(self):
        # AC-KIMI-02：question/user_tool → INPUT，不误报 approval
        state = KimiFile("wire.jsonl")
        base = time.time()
        state.feed(json.dumps({
            "type": "interaction.request", "time": int(base * 1000),
            "agentId": "main", "id": "it-2", "kind": "question",
            "request": {"question": "使用哪个数据库？"}}))
        obs = state.observation(base + 0.1, {})
        self.assertEqual(obs.status, Status.INPUT)
        self.assertEqual(obs.confidence, Confidence.EXACT)
        self.assertIn("数据库", obs.summary)
        state.feed(json.dumps({
            "type": "interaction.resolved", "time": int((base + 1) * 1000),
            "id": "it-2", "response": {"answer": "postgres"}}))
        obs = state.observation(base + 1.1, {})
        self.assertIsNotNone(obs)
        self.assertNotEqual(obs.status, Status.INPUT)

    def test_interaction_user_tool_is_input(self):
        state = KimiFile("wire.jsonl")
        base = time.time()
        state.feed(json.dumps({
            "type": "interaction.request", "time": int(base * 1000),
            "id": "it-3", "kind": "user_tool",
            "request": {"tool": "browser", "prompt": "打开页面并登录"}}))
        obs = state.observation(base + 0.1, {})
        self.assertEqual(obs.status, Status.INPUT)
        self.assertNotEqual(obs.status, Status.WAITING)

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
        """终端可见审批（HIGH observation binding）覆盖会话 WORKING（plan §26）。"""
        from agents.terminal_uia import (ObservedTerminalControl,
                                         TerminalObserver, TerminalBackend)
        from agents.models import (
            ObservationBindingConfidence, TerminalObservationBinding,
            TerminalWindowBinding, WindowBindingConfidence, WindowIdentity,
        )
        config = MemoryConfig()
        monitor = Monitor(config)
        observer = TerminalObserver(TerminalBackend(), cfg={})
        observer._started = True
        control_id = (11, (1, 2))
        observer.controls[control_id] = ObservedTerminalControl(
            control_id=control_id, hwnd=11, window_pid=50, title="codex")
        observer.observations[control_id] = Observation(
            source=EvidenceSource.TERMINAL, timestamp=time.time(),
            status=Status.WAITING, phase=Phase.APPROVAL,
            confidence=Confidence.HIGH, summary="命令执行需要确认",
            expires_at=time.time() + 1.5)
        observer._last_discover = time.time()   # 阻止空发现清掉预置 control
        monitor._terminal_service = WindowsTerminalService(observer)
        inst = AgentInstance(AgentKind.CODEX, 101, "wsl:Ubuntu", cwd="/w", process_token="9")
        fake_window = {inst.key: TerminalWindowBinding(
            window=WindowIdentity(hwnd=11, pid=50, process_created=1.0,
                                  window_class="CASCADIA_HOSTING_WINDOW_CLASS"),
            confidence=WindowBindingConfidence.HIGH, last_seen=time.time())}
        fake_obs = {inst.key: TerminalObservationBinding(
            agent_key=inst.key, control_id=control_id,
            confidence=ObservationBindingConfidence.HIGH, reason="test")}
        watcher = monitor._watchers[AgentKind.CODEX]
        with patch.object(watcher, "poll") as wpoll, \
             patch.object(monitor._terminal_service, "resolve",
                          return_value=(fake_window, fake_obs)), \
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
        from agents.terminal_uia import (ObservedTerminalControl,
                                         TerminalObserver, TerminalBackend)
        from agents.models import TerminalWindowBinding, WindowBindingConfidence
        config = MemoryConfig()
        monitor = Monitor(config)
        observer = TerminalObserver(TerminalBackend(), cfg={})
        observer._started = True
        control_id = (11, (1, 2))
        observer.controls[control_id] = ObservedTerminalControl(
            control_id=control_id, hwnd=11, window_pid=50, title="codex")
        observer.observations[control_id] = Observation(
            source=EvidenceSource.TERMINAL, timestamp=time.time(),
            status=Status.WAITING, phase=Phase.APPROVAL,
            confidence=Confidence.HIGH, expires_at=time.time() + 1.5)
        observer._last_discover = time.time()
        monitor._terminal_service = WindowsTerminalService(observer)
        inst = AgentInstance(AgentKind.CODEX, 101, "wsl:Ubuntu", cwd="/w", process_token="9")
        # AMBIGUOUS：window=None，且没有 observation binding
        fake_window = {inst.key: TerminalWindowBinding(
            confidence=WindowBindingConfidence.AMBIGUOUS,
            last_seen=time.time(), reason="multiple terminal windows")}
        watcher = monitor._watchers[AgentKind.CODEX]
        with patch.object(watcher, "poll") as wpoll, \
             patch.object(monitor._terminal_service, "resolve",
                          return_value=(fake_window, {})), \
             patch.object(monitor._probe, "snapshot",
                          return_value=probe({"windows": (True, [inst])})):
            wpoll.return_value = {inst.key: Observation(
                source=EvidenceSource.SESSION, timestamp=time.time(),
                status=Status.WORKING, turn_active=True,
                confidence=Confidence.HIGH, session_bound=True, summary="修改中")}
            monitor._tick()
        target = monitor.get_target(inst.key)
        # 无安全归属 → 终端审批不归属（plan §25）
        self.assertEqual(target.snapshot.status, Status.WORKING)

    def test_terminal_waiting_kind_mismatch_not_attributed(self):
        """Codex 的 observation control 上命中 Claude 审批文案 → 不能归属给 Codex。"""
        from agents.terminal_uia import (ObservedTerminalControl,
                                         TerminalObserver, TerminalBackend)
        from agents.models import (
            ObservationBindingConfidence, TerminalObservationBinding,
            TerminalWindowBinding, WindowBindingConfidence, WindowIdentity,
        )
        config = MemoryConfig()
        monitor = Monitor(config)
        observer = TerminalObserver(TerminalBackend(), cfg={})
        observer._started = True
        control_id = (11, (1, 2))
        observer.controls[control_id] = ObservedTerminalControl(
            control_id=control_id, hwnd=11, window_pid=50, title="claude")
        observer._last_discover = time.time()
        observer.observations[control_id] = Observation(
            source=EvidenceSource.TERMINAL, timestamp=time.time(),
            status=Status.WAITING, phase=Phase.APPROVAL,
            agent_kind=AgentKind.CLAUDE,
            confidence=Confidence.HIGH, summary="Bash 命令需要确认",
            expires_at=time.time() + 1.5)
        monitor._terminal_service = WindowsTerminalService(observer)
        inst = AgentInstance(AgentKind.CODEX, 101, "wsl:Ubuntu", cwd="/w", process_token="9")
        fake_window = {inst.key: TerminalWindowBinding(
            window=WindowIdentity(hwnd=11, pid=50, process_created=1.0,
                                  window_class="CASCADIA_HOSTING_WINDOW_CLASS"),
            confidence=WindowBindingConfidence.HIGH, last_seen=time.time())}
        fake_obs = {inst.key: TerminalObservationBinding(
            agent_key=inst.key, control_id=control_id,
            confidence=ObservationBindingConfidence.HIGH, reason="test")}
        watcher = monitor._watchers[AgentKind.CODEX]
        with patch.object(watcher, "poll") as wpoll, \
             patch.object(monitor._terminal_service, "resolve",
                          return_value=(fake_window, fake_obs)), \
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
        from agents.terminal_uia import (ObservedTerminalControl,
                                         TerminalObserver, TerminalBackend)
        from agents.models import (
            ObservationBindingConfidence, TerminalObservationBinding,
            TerminalWindowBinding, WindowBindingConfidence, WindowIdentity,
        )
        config = MemoryConfig()
        monitor = Monitor(config)
        observer = TerminalObserver(TerminalBackend(), cfg={})
        observer._started = True
        control_id = (11, (1, 2))
        observer.controls[control_id] = ObservedTerminalControl(
            control_id=control_id, hwnd=11, window_pid=50, title="codex")
        observer._last_discover = time.time()
        observer.activity[control_id] = time.time()   # 泛化终端活动（无 agent_kind）
        monitor._terminal_service = WindowsTerminalService(observer)
        inst = AgentInstance(AgentKind.CODEX, 101, "wsl:Ubuntu", cwd="/w", process_token="9")
        fake_window = {inst.key: TerminalWindowBinding(
            window=WindowIdentity(hwnd=11, pid=50, process_created=1.0,
                                  window_class="CASCADIA_HOSTING_WINDOW_CLASS"),
            confidence=WindowBindingConfidence.HIGH, last_seen=time.time())}
        fake_obs = {inst.key: TerminalObservationBinding(
            agent_key=inst.key, control_id=control_id,
            confidence=ObservationBindingConfidence.HIGH, reason="test")}
        watcher = monitor._watchers[AgentKind.CODEX]
        with patch.object(watcher, "poll") as wpoll, \
             patch.object(monitor._terminal_service, "resolve",
                          return_value=(fake_window, fake_obs)), \
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
        from agents.terminal_uia import (ObservedTerminalControl,
                                         TerminalObserver, TerminalBackend)
        config = MemoryConfig()
        monitor = Monitor(config)
        observer = TerminalObserver(TerminalBackend(), cfg={})
        observer._started = True
        control_id = (11, (1,))
        observer.controls[control_id] = ObservedTerminalControl(
            control_id=control_id, hwnd=11, window_pid=50, title="shell")
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
        observer._on_event(TerminalEvent(control_id=control_id, kind="activity",
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


class SourceToggleTests(unittest.TestCase):
    """v4.1.1 §10.1：windows_enabled=False 真正停止扫描。"""

    def _worker(self, windows_enabled=True):
        cfg = MemoryConfig()
        cfg.set("monitor.windows_enabled", windows_enabled)
        from agents.monitor import ProcessProbeWorker
        return ProcessProbeWorker(cfg)

    def test_windows_disabled_never_scans(self):
        worker = self._worker(windows_enabled=False)
        with patch("agents.monitor.scan_windows") as scan:
            scan.return_value = []
            for _ in range(5):
                worker._tick()
        self.assertEqual(scan.call_count, 0)
        snap = worker.snapshot()
        self.assertNotIn("windows", snap)
        self.assertEqual(worker.windows_scan_ms, 0.0)

    def test_windows_disable_clears_snapshot_and_cache(self):
        worker = self._worker(windows_enabled=True)
        inst = AgentInstance(AgentKind.CODEX, 5, "windows", process_token="5")
        with patch("agents.monitor.scan_windows", return_value=[inst]):
            worker._tick()
        self.assertIn("windows", worker.snapshot())
        self.assertEqual(worker._windows_cache, (inst,))
        # 运行中 True → False：source snapshot 与缓存清掉
        worker.config.set("monitor.windows_enabled", False)
        worker._tick()
        self.assertNotIn("windows", worker.snapshot())
        self.assertEqual(worker._windows_cache, ())
        self.assertEqual(worker.windows_probe_error, "")

    def test_windows_reenable_scans_immediately(self):
        worker = self._worker(windows_enabled=False)
        worker._tick()
        worker.config.set("monitor.windows_enabled", True)
        with patch("agents.monitor.scan_windows", return_value=[]) as scan:
            worker._tick()
        self.assertEqual(scan.call_count, 1)   # 下一 probe 周期立即恢复


class MonitorTickOrderTests(unittest.TestCase):
    """v4.1.1 §10.3：exit 事件后同 tick 不再处理 stale instance。"""

    def test_exit_event_same_tick_skips_stale_instance(self):
        from agents.process_watch import ProcessExitEvent
        from agents.models import ObservationBindingConfidence, \
            TerminalObservationBinding
        config = MemoryConfig()
        monitor = Monitor(config)
        monitor._terminal_service = WindowsTerminalService(None)
        gone = AgentInstance(AgentKind.CODEX, 7, "windows",
                             process_token="tok")
        monitor.instances = {gone.key: gone}
        monitor._exit_watcher = _FakeExitWatcher([
            ProcessExitEvent(key=gone.key, pid=7, process_token="tok",
                             timestamp=time.time())])
        resolved_keys = []
        resolve_calls = []

        def fake_resolve(instances, now):
            resolve_calls.append([i.key for i in instances])
            obs = {i.key: TerminalObservationBinding(
                agent_key=i.key, control_id=(11, (1,)),
                confidence=ObservationBindingConfidence.HIGH)
                for i in instances}
            return ({}, obs)

        with patch.object(monitor._probe, "snapshot",
                          return_value=probe({"windows": (True, [])})), \
             patch.object(monitor._terminal_service, "resolve",
                          side_effect=fake_resolve), \
             patch.object(monitor._watchers[AgentKind.CODEX], "poll",
                          return_value={}):
            monitor._tick()
        # exit 事件 drain 之后重新 snapshot：resolve 不再见到已退出 key
        resolved_keys = [k for call in resolve_calls for k in call]
        self.assertNotIn(gone.key, resolved_keys)
        self.assertNotIn(gone.key, monitor.instances)
        self.assertNotIn(gone.key, monitor.window_bindings)
        self.assertNotIn(gone.key, monitor.terminal_observation_bindings)


class DynamicPollTests(unittest.TestCase):
    """v4.1.1 §11：file_poll_sec 运行中修改下一轮即生效（clamp 保护）。"""

    def test_poll_sec_reads_config_each_time_and_clamps(self):
        config = MemoryConfig()
        monitor = Monitor(config)
        monitor._terminal_service = WindowsTerminalService(None)
        self.assertAlmostEqual(monitor._poll_sec(), 0.5)
        config.set("monitor.file_poll_sec", 1.5)
        self.assertAlmostEqual(monitor._poll_sec(), 1.5)
        config.set("monitor.file_poll_sec", 0.01)   # 异常高频值 → clamp
        self.assertAlmostEqual(monitor._poll_sec(), 0.2)
        config.set("monitor.file_poll_sec", 999)    # 异常低频值 → clamp
        self.assertAlmostEqual(monitor._poll_sec(), 5.0)

if __name__ == "__main__":
    unittest.main()
