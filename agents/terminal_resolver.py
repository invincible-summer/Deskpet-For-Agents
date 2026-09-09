"""终端解析器（v4.1.3 §4-§7）：v3 候选选择 + v4.1.2 严格观察。

  * TerminalWindowResolver —— 恢复 v3 的"只要被动证据能给出一个候选
    HWND，用户就可以显式唤起"语义：Windows native 用 PID ancestor；
    WSL 用被动观察到的 TermControl 标题按 v3 权重评分；mutual-unique
    失败但存在正向 best control 时仍保留其 HWND（AMBIGUOUS）；桌面只
    有一个 WT 顶层窗口时保留该 HWND（NONE）。confidence 不是
    activation gate，wakeability 只由 binding.window 决定。
  * TerminalObservationResolver —— 保持 v4.1.2 严格链：只有 CONFIRMED
    （祖先唯一窗口 + 该窗口唯一被观察 control）和 HIGH（标题证据互相
    唯一）才生成 binding；低置信 Window 候选可供用户显式唤起，但绝不
    自动把 Terminal 文本归给 Agent。

两条链的评分器分开（§4.3）：Window 唤起评分只用 TermControl 自身
标题（v3 拓扑）；Observation 评分保留 v4.1.2 的窗口标题第二证据。
"""
from __future__ import annotations

import re
import time

from .matching import best_effort_scores, mutual_unique_matches
from .models import (
    AgentKind,
    AgentInstance,
    ObservationBindingConfidence,
    TerminalObservationBinding,
    TerminalWindowBinding,
    WindowBindingConfidence,
    WindowIdentity,
)
from .terminal_uia import WT_WINDOW_CLASS, ObservedTerminalControl

# 标题里的 shell 路径标记：`user@host: ~/a/b/c`（大小写不敏感）
_TITLE_PATH_RE = re.compile(r"~/[^\s:·,|]*", re.IGNORECASE)
_KIND_WORD_RE = {
    kind.value: re.compile(rf"\b{re.escape(kind.value)}\b", re.IGNORECASE)
    for kind in AgentKind
}

# v3 轻量窗口目录缓存（§5.2）：不给 Monitor 主循环加成本，
# stale activation 时只 force 一次。
WINDOW_CACHE_SEC = 3.0


