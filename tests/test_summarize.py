"""Display contracts: readable task intent, actionable tools and bounded text."""
import json
import unittest

from agents.summarize import (activity_excerpt, fmt_command, shorten,
                              summarize_text, summarize_tool)


class SummaryTests(unittest.TestCase):
    def test_task_main_clause_and_structured_envelopes(self):
        examples = (
            ("# 标题**加粗**", "标题加粗"),
            ("第一行\n第二行", "第一行"),
            ("## 目标\n修复 WSL 连接\n实现细节", "修复 WSL 连接"),
            ({"objective": "修复 WSL 连接", "reasoning_effort": "high",
              "metadata": {"id": "hidden"}}, "修复 WSL 连接"),
            ('{"goal":{"text":"修复 WSL 连接"},"id":"hidden"}', "修复 WSL 连接"),
            ([{"type": "text", "text": "修复监听"}], "修复监听"),
            ({"transport": {"id": "hidden"}}, ""),
            ('{"objective":', ""),
            ("", ""),
        )
        for value, expected in examples:
            with self.subTest(value=value):
                self.assertEqual(summarize_text(value), expected)
        self.assertEqual(shorten("rg foo_bar /tmp/my_path"), "rg foo_bar /tmp/my_path")

    def test_tool_keeps_actual_object_without_description_or_json_fields(self):
        for name, detail, expected in (
            ("Bash", {"description": "验证监听修复", "command": "python -m unittest",
                      "timeout_ms": 30000}, ("python -m unittest",)),
            ("Read", {"file_path": "agents/monitor.py", "offset": 100}, ("agents/monitor.py",)),
            ("Grep", {"pattern": "host_key", "path": "agents/"}, ("host_key", "agents/")),
            ("Grep", {"pattern": '{"type":', "path": "agents/"}, ('{"type":', "agents/")),
            ("web.run", {"search_query": [{"q": "WSL 连接监听"}]}, ("WSL 连接监听",)),
            ("Bash", {"command": "{ echo hello; }"}, ("{ echo hello; }",)),
            ("exec_command", {"cmd": ["bash", "-lc", "{ echo hello; }"]},
             ("bash -lc { echo hello; }",)),
            ("exec_command", json.dumps({"cmd": "rg foo_bar /tmp/my_path",
                                          "max_output_tokens": 1000}), ("rg foo_bar /tmp/my_path",)),
            ("functions.exec", "text(await tools.exec_command({workdir: '/work', cmd: 'rg foo_bar /tmp/my_path', max_output_tokens: 1000}));",
             ("rg foo_bar /tmp/my_path",)),
            ("Read", [{"file_path": "agents/monitor.py", "transport": "hidden"},
                       {"token": "private"}], ("agents/monitor.py",)),
            ("Bash", "rg tools.exec_command agents/", ("rg tools.exec_command agents/",)),
            ("apply_patch", "*** Begin Patch\n*** Update File: agents/monitor.py\n@@\n-old\n+new\n*** End Patch",
             ("agents/monitor.py",)),
        ):
            with self.subTest(name=name):
                text = summarize_tool(name, detail, 160)
                for part in expected:
                    self.assertIn(part, text)
                for metadata in ("timeout_ms", "max_output_tokens", '\"cmd\"', '\"file_path\"', "*** Begin Patch", "验证监听修复"):
                    self.assertNotIn(metadata, text)

    def test_display_limits_preserve_commands_and_never_dump_unknown_objects(self):
        for limit in (1, 2, 20, 40, 120):
            with self.subTest(limit=limit):
                self.assertLessEqual(len(shorten("这是一条很长的输出" * 30, limit)), limit)
                self.assertLessEqual(len(summarize_text({"goal": "x" * 200}, limit)), limit)
                self.assertLessEqual(len(fmt_command(["bash", "-lc", "x" * 200], limit)), limit)
                self.assertEqual(activity_excerpt("rg *.py " + "x" * 200, limit),
                                 ("rg *.py " + "x" * 200)[:limit - 1] + "…")
        self.assertEqual(fmt_command(["bash", "-lc", "npm test"]), "bash -lc npm test")
        self.assertEqual(summarize_tool("Bash", {"transport": {"id": "private"}}), "")
        self.assertEqual(summarize_tool("Bash", {"description": "修改说明"}), "")
        self.assertEqual(summarize_tool("functions.exec",
                         "text(await tools.exec_command({cmd: dynamicCommand, token: 'private'}));"), "")


if __name__ == "__main__":
    unittest.main()
