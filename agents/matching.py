"""保守的一对一匹配：只有互相唯一的证据才能自动绑定。

同时供 BaseWatcher 的 session 绑定与 TerminalResolver 的 pane 绑定使用。
目标不是"提高自动绑定率"，而是消除顺序依赖：

    match(left=[A, B]) == match(left=[B, A])

判定条件（全部满足才自动匹配）：
  * right 是 left 的唯一 top-1（left 侧无并列）
  * left 是 right 的唯一 top-1（right 侧无并列）
  * score >= min_score
  * 双方 top1-top2 分差 >= min_margin

任何一侧并列或分差不足 → 不匹配（UNKNOWN / AMBIGUOUS 优于错绑）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Hashable, Iterable


@dataclass
class MatchDecision:
    left: Hashable
    right: Hashable | None
    score: int = 0
    left_margin: int = 0   # 该 left 的 top1-top2 分差
    right_margin: int = 0  # 该 right 的 top1-top2 分差
    reason: str = ""


def _pair_score(scores: dict[tuple, int], a, b) -> int | None:
    """对称查表：矩阵键序为 (left, right)，两个方向都要能查到。"""
    v = scores.get((a, b))
    if v is None:
        v = scores.get((b, a))
    return v


def _rankings(side_keys: list, other_keys: list,
              scores: dict[tuple, int]) -> dict:
    """每个 side key 的 (top_other, top_score, margin, 是否唯一)。

    并列 top（另一个 other 同分）时 top 置 None——该侧不唯一。
    second 为 None 时分差视为 top_score（只有唯一候选，天然满足 margin>=1）。
    """
    out: dict = {}
    for s in side_keys:
        pairs = sorted(
            ((_pair_score(scores, s, o), o) for o in other_keys
             if _pair_score(scores, s, o) is not None),
            key=lambda p: (-p[0], str(p[1])))
        if not pairs:
            out[s] = (None, 0, 0, False)
            continue
        top_score, top_other = pairs[0]
        second_score = None
        if len(pairs) > 1:
            second_score = pairs[1][0]
        top = top_other if second_score is None or second_score < top_score else None
        margin = top_score - second_score if second_score is not None else top_score
        out[s] = (top, top_score, margin, top is not None)
    return out


def score_matrix(left_keys: Iterable, right_keys: Iterable,
                 score_fn: Callable) -> dict[tuple, int]:
    """全对评分矩阵；score_fn 返回负值视为不可配对（如 source 冲突）。"""
    lefts, rights = list(left_keys), list(right_keys)
    scores: dict[tuple, int] = {}
    for l in lefts:
        for r in rights:
            try:
                s = int(score_fn(l, r))
            except (TypeError, ValueError):
                continue
            if s >= 0:
                scores[(l, r)] = s
    return scores


def mutual_unique_matches(left_keys: Iterable, right_keys: Iterable,
                          score_fn: Callable, *, min_score: int = 1,
                          min_margin: int = 1) -> dict:
    """互相唯一的一对一匹配；结果与输入顺序无关。

    接口契约：left/right 是 hashable identity key，匹配判定用 key 的
    值相等（== / hash），不依赖对象 identity。

    返回 {left_key: MatchDecision}；未匹配的 left 不出现在结果里。
    """
    lefts, rights = list(left_keys), list(right_keys)
    scores = score_matrix(lefts, rights, score_fn)
    by_left = _rankings(lefts, rights, scores)
    by_right = _rankings(rights, lefts, scores)

    out: dict = {}
    for l in lefts:
        top, top_score, left_margin, unique = by_left[l]
        if top is None or not unique:
            continue
        if top_score < min_score or left_margin < min_margin:
            continue
        r = top
        r_top, _r_score, right_margin, r_unique = by_right[r]
        if r_top != l or not r_unique:
            continue
        if right_margin < min_margin:
            continue
        out[l] = MatchDecision(left=l, right=r, score=top_score,
                               left_margin=left_margin,
                               right_margin=right_margin)
    return out


def best_effort_scores(left_keys: Iterable, right_keys: Iterable,
                      score_fn: Callable) -> dict:
    """诊断辅助：每个 left 的 (best_right, best_score, runner_up_score)。

    仅用于 AMBIGUOUS 的原因展示，不参与绑定判定。
    """
    lefts, rights = list(left_keys), list(right_keys)
    scores = score_matrix(lefts, rights, score_fn)
    out: dict = {}
    for l in lefts:
        pairs = sorted(
            ((_pair_score(scores, l, r), r) for r in rights
             if _pair_score(scores, l, r) is not None),
            key=lambda p: (-p[0], str(p[1])))
        if pairs:
            out[l] = (pairs[0][1], pairs[0][0],
                      pairs[1][0] if len(pairs) > 1 else 0)
        else:
            out[l] = (None, 0, 0)
    return out
