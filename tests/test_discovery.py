"""ProcessProbe 测试：解析、PID reuse、env 隐私、多 distro、UNC 安全（plan §57/§58）。

V3.1：进程树 canonicalization、fallback 代次 token、root metadata 默认关、
按真实 source 的健康隔离、--running --quiet 解析、输出解码。
"""
from __future__ import annotations
import unittest
import unittest.mock

from agents import paths
from agents.discovery import (
    ProcessCandidate,
    WslProcessProbe,
    build_metadata_script,
    canonicalize_agent_processes,
    parse_list_verbose,
    parse_metadata,
    parse_running_quiet,
    filter_env_line,
    _decode_wsl_output,
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


class CanonicalizationTests(unittest.TestCase):
    """wrapper → runtime 只保留最深后代；不跨 kind、不按 kind 全局去重。"""

    def _cand(self, kind, pid, ppid):
        return ProcessCandidate(kind=kind, pid=pid, ppid=ppid)

    def test_wrapper_child_keeps_child(self):
        cands = [self._cand(AgentKind.CLAUDE, 100, 1),    # npm shim
                 self._cand(AgentKind.CLAUDE, 101, 100)]  # node runtime
        canonical, launchers = canonicalize_agent_processes(
            cands, {100: 1, 101: 100})
        self.assertEqual(canonical, {101})
        self.assertEqual(launchers[101], (100,))

    def test_wrapper_chain_keeps_deepest(self):
        cands = [self._cand(AgentKind.KIMI, 10, 1),
                 self._cand(AgentKind.KIMI, 11, 10),
                 self._cand(AgentKind.KIMI, 12, 11)]
        canonical, _ = canonicalize_agent_processes(
            cands, {10: 1, 11: 10, 12: 11})
        self.assertEqual(canonical, {12})

    def test_two_independent_same_kind_kept(self):
        cands = [self._cand(AgentKind.CLAUDE, 101, 1),
                 self._cand(AgentKind.CLAUDE, 201, 1)]
        canonical, launchers = canonicalize_agent_processes(cands, {})
        self.assertEqual(canonical, {101, 201})
        self.assertEqual(launchers, {})

    def test_no_cross_kind_folding(self):
        # Claude wrapper + Codex child：互为祖先但 kind 不同 → 都保留
        cands = [self._cand(AgentKind.CLAUDE, 100, 1),
                 self._cand(AgentKind.CODEX, 101, 100)]
        canonical, launchers = canonicalize_agent_processes(
            cands, {100: 1, 101: 100})
        self.assertEqual(canonical, {100, 101})
        self.assertEqual(launchers, {})

    def test_siblings_not_merged(self):
        cands = [self._cand(AgentKind.CODEX, 200, 100),
                 self._cand(AgentKind.CODEX, 201, 100)]
        canonical, _ = canonicalize_agent_processes(
            cands, {200: 100, 201: 100, 100: 1})
        self.assertEqual(canonical, {200, 201})

    def test_ancestor_via_intermediate_non_match(self):
        # npm → sh(未匹配) → node：跨过中间进程仍识别 wrapper
        cands = [self._cand(AgentKind.CLAUDE, 100, 1),
                 self._cand(AgentKind.CLAUDE, 300, 200)]
        canonical, launchers = canonicalize_agent_processes(
            cands, {100: 1, 200: 100, 300: 200})
        self.assertEqual(canonical, {300})
        self.assertEqual(launchers[300], (100,))


class DistroListParsingTests(unittest.TestCase):
    def test_running_quiet_parsing(self):
        text = "Ubuntu\r\nDebian\r\n"
        self.assertEqual(parse_running_quiet(text), ["Ubuntu", "Debian"])

    def test_running_quiet_skips_header(self):
        text = "\ufeffNAME\nUbuntu\n"
        self.assertEqual(parse_running_quiet(text), ["Ubuntu"])

    def test_list_verbose_parsing(self):
        text = ("  NAME            STATE           VERSION\n"
                "* Ubuntu          Running         2\n"
                "  Debian          Stopped         2\n")
        self.assertEqual(parse_list_verbose(text), ["Ubuntu"])

    def test_decode_utf16_and_utf8(self):
        utf16 = "Ubuntu\nDebian\n".encode("utf-16-le") + b"\x00"
        self.assertIn("Ubuntu", _decode_wsl_output(utf16))
        bom = "Ubuntu\n".encode("utf-16")
        self.assertIn("Ubuntu", _decode_wsl_output(bom))
        utf8 = "Ubuntu\n".encode("utf-8")
        self.assertIn("Ubuntu", _decode_wsl_output(utf8))
        self.assertEqual(_decode_wsl_output(b""), "")


_PS_HEADER = ("  PID  PPID  SID  PGID TPGID TT  UID ETIMES COMMAND\n")


def _ps_line(pid, ppid, comm, args, uid=1000, etimes=60):
    return f" {pid} {ppid} 10 10 10 pts/0 {uid} {etimes} {comm} {args}\n"


def _make_ps_output(rows):
    return _PS_HEADER + "".join(
        _ps_line(*r) for r in rows)


class FallbackTokenTests(unittest.TestCase):
    def test_ticks_present_uses_proc(self):
        probe = WslProcessProbe()
        token, source = probe._resolve_token("Ubuntu", 5, AgentKind.CODEX,
                                             "36791842", 60)
        self.assertEqual(token, "36791842")
        self.assertEqual(source, "proc")

    def test_fallback_token_stable_across_scans(self):
        probe = WslProcessProbe()
        t1, s1 = probe._resolve_token("Ubuntu", 5, AgentKind.CODEX, "", 60)
        t2, s2 = probe._resolve_token("Ubuntu", 5, AgentKind.CODEX, "", 63)
        self.assertEqual(t1, t2)
        self.assertEqual(s1, "fallback")
        self.assertEqual(s2, "fallback")
        self.assertTrue(t1.startswith("fb"))

    def test_pid_reuse_rotates_generation(self):
        probe = WslProcessProbe()
        t1, _ = probe._resolve_token("Ubuntu", 5, AgentKind.CODEX, "", 300)
        # etimes 明显回退 → 同 PID 已被复用
        t2, _ = probe._resolve_token("Ubuntu", 5, AgentKind.CODEX, "", 5)
        self.assertNotEqual(t1, t2)

    def test_kind_change_rotates_generation(self):
        probe = WslProcessProbe()
        t1, _ = probe._resolve_token("Ubuntu", 5, AgentKind.CODEX, "", 60)
        t2, _ = probe._resolve_token("Ubuntu", 5, AgentKind.CLAUDE, "", 62)
        self.assertNotEqual(t1, t2)

    def test_fallback_token_never_empty(self):
        probe = WslProcessProbe()
        token, _ = probe._resolve_token("Ubuntu", 9, AgentKind.KIMI, "", 1)
        self.assertTrue(token)


class RootFallbackTests(unittest.TestCase):
    """root metadata 重试默认关闭；显式开启才允许一次 -u root 调用。"""

    def _probe_with_calls(self, allow_root, missing_meta=True):
        probe = WslProcessProbe(allow_root_metadata=allow_root)
        calls = []

        def fake_run(distro, script, timeout=8.0, user=None):
            calls.append((distro, user))
            if missing_meta:
                return "P\t42\nC\t\nT\t\n"   # 进程存在但读不到 metadata
            return "P\t42\nC\t/w\nT\t123\n"

        return probe, calls, fake_run

    def test_default_never_escalates_to_root(self):
        probe, calls, fake = self._probe_with_calls(allow_root=False)
        with unittest.mock.patch("agents.discovery._run_wsl", side_effect=fake):
            meta = probe._metadata("Ubuntu", [42])
        self.assertEqual(meta[42]["cwd"], "")
        self.assertEqual(calls, [("Ubuntu", None)])
        for _distro, user in calls:
            self.assertNotEqual(user, "root")

    def test_explicit_opt_in_allows_single_root_retry(self):
        probe, calls, fake = self._probe_with_calls(allow_root=True)
        with unittest.mock.patch("agents.discovery._run_wsl", side_effect=fake):
            meta = probe._metadata("Ubuntu", [42])
        self.assertEqual(calls[0], ("Ubuntu", None))
        self.assertEqual(calls[1], ("Ubuntu", "root"))
        self.assertEqual(len(calls), 2)


class MultiDistroScanTests(unittest.TestCase):
    """scan() 返回按真实 source 分组的结果与健康位（隔离失败）。"""

    def test_scan_groups_by_source_and_isolates_failure(self):
        probe = WslProcessProbe()

        def fake_ps(distro, exclude_pids):
            if distro == "Debian":
                raise RuntimeError("wsl.exe failed (1)")
            rows = [
                # wrapper（祖先：命令行自身也匹配 codex）
                (4243, 1, 10, 10, 10, "pts/0", 1000, 60, "npm",
                 "node /usr/lib/codex/codex.js run"),
                # 真正的 runtime
                (4242, 4243, 10, 10, 10, "pts/0", 1000, 60, "codex",
                 "/home/u/.codex/bin/codex"),
            ]
            return rows

        def fake_meta(distro, pids):
            if distro != "Ubuntu":
                return {}
            return {4242: {"cwd": "/home/u/proj", "ticks": "36791842",
                           "uid": "1000", "home": "/home/u",
                           "env": {"WT_SESSION": "g1"}}}

        with unittest.mock.patch.object(WslProcessProbe, "_list_running_distros",
                                        return_value=["Ubuntu", "Debian"]), \
             unittest.mock.patch.object(WslProcessProbe, "_ps_scan",
                                        side_effect=fake_ps), \
             unittest.mock.patch.object(WslProcessProbe, "_metadata",
                                        side_effect=fake_meta):
            instances, healthy = probe.scan()

        self.assertIn("wsl:Ubuntu", instances)
        self.assertIn("wsl:Debian", instances)   # Debian 无缓存 → 空
        self.assertEqual(instances["wsl:Debian"], [])
        self.assertTrue(healthy["wsl:Ubuntu"])
        self.assertFalse(healthy["wsl:Debian"])
        # wrapper 折叠后只剩 runtime 进程
        ubantu = instances["wsl:Ubuntu"]
        self.assertEqual(len(ubantu), 1)
        inst = ubantu[0]
        self.assertEqual(inst.pid, 4242)
        self.assertEqual(inst.process_token, "36791842")
        self.assertEqual(inst.process_token_source, "proc")
        self.assertEqual(inst.cwd, "/home/u/proj")
        self.assertEqual(inst.wt_session, "g1")
        self.assertEqual(inst.launcher_pids, (4243,))

    def test_scan_fallback_token_when_ticks_missing(self):
        probe = WslProcessProbe()

        def fake_ps(distro, exclude_pids):
            return [(50, 1, 10, 10, 10, "pts/0", 1000, 120, "codex",
                     "/usr/bin/codex")]

        def fake_meta(distro, pids):
            return {50: {"cwd": "/w", "ticks": "", "uid": "", "home": "",
                         "env": {}}}

        with unittest.mock.patch.object(WslProcessProbe, "_list_running_distros",
                                        return_value=["Ubuntu"]), \
             unittest.mock.patch.object(WslProcessProbe, "_ps_scan",
                                        side_effect=fake_ps), \
             unittest.mock.patch.object(WslProcessProbe, "_metadata",
                                        side_effect=fake_meta):
            instances, healthy = probe.scan()
        inst = instances["wsl:Ubuntu"][0]
        self.assertTrue(inst.process_token)          # 绝不退化成空
        self.assertEqual(inst.process_token_source, "fallback")
        # key 含 fallback token，而不是裸 PID identity
        self.assertEqual(inst.key,
                         f"wsl:Ubuntu|codex|50|{inst.process_token}")


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
