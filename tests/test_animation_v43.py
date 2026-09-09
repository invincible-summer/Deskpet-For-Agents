"""v4.3 §6：frame 级 LRU / 冷帧 decode slicing / metadata pool。

覆盖 AC43-ANIM-01..08、10（真 Tk root + 真多帧 GIF）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from pet.animator import (
    DECODE_CURRENT,
    DECODE_PREFETCH,
    MAX_ANIMATION_METADATA,
    MAX_DECODE_QUEUE,
    AnimationCursor,
    AnimationScheduler,
    SharedAnimationCache,
)


def _make_gif(path: Path, frames: int = 8, size: int = 96) -> None:
    # RGB 逐帧不同色：PIL 转换的 P 帧 Tk 才能按 -index 读取
    # （直接 new("P") 的多帧 GIF Tk 只能读 index 0）
    imgs = [Image.new("RGB", (size, size),
                      (i * 29 % 256, i * 67 % 256, i * 13 % 256))
            for i in range(frames)]
    imgs[0].save(path, save_all=True, append_images=imgs[1:],
                 duration=83, loop=0)
    Path(str(path) + ".json").write_text(json.dumps(
        {"frames": frames, "width": size, "height": size,
         "delay_ms": 83, "loop": True}), encoding="utf-8")


class FrameLruTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 预算 = 3 帧（96*96*4*3）强制逐出
        self.cache = SharedAnimationCache(max_bytes=96 * 96 * 4 * 3)
        self.sched = AnimationScheduler(self.root, self.cache)

    def tearDown(self):
        self.sched.stop()
        self.cache.free_all()
        self.root.destroy()

    def _gif(self, name: str, frames: int = 8) -> str:
        p = Path(self.tmp.name) / f"{name}.gif"
        _make_gif(p, frames=frames)
        return str(p)

    def _cursor(self, view_id: str, path: str) -> AnimationCursor:
        c = AnimationCursor(view_id)
        anim = self.cache.animation(path)
        c.play(path, "walk", anim)
        self.sched.register(c, lambda _vid: None)
        # 冻结帧推进：测试手动控制 frame_index（避免 root.update()
        # 同时触发 scheduler 的帧定时器推进光标）
        c.paused = True
        return c

    def test_displayed_frame_protected_not_requested(self):
        # AC43-ANIM-01：保护的是实际 displayed key，不是 cursor 想要
        # 但尚未 decode 的 key
        p1 = self._gif("a")
        p2 = self._gif("b")
        c1 = self._cursor("pet-1", p1)
        c2 = self._cursor("pet-2", p2)
        # pet-1 请求 frame 0 → miss 入队 → idle 解码 → 再取命中
        self.assertIsNone(self.sched.frame_image(c1))
        self.root.update()
        img1 = self.sched.frame_image(c1)
        self.assertIsNotNone(img1)
        # PetView 拿到 image 并替换画布时更新 displayed key（测试模拟）
        c1.displayed_frame_key = (p1, 0)
        self.assertEqual(self.sched.protected_frames(), {(p1, 0)})
        # cursor 前进到未解码帧：requested key ≠ protected 集合成员
        c1.frame_index = 5
        requested = self.sched.cursor_frame_key(c1)
        self.assertNotIn(requested, self.sched.protected_frames())
        # 未显示任何帧的 pet-2 不贡献保护键
        self.assertIsNone(c2.displayed_frame_key)

    def test_old_displayed_frame_evictable_after_advance(self):
        # 保护集合跟随实际显示：推进并显示新帧后旧帧可被逐出
        p = self._gif("a")
        c = self._cursor("pet-1", p)
        c.frame_index = 0
        self.sched.frame_image(c)     # 入队 (p,0)
        self.root.update()            # 解码完成
        self.sched.frame_image(c)     # 命中
        c.displayed_frame_key = (p, 0)
        # 显示 frame 3
        c.frame_index = 3
        self.sched.frame_image(c)     # 入队 (p,3)
        self.root.update()
        self.sched.frame_image(c)
        c.displayed_frame_key = (p, 3)
        # 预算只够 3 帧：连续 decode 其他帧后，非保护历史帧被逐出
        for i in (1, 2, 4, 5, 6):
            self.cache.decode_frame((p, i), master=self.root,
                                    protected_frames={(p, 3)})
        self.assertLessEqual(self.cache.total_bytes(), self.cache.max_bytes)
        self.assertIsNotNone(self.cache.lookup_frame((p, 3)))   # 显示帧保留
        # 被逐出的帧可按需重解码（LRU 正常工作）
        self.assertIsNone(self.cache.lookup_frame((p, 0)))

    def test_decode_queue_bounded_and_slice_decodes_one(self):
        # AC43-ANIM-02/03：队列 ≤16；一个 slice 最多 1 次冷解码
        p = self._gif("a", frames=24)
        cursors = []
        for i in range(8):
            c = self._cursor(f"pet-{i + 1}", p)
            c.frame_index = i
            cursors.append(c)
        # 全部 miss 入队（同步 frame_image 不解码）
        before = self.cache.cold_decodes
        for c in cursors:
            self.sched.frame_image(c)
        self.assertEqual(self.cache.cold_decodes, before)
        self.assertLessEqual(self.sched.decode_queue_len(), MAX_DECODE_QUEUE)
        # 直接运行一个 slice（不等事件循环）：只解码 1 帧
        self.sched._schedule_decode_slice()
        self.sched._decode_slice()
        self.assertEqual(self.cache.cold_decodes, before + 1)
        # slice 后仍有剩余请求（下一个 after_idle 已排）
        self.assertGreaterEqual(self.sched.decode_queue_len(), 1)
        self.assertIsNotNone(self.sched._decode_after)

    def test_same_frame_shared_across_pets_single_decode(self):
        # AC43-ANIM-05：8 Pet 同 skin 同 frame → 同一 PhotoImage
        p = self._gif("a")
        cursors = [self._cursor(f"pet-{i + 1}", p) for i in range(8)]
        for c in cursors:
            self.sched.frame_image(c)
        self.root.update()   # 一帧解码
        imgs = [self.sched.frame_image(c) for c in cursors]
        self.assertTrue(all(i is not None for i in imgs))
        self.assertTrue(all(i is imgs[0] for i in imgs))
        self.assertEqual(self.cache.cold_decodes, 1)

    def test_miss_keeps_old_image_and_no_sync_decode(self):
        # AC43-ANIM-04：miss 返回 None；旧图保持（无 busy-wait）
        p = self._gif("a")
        c = self._cursor("pet-1", p)
        self.sched.frame_image(c)     # 入队
        self.root.update()            # 解码完成
        old = self.sched.frame_image(c)
        self.assertIsNotNone(old)
        c.displayed_frame_key = self.sched.cursor_frame_key(c)
        c.frame_index = 4
        got = self.sched.frame_image(c)
        self.assertIsNone(got)          # 入队而不是同步解码
        # 再查仍 None（不重复解码）
        self.assertIsNone(self.sched.frame_image(c))
        # 旧帧仍在缓存
        self.assertIsNotNone(self.cache.lookup_frame((c.path, 0)))

    def test_floor_diagnostics_when_displayed_exceed_budget(self):
        # AC43-ANIM-07：显示帧 floor 超预算 → 显式诊断而不是隐式超额
        tiny = SharedAnimationCache(max_bytes=1)   # 预算装不下任何帧
        p = self._gif("a")
        img = tiny.decode_frame((p, 0), master=self.root,
                                protected_frames={(p, 0)})
        self.assertIsNotNone(img)   # 显示帧不被删
        stats = tiny.stats(protected_frames={(p, 0)})
        self.assertGreater(stats["cache_bytes"], tiny.max_bytes)
        self.assertTrue(stats["cache_over_budget_due_to_displayed_frames"])
        self.assertGreater(stats["cache_required_floor_bytes"], 0)
        tiny.free_all()

    def test_metadata_pool_cap_with_pinned_exempt(self):
        # AC43-ANIM-10：metadata ≤48；pinned（显示中/队列中）不淘汰
        paths = [self._gif(f"m{i}") for i in range(50)]
        pinned = paths[0]
        for p in paths:
            self.cache.animation(p)
        self.assertGreater(len(self.cache._pool), MAX_ANIMATION_METADATA)
        self.cache.evict_metadata({pinned})
        self.assertEqual(len(self.cache._pool), MAX_ANIMATION_METADATA)
        self.assertIn(pinned, self.cache._pool)

    def test_prefetch_yields_to_current(self):
        # PREFETCH 让路：队列存在 CURRENT 时不入 prefetch
        p = self._gif("a")
        c = self._cursor("pet-1", p)
        self.sched.frame_image(c)   # CURRENT 入队
        self.sched._maybe_prefetch(c, (p, 0))
        self.assertEqual(self.sched.decode_queue_len(), 1)
        req = next(iter(self.sched._decode_queue.values()))
        self.assertEqual(req.priority, DECODE_CURRENT)
        # 处理完 CURRENT 后 prefetch 可以进入
        self.root.update()
        self.sched.frame_image(c)   # hit（已解码）
        self.sched._maybe_prefetch(c, (p, 0))
        self.assertEqual(self.sched.decode_queue_len(), 1)
        req = next(iter(self.sched._decode_queue.values()))
        self.assertEqual(req.priority, DECODE_PREFETCH)
        self.sched._decode_queue.clear()


if __name__ == "__main__":
    unittest.main()
