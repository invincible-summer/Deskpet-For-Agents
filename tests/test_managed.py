"""Protocol-level tests for the manager-owned Codex app-server sessions."""

from __future__ import annotations

import json
import queue
import threading
import time
import unittest

from agents.managed import ManagedManager


class FakeTransport:
    """A JSONL-ish app-server transport that answers the startup requests."""

    def __init__(self, command, source):
        self.command = command
        self.source = source
        self.writes = []
        self._incoming = queue.Queue()
        self._closed = False
        self.pid = 5000

    def send(self, message):
        self.writes.append(message)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            self.emit({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif method == "thread/start":
            self.emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "thread": {
                            "id": f"thr-{self.source}",
                            "sessionId": f"sess-{self.source}",
                            "cwd": message["params"]["cwd"],
                        }
                    },
                }
            )
        elif method == "thread/resume":
            self.emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "thread": {
                            "id": message["params"]["threadId"],
                            "sessionId": f"sess-{self.source}",
                        }
                    },
                }
            )
        elif method == "turn/start":
            self.emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"turn": {"id": f"turn-{len(self.writes)}"}},
                }
            )
        elif method == "turn/interrupt":
            self.emit({"jsonrpc": "2.0", "id": request_id, "result": {}})

    def emit(self, message):
        if not self._closed:
            self._incoming.put(json.dumps(message, ensure_ascii=False) + "\n")

    def readline(self):
        if self._closed:
            return ""
        try:
            return self._incoming.get(timeout=0.1)
        except queue.Empty:
            return None

    def close(self):
        self._closed = True
        self._incoming.put("")


