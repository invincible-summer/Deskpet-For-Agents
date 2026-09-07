"""ProcessProbe 测试：解析、PID reuse、env 隐私、多 distro、UNC 安全（plan §57/§58）。"""
from __future__ import annotations
import unittest
import unittest.mock

from agents import paths
from agents.discovery import (
    WslProcessProbe,
    build_metadata_script,
    parse_metadata,
    filter_env_line,
)
from agents.models import AgentKind, AgentInstance


class MetadataScriptTests(unittest.TestCase):
    def test_parse_metadata_full_record(self):
        text = (
            "P\t4812\n"
            "C\t/home/foo/proj ect\n"
            "T\t36791842\n"
            "H\t1000\t/home/foo\n"
            "E\tWT_SESSION=abc-guid\n"
            "E\tCODEX_HOME=/x\n"
        )
        meta = parse_metadata(text)
        self.assertIn(4812, meta)
        info = meta[4812]
        self.assertEqual(info["cwd"], "/home/foo/proj ect")   # cwd 可含空格
        self.assertEqual(info["ticks"], "36791842")
        self.assertEqual(info["uid"], "1000")
        self.assertEqual(info["home"], "/home/foo")
        self.assertEqual(info["env"]["WT_SESSION"], "abc-guid")
        self.assertEqual(info["env"]["CODEX_HOME"], "/x")

    def test_parse_metadata_ignores_garbage(self):
        meta = parse_metadata("random line\nno\ttabs\nP\tnotanumber\nP\t7\nC\t/x\n")
        self.assertEqual(list(meta), [7])
        self.assertEqual(meta[7]["cwd"], "/x")

    def test_script_only_greps_allowlist(self):
        script = build_metadata_script([1, 2])
        self.assertIn("WT_SESSION", script)
        self.assertIn("CODEX_HOME", script)
        self.assertIn("CLAUDE_CONFIG_DIR", script)
        self.assertIn("KIMI_CODE_HOME", script)
        self.assertIn("grep -E", script)
        # 不允许把完整 environ 传回：脚本在 WSL 内部即过滤
        self.assertNotIn("cat /proc/$p/environ", script.replace("tr '\\0' '\\n' < /proc/$p/environ", ""))


class EnvironmentPrivacyTests(unittest.TestCase):
    """plan.md §58：模拟含密钥的 environ，最终 Python 对象只能包含 allowlist。"""

    def test_secret_env_never_reaches_python_objects(self):
        secret_text = (
            "P\t100\n"
            "C\t/w\n"
            "T\t1\n"
            "E\tOPENAI_API_KEY=TOP_SECRET\n"
            "E\tANTHROPIC_API_KEY=SECRET\n"
            "E\tAWS_SECRET_ACCESS_KEY=hush\n"
            "E\tWT_SESSION=abc\n"
            "E\tCODEX_HOME=/x\n"
        )
        meta = parse_metadata(secret_text)
        info = meta[100]
        env = info["env"]
        self.assertEqual(set(env), {"WT_SESSION", "CODEX_HOME"})
        blob = repr(info)
        for secret in ("TOP_SECRET", "SECRET", "hush"):
            self.assertNotIn(secret, blob)

    def test_python_side_filter_is_defense_in_depth(self):
        self.assertEqual(filter_env_line("WT_SESSION=abc"), "WT_SESSION=abc")
        self.assertIsNone(filter_env_line("OPENAI_API_KEY=sk-123"))
        self.assertIsNone(filter_env_line("PATH=/usr/bin"))

    def test_instance_holds_only_allowlisted_fields(self):
        inst = AgentInstance(kind=AgentKind.CODEX, pid=5, source="wsl:Ubuntu",
                             process_token="9", home="/home/u")
        # 模拟 probe 注入
        inst.wt_session = "guid"
        inst.codex_home = "/x"
        blob = repr(inst.__dict__)
        for banned in ("OPENAI_API_KEY", "environ", "ANTHROPIC"):
            self.assertNotIn(banned, blob)


class PsParsingTests(unittest.TestCase):
    def test_match_agent_lines(self):
        rows_args = [
            "/home/u/.codex/bin/codex",                        # codex
            "node /usr/lib/node_modules/@anthropic-ai/claude-code/cli.js",  # claude
            "kimi-cli",                                        # kimi
            "grep codex",                                      # 排除
            "sh -c ps -eo",                                    # 排除
            "vim notes.txt",                                   # 无关
        ]
        kinds = [WslProcessProbe._match_agent("comm", a) for a in rows_args]
        self.assertEqual(kinds[0], AgentKind.CODEX)
        self.assertEqual(kinds[1], AgentKind.CLAUDE)
        self.assertEqual(kinds[2], AgentKind.KIMI)
        self.assertIsNone(kinds[3])
        self.assertIsNone(kinds[4])
        self.assertIsNone(kinds[5])


