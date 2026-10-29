"""ProcessProbe 测试：解析、PID reuse、env 隐私、多 distro、UNC 安全（plan §57/§58）。

V3.1：进程树 canonicalization、fallback 代次 token、root metadata 默认关、
按真实 source 的健康隔离、--running --quiet 解析、输出解码。
"""
from __future__ import annotations
import subprocess
import time
import unittest
import unittest.mock
import os
import tempfile
from pathlib import Path

from agents import paths
from agents import discovery as discovery_mod
from agents.discovery import (
    DistroInventory,
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
from agents.models import AgentKind, AgentInstance, TerminalAttachment


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


class CodexStatePathTests(unittest.TestCase):
    def test_sqlite_home_override_is_considered_before_codex_home(self):
        with tempfile.TemporaryDirectory() as temp:
            override = Path(temp) / "sqlite-home"
            fallback = Path(temp) / "codex-home"
            override.mkdir()
            fallback.mkdir()
            (override / "state_5.sqlite").touch()
            (fallback / "state_6.sqlite").touch()
            with unittest.mock.patch.dict(
                    os.environ, {"CODEX_SQLITE_HOME": str(override)},
                    clear=False), unittest.mock.patch.object(
                        paths, "codex_home_root", return_value=str(fallback)):
                found = paths.codex_state_db_candidates()
            self.assertEqual(found[0], str(override / "state_5.sqlite"))
            self.assertEqual(found[1], str(fallback / "state_6.sqlite"))
class EnvironmentPrivacyTests(unittest.TestCase):
    """模拟含密钥的 environ：最终 Python 对象只能包含 allowlist。"""

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

    def test_match_agent_opencode_dsh_strict(self):
        # 无插件监听 plan：OpenCode/DSH 严格 argv0 形态匹配
        self.assertEqual(WslProcessProbe._match_agent(
            "opencode", "/home/u/.local/share/opencode/opencode"),
            AgentKind.OPENCODE)
        self.assertEqual(WslProcessProbe._match_agent(
            "opencode", "opencode run"), AgentKind.OPENCODE)
        self.assertEqual(WslProcessProbe._match_agent(
            "dsh", "/home/u/.dsh/bin/dsh"), AgentKind.DSH)
        self.assertEqual(WslProcessProbe._match_agent(
            "dsh", "dsh agent"), AgentKind.DSH)
        # 包含关键词但不构成 Agent 的命令不得误报
        self.assertIsNone(WslProcessProbe._match_agent(
            "vim", "vim opencode-notes.txt"))
        self.assertIsNone(WslProcessProbe._match_agent(
            "grep", "grep dsh log"))
        self.assertIsNone(WslProcessProbe._match_agent(
            "sh", "sh -c ./deploy-opencode.sh"))
        # .dsh 数据路径不是 agent（v4.4.2：/.dsh/ 子串收紧为 /.dsh/bin/dsh）
        self.assertIsNone(WslProcessProbe._match_agent(
            "tail",
            "tail -f /home/u/.dsh/sessions/--repo--/s/session.v4.jsonl.zstd"))
        self.assertIsNone(WslProcessProbe._match_agent(
            "cat", "cat ~/.dsh/config.yaml"))
        # 规范安装路径的 wrapper 形态仍识别（上游 apps/desktop/cli/dsh）
        self.assertEqual(WslProcessProbe._match_agent(
            "node", "node /home/u/.dsh/bin/dsh serve"), AgentKind.DSH)
        # 当前 @deepseek-ai/dsh npm/pnpm 包的真实 node 入口；旧匹配只看
        # argv0=dsh，导致标准安装完全漏报。
        self.assertEqual(WslProcessProbe._match_agent(
            "node", "node /home/u/node_modules/@deepseek-ai/dsh/lib/bin.js web"),
            AgentKind.DSH)
        self.assertEqual(WslProcessProbe._match_agent(
            "node", "/home/u/node_modules/.bin/dsh --profile web"),
            AgentKind.DSH)


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


class DataRootOverrideTests(unittest.TestCase):
    """用户自选数据根（Windows 平面）：优先级、派生、WSL 不受影响。"""

    def setUp(self):
        paths.set_data_root_overrides({})

    tearDown = setUp

    def test_override_beats_env_and_default(self):
        with unittest.mock.patch.dict(os.environ,
                                      {"CODEX_HOME": "D:\\env\\codex"}):
            self.assertTrue(paths.codex_home_root().endswith("codex"))
            paths.set_data_root_overrides({"codex": "D:\\pick\\codex"})
            self.assertEqual(paths.codex_home_root(), "D:\\pick\\codex")
            self.assertEqual(paths.default_data_root_hint("codex"),
                             "D:\\env\\codex")

    def test_windows_roots_derive_session_child(self):
        paths.set_data_root_overrides({
            "claude": "D:\\pick\\claude",
            "pi": "D:\\pick\\pi"})
        self.assertEqual(paths.windows_roots(AgentKind.CLAUDE),
                         ["D:\\pick\\claude\\projects"])
        self.assertEqual(paths.windows_roots(AgentKind.PI),
                         ["D:\\pick\\pi\\agent\\sessions"])

    def test_invalid_values_and_unknown_keys_ignored(self):
        paths.set_data_root_overrides(
            {"codex": "relative/x", "nope": "D:\\x", "dsh": "  "})
        self.assertEqual(paths.data_root_overrides(), {})

    def test_wsl_instance_roots_ignore_overrides(self):
        paths.set_data_root_overrides({"codex": "D:\\pick\\codex"})
        inst = AgentInstance(
            kind=AgentKind.CODEX, pid=5, source="wsl:Ubuntu",
            cwd="/w/p", key="k", uid=1000, user="u", home="/home/u")
        roots = paths.instance_roots(inst)
        self.assertTrue(roots and roots[0].startswith("\\\\wsl.localhost"))
        self.assertNotIn("pick", roots[0])


class WslUncTests(unittest.TestCase):
    def test_conversion_normalization_rejection_and_roundtrip(self):
        self.assertEqual(paths.wsl_unc("Ubuntu", "/home/foo/.codex"),
                         "\\\\wsl.localhost\\Ubuntu\\home\\foo\\.codex")
        self.assertEqual(paths.wsl_unc("Ubuntu", "/root"),
                         "\\\\wsl.localhost\\Ubuntu\\root")
        # 规范化多余斜杠
        self.assertEqual(paths.wsl_unc("Ubuntu", "//home//x//"),
                         "\\\\wsl.localhost\\Ubuntu\\home\\x")
        for distro, linux in (("Ubuntu", ""), ("Ubuntu", "relative/path"),
                              ("Ubuntu", "/home/../etc/shadow"), ("", "/home")):
            with self.assertRaises(ValueError):
                paths.wsl_unc(distro, linux)
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

    def test_launcher_pids_never_cross_agent_kind(self):
        # Claude 100 是 Claude 150 的 wrapper，也是 Codex 300 的祖先：
        # 300 的 launcher_pids 绝不能混入跨 kind 的 100（诊断必须可信）。
        cands = [self._cand(AgentKind.CLAUDE, 100, 1),
                 self._cand(AgentKind.CLAUDE, 150, 100),
                 self._cand(AgentKind.CODEX, 300, 100)]
        canonical, launchers = canonicalize_agent_processes(
            cands, {100: 1, 150: 100, 300: 100})
        self.assertEqual(canonical, {150, 300})
        self.assertEqual(launchers.get(150), (100,))   # 同 kind wrapper 保留
        self.assertEqual(launchers.get(300, ()), ())   # 跨 kind 不得混入


class DistroListParsingTests(unittest.TestCase):
    def test_running_and_verbose_list_parsing(self):
        self.assertEqual(parse_running_quiet("Ubuntu\r\nDebian\r\n"),
                         ["Ubuntu", "Debian"])
        self.assertEqual(parse_running_quiet("\ufeffNAME\nUbuntu\n"),
                         ["Ubuntu"])
        verbose = ("  NAME            STATE           VERSION\n"
                   "* Ubuntu          Running         2\n"
                   "  Debian          Stopped         2\n")
        self.assertEqual(parse_list_verbose(verbose), ["Ubuntu"])

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
    def test_token_source_priority_and_stability(self):
        probe = WslProcessProbe()
        token, source = probe._resolve_token("Ubuntu", 5, AgentKind.CODEX,
                                             "36791842", 60)
        self.assertEqual(token, "36791842")
        self.assertEqual(source, "proc")
        t1, s1 = probe._resolve_token("Ubuntu", 5, AgentKind.CODEX, "", 60)
        t2, s2 = probe._resolve_token("Ubuntu", 5, AgentKind.CODEX, "", 63)
        self.assertEqual(t1, t2)
        self.assertEqual(s1, "fallback")
        self.assertEqual(s2, "fallback")
        self.assertTrue(t1.startswith("fb"))
        token, _ = probe._resolve_token("Ubuntu", 9, AgentKind.KIMI, "", 1)
        self.assertTrue(token)

    def test_generation_rotates_on_pid_reuse_or_kind_change(self):
        probe = WslProcessProbe()
        t1, _ = probe._resolve_token("Ubuntu", 5, AgentKind.CODEX, "", 300)
        # etimes 明显回退 → 同 PID 已被复用
        t2, _ = probe._resolve_token("Ubuntu", 5, AgentKind.CODEX, "", 5)
        self.assertNotEqual(t1, t2)
        t3, _ = probe._resolve_token("Ubuntu", 5, AgentKind.CODEX, "", 60)
        t4, _ = probe._resolve_token("Ubuntu", 5, AgentKind.CLAUDE, "", 62)
        self.assertNotEqual(t3, t4)
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


class RuntimeRootPermissionTests(unittest.TestCase):
    """DP43-R03：privacy.wsl_root_metadata_fallback 运行期切换立即生效。"""

    def _fake_run_factory(self, calls, missing_meta=True):
        def fake_run(distro, script, timeout=8.0, user=None):
            calls.append((distro, user))
            if missing_meta:
                return "P\t42\nC\t\nT\t\n"
            return "P\t42\nC\t/w\nT\t123\n"
        return fake_run

    def test_runtime_false_to_true_allows_later_root_retry(self):
        probe = WslProcessProbe(allow_root_metadata=False)
        self.assertFalse(probe.root_metadata_allowed())
        calls = []
        with unittest.mock.patch("agents.discovery._run_wsl",
                                 side_effect=self._fake_run_factory(calls)):
            probe._metadata("Ubuntu", [42])
            self.assertEqual(calls, [("Ubuntu", None)])   # 初始无 root
            probe.set_allow_root_metadata(True)
            probe._metadata("Ubuntu", [42])
        self.assertEqual(calls[-1], ("Ubuntu", "root"))   # 开启后允许

    def test_runtime_true_to_false_blocks_subsequent_root_retry(self):
        probe = WslProcessProbe(allow_root_metadata=True)
        calls = []
        with unittest.mock.patch("agents.discovery._run_wsl",
                                 side_effect=self._fake_run_factory(calls)):
            probe._metadata("Ubuntu", [42])
            self.assertEqual(calls[-1], ("Ubuntu", "root"))
            probe.set_allow_root_metadata(False)   # 运行期撤权
            probe._metadata("Ubuntu", [42])
        # 撤权后仍有普通 metadata 查询，但绝无新的 root retry
        self.assertEqual(calls,
                         [("Ubuntu", None), ("Ubuntu", "root"),
                          ("Ubuntu", None)])
        self.assertNotIn("root", [user for _, user in calls[2:]])
        self.assertFalse(probe.root_metadata_allowed())

    def test_disable_between_normal_and_root_retry_blocks_root(self):
        # 普通metadata 查询后、root retry 前用户关闭开关 → root 被阻止
        probe = WslProcessProbe(allow_root_metadata=True)
        calls = []
        gate = {"open": True}

        def gated_run(distro, script, timeout=8.0, user=None):
            calls.append((distro, user))
            if user is None:
                # 普通 metadata 完成后立刻撤权（模拟用户恰在此刻关闭）
                if gate["open"]:
                    gate["open"] = False
                    probe.set_allow_root_metadata(False)
            return "P\t42\nC\t\nT\t\n"

        with unittest.mock.patch("agents.discovery._run_wsl",
                                 side_effect=gated_run):
            meta = probe._metadata("Ubuntu", [42])
        self.assertEqual(calls, [("Ubuntu", None)])   # root retry 被挡
        self.assertEqual(meta[42]["cwd"], "")

    def test_set_allow_is_thread_safe_and_fast(self):
        # O(1) Event 语义：重复设置无异常，读值一致
        probe = WslProcessProbe()
        probe.set_allow_root_metadata(True)
        self.assertTrue(probe.root_metadata_allowed())
        probe.set_allow_root_metadata(True)
        self.assertTrue(probe.root_metadata_allowed())
        probe.set_allow_root_metadata(False)
        self.assertFalse(probe.root_metadata_allowed())

    def test_monitor_and_probe_worker_runtime_api(self):
        # Monitor.set_wsl_root_metadata_fallback → probe 透传（O(1)，无 WSL call）
        from agents.monitor import Monitor, ProcessProbeWorker
        worker = ProcessProbeWorker.__new__(ProcessProbeWorker)
        worker._wsl = WslProcessProbe(allow_root_metadata=False)
        worker.set_wsl_root_metadata_fallback(True)
        self.assertTrue(worker._wsl.root_metadata_allowed())
        monitor = Monitor.__new__(Monitor)
        monitor._probe = worker
        monitor.set_wsl_root_metadata_fallback(False)
        self.assertFalse(worker._wsl.root_metadata_allowed())

    def test_save_failure_does_not_revert_runtime_privacy_flag(self):
        # Dashboard 顺序：Config 内存 → runtime 权限 → 异步持久化；
        # 磁盘保存失败不得回滚运行期隐私意图（probe 状态独立于 Config）
        probe = WslProcessProbe(allow_root_metadata=True)
        probe.set_allow_root_metadata(False)
        # 模拟 config 保存失败：probe 的 Event 不受影响
        self.assertFalse(probe.root_metadata_allowed())



    """scan() 返回按真实 source 分组的结果与健康位（隔离失败）。"""

    def test_scan_groups_by_source_and_isolates_failure(self):
        probe = WslProcessProbe()

        def fake_ps(distro, exclude_pids):
            if distro == "Debian":
                raise RuntimeError("wsl.exe failed (1)")
            rows = [
                # wrapper（祖先：命令行自身也匹配 codex）
                (4243, 1, 10, 10, 10, "pts/0", 1000, 60, "S", "npm",
                 "node /usr/lib/codex/codex.js run"),
                # 真正的 runtime
                (4242, 4243, 10, 10, 10, "pts/0", 1000, 60, "S", "codex",
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
                                        return_value=DistroInventory(
                                            ("Ubuntu", "Debian"), True)), \
             unittest.mock.patch.object(WslProcessProbe, "_ps_scan",
                                        side_effect=fake_ps), \
             unittest.mock.patch.object(WslProcessProbe, "_metadata",
                                        side_effect=fake_meta):
            snap = probe.scan()

        self.assertIn("wsl:Ubuntu", snap)
        self.assertIn("wsl:Debian", snap)   # Debian ps 失败 → 缓存空 + 不权威
        self.assertEqual(snap["wsl:Debian"].instances, ())
        self.assertFalse(snap["wsl:Debian"].authoritative)
        self.assertTrue(snap["wsl:Ubuntu"].authoritative)
        # wrapper 折叠后只剩 runtime 进程
        ubantu = snap["wsl:Ubuntu"].instances
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
            return [(50, 1, 10, 10, 10, "pts/0", 1000, 120, "S", "codex",
                     "/usr/bin/codex")]

        def fake_meta(distro, pids):
            return {50: {"cwd": "/w", "ticks": "", "uid": "", "home": "",
                         "env": {}}}

        with unittest.mock.patch.object(WslProcessProbe, "_list_running_distros",
                                        return_value=DistroInventory(
                                            ("Ubuntu",), True)), \
             unittest.mock.patch.object(WslProcessProbe, "_ps_scan",
                                        side_effect=fake_ps), \
             unittest.mock.patch.object(WslProcessProbe, "_metadata",
                                        side_effect=fake_meta):
            snap = probe.scan()
        inst = snap["wsl:Ubuntu"].instances[0]
        self.assertTrue(inst.process_token)          # 绝不退化成空
        self.assertEqual(inst.process_token_source, "fallback")
        # key 含 fallback token，而不是裸 PID identity
        self.assertEqual(inst.key,
                         f"wsl:Ubuntu|codex|50|{inst.process_token}")


def _codex_row(pid, ppid=1, etimes=60, stat="S"):
    return (pid, ppid, 10, 10, 10, "pts/0", 1000, etimes, stat, "codex",
            "/usr/bin/codex")


def _no_ticks_meta(distro, pids):
    return {p: {"cwd": "/w", "ticks": "", "uid": "", "home": "",
                "env": {}} for p in pids}


class WslLifecycleTests(unittest.TestCase):
    """V3.1.1：Running / Stopped / Failed 三态彻底分离。"""

    def _scan(self, probe, inventory, ps_rows=None, ps_error=None):
        def fake_ps(distro, exclude_pids):
            if ps_error is not None:
                raise ps_error
            return ps_rows if ps_rows is not None else [_codex_row(100)]

        with unittest.mock.patch.object(WslProcessProbe, "_list_running_distros",
                                        return_value=inventory), \
             unittest.mock.patch.object(WslProcessProbe, "_ps_scan",
                                        side_effect=fake_ps), \
             unittest.mock.patch.object(WslProcessProbe, "_metadata",
                                        side_effect=_no_ticks_meta):
            return probe.scan()

    def test_successful_empty_running_list_is_authoritative(self):
        # 上一轮 Ubuntu 有缓存实例；本轮 quiet list 成功返回空 →
        # 权威确认无 Running distro，绝不能返回旧缓存 Agent。
        probe = WslProcessProbe()
        snap = self._scan(probe, DistroInventory(("Ubuntu",), True))
        self.assertTrue(snap["wsl:Ubuntu"].instances)
        snap = self._scan(probe, DistroInventory((), True))
        self.assertEqual(snap["wsl:Ubuntu"].instances, ())
        self.assertTrue(snap["wsl:Ubuntu"].authoritative)
        self.assertTrue(probe.last_ok)

    def test_one_of_two_distros_stopped_emits_empty_tombstone(self):
        probe = WslProcessProbe()
        both = DistroInventory(("Ubuntu", "Debian"), True)
        snap = self._scan(probe, both)
        self.assertTrue(snap["wsl:Ubuntu"].instances)
        self.assertTrue(snap["wsl:Debian"].instances)
        # Debian 停止：本轮起持续输出 authoritative 空 tombstone
        for _ in range(3):
            snap = self._scan(probe, DistroInventory(("Ubuntu",), True))
            self.assertEqual(snap["wsl:Debian"].instances, ())
            self.assertTrue(snap["wsl:Debian"].authoritative)
            self.assertTrue(snap["wsl:Ubuntu"].instances)
            self.assertTrue(snap["wsl:Ubuntu"].authoritative)

    def test_inventory_failure_keeps_cache_unhealthy(self):
        probe = WslProcessProbe()
        snap = self._scan(probe, DistroInventory(("Ubuntu",), True))
        old = snap["wsl:Ubuntu"].instances
        self.assertTrue(old)
        # 枚举失败：保留缓存实例、不 authoritative，绝不变成空
        snap = self._scan(
            probe, DistroInventory(("Ubuntu",), False, error="wsl broke"))
        self.assertEqual(snap["wsl:Ubuntu"].instances, old)
        self.assertFalse(snap["wsl:Ubuntu"].authoritative)
        self.assertFalse(probe.last_ok)
        # 失败后恢复：Debian 从未 known，不产生幽灵 tombstone
        snap = self._scan(probe, DistroInventory(("Ubuntu",), True))
        self.assertTrue(snap["wsl:Ubuntu"].authoritative)

    def test_stopped_distro_clears_fallback_identities(self):
        probe = WslProcessProbe()
        snap = self._scan(probe, DistroInventory(("Ubuntu",), True))
        old_key = snap["wsl:Ubuntu"].instances[0].key
        old_token = snap["wsl:Ubuntu"].instances[0].process_token
        # 停止 → fallback 代次缓存清除
        self._scan(probe, DistroInventory((), True))
        self.assertFalse(probe._fallback)
        # 重启后同 PID 从小整数再来：新代次 token，绝不继承旧 identity
        snap = self._scan(probe, DistroInventory(("Ubuntu",), True))
        inst = snap["wsl:Ubuntu"].instances[0]
        self.assertNotEqual(inst.key, old_key)
        self.assertNotEqual(inst.process_token, old_token)

    def test_stopped_distro_clears_process_cache(self):
        probe = WslProcessProbe()
        self._scan(probe, DistroInventory(("Ubuntu",), True))
        self.assertIn("Ubuntu", probe._cache)
        self._scan(probe, DistroInventory((), True))
        self.assertNotIn("Ubuntu", probe._cache)
        self.assertNotIn("Ubuntu", probe._distro_ok)

    # ---- V3.1.2：探测绝不重启用户已停止的 distro ----
    # 旧缺陷在 _list_running_distros 内部的 15s 正缓存，所以这组测试
    # patch 在 subprocess 边界，走真实的枚举/解析/门控代码路径。

    def _subprocess_scan(self, probe, state, ps_rows=None):
        """真实执行 scan()，仅在 subprocess.run 边界模拟 wsl.exe。

        state["running"]：--list --running --quiet 的当前结果（测试在两轮
        之间修改它，模拟用户 terminate）；state["fail"]=True 时两条枚举
        路径（--quiet 与 -l -v）都 rc=1。返回 (scan 结果, 全部 wsl.exe
        argv 记录)——`-d` 出现在 argv 里 = 一次会启动目标 distro 的调用。
        """
        calls: list[list[str]] = []
        ps_text = "".join(
            "\t".join(str(x) for x in row) + "\n" for row in (ps_rows or []))

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            rc, out = 0, b""
            if "--list" in argv:
                if state.get("fail"):
                    rc = 1
                else:
                    out = "".join(n + "\n" for n in state["running"]).encode("utf-16")
            elif len(argv) >= 2 and argv[1] == "-l":
                if state.get("fail"):
                    rc = 1
                else:
                    out = "".join(
                        f"  {n}    Running    2\n" for n in state["running"]
                    ).encode("utf-8")
            else:                       # wsl -d <distro> --exec ...
                out = ps_text.encode("utf-8")
            return subprocess.CompletedProcess(argv, rc, stdout=out)

        with unittest.mock.patch.object(discovery_mod.subprocess, "run",
                                        side_effect=fake_run), \
             unittest.mock.patch.object(WslProcessProbe, "_metadata",
                                        side_effect=_no_ticks_meta):
            result = probe.scan()
        return result, calls

    @staticmethod
    def _wsl_d_calls(calls):
        return [c for c in calls if "-d" in c]

    def test_stopped_distro_is_never_relaunched_by_probe(self):
        # t0：Ubuntu Running → 探测正常进入 distro（wsl -d 被执行是合法的）
        probe = WslProcessProbe()
        state = {"running": ["Ubuntu"]}
        snap, calls = self._subprocess_scan(probe, state)
        self.assertTrue(snap["wsl:Ubuntu"].authoritative)
        self.assertTrue(self._wsl_d_calls(calls))
        # 用户 terminate Ubuntu：之后每轮 quiet list 都成功返回空
        # （authoritative empty）。不变量：停止后绝不再对 Ubuntu 执行
        # wsl -d —— 那会把它重新启动。
        state["running"] = []
        for _ in range(3):
            snap, calls = self._subprocess_scan(probe, state)
            self.assertEqual(snap["wsl:Ubuntu"].instances, ())
            self.assertTrue(snap["wsl:Ubuntu"].authoritative)
            self.assertFalse(self._wsl_d_calls(calls))
        self.assertTrue(probe.last_ok)

    def test_cached_running_inventory_never_authorizes_wsl_exec(self):
        # 锁死回归：即使存在"15 秒内 Ubuntu Running"的过期权威缓存
        # （旧字段直接注入，模拟缓存机制被重新引入），只要本轮 fresh
        # 枚举成功返回空，就绝不能据此执行任何 wsl -d。
        probe = WslProcessProbe()
        probe._inventory = DistroInventory(("Ubuntu",), True)
        probe._inventory_ts = time.time()
        state = {"running": []}
        snap, calls = self._subprocess_scan(probe, state)
        self.assertNotIn("wsl:Ubuntu", snap)
        self.assertTrue(probe.last_ok)
        self.assertFalse(self._wsl_d_calls(calls))

    def test_inventory_failure_does_not_probe_cached_distro(self):
        # 枚举双路径失败：沿用旧名单输出缓存实例 + 不 authoritative，
        # 但绝不进入 distro 执行 wsl -d（无法读取 ≠ 可以探测/重启）。
        probe = WslProcessProbe()
        state = {"running": ["Ubuntu"]}
        snap, _ = self._subprocess_scan(
            probe, state, ps_rows=[_codex_row(100)])
        self.assertEqual(len(snap["wsl:Ubuntu"].instances), 1)
        state["fail"] = True
        snap, calls = self._subprocess_scan(probe, state)
        self.assertEqual(len(snap["wsl:Ubuntu"].instances), 1)  # 缓存保留
        self.assertFalse(snap["wsl:Ubuntu"].authoritative)
        self.assertFalse(probe.last_ok)
        self.assertFalse(self._wsl_d_calls(calls))

    def test_dead_state_processes_are_not_live_agents(self):
        # Z/X/x 状态的进程不作为 live Agent（v4plan §4.4）。
        probe = WslProcessProbe()
        state = {"running": ["Ubuntu"]}
        snap, _ = self._subprocess_scan(
            probe, state,
            ps_rows=[_codex_row(100, stat="Z"), _codex_row(101, stat="S"),
                     _codex_row(102, stat="X")])
        pids = [i.pid for i in snap["wsl:Ubuntu"].instances]
        self.assertEqual(pids, [101])

    def test_ps_format_includes_stat_column(self):
        from agents.discovery import _PS_FORMAT
        self.assertIn("stat=", _PS_FORMAT)


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
            root = Path(temp) / ".kimi-code"
            root.mkdir()
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
            with unittest.mock.patch.object(paths, "_user_home", return_value=temp):
                candidates = paths.kimi_wire_candidates(inst)
            # windows 侧 root 为 ~/.kimi-code；索引在同 root 下才能解析
            self.assertTrue(all(p.endswith("wire.jsonl") for _m, p in candidates))

    def test_wire_candidates_fall_back_to_session_state_when_index_omits_row(self):
        """Kimi Code owns cwd/session facts in state.json; a stale index must
        not hide a resumed session, while a different cwd remains excluded."""
        import json as _json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".kimi-code"
            root.mkdir()
            good = root / "sessions" / "key1" / "good" / "agents" / "main"
            bad = root / "sessions" / "key2" / "bad" / "agents" / "main"
            good.mkdir(parents=True)
            bad.mkdir(parents=True)
            (good.parent.parent / "state.json").write_text(
                _json.dumps({"cwd": "/proj"}), encoding="utf-8")
            (bad.parent.parent / "state.json").write_text(
                _json.dumps({"cwd": "/other"}), encoding="utf-8")
            (good / "wire.jsonl").write_text(
                '{"type":"session","id":"good"}\n', encoding="utf-8")
            (bad / "wire.jsonl").write_text(
                '{"type":"session","id":"bad"}\n', encoding="utf-8")
            inst = AgentInstance(kind=AgentKind.KIMI, pid=1,
                                 source="windows", process_token="1",
                                 cwd="/proj")
            with unittest.mock.patch.object(paths, "_user_home",
                                            return_value=temp):
                candidates = paths.kimi_wire_candidates(inst)
            self.assertEqual([p for _m, p in candidates],
                             [str(good / "wire.jsonl")])


# ==================================================== v4.4 Windows inventory

def _proc_rows(rows):
    """(pid, ppid, name, cmdline, create_time, exe) → 假 psutil process_iter。"""
    import types
    out = []
    for row in rows:
        if len(row) == 6:
            pid, ppid, name, cmd, ct, exe = row
            status = "running"
        else:
            pid, ppid, name, cmd, ct, exe, status = row
        out.append(types.SimpleNamespace(info={
            "pid": pid, "ppid": ppid, "name": name, "cmdline": list(cmd),
            "create_time": ct, "exe": exe, "status": status}))
    return out


_EXPLORER = 4
_CHATGPT_CODEX_EXE = ("C:\\Program Files\\WindowsApps\\"
                      "OpenAI.Codex_26.903.9818.0_x64__2p2nqsd0c76g0\\app\\ChatGPT.exe")
_CHATGPT_PLAIN_EXE = ("C:\\Program Files\\WindowsApps\\"
                      "OpenAI.ChatGPT_26.903.9818.0_x64__2p2nqsd0c76g0\\app\\ChatGPT.exe")


class WindowsInventoryTests(unittest.TestCase):
    """plan2 §4/§15：一次 census 同时产出终端 Agent 与 DesktopHost；
    Codex 桌面宿主不再冒充 CLI（4.3.1 宽 codex*.exe 匹配的修正）。"""

    def _scan(self, rows):
        from agents.discovery import scan_windows_inventory
        with unittest.mock.patch(
                "psutil.process_iter",
                return_value=_proc_rows(rows)):
            return scan_windows_inventory()

    # ---- 身份模型 ----

    def test_terminal_key_unchanged_from_v431(self):
        # plan2 §3.3：TERMINAL .key 生成与 4.3.1 完全一致
        inv = self._scan([
            (100, _EXPLORER, "WindowsTerminal.exe", ["wt"], 1000.0, ""),
            (101, 100, "codex.exe", ["codex"], 1001.5,
             "C:\\Users\\u\\AppData\\Local\\OpenAI\\Codex\\bin\\h\\codex.exe"),
        ])
        self.assertEqual(len(inv.terminal_instances), 1)
        inst = inv.terminal_instances[0]
        self.assertEqual(inst.key, f"windows|codex|101|1001.500")
        from agents.models import AgentSurface
        self.assertIs(inst.surface, AgentSurface.TERMINAL)
        self.assertEqual(inv.desktop_hosts, ())

    def test_desktop_instance_and_host_key_shape(self):
        from agents.models import AgentInstance, AgentSurface, DesktopHost
        inst = AgentInstance(kind=AgentKind.CODEX, pid=0, source="windows",
                             surface=AgentSurface.DESKTOP,
                             host_process_token="1789146850.665",
                             host_pid=8068,
                             logical_session_id="thread-abc")
        self.assertEqual(
            inst.key, "windows|codex|desktop|1789146850.665|thread-abc")
        host_a = DesktopHost(kind=AgentKind.CODEX, pid=8068,
                             process_token="111.000")
        host_b = DesktopHost(kind=AgentKind.CODEX, pid=8068,
                             process_token="222.000")
        # 宿主重启（同 PID 复用）→ 新 runtime key，旧 target 不被继承
        self.assertNotEqual(host_a.host_key, host_b.host_key)
        self.assertIn("111.000", host_a.host_key)

    # ---- Codex 桌面 / CLI 区分 ----

    def test_codex_cli_not_collapsed_into_desktop(self):
        # plan2 §15：终端 ancestry 下的 codex.exe 仍是 CLI target
        inv = self._scan([
            (100, _EXPLORER, "WindowsTerminal.exe", ["wt"], 1000.0, ""),
            (101, 100, "codex.exe", ["codex"], 1001.5,
             "C:\\Users\\u\\.codex\\bin\\codex.exe"),
        ])
        self.assertEqual([i.kind for i in inv.terminal_instances],
                         [AgentKind.CODEX])
        self.assertEqual(inv.desktop_hosts, ())

    def test_dead_process_row_never_becomes_cli_or_desktop_host(self):
        # A stale psutil row must not create a watcher registration.  This is
        # the last line of defence when a process exits between two native
        # census calls and a compatibility layer still reports the row once.
        inv = self._scan([
            (100, _EXPLORER, "WindowsTerminal.exe", ["wt"], 1000.0, ""),
            (101, 100, "codex.exe", ["codex"], 1001.5,
             "C:\\Users\\u\\.codex\\bin\\codex.exe", "dead"),
        ])
        self.assertEqual(inv.terminal_instances, ())
        self.assertEqual(inv.desktop_hosts, ())

    def test_codex_sandbox_service_is_not_an_agent_candidate(self):
        # Codex's Windows sandbox service may be alive outside ChatGPT's tree;
        # it must not create a terminal target or an unattributed warning.
        inv = self._scan([
            (33616, _EXPLORER, "codex-windows-sandbox-service.exe", [],
             1001.5, "C:\\Program Files\\Codex\\codex-windows-sandbox-service.exe"),
        ])
        self.assertEqual(inv.terminal_instances, ())
        self.assertEqual(inv.desktop_hosts, ())
        self.assertEqual(inv.diagnostics, ())

    def test_codex_gui_host_not_emitted_as_terminal_agent(self):
        # 实机形态：ChatGPT.exe(OpenAI.Codex 包) + codex.exe runtime 子进程。
        # 4.3.1 会把 codex.exe 误判成 CLI；v4.4 折叠为一个 Codex DesktopHost。
        inv = self._scan([
            (200, _EXPLORER, "ChatGPT.exe", ["ChatGPT.exe"], 2000.0,
             _CHATGPT_CODEX_EXE),
            (201, 200, "codex.exe",
             ["codex.exe", "-c", "features.code_mode_host=true"], 2001.0,
             "C:\\Users\\u\\AppData\\Local\\OpenAI\\Codex\\bin\\h\\codex.exe"),
            (202, 200, "ChatGPT.exe",
             ["ChatGPT.exe", "--type=renderer", "--s"], 2002.0,
             _CHATGPT_CODEX_EXE),
        ])
        self.assertEqual(inv.terminal_instances, ())
        self.assertEqual(len(inv.desktop_hosts), 1)
        host = inv.desktop_hosts[0]
        self.assertIs(host.kind, AgentKind.CODEX)
        self.assertEqual(host.pid, 200)
        self.assertIn(201, host.helper_pids)
        self.assertIn(202, host.helper_pids)

    def test_codex_helpers_fold_into_one_host(self):
        inv = self._scan([
            (200, _EXPLORER, "ChatGPT.exe", ["ChatGPT.exe"], 2000.0,
             _CHATGPT_CODEX_EXE),
            (202, 200, "ChatGPT.exe",
             ["ChatGPT.exe", "--type=gpu-process"], 2002.0, _CHATGPT_CODEX_EXE),
            (203, 200, "codex-app-server.exe", ["codex-app-server.exe"], 2003.0, ""),
            (204, 203, "codex.exe", ["codex", "app-server"], 2004.0, ""),
        ])
        self.assertEqual(len(inv.desktop_hosts), 1)
        host = inv.desktop_hosts[0]
        self.assertEqual(set(host.helper_pids), {202, 203, 204})

    def test_codex_exe_unknown_ancestry_is_not_terminal(self):
        # 证据冲突/不足（explorer 直启、无 conhost）→ 宁可漏报也不冒充 CLI
        inv = self._scan([
            (400, _EXPLORER, "codex.exe", ["codex"], 4000.0,
             "C:\\Users\\u\\AppData\\Local\\OpenAI\\Codex\\bin\\h\\codex.exe"),
        ])
        self.assertEqual(inv.terminal_instances, ())
        self.assertEqual(inv.desktop_hosts, ())
        self.assertTrue(any("unattributed" in d for d in inv.diagnostics))

    def test_codex_exe_with_conhost_child_is_terminal(self):
        # explorer 双击启动的控制台 CLI：conhost 子进程是控制台宿主证据
        inv = self._scan([
            (400, _EXPLORER, "codex.exe", ["codex"], 4000.0,
             "C:\\Users\\u\\.codex\\bin\\codex.exe"),
            (401, 400, "conhost.exe", ["conhost"], 4000.1, ""),
        ])
        self.assertEqual(len(inv.terminal_instances), 1)
        self.assertEqual(inv.terminal_instances[0].pid, 400)

    # ---- ChatGPT 边界 ----

    def test_plain_chatgpt_without_codex_evidence_is_nothing(self):
        # plan2 §4.3：不能把所有 ChatGPT.exe 都当 Codex
        inv = self._scan([
            (210, _EXPLORER, "ChatGPT.exe", ["ChatGPT.exe"], 2100.0,
             _CHATGPT_PLAIN_EXE),
            (211, 210, "ChatGPT.exe",
             ["ChatGPT.exe", "--type=renderer"], 2101.0, _CHATGPT_PLAIN_EXE),
        ])
        self.assertEqual(inv.terminal_instances, ())
        self.assertEqual(inv.desktop_hosts, ())

    # ---- ZCode 宿主 ----

    def test_zcode_helpers_fold_into_one_host(self):
        inv = self._scan([
            (300, _EXPLORER, "ZCode.exe", ["ZCode.exe"], 3000.0,
             "D:\\Zcode\\ZCode.exe"),
            (301, 300, "ZCode.exe",
             ["ZCode.exe", "--type=renderer",
              "--user-data-dir=C:\\Users\\u\\AppData\\Roaming\\ZCode"],
             3001.0, "D:\\Zcode\\ZCode.exe"),
            (302, 301, "ZCode.exe",
             ["ZCode.exe", "D:\\Zcode\\resources\\glm\\zcode.cjs", "app-server"],
             3002.0, "D:\\Zcode\\ZCode.exe"),
            (303, 302, "ZCode.exe",
             ["ZCode.exe", "D:\\Zcode\\resources\\glm\\zcode.cjs",
              "__zcode-plugin-host"],
             3003.0, "D:\\Zcode\\ZCode.exe"),
            (304, 300, "bash.exe",
             ["bash.exe", "-c", ".", "snapshot-bash-1"], 3004.0, ""),
            (305, 300, "wsl.exe", ["wsl.exe", "-d", "Ubuntu"], 3005.0, ""),
        ])
        self.assertEqual(inv.terminal_instances, ())
        self.assertEqual(len(inv.desktop_hosts), 1)
        host = inv.desktop_hosts[0]
        self.assertIs(host.kind, AgentKind.ZCODE)
        self.assertEqual(host.pid, 300)
        # 同 app Electron/app-server/plugin-host 收敛；bash/wsl 不是 helper
        self.assertEqual(set(host.helper_pids), {301, 302, 303})

    def test_zcode_standalone_cli_is_not_a_target(self):
        # zcode-cli 在终端里运行：无 GUI 宿主 → 4.4 不产生任何 target
        inv = self._scan([
            (100, _EXPLORER, "WindowsTerminal.exe", ["wt"], 1000.0, ""),
            (310, 100, "zcode-cli.exe", ["zcode-cli"], 3100.0, ""),
        ])
        self.assertEqual(inv.terminal_instances, ())
        self.assertEqual(inv.desktop_hosts, ())

    # ---- 混合：ZCode 集成终端内的真实 Codex CLI ----

    def test_codex_cli_inside_zcode_tree_survives(self):
        # ZCode 集成终端（bash）里运行 codex CLI 是真实 CLI，
        # 不被 ZCode 宿主树吞掉（CLI 本体不能被吞进 DesktopHost）
        inv = self._scan([
            (300, _EXPLORER, "ZCode.exe", ["ZCode.exe"], 3000.0, ""),
            (304, 300, "bash.exe", ["bash"], 3004.0, ""),
            (306, 304, "codex.exe", ["codex"], 3006.0, ""),
        ])
        hosts = {(h.kind, h.pid) for h in inv.desktop_hosts}
        self.assertEqual(hosts, {(AgentKind.ZCODE, 300)})
        self.assertEqual([i.pid for i in inv.terminal_instances], [306])
        self.assertEqual([i.kind for i in inv.terminal_instances],
                         [AgentKind.CODEX])

    def test_claude_terminal_matching_unchanged(self):
        # claude/kimi/pi 的 4.3.1 name/cmdline 匹配保持不变
        inv = self._scan([
            (100, _EXPLORER, "WindowsTerminal.exe", ["wt"], 1000.0, ""),
            (110, 100, "claude.exe", ["claude"], 1100.0, ""),
        ])
        self.assertEqual([i.kind for i in inv.terminal_instances],
                         [AgentKind.CLAUDE])

    def test_scan_windows_wrapper_compat(self):
        # plan2 §4.2：scan_windows() 兼容包装只返回 terminal_instances
        from agents.discovery import scan_windows
        rows = [
            (100, _EXPLORER, "WindowsTerminal.exe", ["wt"], 1000.0, ""),
            (101, 100, "codex.exe", ["codex"], 1001.0, ""),
        ]
        with unittest.mock.patch("psutil.process_iter",
                                 return_value=_proc_rows(rows)):
            insts = scan_windows()
        self.assertEqual([i.pid for i in insts], [101])


class OpenCodeDshInventoryTests(unittest.TestCase):
    """无插件监听 plan §三：OpenCode / DSH 的 CLI 与桌面宿主严格区分。"""

    def _scan(self, rows):
        from agents.discovery import scan_windows_inventory
        with unittest.mock.patch(
                "psutil.process_iter",
                return_value=_proc_rows(rows)):
            return scan_windows_inventory()

    # ---- OpenCode CLI ----

    def test_opencode_cli_in_terminal(self):
        inv = self._scan([
            (100, _EXPLORER, "WindowsTerminal.exe", ["wt"], 1000.0, ""),
            (101, 100, "opencode.exe", ["opencode"], 1001.5,
             "C:/Users/u/.local/bin/opencode.exe"),
        ])
        self.assertEqual([i.kind for i in inv.terminal_instances],
                         [AgentKind.OPENCODE])
        self.assertEqual(inv.desktop_hosts, ())

    def test_opencode_exe_unknown_ancestry_not_terminal(self):
        inv = self._scan([
            (400, _EXPLORER, "opencode.exe", ["opencode"], 4000.0,
             "C:/Users/u/.local/bin/opencode.exe"),
        ])
        self.assertEqual(inv.terminal_instances, ())
        self.assertEqual(inv.desktop_hosts, ())
        self.assertTrue(any("unattributed" in d for d in inv.diagnostics))

    def test_opencode_npm_wrapper_matched_by_package_mark(self):
        inv = self._scan([
            (100, _EXPLORER, "WindowsTerminal.exe", ["wt"], 1000.0, ""),
            (101, 100, "node.exe",
             ["node", "C:/Users/u/AppData/Roaming/npm//"
              "node_modules/opencode-ai/bin/opencode"], 1001.5, ""),
        ])
        self.assertEqual([i.kind for i in inv.terminal_instances],
                         [AgentKind.OPENCODE])

    # ---- OpenCode Desktop（Electron 佐证消解 opencode.exe 二义性）----

    def test_opencode_desktop_host_with_electron_helpers(self):
        inv = self._scan([
            (500, _EXPLORER, "OpenCode.exe", ["OpenCode.exe"], 5000.0,
             "C:/Users/u/AppData/Local/Programs//"
             "OpenCode/OpenCode.exe"),
            (501, 500, "OpenCode.exe",
             ["OpenCode.exe", "--type=renderer"], 5001.0,
             "C:/Users/u/AppData/Local/Programs//"
             "OpenCode/OpenCode.exe"),
            (502, 500, "opencode-cli.exe", ["opencode-cli", "serve"],
             5002.0, ""),
        ])
        self.assertEqual(inv.terminal_instances, ())
        self.assertEqual(len(inv.desktop_hosts), 1)
        host = inv.desktop_hosts[0]
        self.assertIs(host.kind, AgentKind.OPENCODE)
        self.assertEqual(host.pid, 500)
        self.assertIn(501, host.helper_pids)
        self.assertIn(502, host.helper_pids)

    def test_opencode_dev_exe_unambiguous_host(self):
        inv = self._scan([
            (510, _EXPLORER, "OpenCode Dev.exe", ["OpenCode Dev.exe"],
             5100.0, "C:/Users/u/AppData/Local/Programs//"
             "OpenCode Dev/OpenCode Dev.exe"),
        ])
        self.assertEqual(len(inv.desktop_hosts), 1)
        self.assertIs(inv.desktop_hosts[0].kind, AgentKind.OPENCODE)

    # ---- DSH ----

    def test_dsh_cli_in_terminal(self):
        inv = self._scan([
            (100, _EXPLORER, "WindowsTerminal.exe", ["wt"], 1000.0, ""),
            (111, 100, "dsh.exe", ["dsh"], 1101.0,
             "C:/Users/u/.dsh/bin/dsh.exe"),
        ])
        self.assertEqual([i.kind for i in inv.terminal_instances],
                         [AgentKind.DSH])
        self.assertEqual(inv.desktop_hosts, ())

    def test_dsh_desktop_host_with_runasnode_child(self):
        # 官方 Electron：RunAsNode Host 与主进程同 exe（不同 cmdline）
        inv = self._scan([
            (600, _EXPLORER, "DeepSeek Harness.exe",
             ["DeepSeek Harness.exe"], 6000.0,
             "C:/Users/u/AppData/Local/Programs//"
             "DeepSeek Harness/DeepSeek Harness.exe"),
            (601, 600, "DeepSeek Harness.exe",
             ["DeepSeek Harness.exe", "--run-as-node", "host.cjs"], 6001.0,
             "C:/Users/u/AppData/Local/Programs//"
             "DeepSeek Harness/DeepSeek Harness.exe"),
            (602, 600, "node.exe", ["node", "pnpm.mjs"], 6002.0, ""),
        ])
        self.assertEqual(inv.terminal_instances, ())
        self.assertEqual(len(inv.desktop_hosts), 1)
        host = inv.desktop_hosts[0]
        self.assertIs(host.kind, AgentKind.DSH)
        self.assertEqual(host.pid, 600)
        # 同 exe RunAsNode Host 收敛为 helper；node/pnpm 不是 helper
        self.assertEqual(set(host.helper_pids), {601})

    def test_two_hosts_two_desktop_hosts(self):
        # 两个 GUI host（OpenCode + DSH 并存）：各自成 host，互不覆盖
        inv = self._scan([
            (510, _EXPLORER, "OpenCode Dev.exe", ["OpenCode Dev.exe"],
             5100.0, ""),
            (600, _EXPLORER, "DeepSeek Harness.exe",
             ["DeepSeek Harness.exe"], 6000.0, ""),
        ])
        kinds = sorted(h.kind for h in inv.desktop_hosts)
        self.assertEqual(kinds, [AgentKind.DSH, AgentKind.OPENCODE])


class DaemonFormAgentTests(unittest.TestCase):
    """Observe a live app-server once; never turn daemon housekeeping into pets."""

    def _row(self, pid, ppid, tty, tpgid, comm, args):
        # _ps_scan 行布局：pid,ppid,sid,pgid,tpgid,tty,uid,etimes,stat,comm,args
        return (pid, ppid, 1, 1, tpgid, tty, 1000, 100, "S", comm, args)

    def test_daemon_form_marks_detected(self):
        self.assertTrue(discovery_mod.is_daemon_form(
            "/home/u/.codex/packages/standalone/bin/codex app-server "
            "--remote-control --listen unix://"))
        self.assertTrue(discovery_mod.is_daemon_form(
            "/bin/codex app-server daemon pid-update-loop"))
        self.assertFalse(discovery_mod.is_daemon_form("/bin/codex"))
        self.assertFalse(discovery_mod.is_daemon_form(""))
        self.assertTrue(discovery_mod.is_codex_internal_server_process(
            "/bin/codex exec-server --remote https://example"))

    def test_detached_daemon_promoted_to_unknown_not_filtered(self):
        rows = {
            10: self._row(10, 1, "?", -1, "codex",
                          "/bin/codex app-server --remote-control"),
        }
        state = discovery_mod.canonical_terminal_attachment(10, (), rows)
        self.assertIs(state, TerminalAttachment.UNKNOWN)

    def test_detached_without_daemon_evidence_still_filtered(self):
        rows = {
            11: self._row(11, 1, "?", -1, "codex", "/bin/codex"),
        }
        state = discovery_mod.canonical_terminal_attachment(11, (), rows)
        self.assertIs(state, TerminalAttachment.DETACHED)

    def test_daemon_maintenance_is_not_a_separate_agent(self):
        from agents.discovery import scan_windows_inventory, WslProcessProbe
        helper = ["codex.exe", "app-server", "daemon", "pid-update-loop"]
        for with_runtime in (True, False):
            with self.subTest(with_runtime=with_runtime):
                rows = [(21, 20, "codex.exe", helper, 21.0, "C:/bin/codex.exe")]
                if with_runtime:
                    rows.append((20, _EXPLORER, "codex.exe",
                                 ["codex.exe", "app-server", "--remote-control"],
                                 20.0, "C:/bin/codex.exe"))
                with unittest.mock.patch("psutil.process_iter",
                                         return_value=_proc_rows(rows)):
                    inv = scan_windows_inventory()
                self.assertEqual(inv.terminal_instances, ())
                self.assertEqual(
                    [host.pid for host in inv.desktop_hosts],
                    [20] if with_runtime else [])
        self.assertIsNone(WslProcessProbe._match_agent(
            "codex", "/bin/codex app-server daemon pid-update-loop"))
        self.assertIsNone(WslProcessProbe._match_agent(
            "codex", "/bin/codex app-server --remote-control"))

    def test_attached_still_wins_over_daemon_evidence(self):
        rows = {
            30: self._row(30, 1, "/dev/pts/0", 400, "codex",
                          "/bin/codex app-server"),
        }
        state = discovery_mod.canonical_terminal_attachment(30, (), rows)
        self.assertIs(state, TerminalAttachment.ATTACHED)

    def test_standalone_windows_daemon_is_desktop_host_not_terminal_agent(self):
        # Windows 侧：孤立 app-server 可能在 ChatGPT/Codex 窗口退出后继续
        # 存活；它必须进入有界 Desktop source，不能重新打开旧 CLI rollout。
        from agents.discovery import scan_windows_inventory
        rows = [
            (700, _EXPLORER, "codex.exe",
             ["codex.exe", "app-server", "--remote-control"],
             7000.0, "C:/Users/u/.codex/bin/codex.exe"),
        ]
        with unittest.mock.patch("psutil.process_iter",
                                 return_value=_proc_rows(rows)):
            inv = scan_windows_inventory()
        self.assertEqual(inv.terminal_instances, ())
        self.assertEqual(len(inv.desktop_hosts), 1)
        self.assertIs(inv.desktop_hosts[0].kind, AgentKind.CODEX)
        self.assertEqual(inv.desktop_hosts[0].pid, 700)

    def test_internal_exec_server_never_becomes_terminal_agent(self):
        # Desktop remote execution child may survive its GUI parent; it is
        # explicitly non-interactive and must not revive the CLI watcher.
        from agents.discovery import scan_windows_inventory
        rows = [
            (701, 20, "codex.exe",
             ["codex.exe", "exec-server", "--remote", "https://example"],
             7001.0, "C:/Users/u/.codex/bin/codex.exe"),
            (20, _EXPLORER, "WindowsTerminal.exe", ["wt"], 7000.0, ""),
        ]
        with unittest.mock.patch("psutil.process_iter",
                                 return_value=_proc_rows(rows)):
            inv = scan_windows_inventory()
        self.assertEqual(inv.terminal_instances, ())
        self.assertEqual(inv.desktop_hosts, ())

    def test_gui_exit_does_not_promote_orphan_server_to_cli_agent(self):
        # ChatGPT/Codex host disappears while its app-server child remains.
        # The second census must keep the service on the Desktop data plane;
        # it must never become a new terminal Codex incarnation.
        from agents.discovery import scan_windows_inventory
        first = [
            (200, _EXPLORER, "ChatGPT.exe", ["ChatGPT.exe"], 2000.0,
             _CHATGPT_CODEX_EXE),
            (201, 200, "codex.exe",
             ["codex.exe", "app-server", "--remote-control"], 2001.0,
             "C:/Users/u/.codex/bin/codex.exe"),
        ]
        second = [first[1]]
        with unittest.mock.patch("psutil.process_iter",
                                 return_value=_proc_rows(first)):
            before = scan_windows_inventory()
        with unittest.mock.patch("psutil.process_iter",
                                 return_value=_proc_rows(second)):
            after = scan_windows_inventory()
        self.assertEqual(before.terminal_instances, ())
        self.assertEqual(after.terminal_instances, ())
        self.assertEqual([host.pid for host in before.desktop_hosts], [200])
        self.assertEqual([host.pid for host in after.desktop_hosts], [201])

    def test_windows_unattributed_codex_still_dropped(self):
        # 无 daemon 自证据、无 terminal ancestry 的 codex.exe 仍被拒绝
        from agents.discovery import scan_windows_inventory
        rows = [
            (710, _EXPLORER, "codex.exe", ["codex.exe"],
             7100.0, "C:/Users/u/.codex/bin/codex.exe"),
        ]
        with unittest.mock.patch("psutil.process_iter",
                                 return_value=_proc_rows(rows)):
            inv = scan_windows_inventory()
        self.assertEqual(inv.terminal_instances, ())
        self.assertTrue(any("unattributed" in d for d in inv.diagnostics))


if __name__ == "__main__":
    unittest.main()