def _squash(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


def _cwd_tilde(cwd: str, user: str) -> str:
    """WSL cwd → `~/a/b` 形态（无法归一化时返回空）。"""
    cwd = str(cwd or "").replace("\\", "/").rstrip("/").lower()
    if not cwd or not user:
        return ""
    home = f"/home/{user.lower().strip()}"
    if cwd == home:
        return "~"
    if cwd.startswith(home + "/"):
        return "~/" + cwd[len(home) + 1:]
    return ""


class _BaseTerminalTitleScorer:
    """Agent ↔ 终端标题的 v3 权重评分（kind+3 / cwd+2 / user@+1 / distro+1）。

    保留 v4.1.2 已完成的纯安全修正（不改变 v3 决策拓扑）：词边界 kind
    （防 "pi" 命中 "pip"）、~/ 路径标记归一化、user@host 假命中防护、
    distro 词边界。
    """

    HIGH_MIN_SCORE = 3
    HIGH_MIN_MARGIN = 1

    def score_title(self, inst: AgentInstance, title: str) -> tuple[int, str]:
        """评分与依据（证据可核验、误报率低）。

        * kind：词边界匹配（防 "pi" 命中 "pip"）+3；
        * cwd：标题含 `~/a/b` 路径标记时按路径比对（相等或互为前缀）+2；
          无标记时才退回 basename 出现 +2（basename 与用户名相同则不计，
          否则 "user@host" 形态的每个标题都会假命中 home 目录）；
        * user@：标题以 `<user>@` 开头或包含 +1；
        * distro：词边界匹配 +1。
        """
        title = _squash(title)
        if not title:
            return 0, ""
        score = 0
        parts: list[str] = []
        kind = getattr(inst, "kind", None)
        comm = kind.value if kind is not None else ""
        if comm:
            word_re = _KIND_WORD_RE.get(comm)
            if word_re is not None and word_re.search(title):
                score += 3
                parts.append("kind")
        user = str(getattr(inst, "user", "") or "").strip()
        cwd = str(getattr(inst, "cwd", "") or "")
        tilde = _cwd_tilde(cwd, user)
        title_match = _TITLE_PATH_RE.search(title)
        home_only = tilde == "~" and re.search(r"(?:^|\s)~(?:\s|$)", title)
        if title_match is not None:
            title_path = title_match.group(0).rstrip("/").lower()
            if tilde and tilde != "~" and (
                    title_path == tilde
                    or title_path.startswith(tilde + "/")
                    or tilde.startswith(title_path + "/")):
                score += 2
                parts.append("cwd")
        elif home_only:
            score += 2
            parts.append("cwd")
        elif cwd:
            base = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
            if (base and base != user.lower()
                    and re.search(rf"(?<!\w){re.escape(base)}(?!\w)", title)):
                score += 2
                parts.append("cwd")
        if user:
            low = title.lower()
            if low.startswith(user.lower() + "@") or (user.lower() + "@") in low:
                score += 1
                parts.append("user@")
        distro = str(getattr(inst, "distro", "") or "").lower()
        if distro and re.search(rf"(?<!\w){re.escape(distro)}(?!\w)", title):
            score += 1
            parts.append("distro")
        return score, "+".join(parts)


class _V3WindowControlScorer(_BaseTerminalTitleScorer):
    """Window 唤起链的 control 评分（§4.3）：只用 TermControl 自身标题。

    WT 顶层窗口标题是窗口级共享证据，不进入 Window wake 评分——
    这是 v3 的决策拓扑，v4.1.3 不再增强。
    """

    def score_control(self, inst: AgentInstance,
                      control: ObservedTerminalControl) -> int:
        return self.score_title(inst, control.title)[0]

    def reason_for(self, inst: AgentInstance,
                   control: ObservedTerminalControl) -> str:
        return self.score_title(inst, control.title)[1] or "no-evidence"


class _ObservationScorer(_BaseTerminalTitleScorer):
    """观察链评分（保持 v4.1.2）：窗口标题作为第二条证据。

    TermControl 的 UIA Name 有时停留在 profile 名（如 "Ubuntu"），
    而 WT 顶层窗口标题跟随当前活动 tab 的 shell 标题。但窗口标题是
    窗口级共享证据——同一窗口有多个被观察 control（split pane）时
    无法安全归属，只有唯一 control 时才作为第二证据（互斥唯一门槛
    不变）。
    """

    def titles_for(self, control: ObservedTerminalControl,
                   window_titles: dict[int, str],
                   controls_per_hwnd: dict[int, int] | None = None) -> list[str]:
        titles = []
        if control.title:
            titles.append(control.title)
        same_window = (controls_per_hwnd.get(control.hwnd, 1)
                       if controls_per_hwnd else 1)
        win_title = window_titles.get(control.hwnd, "")
        if win_title and same_window <= 1 and win_title not in titles:
            titles.append(win_title)
        return titles

    def score_control(self, inst, control, window_titles,
                      controls_per_hwnd: dict[int, int] | None = None) -> int:
        best = 0
        for title in self.titles_for(control, window_titles,
                                     controls_per_hwnd):
            best = max(best, self.score_title(inst, title)[0])
        return best

    def reason(self, inst, control, window_titles,
               controls_per_hwnd: dict[int, int] | None = None) -> str:
        best = ""
        for title in self.titles_for(control, window_titles,
                                     controls_per_hwnd):
            reason = self.score_title(inst, title)[1]
            if reason and len(reason) > len(best):
                best = reason
        return best or "no-evidence"


def _enum_wt_rows(enum_windows) -> list:
    """enum_windows() → WT 顶层窗口行 [(hwnd, pid, title, class)]。"""
    try:
        if enum_windows is not None:
            rows = enum_windows()
        else:
            from actions import winkeys
            rows = winkeys.enum_windows()
    except Exception:
        rows = []
    return [r for r in rows if str(r[3]) == WT_WINDOW_CLASS]


class _WindowCatalog:
    """Windows Terminal 顶层窗口目录（§5.1：独立于 UIA layout）。

    窗口列表来自 winkeys.enum_windows()，身份来自
    winkeys.window_identity(hwnd)（测试经 identity_for_hwnd 注入非零
    process_created）。身份读取失败绝不构造假身份（§5.3）。
    """

    def __init__(self, enum_windows=None, identity_for_hwnd=None):
        self._enum_windows = enum_windows
        self._identity_for_hwnd = identity_for_hwnd
        self._rows: list = []
        self._titles: dict[int, str] = {}
        self._identities: dict[int, WindowIdentity] = {}
        self._ts = 0.0
        self.refresh(force=True)

    def refresh(self, force: bool = False):
        now = time.time()
        if not force and now - self._ts < WINDOW_CACHE_SEC:
            return
        self._ts = now
        self._rows = _enum_wt_rows(self._enum_windows)
        self._titles = {int(r[0]): str(r[2]) for r in self._rows if r[2]}
        self._identities = {}

    def invalidate(self):
        """下一次 refresh 强制重新枚举（stale activation 前调用）。"""
        self._ts = 0.0
        self._identities = {}

    def rows(self) -> list:
        return self._rows

    def hwnds(self) -> list[int]:
        return [int(r[0]) for r in self._rows]

    def title(self, hwnd: int) -> str:
        return self._titles.get(int(hwnd), "")

    def identity(self, hwnd: int) -> WindowIdentity | None:
        hwnd = int(hwnd)
        if hwnd in self._identities:
            return self._identities[hwnd]
        identity = None
        try:
            if self._identity_for_hwnd is not None:
                identity = self._identity_for_hwnd(hwnd)
            else:
                from actions import winkeys
                identity = winkeys.window_identity(hwnd)
        except Exception:
            identity = None
        if identity is not None:
            self._identities[hwnd] = identity
        return identity


class _WindowTitleIndex:
    """观察链的 WT 窗口标题缓存（3 秒；enum 兜底）。"""

    def __init__(self, enum_windows=None):
        self._enum_windows = enum_windows
        self.titles: dict[int, str] = {}
        self._ts = 0.0

    def refresh(self):
        now = time.time()
        if self.titles and now - self._ts < WINDOW_CACHE_SEC:
            return
        self._ts = now
        rows = _enum_wt_rows(self._enum_windows)
        self.titles = {int(r[0]): str(r[2]) for r in rows if r[2]}


class TerminalWindowResolver:
    """Agent → Windows Terminal 顶层窗口候选（v4.1.3 §6）。

    v3 决策顺序（§6.3）：
      1. native PID ancestor 唯一窗口 → CONFIRMED（不受 control 数量
         影响）；>1 窗口 → AMBIGUOUS + window=None
      2. Agent ↔ control mutual-unique（min_score=3, margin=1）
         → HIGH + control.hwnd
      3. mutual-unique 失败但存在正向 best control → AMBIGUOUS +
         best control.hwnd（★ v3 关键行为）
      4. 无正向证据 + 唯一 WT 窗口 → NONE + 该窗口（single-window-
         fallback）
      5. 多窗口无证据 / 无 WT 窗口 → NONE + window=None
    """

    def __init__(self, enum_windows=None, ancestor_pids=None,
                 identity_for_hwnd=None):
        self._enum_windows = enum_windows
        self._ancestor_pids = ancestor_pids
        self._identity_for_hwnd = identity_for_hwnd
        self._scorer = _V3WindowControlScorer()
        self._catalog = _WindowCatalog(enum_windows, identity_for_hwnd)

    # ---- 注入点（测试用） ----
    def _ancestors(self, pid: int) -> set[int]:
        if self._ancestor_pids is not None:
            try:
                return self._ancestor_pids(pid)
            except Exception:
                return {pid}
        try:
            from actions import winkeys
            return winkeys._ancestor_pids(pid)
        except Exception:
            return {pid}

    def invalidate_window_cache(self):
        """stale activation 路径：强制下一次 resolve 重新枚举（§8.3）。"""
        self._catalog.invalidate()

    def resolve(self, instances: list, controls: dict,
                now: float) -> dict[str, TerminalWindowBinding]:
        self._catalog.refresh()
        rows = self._catalog.rows()
        hwnds = self._catalog.hwnds()

        out: dict[str, TerminalWindowBinding] = {}
        scored_instances: list = []

        for inst in instances:
            key = inst.key
            source = str(getattr(inst, "source", ""))
            if source == "windows" and inst.pid:
                parents = self._ancestors(inst.pid)
                matches = list(dict.fromkeys(
                    int(r[0]) for r in rows if int(r[1]) in parents))
                if len(matches) == 1:
                    # 唯一窗口 → CONFIRMED；不要求窗口下只有一个 control
                    # （window-only 激活不再受 exact-pane 语义限制）
                    out[key] = self._binding_from_hwnd(
                        matches[0], now,
                        WindowBindingConfidence.CONFIRMED,
                        "windows-ancestor")
                    continue
                if len(matches) > 1:
                    out[key] = TerminalWindowBinding(
                        confidence=WindowBindingConfidence.AMBIGUOUS,
                        last_seen=now, reason="multi-window-ancestor")
                    continue
                # 祖先链找不到窗口（非 WT 宿主或已退出）→ 落到 v3 打分路径
            scored_instances.append(inst)

        # ---- v3 评分：Agent ↔ ObservedTerminalControl（只用 control 标题）
        inst_by_key = {inst.key: inst for inst in scored_instances}
        control_ids = list(controls.keys())

        def score_fn(agent_key: str, control_id: tuple) -> int:
            return self._scorer.score_control(
                inst_by_key[agent_key], controls[control_id])

        decisions = mutual_unique_matches(
            [inst.key for inst in scored_instances], control_ids, score_fn,
            min_score=self._scorer.HIGH_MIN_SCORE,
            min_margin=self._scorer.HIGH_MIN_MARGIN)
        diagnostics = best_effort_scores(
            [inst.key for inst in scored_instances], control_ids, score_fn)

        for inst in scored_instances:
            key = inst.key
            dec = decisions.get(key)
            best_control, best_score, runner_up = diagnostics.get(
                key, (None, 0, 0))
            if dec is not None:
                control = controls[dec.right]
                out[key] = self._binding_from_hwnd(
                    control.hwnd, now, WindowBindingConfidence.HIGH,
                    self._scorer.reason_for(inst, control),
                    score=dec.score, runner_up=runner_up,
                    title=control.title)
                continue
            if best_control is not None and best_score > 0:
                # v3 关键行为（§6.3-2）：正向证据不够唯一时，仍把 best
                # control 所属窗口留作用户显式唤起候选（AMBIGUOUS）。
                # confidence 只限制 observation attribution，不拦唤起。
                control = controls[best_control]
                out[key] = self._binding_from_hwnd(
                    control.hwnd, now, WindowBindingConfidence.AMBIGUOUS,
                    "best-positive-candidate",
                    score=best_score, runner_up=runner_up,
                    title=control.title)
                continue
            if len(hwnds) == 1:
                # 唯一 WT 顶层窗口兜底（§6.3-3）：可唤起，不归属证据
                out[key] = self._binding_from_hwnd(
                    hwnds[0], now, WindowBindingConfidence.NONE,
                    "single-window-fallback")
                continue
            if hwnds:
                out[key] = TerminalWindowBinding(
                    confidence=WindowBindingConfidence.NONE, last_seen=now,
                    reason="multiple-terminal-windows-no-evidence")
            else:
                out[key] = TerminalWindowBinding(
                    confidence=WindowBindingConfidence.NONE, last_seen=now,
                    reason="no-terminal-window")
        return out

    def _binding_from_hwnd(self, hwnd: int, now: float,
                           confidence: WindowBindingConfidence, reason: str,
                           score: int = 0, runner_up: int = 0,
                           title: str | None = None) -> TerminalWindowBinding:
        """所有 confidence 共用的候选构造（§6.4）：候选 HWND 必须有完整
        WindowIdentity；身份失败 → window=None + reason 后缀，用户
        action 不会触碰该 HWND。"""
        text = str(title if title is not None
                   else self._catalog.title(hwnd) or "")[:80]
        identity = self._catalog.identity(hwnd)
        if identity is None:
            return TerminalWindowBinding(
                window=None, title=text, confidence=confidence,
                last_seen=now, reason=reason + "·window-identity-failed",
                score=score, runner_up_score=runner_up)
        return TerminalWindowBinding(
            window=identity, title=text, confidence=confidence,
            last_seen=now, validated_at=now, reason=reason,
            score=score, runner_up_score=runner_up)


class TerminalObservationResolver:
    """Agent ↔ 被观察 TermControl 的安全归属（v4.1.3 §7，保持 v4.1.2）。

    只产出 CONFIRMED/HIGH：
      * CONFIRMED —— Windows native 祖先链唯一定位窗口，且该窗口当前
        恰好只有一个被观察 TermControl；
      * HIGH —— 标题证据互相唯一匹配。
    其余情况（多 control 无证据 / 低置信 Window 候选 / AMBIGUOUS）
    一律不生成 binding——宁可没有 terminal evidence，也不能错归。
    低置信 Window binding（AMBIGUOUS/NONE+窗口）永远不直接授予
    observation attribution；但本 resolver 依据自己独立的严格 control
    证据得出 HIGH 是允许的。
    """

    def __init__(self, enum_windows=None, ancestor_pids=None):
        self._enum_windows = enum_windows
        self._ancestor_pids = ancestor_pids
        self._scorer = _ObservationScorer()
        self._titles = _WindowTitleIndex(enum_windows)

    def resolve(self, instances: list, controls: dict,
                window_bindings: dict, now: float,
                layout=None) -> dict[str, TerminalObservationBinding]:
        self._titles.refresh()
        titles = self._titles.titles
        out: dict[str, TerminalObservationBinding] = {}
        remaining: list = []

        # 1) CONFIRMED：唯一祖先窗口 + 该窗口唯一被观察 control
        for inst in instances:
            binding = window_bindings.get(inst.key)
            if (binding is not None and binding.window is not None
                    and binding.confidence is WindowBindingConfidence.CONFIRMED):
                in_window = [c for c in controls.values()
                             if c.hwnd == binding.hwnd]
                if len(in_window) == 1:
                    out[inst.key] = TerminalObservationBinding(
                        agent_key=inst.key,
                        control_id=in_window[0].control_id,
                        confidence=ObservationBindingConfidence.CONFIRMED,
                        reason="windows-ancestor+sole-control")
                    continue
            remaining.append(inst)

        # 2) HIGH：标题证据互相唯一（顺序无关；窗口标题仅作第二证据）
        inst_by_key = {inst.key: inst for inst in remaining}
        control_ids = list(controls.keys())
        controls_per_hwnd: dict[int, int] = {}
        for c in controls.values():
            controls_per_hwnd[c.hwnd] = controls_per_hwnd.get(c.hwnd, 0) + 1

        def score_fn(agent_key: str, control_id: tuple) -> int:
            return self._scorer.score_control(
                inst_by_key[agent_key], controls[control_id], titles,
                controls_per_hwnd)

        decisions = mutual_unique_matches(
            [inst.key for inst in remaining], control_ids, score_fn,
            min_score=self._scorer.HIGH_MIN_SCORE,
            min_margin=self._scorer.HIGH_MIN_MARGIN)
        for inst in remaining:
            dec = decisions.get(inst.key)
            if dec is not None:
                control = controls[dec.right]
                out[inst.key] = TerminalObservationBinding(
                    agent_key=inst.key,
                    control_id=control.control_id,
                    confidence=ObservationBindingConfidence.HIGH,
                    reason=self._scorer.reason(
                        inst, control, titles, controls_per_hwnd))
        return out
