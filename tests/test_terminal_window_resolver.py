"""Terminal Window candidate selection 测试（v4.1.3 §25）。

把 Window 唤起候选语义从 UIA recognizer 测试中分离，验证 v3 决策拓扑
（§6.3）与 WindowIdentity 完整性（§5.3）：

  * native 唯一祖先 → CONFIRMED（不受 control 数量影响）；
  * WSL mutual-unique → HIGH；
  * ★ 正向证据不唯一 → AMBIGUOUS **仍携带 best control 的 HWND**
    （v3 关键行为，v4.1.3 发布阻断测试）；
  * 唯一 WT 窗口兜底 → NONE + window；
  * 多窗口/无窗口无证据 → NONE + window=None；
  * 输出与 Agent 输入顺序无关；
  * 身份读取失败绝不构造 process_created=0 的假生产 binding（所有
    identity 经 identity_for_hwnd 注入非零 process_created）。
"""
from __future__ import annotations
import unittest

from agents.models import (
    AgentInstance,
    AgentKind,
    WindowBindingConfidence,
    WindowIdentity,
)
from agents.terminal_resolver import TerminalWindowResolver, _V3WindowControlScorer
from agents.terminal_uia import WT_WINDOW_CLASS, ObservedTerminalControl

NOW = 1_000_000.0
CREATED = 1234.5   # 所有注入身份的非零 process_created


def control(hwnd: int, rid, title: str = "") -> ObservedTerminalControl:
    return ObservedTerminalControl(control_id=(hwnd, tuple(rid)), hwnd=hwnd,
                                   window_pid=hwnd + 100, title=title)


def _wsl(kind=AgentKind.CODEX, cwd="/w/x", user="", pid=1):
    return AgentInstance(kind=kind, pid=pid, source="wsl:Ubuntu",
                         process_token=str(pid), cwd=cwd, user=user)


def make_resolver(rows, ancestors=None, identity=None):
    """rows: [(hwnd, pid, title)]（class 自动 WT）。

    identity 缺省注入非零 process_created 的完整 WindowIdentity；
    传 identity=lambda h: None 可模拟身份读取失败（§5.3）。
    """
    full = [(h, p, t, WT_WINDOW_CLASS) for h, p, t in rows]
    by_hwnd = {int(row[0]): int(row[1]) for row in full}
    calls = {"enum": 0}

    def enum_windows():
        calls["enum"] += 1
        return list(full)

    if identity is None:
        def identity(hwnd):
            pid = by_hwnd.get(int(hwnd), int(hwnd) + 100)
            return WindowIdentity(hwnd=int(hwnd), pid=pid,
                                  process_created=CREATED,
                                  window_class=WT_WINDOW_CLASS)

    resolver = TerminalWindowResolver(enum_windows=enum_windows,
                                      ancestor_pids=ancestors,
                                      identity_for_hwnd=identity)
    resolver.enum_calls = calls
    return resolver


class NativeAncestorTests(unittest.TestCase):
    """§25-1/2/3：Windows native PID 祖先链。"""

    def test_unique_ancestor_confirmed_with_full_identity(self):
        resolver = make_resolver([(11, 50, "codex")],
                                 ancestors=lambda pid: {pid, 50})
        inst = AgentInstance(AgentKind.CODEX, 99, "windows", process_token="9")
        b = resolver.resolve([inst], {}, NOW)[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.CONFIRMED)
        self.assertEqual(b.hwnd, 11)
        self.assertEqual(b.reason, "windows-ancestor")
        # 候选 HWND 必须带完整 incarnation 身份（§2.1：v3 只选 HWND，
        # v4.1.3 补 WindowIdentity）
        self.assertEqual(b.window.pid, 50)
        self.assertEqual(b.window.process_created, CREATED)
        self.assertEqual(b.window.window_class, WT_WINDOW_CLASS)
        self.assertTrue(b.wakeable)

    def test_unique_ancestor_multi_controls_still_confirmed(self):
        """窗口唯一即 CONFIRMED，不要求唯一 control（exact-pane 残留已删）。"""
        resolver = make_resolver([(11, 50, "wt")],
                                 ancestors=lambda pid: {pid, 50})
        inst = AgentInstance(AgentKind.CODEX, 99, "windows", process_token="9")
        controls = {
            (11, (1,)): control(11, (1,), "codex"),
            (11, (2,)): control(11, (2,), "claude"),
        }
        b = resolver.resolve([inst], controls, NOW)[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.CONFIRMED)
        self.assertEqual(b.hwnd, 11)

    def test_multi_window_ancestor_ambiguous_no_window(self):
        resolver = make_resolver([(1, 10, "wt"), (2, 10, "wt")],
                                 ancestors=lambda pid: {10})
        inst = AgentInstance(AgentKind.CODEX, 99, "windows", process_token="9")
        b = resolver.resolve([inst], {}, NOW)[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.AMBIGUOUS)
        self.assertIsNone(b.window)
        self.assertFalse(b.wakeable)
        self.assertEqual(b.reason, "multi-window-ancestor")


