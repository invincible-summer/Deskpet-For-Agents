"""终端解析器（v4.1.1 plan §5）：window-only 与 observation-only 彻底分离。

  * TerminalWindowResolver —— 只回答"Agent 大概在哪个 Windows Terminal
    顶层窗口"（用户点击时恢复并前置这个窗口，产品不承诺 Tab/Pane）；
  * TerminalObservationResolver —— 只回答"哪个被观察 TermControl 的
    WAITING/activity 证据可以安全归给哪个 Agent"。

两条链互不污染：FALLBACK 窗口绑定允许唤起但不赋予 observation
attribution；observation 的 control_id 只是 UIA 内部短生命周期句柄，
不进公开 window binding（plan §4.4）。

Windows 原生 Agent：PID 祖先链 → 唯一窗口 → CONFIRMED（不要求窗口下
只有一个 control；旧逻辑的"窗口唯一但多 pane = AMBIGUOUS"是 exact-pane
产品语义的残留，已删除）。

WSL Agent：无法通过 Linux PID 直接证明 Windows HWND，继续只读保守评分：
kind token / normalized ~/path / user@ / distro / control 标题 / 顶层
窗口标题；互相唯一匹配（min_score=3、双侧分差 >=1）→ HIGH。
证据不足 → AMBIGUOUS（多窗口）或 FALLBACK（唯一窗口兜底）——fail-closed。
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


def _squash(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


class _EvidenceScorer:
    """Agent ↔ TermControl 的标题证据评分（WSL 主路径，v4.1.1 复用 V4.1.1
    评分修正：词边界 kind、~/ 路径标记归一化、user@host 假命中防护）。"""

    HIGH_MIN_SCORE = 3
    HIGH_MIN_MARGIN = 1

    def score(self, inst: AgentInstance, title: str) -> tuple[int, str]:
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

    def titles_for(self, control: ObservedTerminalControl,
                   window_titles: dict[int, str],
                   controls_per_hwnd: dict[int, int] | None = None) -> list[str]:
        """control 的证据标题集合：TermControl Name + 顶层窗口标题。

        TermControl 的 UIA Name 有时停留在 profile 名（如 "Ubuntu"），
        而 WT 顶层窗口标题跟随当前活动 tab 的 shell 标题
        （"user@host: ~/path"）。但窗口标题是窗口级共享证据——同一
        窗口有多个被观察 control（split pane）时无法安全归属给其中
        任何一个，只有唯一 control 时才作为第二条证据（互斥唯一门槛
        不变）。
        """
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
            best = max(best, self.score(inst, title)[0])
        return best

    def reason(self, inst, control, window_titles,
               controls_per_hwnd: dict[int, int] | None = None) -> str:
        best = ""
        for title in self.titles_for(control, window_titles,
                                     controls_per_hwnd):
            reason = self.score(inst, title)[1]
            if reason and len(reason) > len(best):
                best = reason
        return best or "no-evidence"


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


class _WindowIndex:
    """窗口身份/标题的 resolver 侧缓存（UIA layout 优先，enum 兜底）。"""

    def __init__(self, enum_windows=None, layout=None):
        self._enum_windows = enum_windows
        self.windows: dict[int, WindowIdentity] = {}
        self.titles: dict[int, str] = {}
        self._enum_rows: list = []
        self._enum_ts = 0.0
        if layout is not None:
            self.windows = dict(layout.windows)
        self._refresh_enum(force=True)

    def _refresh_enum(self, force: bool = False):
        now = time.time()
        if not force and now - self._enum_ts < 3.0:
            return
        self._enum_ts = now
        try:
            if self._enum_windows is not None:
                rows = self._enum_windows()
            else:
                from actions import winkeys
                rows = winkeys.enum_windows()
        except Exception:
            rows = []
        self._enum_rows = [r for r in rows
                           if str(r[3]) == WT_WINDOW_CLASS]

    def ensure_identities(self):
        """给没有完整 incarnation 身份的 WT 窗口补 psutil create_time。

        enum_windows 只提供 (hwnd, pid, title, class)；缺少 create_time
        的 WindowIdentity 无法防 PID 复用，激活前必须补全（fail-closed）。
        """
        for row in self._enum_rows:
            hwnd, pid, title, cls = int(row[0]), row[1], row[2], row[3]
            self.titles.setdefault(hwnd, str(title))
            if hwnd in self.windows:
                continue
            identity = None
            try:
                if self._enum_windows is None:
                    from actions import winkeys
                    identity = winkeys.window_identity(hwnd)
                else:
                    identity = WindowIdentity(
                        hwnd=hwnd, pid=int(pid), process_created=0.0,
                        window_class=str(cls))
            except Exception:
                identity = None
            if identity is not None:
                self.windows[hwnd] = identity

    def hwnds(self) -> set[int]:
        return set(self.windows) | {int(r[0]) for r in self._enum_rows}


class TerminalWindowResolver:
    """Agent → Windows Terminal 顶层窗口的置信度解析（plan §5）。"""

    def __init__(self, enum_windows=None, ancestor_pids=None):
        self._enum_windows = enum_windows
        self._ancestor_pids = ancestor_pids
        self._scorer = _EvidenceScorer()

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

    def resolve(self, instances: list, controls: dict, now: float,
                layout=None) -> dict[str, TerminalWindowBinding]:
        index = _WindowIndex(self._enum_windows, layout)
        index.ensure_identities()
        windows = index.windows
        wins = [(hwnd, ident.pid) for hwnd, ident in windows.items()]

        out: dict[str, TerminalWindowBinding] = {}
        scored_instances: list = []

        for inst in instances:
            key = inst.key
            source = str(getattr(inst, "source", ""))
            if source == "windows" and inst.pid:
                parents = self._ancestors(inst.pid)
                matches = list(dict.fromkeys(
                    int(hwnd) for hwnd, wpid in wins if int(wpid) in parents))
                if len(matches) == 1:
                    # 唯一窗口 → CONFIRMED；不要求窗口下只有一个 control
                    # （window-only 激活不再受 exact-pane 语义限制）
                    hwnd = matches[0]
                    out[key] = self._binding(
                        hwnd, windows, index.titles.get(hwnd, ""), now,
                        WindowBindingConfidence.CONFIRMED,
                        "windows-ancestor")
                    continue
                if len(matches) > 1:
                    out[key] = TerminalWindowBinding(
                        confidence=WindowBindingConfidence.AMBIGUOUS,
                        last_seen=now, reason="multi-window-ancestor")
                    continue
                # 祖先链找不到窗口（非 WT 宿主或已退出）→ 落到打分路径再试
            scored_instances.append(inst)

        # 互相唯一评分分配（结果与实例顺序无关）
        inst_by_key = {inst.key: inst for inst in scored_instances}
        control_ids = list(controls.keys())
        controls_per_hwnd: dict[int, int] = {}
        for c in controls.values():
            controls_per_hwnd[c.hwnd] = controls_per_hwnd.get(c.hwnd, 0) + 1

        def score_fn(agent_key: str, control_id: tuple) -> int:
            return self._scorer.score_control(
                inst_by_key[agent_key], controls[control_id], index.titles,
                controls_per_hwnd)

        decisions = mutual_unique_matches(
            [inst.key for inst in scored_instances], control_ids, score_fn,
            min_score=self._scorer.HIGH_MIN_SCORE,
            min_margin=self._scorer.HIGH_MIN_MARGIN)
        diagnostics = best_effort_scores(
            [inst.key for inst in scored_instances], control_ids, score_fn)

        wt_hwnds = index.hwnds()
        for inst in scored_instances:
            key = inst.key
            dec = decisions.get(key)
            best_control, best_score, runner_up = diagnostics.get(
                key, (None, 0, 0))
            if dec is not None:
                control = controls[dec.right]
                out[key] = self._binding(
                    control.hwnd, windows, control.title, now,
                    WindowBindingConfidence.HIGH,
                    self._scorer.reason(inst, control, index.titles,
                                        controls_per_hwnd),
                    score=dec.score, runner_up=runner_up)
                continue
            # 唯一 WT 顶层窗口兜底：允许唤起窗口，但 observation 不归属
            if len(wt_hwnds) == 1:
                hwnd = next(iter(wt_hwnds))
                out[key] = self._binding(
                    hwnd, windows, index.titles.get(hwnd, ""), now,
                    WindowBindingConfidence.FALLBACK,
                    "single-window-fallback")
                continue
            if best_control is not None and best_score > 0:
                # 有正向证据但不满足互相唯一 → AMBIGUOUS（window=None，不猜）
                out[key] = TerminalWindowBinding(
                    confidence=WindowBindingConfidence.AMBIGUOUS,
                    last_seen=now,
                    title=controls[best_control].title,
                    reason="non-unique-evidence",
                    score=best_score, runner_up_score=runner_up)
                continue
            if wt_hwnds:
                out[key] = TerminalWindowBinding(
                    confidence=WindowBindingConfidence.AMBIGUOUS,
                    last_seen=now, reason="multiple terminal windows")
            else:
                out[key] = TerminalWindowBinding(
                    confidence=WindowBindingConfidence.NONE, last_seen=now)
        return out

    @staticmethod
    def _binding(hwnd: int, windows: dict, title: str, now: float,
                 confidence: WindowBindingConfidence, reason: str,
                 score: int = 0, runner_up: int = 0) -> TerminalWindowBinding:
        ident = windows.get(hwnd)
        return TerminalWindowBinding(
            window=ident, title=str(title or "")[:80],
            confidence=confidence, last_seen=now, validated_at=now,
            reason=reason, score=score, runner_up_score=runner_up)


class TerminalObservationResolver:
    """Agent ↔ 被观察 TermControl 的安全归属（plan §4.4/§7.4）。

    只产出 CONFIRMED/HIGH：
      * CONFIRMED —— Windows native 祖先链唯一定位窗口，且该窗口当前
        恰好只有一个被观察 TermControl；
      * HIGH —— 标题证据互相唯一匹配。
    其余情况（多 control 无证据 / FALLBACK 窗口兜底 / AMBIGUOUS）一律
    不生成 binding——宁可没有 terminal evidence，也不能错归。
    """

    def __init__(self, enum_windows=None, ancestor_pids=None):
        self._enum_windows = enum_windows
        self._ancestor_pids = ancestor_pids
        self._scorer = _EvidenceScorer()

    def resolve(self, instances: list, controls: dict,
                window_bindings: dict, now: float,
                layout=None) -> dict[str, TerminalObservationBinding]:
        index = _WindowIndex(self._enum_windows, layout)
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

        # 2) HIGH：标题证据互相唯一（顺序无关）
        inst_by_key = {inst.key: inst for inst in remaining}
        control_ids = list(controls.keys())
        controls_per_hwnd: dict[int, int] = {}
        for c in controls.values():
            controls_per_hwnd[c.hwnd] = controls_per_hwnd.get(c.hwnd, 0) + 1

        def score_fn(agent_key: str, control_id: tuple) -> int:
            return self._scorer.score_control(
                inst_by_key[agent_key], controls[control_id], index.titles,
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
                        inst, control, index.titles, controls_per_hwnd))
        return out
