"""summarize 单元测试（v4.3.1 §20.4 正规化 unittest）。

覆盖 markdown 清理、首行提取、空输入、截断、fmt_command；末尾附
轻量 import smoke（真实模块导入，无 module-level print/assert）。
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

import sys
sys.path.insert(0, str(ROOT))

from agents.summarize import fmt_command, shorten


class ShortenTests(unittest.TestCase):
    def test_markdown_markers_removed(self):
        self.assertEqual(shorten("# 标题**加粗**", 50), "标题加粗")

    def test_first_line_only(self):
        self.assertEqual(shorten("第一行\n第二行", 50), "第一行")

    def test_empty_input(self):
        self.assertEqual(shorten("", 50), "")

    def test_long_text_truncated_with_ellipsis(self):
        long_text = "这是一条很长的输出" * 20
        s = shorten(long_text, 40)
        self.assertLessEqual(len(s), 41)
        self.assertTrue(s.endswith("…"), s)

    def test_short_text_untouched(self):
        self.assertEqual(shorten("短文本", 40), "短文本")


class FmtCommandTests(unittest.TestCase):
    def test_joined_with_spaces(self):
        self.assertEqual(fmt_command(["bash", "-lc", "npm test"]),
                         "bash -lc npm test")

    def test_truncated_to_limit(self):
        cmd = fmt_command(["bash", "-lc", "x" * 200], 30)
        self.assertLessEqual(len(cmd), 30)
        self.assertIn("…", cmd)


class ImportSmokeTests(unittest.TestCase):
    def test_core_modules_import(self):
        import agents.claude  # noqa: F401
        import agents.codex  # noqa: F401
        import agents.kimi  # noqa: F401
        import agents.pi  # noqa: F401
        import agents.summarize  # noqa: F401
        import pet.app  # noqa: F401
        import pet.autostart  # noqa: F401
        import pet.tray  # noqa: F401


if __name__ == "__main__":
    unittest.main()