class WslCandidateTests(unittest.TestCase):
    """§25-4/5/6：WSL 标题评分的 v3 候选选择。"""

    def test_mutual_unique_high(self):
        resolver = make_resolver([(11, 5, "u@box:~/DeskPet")])
        inst = _wsl(cwd="/home/u/DeskPet", user="u")
        controls = {
            (11, (1,)): control(11, (1,), "u@box:~/DeskPet"),
            (11, (2,)): control(11, (2,), "PowerShell"),
        }
        b = resolver.resolve([inst], controls, NOW)[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.HIGH)
        self.assertEqual(b.hwnd, 11)
        self.assertIn("cwd", b.reason)
        self.assertEqual(b.window.process_created, CREATED)

    def test_positive_non_unique_keeps_window_ambiguous(self):
        """★ v4.1.3 发布阻断（§25-5）：证据正向但不互相唯一时，
        AMBIGUOUS 仍携带 best control 的 HWND（v3 关键行为）；
        v4.1.2 曾把它改成 window=None → 用户无法唤起。"""
        resolver = make_resolver([(11, 5, "u@box:~/DeskPet"),
                                  (22, 6, "u@box:~/DeskPet")])
        inst = _wsl(cwd="/home/u/DeskPet", user="u")
        controls = {
            (11, (1,)): control(11, (1,), "u@box:~/DeskPet"),
            (22, (2,)): control(22, (2,), "u@box:~/DeskPet"),
        }
        b = resolver.resolve([inst], controls, NOW)[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.AMBIGUOUS)
        self.assertIsNotNone(b.window)          # ★ 不再 fail-closed 丢窗口
        self.assertEqual(b.hwnd, 11)            # 同分并列取确定序的第一个
        self.assertTrue(b.wakeable)
        self.assertEqual(b.reason, "best-positive-candidate")
        self.assertEqual(b.score, 3)

    def test_low_score_user_only_best_positive_ambiguous(self):
        """§25-6：只命中低分 user@（+1）也保留 best control 的窗口
        （v3 行为：confidence 不拦用户显式唤起）。"""
        resolver = make_resolver([(11, 5, "wt"), (22, 6, "wt")])
        inst = _wsl(cwd="/home/dev/proj/app", user="dev")
        controls = {(11, (1,)): control(11, (1,), "dev@box")}
        b = resolver.resolve([inst], controls, NOW)[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.AMBIGUOUS)
        self.assertEqual(b.hwnd, 11)
        self.assertEqual(b.score, 1)


class SoleWindowFallbackTests(unittest.TestCase):
    """§25-7/8/9：无正向证据时的窗口兜底。"""

    def test_no_evidence_sole_window_none_with_window(self):
        resolver = make_resolver([(11, 5, "shell")])
        inst = _wsl(cwd="/home/dev/proj/app", user="dev")
        # "PowerShell" 不含任何 kind/cwd/user/distro 证据（注意
        # "Ubuntu" 这类 profile 名会命中 distro 弱证据 +1）
        controls = {(11, (1,)): control(11, (1,), "PowerShell")}
        b = resolver.resolve([inst], controls, NOW)[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.NONE)
        self.assertEqual(b.hwnd, 11)
        self.assertTrue(b.wakeable)
        self.assertEqual(b.reason, "single-window-fallback")

    def test_no_evidence_multiple_windows_none_no_window(self):
        resolver = make_resolver([(11, 5, "a"), (22, 6, "b")])
        inst = _wsl(cwd="/home/dev/proj/app", user="dev")
        controls = {(11, (1,)): control(11, (1,), "PowerShell")}
        b = resolver.resolve([inst], controls, NOW)[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.NONE)
        self.assertIsNone(b.window)
        self.assertEqual(b.reason, "multiple-terminal-windows-no-evidence")

    def test_no_wt_window_none_no_window(self):
        resolver = make_resolver([])
        inst = _wsl()
        b = resolver.resolve([inst], {}, NOW)[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.NONE)
        self.assertIsNone(b.window)
        self.assertEqual(b.reason, "no-terminal-window")