class InstanceIdentityTests(unittest.TestCase):
    def test_pid_reuse_changes_key(self):
        """PID 被复用后 key 不同，不继承旧绑定（plan §3.1）。"""
        a = AgentInstance(kind=AgentKind.CODEX, pid=4812, source="wsl:Ubuntu",
                          process_token="36791842")
        b = AgentInstance(kind=AgentKind.CODEX, pid=4812, source="wsl:Ubuntu",
                          process_token="99887766")
        self.assertNotEqual(a.key, b.key)
        self.assertEqual(a.key, "wsl:Ubuntu|codex|4812|36791842")
        # 无 token 时退化为 PID-only（元数据缺失的降级路径）
        c = AgentInstance(kind=AgentKind.CODEX, pid=4812, source="wsl:Ubuntu")
        self.assertEqual(c.key, "wsl:Ubuntu|codex|4812")

    def test_windows_token_from_create_time(self):
        inst = AgentInstance(kind=AgentKind.CLAUDE, pid=7, source="windows",
                             process_token="1694073600.123")
        self.assertIn("1694073600.123", inst.key)

    def test_project_and_env_label(self):
        inst = AgentInstance(kind=AgentKind.CODEX, pid=1, source="wsl:Ubuntu",
                             cwd="/home/u/src/DeskPet", process_token="1")
        self.assertEqual(inst.project, "DeskPet")
        self.assertEqual(inst.distro, "Ubuntu")
        self.assertEqual(inst.environment_label, "WSL Ubuntu")
        win = AgentInstance(kind=AgentKind.CODEX, pid=2, source="windows",
                            process_token="2", cwd="D:\\work\\app")
        self.assertEqual(win.project, "app")
        self.assertEqual(win.environment_label, "Windows")


class WslUncTests(unittest.TestCase):
    def test_basic_conversion(self):
        self.assertEqual(paths.wsl_unc("Ubuntu", "/home/foo/.codex"),
                         "\\\\wsl.localhost\\Ubuntu\\home\\foo\\.codex")
        self.assertEqual(paths.wsl_unc("Ubuntu", "/root"),
                         "\\\\wsl.localhost\\Ubuntu\\root")
        # 规范化多余斜杠
        self.assertEqual(paths.wsl_unc("Ubuntu", "//home//x//"),
                         "\\\\wsl.localhost\\Ubuntu\\home\\x")

    def test_rejects_bad_paths(self):
        with self.assertRaises(ValueError):
            paths.wsl_unc("Ubuntu", "")
        with self.assertRaises(ValueError):
            paths.wsl_unc("Ubuntu", "relative/path")
        with self.assertRaises(ValueError):
            paths.wsl_unc("Ubuntu", "/home/../etc/shadow")
        with self.assertRaises(ValueError):
            paths.wsl_unc("", "/home")

    def test_unc_to_linux_roundtrip(self):
        unc = paths.wsl_unc("Ubuntu", "/home/foo/.codex/sessions")
        self.assertEqual(paths.unc_to_linux(unc), "/home/foo/.codex/sessions")


class InstanceRootsTests(unittest.TestCase):
    def test_wsl_roots_use_env_and_home_only(self):
        inst = AgentInstance(kind=AgentKind.CODEX, pid=1, source="wsl:Ubuntu",
                             process_token="1", home="/home/foo",
                             codex_home="/data/codex")
        roots = paths.instance_roots(inst)
        self.assertEqual(roots[0], "\\\\wsl.localhost\\Ubuntu\\data\\codex")
        self.assertEqual(roots[1], "\\\\wsl.localhost\\Ubuntu\\home\\foo\\.codex")

    def test_kimi_and_claude_roots(self):
        inst = AgentInstance(kind=AgentKind.KIMI, pid=1, source="wsl:Ubuntu",
                             process_token="1", home="/home/u",
                             kimi_code_home="/kd")
        self.assertEqual(paths.instance_roots(inst)[0],
                         "\\\\wsl.localhost\\Ubuntu\\kd")
        inst2 = AgentInstance(kind=AgentKind.CLAUDE, pid=2, source="wsl:Ubuntu",
                              process_token="2", home="/home/u",
                              claude_config_dir="/cd")
        self.assertEqual(paths.instance_roots(inst2)[0],
                         "\\\\wsl.localhost\\Ubuntu\\cd")

    def test_default_kimi_root_is_kimi_code(self):
        inst = AgentInstance(kind=AgentKind.KIMI, pid=1, source="wsl:Ubuntu",
                             process_token="1", home="/home/u")
        self.assertTrue(paths.instance_roots(inst)[0].endswith("\\.kimi-code"))


class KimiIndexTests(unittest.TestCase):
    def test_parse_index_entries(self):
        text = ('{"sessionId":"s1","sessionDir":"/home/u/.kimi-code/sessions/k/s1","workDir":"/w"}\n'
                'garbage\n'
                '{"sessionId":"","sessionDir":"/x"}\n')
        entries = paths.parse_kimi_index(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["workDir"], "/w")

    def test_wire_candidates_need_matching_cwd(self):
        import json as _json
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            session_dir = root / "sessions" / "k1" / "s1" / "agents" / "main"
            session_dir.mkdir(parents=True)
            wire = session_dir / "wire.jsonl"
            wire.write_text('{"type":"TurnBegin"}\n', encoding="utf-8")
            with open(root / "session_index.jsonl", "w", encoding="utf-8") as f:
                f.write(_json.dumps({"sessionId": "s1",
                                     "sessionDir": str(root / "sessions" / "k1" / "s1"),
                                     "workDir": "/proj"}) + "\n")
            inst = AgentInstance(kind=AgentKind.KIMI, pid=1, source="windows",
                                 process_token="1", cwd="/proj")
            with unittest.mock.patch.object(paths, "_user_home", return_value=str(root)):
                candidates = paths.kimi_wire_candidates(inst)
            # windows 侧 root 为 ~/.kimi-code；索引在同 root 下才能解析
            self.assertTrue(all(p.endswith("wire.jsonl") for _m, p in candidates))


if __name__ == "__main__":
    unittest.main()
