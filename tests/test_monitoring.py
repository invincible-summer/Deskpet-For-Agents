"""Pure monitoring tests: no Tk window, subprocess, or live Agent required."""

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
from agents.kimi import KimiFile
from agents.models import AgentInstance, AgentKind, Snapshot, Status
from agents.monitor import Monitor
from agents.pi import PiFile
from agents.summarize import classify_tool, fmt_command, shorten
from agents.tailer import FileTailer


class MemoryConfig:
    """Small dotted config replacement for monitor-only tests."""

    def __init__(self, data=None):
        self.data = data or {
            "monitor": {
                "agents": {kind.value: True for kind in AgentKind},
                "wsl_enabled": False,
                "windows_scan_sec": 3600,
                "wsl_scan_sec": 3600,
                "gone_grace_sec": 30,
                "working_hold_sec": 90,
            }
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


class WatcherLifecycleTests(unittest.TestCase):
    def test_codex_silence_never_creates_a_guessed_approval(self):
        state = CodexFile("codex.jsonl")
        state.feed(json.dumps({
            "type": "event_msg",
            "timestamp": "2020-01-01T00:00:00Z",
            "payload": {"type": "task_started", "task": "old task"},
        }))
        status, approval = state.status(time.time() + 120, {"waiting_quiet_sec": 0})
        self.assertEqual(status, Status.WORKING)
        self.assertIsNone(approval)

        state.feed(json.dumps({
            "type": "event_msg",
            "timestamp": "2020-01-01T00:00:00Z",
            "payload": {"type": "task_complete", "last_agent_message": "finished"},
        }))
        status, _ = state.status(time.time(), {})
        self.assertEqual(status, Status.IDLE)

    def test_claude_old_completion_is_not_new_done_or_waiting(self):
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
        status, approval = state.status(time.time(), {"waiting_quiet_sec": 0})
        self.assertEqual(status, Status.IDLE)
        self.assertIsNone(approval)

    def test_kimi_preserves_explicit_zero_request_id_and_identity(self):
        state = KimiFile("wire.jsonl")
        state.feed(json.dumps({
            "type": "ApprovalRequest",
            "timestamp": "2020-01-01T00:00:00Z",
            "payload": {
                "type": "ApprovalRequest", "id": 0,
                "thread_id": "thread-1", "turn_id": "turn-1", "item_id": "item-1",
                "command": "pytest -q",
            },
        }))
        status, approval = state.status(time.time(), {})
        self.assertEqual(status, Status.WAITING)
        self.assertIsNotNone(approval)
        self.assertEqual(approval.request_id, 0)
        self.assertEqual(approval.thread_id, "thread-1")
        self.assertEqual(approval.turn_id, "turn-1")
        self.assertEqual(approval.item_id, "item-1")
        state.feed(json.dumps({
            "type": "ApprovalResponse", "payload": {"type": "ApprovalResponse", "id": 1}
        }))
        self.assertIsNotNone(state.pending)
        state.feed(json.dumps({
            "type": "ApprovalResponse", "payload": {"type": "ApprovalResponse", "id": 0}
        }))
        self.assertIsNone(state.pending)

    def test_pi_reports_local_summary_and_does_not_use_cwd_as_title(self):
        state = PiFile("pi.jsonl")
        state.feed(json.dumps({"type": "session", "cwd": "/work/project"}))
        state.feed(json.dumps({
            "type": "message", "timestamp": "2020-01-01T00:00:00Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Done"}]},
        }))
        snap = Snapshot("k", AgentKind.PI, "windows", 1, status=Status.IDLE)
        state.fill_snapshot(snap)
        self.assertEqual(snap.title, "")
        self.assertEqual(snap.cwd, "/work/project")
        self.assertEqual(snap.summary, "Done")


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
            watcher = CodexWatcher({"active_file_window_sec": 3600, "file_scan_sec": 1})
            one = AgentInstance(AgentKind.CODEX, 1, "windows", session_id="s1")
            two = AgentInstance(AgentKind.CODEX, 2, "windows", session_id="s2")
            active = [(first.stat().st_mtime, str(first)), (second.stat().st_mtime, str(second))]
            with patch("agents.base.paths.session_files", return_value=active):
                snapshots = watcher.poll([one, two])
            bound = {snap.key: snap.session_file for snap in snapshots}
            self.assertEqual(bound[one.key], str(first))
            self.assertEqual(bound[two.key], str(second))

            ambiguous = CodexWatcher({"active_file_window_sec": 3600})
            no_id = Path(temp) / "three.jsonl"
            no_id.write_text('{"type":"event_msg","payload":{"type":"token_count"}}\n', encoding="utf-8")
            candidates = [(no_id.stat().st_mtime, str(no_id)), (second.stat().st_mtime, str(second))]
            a = AgentInstance(AgentKind.CODEX, 3, "windows")
            b = AgentInstance(AgentKind.CODEX, 4, "windows")
            with patch("agents.base.paths.session_files", return_value=candidates):
                snapshots = ambiguous.poll([a, b])
            self.assertTrue(all(s.status == Status.UNKNOWN for s in snapshots))
            self.assertTrue(all(not s.session_file for s in snapshots))

    def test_manual_binding_survives_directory_scan_exclusion(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manual.jsonl"
            path.write_text('{"type":"event_msg","payload":{"type":"token_count"}}\n', encoding="utf-8")
            instance = AgentInstance(AgentKind.CODEX, 10, "windows")
            watcher = CodexWatcher({
                "active_file_window_sec": 1,
                "session_bindings": {instance.key: str(path)},
            })
            with patch("agents.base.paths.session_files", return_value=[]):
                snapshots = watcher.poll([instance])
            self.assertEqual(snapshots[0].session_file, str(path))


class MonitorManagedTests(unittest.TestCase):
    def test_managed_snapshots_are_refreshed_without_readonly_discovery(self):
        config = MemoryConfig()
        monitor = Monitor(config)
        key = "managed|windows|1"

        class FakeManager:
            _stopped = False

            def __init__(self):
                self.status = Status.WORKING
                self.summary = "正在连接"
                self.instance = AgentInstance(AgentKind.CODEX, 0, "windows", key=key)

            def instances(self):
                return [self.instance]

            def snapshots(self):
                return {key: Snapshot(
                    key, AgentKind.CODEX, "windows", 0, status=self.status,
                    summary=self.summary, last_line=self.summary, connection="managed",
                )}

        manager = FakeManager()
        monitor.attach_managed(manager)
        # No Windows/WSL scan is due in this isolated test.
        monitor._last_windows_scan = time.time()
        monitor._tick()
        self.assertEqual(monitor.get_state()[1][key].summary, "正在连接")
        self.assertEqual(monitor.get_state()[1][key].connection, "managed")
        manager.status = Status.DONE
        manager.summary = "已完成"
        monitor._tick()
        self.assertEqual(monitor.get_state()[1][key].status, Status.DONE)
        self.assertEqual(monitor.get_state()[1][key].summary, "已完成")

    def test_pinned_managed_key_survives_empty_async_instance_read(self):
        config = MemoryConfig()
        monitor = Monitor(config)
        key = "managed|windows|async"
        config.set("monitor.pinned", key)

        class FakeManager:
            _stopped = False

            def __init__(self):
                self.present = True
                self.instance = AgentInstance(AgentKind.CODEX, 0, "windows", key=key)

            def instances(self):
                return [self.instance] if self.present else []

            def snapshots(self):
                return {key: Snapshot(
                    key, AgentKind.CODEX, "windows", 0, status=Status.UNKNOWN,
                    summary="连接中", connection="managed",
                )} if self.present else {}

        manager = FakeManager()
        monitor.attach_managed(manager)
        monitor._last_windows_scan = time.time()
        monitor._tick()
        self.assertEqual(monitor.primary_key, key)
        manager.present = False
        monitor._tick()
        self.assertEqual(monitor.primary_key, key)
        self.assertIn(key, monitor.instances)


if __name__ == "__main__":
    unittest.main()