class ScreenEvidenceTests(unittest.TestCase):
    """v4.1.4：标题停在 profile 名（"Ubuntu"）时，屏幕摘要区分窗口。

    实机回归（2026-09-09）：两个 WT 窗口各运行一个 Agent，标题全是
    "Ubuntu" → distro+1 并列 → 所有 Agent 坍缩到同一个窗口。屏幕上的
    Agent 标识/项目路径（仅内存摘要）让 mutual-unique 恢复区分。
    """

    def _setup(self):
        resolver = make_resolver([(68024, 12044, "Ubuntu"),
                                  (984948, 12044, "Ubuntu")])
        kimi = _wsl(kind=AgentKind.KIMI, cwd="/home/u/Edu_Agent",
                    user="u", pid=1)
        claude = _wsl(kind=AgentKind.CLAUDE, cwd="/home/u/LCR",
                      user="u", pid=2)
        controls = {
            (68024, (1,)): control(68024, (1,), "Ubuntu"),
            (984948, (1,)): control(984948, (1,), "Ubuntu"),
        }
        return resolver, kimi, claude, controls

    def test_without_screens_ties_collapse_to_same_window(self):
        """v4.1.3 基线（修复前行为）：无屏幕证据 → 全部 AMBIGUOUS 同窗口。"""
        resolver, kimi, claude, controls = self._setup()
        out = resolver.resolve([kimi, claude], controls, NOW)
        self.assertEqual(out[kimi.key].confidence,
                         WindowBindingConfidence.AMBIGUOUS)
        self.assertEqual(out[kimi.key].hwnd, out[claude.key].hwnd)
        self.assertEqual(out[kimi.key].score, 1)   # 只有 distro 弱证据

    def test_screens_split_agents_to_distinct_windows_high(self):
        resolver, kimi, claude, controls = self._setup()
        screens = {
            (68024, (1,)): "Kimi CLI · ~/Edu_Agent · u@box",
            (984948, (1,)): "Claude Code · ~/LCR · u@box",
        }
        out = resolver.resolve([kimi, claude], controls, NOW, screens=screens)
        self.assertEqual(out[kimi.key].confidence, WindowBindingConfidence.HIGH)
        self.assertEqual(out[kimi.key].hwnd, 68024)
        self.assertEqual(out[claude.key].confidence, WindowBindingConfidence.HIGH)
        self.assertEqual(out[claude.key].hwnd, 984948)
        self.assertGreaterEqual(out[kimi.key].score, 5)   # kind+cwd+user@

    def test_screen_text_never_enters_binding(self):
        """隐私合同：屏幕文本只产生分数/证据 token，不进 title/reason。"""
        resolver, kimi, claude, controls = self._setup()
        screens = {
            (68024, (1,)): "Kimi CLI · ~/Edu_Agent · SECRETTOKEN123",
            (984948, (1,)): "Claude Code · ~/LCR · u@box",
        }
        out = resolver.resolve([kimi, claude], controls, NOW, screens=screens)
        binding = out[kimi.key]
        self.assertNotIn("SECRETTOKEN123", binding.title)
        self.assertNotIn("SECRETTOKEN123", binding.reason)
        self.assertEqual(binding.title, "Ubuntu")   # 只携带 control 标题

    def test_partial_screens_still_best_positive(self):
        """只有部分 control 有屏幕摘要：不猜，退回 v3 AMBIGUOUS 候选。"""
        resolver, kimi, claude, controls = self._setup()
        screens = {(68024, (1,)): "Kimi CLI · ~/Edu_Agent · u@box"}
        out = resolver.resolve([kimi, claude], controls, NOW, screens=screens)
        # kimi: A=6/B=1 唯一；claude: A/B 都只有 user@ 并列 → AMBIGUOUS
        self.assertEqual(out[kimi.key].confidence, WindowBindingConfidence.HIGH)
        self.assertEqual(out[claude.key].confidence,
                         WindowBindingConfidence.AMBIGUOUS)