class ManagedManagerTest(unittest.TestCase):
    def setUp(self):
        self.transports = []

        def factory(command, source):
            transport = FakeTransport(command, source)
            self.transports.append(transport)
            return transport

        self.manager = ManagedManager(
            {
                "managed": {"history_limit": 3, "rpc_timeout": 2},
                "connection_mode": "hybrid",
            },
            transport_factory=factory,
        )

    def tearDown(self):
        self.manager.stop()

    @staticmethod
    def wait_for(predicate, timeout=2):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return bool(predicate())

    def test_create_is_immediate_and_backend_is_shared_per_source(self):
        first = self.manager.create("/tmp/one")
        second = self.manager.create("/tmp/two")
        third = self.manager.create("/tmp/three", "wsl:Ubuntu")
        self.assertTrue(first.startswith("managed|windows|"))
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, third)
        self.assertTrue(self.wait_for(lambda: len(self.transports) == 2))
        self.assertEqual(len(self.manager.instances()), 3)
        self.assertTrue(self.wait_for(lambda: self.manager.details(first)["thread_id"] == "thr-windows"))
        self.assertEqual(self.manager.details(second)["thread_id"], "thr-windows")
        started = self.manager.instances()[0].started_at
        self.manager._touch(self.manager._sessions[first])
        self.assertEqual(self.manager.instances()[0].started_at, started)

    def test_readonly_mode_rejects_new_controlled_sessions(self):
        manager = ManagedManager({"connection_mode": "readonly"}, transport_factory=lambda *_: None)
        self.addCleanup(manager.stop)
        with self.assertRaisesRegex(RuntimeError, "只读监听"):
            manager.create("/tmp/project")

    def test_initialize_thread_start_and_turn_start_use_json_rpc(self):
        key = self.manager.create("/tmp/project")
        self.assertTrue(self.wait_for(lambda: self.manager.details(key)["thread_id"] == "thr-windows"))
        ok, _ = self.manager.send(key, "run the tests")
        self.assertTrue(ok)
        self.assertTrue(self.wait_for(lambda: self.manager.details(key)["turn_id"].startswith("turn-")))
        methods = [item.get("method") for item in self.transports[0].writes]
        self.assertEqual(methods[:4], ["initialize", "initialized", "thread/start", "turn/start"])
        thread_start = self.transports[0].writes[2]
        self.assertEqual(thread_start["params"]["approvalPolicy"], "on-request")
        self.assertEqual(thread_start["params"]["sandbox"], "workspace-write")
        self.assertNotIn("--experimental", self.transports[0].command)

    def test_approval_request_is_deduplicated_and_wire_id_type_is_preserved(self):
        key = self.manager.create("/tmp/project")
        self.assertTrue(self.wait_for(lambda: self.manager.details(key)["thread_id"] != ""))
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thr-windows",
                "turnId": "turn-1",
                "itemId": "item-1",
                "command": "pytest -q",
            },
        }
        self.transports[0].emit(request)
        self.transports[0].emit(request)
        self.assertTrue(self.wait_for(lambda: len(self.manager.details(key)["pending"]) == 1))
        pending = self.manager.details(key)["pending"][0]
        # The UI receives an opaque generation/sequence token; the original
        # JSON-RPC id is retained internally so its type is preserved on wire.
        self.assertEqual(pending["request_id"], "g1:r1")
        self.assertIn("pytest -q", pending["summary"])

        ok, message = self.manager.approve(key, pending["request_id"], "accept")
        self.assertTrue(ok, message)
        self.assertTrue(self.wait_for(lambda: any(
            item.get("id") == 7 and item.get("result", {}).get("decision") == "accept"
            for item in self.transports[0].writes
        )))
        response = next(item for item in self.transports[0].writes if item.get("id") == 7)
        self.assertIs(type(response["id"]), int)
        self.assertEqual(self.manager.details(key)["pending"][0]["state"], "submitting")
        self.transports[0].emit({
            "method": "item/completed",
            "params": {
                "threadId": "thr-windows", "turnId": "turn-1",
                "item": {"id": "item-1", "type": "commandExecution", "command": "pytest -q"},
            },
        })
        self.assertTrue(self.wait_for(lambda: not self.manager.details(key)["pending"]))

    def test_user_input_is_manual_even_when_auto_is_enabled(self):
        key = self.manager.create("/tmp/project")
        self.assertTrue(self.wait_for(lambda: self.manager.details(key)["thread_id"] != ""))
        self.assertTrue(self.manager.set_auto(key, True)[0])
        self.transports[0].emit({
            "id": "ask-1",
            "method": "item/tool/requestUserInput",
            "params": {
                "threadId": "thr-windows", "turnId": "turn-1", "itemId": "input-1",
                "isBlocking": True,
                "questions": [{"id": "choice", "header": "Choice", "question": "Pick one"}],
            },
        })
        self.assertTrue(self.wait_for(lambda: len(self.manager.details(key)["pending"]) == 1))
        self.assertFalse(any(
            item.get("id") == "ask-1" for item in self.transports[0].writes
        ))
        request_id = self.manager.details(key)["pending"][0]["request_id"]
        ok, _ = self.manager.answer(key, request_id, {"choice": {"answers": ["A"]}})
        self.assertTrue(ok)
        self.assertTrue(self.wait_for(lambda: any(
            item.get("id") == "ask-1" and item.get("result", {}).get("answers", {}).get("choice", {}).get("answers") == ["A"]
            for item in self.transports[0].writes
        )))

    def test_unknown_server_request_gets_error_and_does_not_create_pending(self):
        key = self.manager.create("/tmp/project")
        self.assertTrue(self.wait_for(lambda: self.manager.details(key)["thread_id"] != ""))
        self.transports[0].emit({"id": "x", "method": "unknown/method", "params": {"threadId": "thr-windows"}})
        self.assertTrue(self.wait_for(lambda: any(
            item.get("id") == "x" and item.get("error", {}).get("code") == -32601
            for item in self.transports[0].writes
        )))
        self.assertEqual(self.manager.details(key)["pending"], [])

    def test_history_is_bounded_and_auto_resets_after_disconnect(self):
        key = self.manager.create("/tmp/project")
        self.assertTrue(self.wait_for(lambda: self.manager.details(key)["thread_id"] != ""))
        for index in range(8):
            self.transports[0].emit({
                "method": "item/completed",
                "params": {"threadId": "thr-windows", "turnId": "turn-1", "item": {
                    "id": f"msg-{index}", "type": "agentMessage", "text": f"reply {index}"
                }},
            })
        self.assertTrue(self.wait_for(lambda: len(self.manager.details(key)["history"]) <= 3))
        self.assertTrue(self.manager.set_auto(key, True)[0])
        self.transports[0].close()
        self.assertTrue(self.wait_for(lambda: self.manager.details(key)["auto"] is False))
        self.assertEqual(self.manager.details(key)["pending"], [])

    def test_user_message_items_are_not_recorded_as_assistant(self):
        key = self.manager.create("/tmp/project")
        self.assertTrue(self.wait_for(lambda: self.manager.details(key)["thread_id"] != ""))
        self.transports[0].emit({
            "method": "item/completed",
            "params": {
                "threadId": "thr-windows",
                "item": {"id": "u-1", "type": "message", "role": "user", "text": "hello"},
            },
        })
        self.assertTrue(self.wait_for(lambda: any(
            item.get("text") == "hello" for item in self.manager.details(key)["history"]
        )))
        entry = next(item for item in self.manager.details(key)["history"] if item.get("text") == "hello")
        self.assertEqual(entry["role"], "user")

    def test_server_request_resolved_notification_removes_pending(self):
        key = self.manager.create("/tmp/project")
        self.assertTrue(self.wait_for(lambda: self.manager.details(key)["thread_id"] != ""))
        self.transports[0].emit({
            "id": "resolved-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thr-windows", "turnId": "turn-1", "itemId": "item-1", "command": "ls"},
        })
        self.assertTrue(self.wait_for(lambda: len(self.manager.details(key)["pending"]) == 1))
        self.transports[0].emit({
            "method": "serverRequest/resolved",
            "params": {"threadId": "thr-windows", "requestId": "resolved-1"},
        })
        self.assertTrue(self.wait_for(lambda: not self.manager.details(key)["pending"]))


if __name__ == "__main__":
    unittest.main()
