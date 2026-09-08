"""互相唯一匹配测试：顺序无关、并列拒绝、分差门槛（V3.1 审核 §7/§21）。"""
from __future__ import annotations
import unittest

from agents.matching import (
    MatchDecision,
    best_effort_scores,
    mutual_unique_matches,
)


def matrix_fn(matrix: dict):
    return lambda l, r: matrix.get((l, r), -1)


class Key:
    """equal-but-not-identical 的自定义 key：验证匹配用值相等语义。"""

    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return isinstance(other, Key) and self.value == other.value

    def __hash__(self):
        return hash(self.value)

    def __repr__(self):
        return f"Key({self.value!r})"


class MutualUniqueTests(unittest.TestCase):
    def test_mutual_top1_matches(self):
        matrix = {("A", "X"): 5, ("A", "Y"): 1, ("B", "X"): 1, ("B", "Y"): 4}
        out = mutual_unique_matches(["A", "B"], ["X", "Y"], matrix_fn(matrix))
        self.assertEqual(out["A"].right, "X")
        self.assertEqual(out["B"].right, "Y")
        self.assertEqual(out["A"].score, 5)

    def test_left_tie_rejected(self):
        # A 对 X/Y 同分：A 侧不唯一 → 不匹配
        matrix = {("A", "X"): 5, ("A", "Y"): 5, ("B", "X"): 0, ("B", "Y"): 4}
        out = mutual_unique_matches(["A", "B"], ["X", "Y"], matrix_fn(matrix))
        self.assertNotIn("A", out)

    def test_right_tie_rejected(self):
        # X 对 A/B 同分：X 侧不唯一 → 不匹配（rival 检测）
        matrix = {("A", "X"): 5, ("A", "Y"): 1, ("B", "X"): 5, ("B", "Y"): 0}
        out = mutual_unique_matches(["A", "B"], ["X", "Y"], matrix_fn(matrix))
        self.assertNotIn("A", out)
        self.assertNotIn("B", out)

    def test_margin_insufficient_rejected(self):
        # 双向 top-1 唯一但 X 对 A/B 只差 1 < min_margin=2
        matrix = {("A", "X"): 5, ("A", "Y"): 1, ("B", "X"): 4, ("B", "Y"): 0}
        out = mutual_unique_matches(["A", "B"], ["X", "Y"], matrix_fn(matrix),
                                    min_score=3, min_margin=2)
        self.assertNotIn("A", out)
        out1 = mutual_unique_matches(["A", "B"], ["X", "Y"], matrix_fn(matrix),
                                     min_score=3, min_margin=1)
        self.assertEqual(out1["A"].right, "X")

    def test_min_score_gate(self):
        matrix = {("A", "X"): 2}
        out = mutual_unique_matches(["A"], ["X"], matrix_fn(matrix),
                                    min_score=3)
        self.assertNotIn("A", out)
        out = mutual_unique_matches(["A"], ["X"], matrix_fn(matrix), min_score=2)
        self.assertEqual(out["A"].right, "X")

    def test_order_independence(self):
        matrix = {("A", "X"): 5, ("A", "Y"): 4, ("B", "X"): 4, ("B", "Y"): 3}
        left = ["A", "B"]
        r1 = mutual_unique_matches(left, ["X", "Y"], matrix_fn(matrix))
        r2 = mutual_unique_matches(list(reversed(left)), ["Y", "X"],
                                   matrix_fn(matrix))
        self.assertEqual({k: v.right for k, v in r1.items()},
                         {k: v.right for k, v in r2.items()})

    def test_one_to_one(self):
        # 一对一约束：X 不能同时绑 A 和 B
        matrix = {("A", "X"): 5, ("B", "X"): 4}
        out = mutual_unique_matches(["A", "B"], ["X"], matrix_fn(matrix))
        self.assertEqual(out["A"].right, "X")
        self.assertNotIn("B", out)

    def test_negative_scores_excluded(self):
        matrix = {("A", "X"): -1, ("A", "Y"): 2}
        out = mutual_unique_matches(["A"], ["X", "Y"], matrix_fn(matrix))
        self.assertEqual(out["A"].right, "Y")

    def test_no_pairs_no_match(self):
        out = mutual_unique_matches(["A"], ["X"], lambda l, r: -1)
        self.assertEqual(out, {})

    def test_margins_reported(self):
        matrix = {("A", "X"): 5, ("A", "Y"): 3, ("B", "Y"): 4}
        out = mutual_unique_matches(["A", "B"], ["X", "Y"], matrix_fn(matrix))
        dec: MatchDecision = out["A"]
        self.assertEqual(dec.left_margin, 2)   # A: 5 - 3
        self.assertEqual(dec.right_margin, 5)  # X 只有 A（无次佳 → 分差=分值）
        self.assertEqual(out["B"].right, "Y")

    def test_best_effort_diagnostics(self):
        matrix = {("A", "X"): 5, ("A", "Y"): 3}
        diag = best_effort_scores(["A"], ["X", "Y"], matrix_fn(matrix))
        self.assertEqual(diag["A"], ("X", 5, 3))

    def test_mutual_match_uses_equality_not_identity(self):
        # 接口契约是 key 值相等（== / hash）而非对象 identity：
        # 自定义 key 类的等值实例（非 interned、非同对象）必须正常
        # 完成互相唯一匹配，且用全新等值 key 能取回判定。
        la, lb = Key("A"), Key("B")
        rx, ry = Key("X"), Key("Y")
        matrix = {(la, rx): 5, (la, ry): 1, (lb, rx): 1, (lb, ry): 4}
        out = mutual_unique_matches([la, lb], [rx, ry], matrix_fn(matrix))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[Key("A")].right, Key("X"))
        self.assertEqual(out[Key("B")].right, Key("Y"))
        self.assertEqual(out[Key("A")].score, 5)


if __name__ == "__main__":
    unittest.main()