class NativeMultiWindowScoringTests(unittest.TestCase):
    """v4.1.4：native 多窗口祖先先走评分链（屏幕/标题证据可定位）。"""

    def test_native_multi_window_screen_evidence_high(self):
        resolver = make_resolver([(1, 10, "wt"), (2, 10, "wt")],
                                 ancestors=lambda pid: {10})
        inst = AgentInstance(AgentKind.CODEX, 99, "windows",
                             process_token="9", cwd="C:\\proj\\alpha")
        controls = {
            (1, (1,)): control(1, (1,), "Ubuntu"),
            (2, (1,)): control(2, (1,), "Ubuntu"),
        }
        screens = {(2, (1,)): "codex · C:\\proj\\alpha"}
        b = resolver.resolve([inst], controls, NOW, screens=screens)[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.HIGH)
        self.assertEqual(b.hwnd, 2)
        self.assertTrue(b.wakeable)

    def test_native_multi_window_ancestor_bounds_candidates(self):
        """祖先窗口集限定候选：窗口集外的 control 证据再强也不选。"""
        resolver = make_resolver([(1, 10, "wt"), (2, 10, "wt"),
                                  (3, 20, "wt")],
                                 ancestors=lambda pid: {10})
        inst = AgentInstance(AgentKind.CODEX, 99, "windows",
                             process_token="9", cwd="C:\\proj\\alpha")
        controls = {
            (1, (1,)): control(1, (1,), "PowerShell"),
            (2, (1,)): control(2, (1,), "PowerShell"),
            (3, (1,)): control(3, (1,), "codex alpha"),
        }
        b = resolver.resolve([inst], controls, NOW)[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.AMBIGUOUS)
        self.assertIsNone(b.window)              # 不猜窗口集外的强证据
        self.assertEqual(b.reason, "multi-window-ancestor")

    def test_native_multi_window_no_evidence_ambiguous_no_window(self):
        resolver = make_resolver([(1, 10, "wt"), (2, 10, "wt")],
                                 ancestors=lambda pid: {10})
        inst = AgentInstance(AgentKind.CODEX, 99, "windows", process_token="9")
        b = resolver.resolve([inst], {}, NOW)[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.AMBIGUOUS)
        self.assertIsNone(b.window)
        self.assertEqual(b.reason, "multi-window-ancestor")


class OrderIndependenceTests(unittest.TestCase):
    """§25-10：resolver 输出与 Agent 输入顺序无关。"""

    def test_output_independent_of_agent_order(self):
        rows = [(11, 5, "wt")]
        controls = {
            (11, (1,)): control(11, (1,), "codex u1@box:~/alpha"),
            (11, (2,)): control(11, (2,), "claude u2@box:~/beta"),
        }
        codex = _wsl(kind=AgentKind.CODEX, cwd="/w/alpha", user="u1", pid=1)
        claude = _wsl(kind=AgentKind.CLAUDE, cwd="/w/beta", user="u2", pid=2)
        r1 = make_resolver(rows).resolve([codex, claude], controls, NOW)
        r2 = make_resolver(rows).resolve([claude, codex], controls, NOW)
        self.assertEqual(
            {k: (v.confidence, v.hwnd, v.reason) for k, v in r1.items()},
            {k: (v.confidence, v.hwnd, v.reason) for k, v in r2.items()})
        self.assertEqual(r1[codex.key].hwnd, 11)
        self.assertEqual(r1[codex.key].confidence, WindowBindingConfidence.HIGH)
        self.assertEqual(r1[claude.key].confidence, WindowBindingConfidence.HIGH)


class WindowIdentityIntegrityTests(unittest.TestCase):
    """§5.3：候选 HWND 身份失败 → 不构造假身份、不携带窗口。"""

    def test_identity_failure_keeps_confidence_drops_window(self):
        resolver = make_resolver([(11, 50, "codex")],
                                 ancestors=lambda pid: {pid, 50},
                                 identity=lambda hwnd: None)
        inst = AgentInstance(AgentKind.CODEX, 99, "windows", process_token="9")
        b = resolver.resolve([inst], {}, NOW)[inst.key]
        self.assertEqual(b.confidence, WindowBindingConfidence.CONFIRMED)
        self.assertIsNone(b.window)
        self.assertFalse(b.wakeable)
        self.assertIn("window-identity-failed", b.reason)
        self.assertIn("windows-ancestor", b.reason)


class WindowCatalogCacheTests(unittest.TestCase):
    """§5.2：3 秒窗口目录缓存；invalidate 后强制重新枚举。"""

    def test_cache_and_invalidate(self):
        resolver = make_resolver([(11, 5, "wt")])
        inst = _wsl()
        resolver.resolve([inst], {}, NOW)
        first = resolver.enum_calls["enum"]
        self.assertEqual(first, 1)
        resolver.resolve([inst], {}, NOW + 1.0)   # 3s 内：不再枚举
        self.assertEqual(resolver.enum_calls["enum"], 1)
        resolver.invalidate_window_cache()
        resolver.resolve([inst], {}, NOW + 2.0)   # 失效后：强制枚举
        self.assertEqual(resolver.enum_calls["enum"], 2)


class ScoringRegressionTests(unittest.TestCase):
    """v4.1.2 评分安全修正回归（保留，不改变 v3 决策拓扑，§4.2）。

    * 词边界：kind "pi" 不得命中 "pip"；"codex" 命中 "codex · task"；
    * ~/路径标记：`user@host: ~/a/b` 按 cwd 归一化比对，不用裸 basename
      （用户名==家目录名时会造成所有同用户 control 假命中）；
    * "Ubuntu" 这类无路径标题不产生假 HIGH（fail-closed）。
    """

    def _inst(self, kind=AgentKind.CODEX, cwd="/w/x", user=""):
        return AgentInstance(kind=kind, pid=1, source="wsl:Ubuntu",
                             process_token="9", cwd=cwd, user=user)

    def test_pi_word_boundary_not_pip(self):
        scorer = _V3WindowControlScorer()
        score, reason = scorer.score_title(self._inst(kind=AgentKind.PI),
                                           "pip install requests")
        self.assertEqual(score, 0)
        self.assertNotIn("kind", reason)

    def test_kind_word_boundary_matches(self):
        scorer = _V3WindowControlScorer()
        score, _ = scorer.score_title(self._inst(kind=AgentKind.CODEX),
                                      "codex · 编码中")
        self.assertGreaterEqual(score, 3)

    def test_tilde_path_marker_scores_cwd(self):
        scorer = _V3WindowControlScorer()
        inst = self._inst(cwd="/home/dev/proj/app", user="dev")
        score, reason = scorer.score_title(inst, "dev@box: ~/proj/app")
        self.assertIn("cwd", reason)
        self.assertIn("user@", reason)

    def test_home_dir_basename_does_not_match_user_host(self):
        """Agent 在家目录：不得因 basename==用户名 命中所有 user@host 标题。"""
        scorer = _V3WindowControlScorer()
        inst = self._inst(cwd="/home/dev", user="dev")
        score, reason = scorer.score_title(inst, "dev@box: ~/proj/app")
        # 只有 user@ 弱证据，绝不能有 cwd
        self.assertNotIn("cwd", reason)
        self.assertLess(score, 3)

    def test_generic_profile_title_not_high(self):
        """两个 WT 窗口 + generic profile 标题（"Ubuntu"）→ 不产生假 HIGH；
        distro 弱证据（+1）只够 AMBIGUOUS 候选（v3 语义，可唤起）。"""
        resolver = make_resolver([(11, 5, "a"), (22, 6, "b")])
        inst = self._inst(cwd="/home/dev/proj/app", user="dev")
        controls = {(11, (1,)): control(11, (1,), "Ubuntu")}
        b = resolver.resolve([inst], controls, NOW)[inst.key]
        self.assertNotEqual(b.confidence, WindowBindingConfidence.HIGH)
        self.assertEqual(b.confidence, WindowBindingConfidence.AMBIGUOUS)
        self.assertEqual(b.score, 1)   # 只有 distro 弱证据
        self.assertTrue(b.wakeable)

    def test_two_distinct_paths_both_high(self):
        """两个不同项目的 control + 两个对应 Agent → 双向唯一都 HIGH。"""
        resolver = make_resolver([(11, 5, "wt")])
        a = self._inst(kind=AgentKind.CODEX, cwd="/home/dev/alpha", user="dev")
        b = self._inst(kind=AgentKind.CLAUDE, cwd="/home/dev/beta", user="dev")
        controls = {
            (11, (1,)): control(11, (1,), "dev@box: ~/alpha"),
            (11, (2,)): control(11, (2,), "dev@box: ~/beta"),
        }
        bindings = resolver.resolve([a, b], controls, NOW)
        self.assertEqual(bindings[a.key].confidence, WindowBindingConfidence.HIGH)
        self.assertEqual(bindings[b.key].confidence, WindowBindingConfidence.HIGH)

    def test_close_competition_with_margin_still_high(self):
        """两个 control 同 kind 同 user、各自 cwd 差异化 → margin 足够仍 HIGH。"""
        resolver = make_resolver([(11, 5, "wt")])
        a = self._inst(kind=AgentKind.CODEX, cwd="/w/alpha", user="u")
        b = AgentInstance(AgentKind.CODEX, 2, "wsl:Ubuntu",
                          process_token="10", cwd="/w/beta", user="u")
        controls = {
            (11, (1,)): control(11, (1,), "codex alpha u@box"),
            (11, (2,)): control(11, (2,), "codex beta u@box"),
        }
        bindings = resolver.resolve([a, b], controls, NOW)
        # A: X=6, Y=4；B: X=4, Y=6 → 双向唯一，margin=2 → 都 HIGH
        self.assertEqual(bindings[a.key].confidence, WindowBindingConfidence.HIGH)
        self.assertEqual(bindings[b.key].confidence, WindowBindingConfidence.HIGH)


if __name__ == "__main__":
    unittest.main()
