"""动画播放器：进程级共享缓存 + 每 View 光标 + 单调度器（v4plan §11）。

V4.1 Fleet 合同：
  * SharedAnimationCache —— 整个 DeskPet 进程一份 decoded PhotoImage
    预算（默认 48 MB），多个 Pet 复用同一批已解码帧（同一 Tk
    interpreter 的 PhotoImage 可跨 Toplevel 显示）；
  * AnimationCursor —— 每只 Pet 一个轻量光标（view_id/帧号/速度/
    暂停），不持有任何解码数据；
  * AnimationScheduler —— 只保留一个最早 due 的 root.after()，到期
    批量推进所有 due 光标；hidden Pet（paused）不推进帧；
  * 绝不出现 N Pet × N timer × N cache 的线性复制。

V4.3 §6 多皮肤合同（plan2）：
  * cache 保护粒度从"整个 active animation path"细化为**实际显示的
    FrameKey**（(path, index)）——8 只不同皮肤的 Pet 各自只钉住当前
    显示帧，历史帧一律受全局 LRU 预算约束；
  * 冷帧解码进有界队列（≤16），每个 Tk idle slice 最多解码 1 帧
    （一个 callback 绝不连续解码 2~8 个不同皮肤冷帧）；
  * Animation metadata pool 上限 48（对象级，decoded frame 仍只受
    frame LRU/48MB 控制），8 套皮肤状态切换不因 metadata thrash 反复
    读 JSON。
"""
import json
import os
import time
import tkinter as tk
from collections import OrderedDict
from dataclasses import dataclass, field

FrameKey = tuple[str, int]   # (gif_path, frame_index)

# 冷帧解码优先级（v4.3 §6.3）
DECODE_CURRENT = 0     # 光标正在等待显示的帧
DECODE_PREFETCH = 1    # 下一帧前瞻
MAX_DECODE_QUEUE = 16  # 8 current + 8 next-frame lookahead
MAX_COLD_DECODE_PER_SLICE = 1
# Animation 元数据对象上限（v4.3 §6.2.1）：不是 decoded frame 上限
MAX_ANIMATION_METADATA = 48


@dataclass
class FrameDecodeRequest:
    """一个排队等待冷解码的 FrameKey（waiting views ≤8）。"""
    key: FrameKey
    priority: int = DECODE_CURRENT
    waiting_views: set[str] = field(default_factory=set)


class Animation:
    """一个 GIF 的元数据 + 惰性解码帧（LRU）。"""

    def __init__(self, path: str, meta: dict):
        self.path = path
        try:
            self.n = max(1, int(meta.get("frames", 1)))
        except (TypeError, ValueError):
            self.n = 1
        try:
            self.base_delay = max(1, int(meta.get("delay_ms", 83)))
        except (TypeError, ValueError):
            self.base_delay = 83
        self.loop = bool(meta.get("loop", True))
        try:
            self.width = max(0, int(meta.get("width", 0)))
            self.height = max(0, int(meta.get("height", 0)))
        except (TypeError, ValueError):
            self.width = self.height = 0
        self._frames: OrderedDict[int, tk.PhotoImage] = OrderedDict()

    def frame_bytes(self) -> int:
        return len(self._frames) * max(1, self.width * self.height * 4)

    def frame(self, i: int, master=None) -> tk.PhotoImage:
        img = self._frames.get(i)
        if img is None:
            # master：显式绑定所属 interpreter（v4.2.1）——不依赖
            # _default_root，避免多 App/测试生命周期下错绑到别的 root
            try:
                img = tk.PhotoImage(master=master, file=self.path,
                                    format=f"gif -index {i}")
            except tk.TclError:
                # Pillow 保存时会合并相邻相同帧，实际帧数可能比 meta 少
                self.n = max(1, i)
                img = self._frames.get(0)
                if img is None:
                    img = tk.PhotoImage(master=master, file=self.path,
                                        format="gif -index 0")
                    self._frames[0] = img
                return img
            self._frames[i] = img
        self._frames.move_to_end(i)
        return img

    def free(self):
        self._frames.clear()


class SharedAnimationCache:
    """进程级共享动画缓存：一条全局 byte 预算（v4plan §11.2；v4.3 §6.2）。

    多个 Pet 请求同一 FrameKey → 同一个 PhotoImage 对象。预算不足时
    按**全局最老 FrameKey** 逐帧逐出（v4.3：不再按 animation path
    整组跳过）；`protected_frames`（实际正在显示的帧，来自 cursors
    的 displayed_frame_key）永不逐出。

    如果 8 个显示帧自身的理论内存 floor 已高于预算，不能删正在显示
    的 Tk image——stats 报告
    cache_required_floor_bytes / cache_over_budget_due_to_displayed_frames，
    隐式无限超额是被禁止的显式诊断路径。
    """

    def __init__(self, max_bytes: int = 48 * 1024 * 1024):
        self.max_bytes = int(max_bytes)
        self._pool: OrderedDict[str, Animation] = OrderedDict()
        # 全局 frame LRU（v4.3 §6.2）：key → None，只记访问顺序
        self._frame_lru: OrderedDict[FrameKey, None] = OrderedDict()
        # metadata 访问顺序（MAX_ANIMATION_METADATA 淘汰依据）
        self._meta_lru: OrderedDict[str, None] = OrderedDict()
        self.cold_decodes = 0

    def animation(self, path: str) -> Animation | None:
        anim = self._pool.get(path)
        if anim is not None:
            self._pool.move_to_end(path)
            self._meta_lru[path] = None
            self._meta_lru.move_to_end(path)
            return anim
        if not path or not os.path.isfile(path + ".json"):
            return None
        try:
            with open(path + ".json", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            return None
        anim = Animation(path, meta)
        self._pool[path] = anim
        self._meta_lru[path] = None
        return anim

    # ------------------------------------------------------------ 查询/解码
    def lookup_frame(self, key: FrameKey) -> tk.PhotoImage | None:
        """只查不解码（v4.3 §6.3 生产路径）。命中 touch 全局 LRU。"""
        path, index = key
        anim = self._pool.get(path)
        if anim is None:
            anim = self.animation(path)
            if anim is None:
                return None
        img = anim._frames.get(index)
        if img is None:
            self._frame_lru.pop(key, None)   # 清掉已逐出的陈旧 LRU 项
            return None
        self._frame_lru[key] = None
        self._frame_lru.move_to_end(key)
        self._meta_lru[path] = None
        self._meta_lru.move_to_end(path)
        return img

    def decode_frame(self, key: FrameKey, master=None,
                     protected_frames=frozenset()) -> tk.PhotoImage | None:
        """同步解码一个冷帧（只能在 Tk 线程调用；v4.3 §6.3 slice 入口）。

        解码后 touch 全局 LRU 并执行预算逐出（protected frames 跳过）。
        """
        path, index = key
        anim = self.animation(path)
        if anim is None:
            return None
        img = anim.frame(index, master=master)
        if img is None:
            return None
        self.cold_decodes += 1
        self._frame_lru[key] = None
        self._frame_lru.move_to_end(key)
        self._enforce_bytes(set(protected_frames))
        return img

    def frame(self, path: str, index: int, keep_paths: set[str] = frozenset(),
              master=None):
        """同步取帧（兼容入口：测试/一次性构建用）。

        v4.3：keep_paths 不再整组保护；保护集合应传 displayed
        FrameKey（经 scheduler）。此入口未指定时只做 LRU 逐出。
        """
        key = (str(path), int(index))
        img = self.lookup_frame(key)
        if img is not None:
            return img
        return self.decode_frame(key, master=master)

    def total_bytes(self) -> int:
        return sum(a.frame_bytes() for a in self._pool.values())

    # ------------------------------------------------------------ 逐出
    def _enforce_bytes(self, protected_frames: set[FrameKey]):
        """从全局最老 FrameKey 开始逐出；protected 跳过（§6.2）。"""
        total = self.total_bytes()
        if total <= self.max_bytes:
            return
        for key in list(self._frame_lru):
            if total <= self.max_bytes:
                return
            if key in protected_frames:
                continue
            path, index = key
            anim = self._pool.get(path)
            self._frame_lru.pop(key, None)
            if anim is None:
                continue
            size = max(1, anim.width * anim.height * 4)
            if anim._frames.pop(index, None) is not None:
                total -= size

    def evict_metadata(self, pinned_paths: set[str] = frozenset()):
        """metadata pool 收敛到 MAX_ANIMATION_METADATA（§6.2.1）。

        只淘汰：未被 displayed/decode-queue 指向（pinned）、最久未访问
        的 metadata 对象；其 decoded frames 交由 frame LRU 正常释放
        （这里 free 后 protected 帧会由下次 decode 重建——因此调用方
        必须先把 protected paths 放进 pinned）。
        """
        while len(self._pool) > MAX_ANIMATION_METADATA:
            victim = None
            for path in self._meta_lru:
                if path in pinned_paths:
                    continue
                victim = path
                break
            if victim is None:
                return   # 全部 pinned：本轮不淘汰
            self._pool.pop(victim, None)
            self._meta_lru.pop(victim, None)
            for key in list(self._frame_lru):
                if key[0] == victim:
                    self._frame_lru.pop(key, None)

    def prune(self, alive_paths: set[str], keep_paths: set[str] = frozenset()):
        """释放不在当前皮肤路径集中的动画（换肤/换尺寸后调用）。"""
        for path in list(self._pool):
            if path not in alive_paths:
                self._pool.pop(path, None)
                self._meta_lru.pop(path, None)
        for key in list(self._frame_lru):
            if key[0] not in alive_paths:
                self._frame_lru.pop(key, None)

    def free_all(self):
        for a in self._pool.values():
            a.free()
        self._pool.clear()
        self._frame_lru.clear()
        self._meta_lru.clear()

    # ------------------------------------------------------------ 统计
    def _frame_size(self, key: FrameKey) -> int:
        anim = self._pool.get(key[0])
        if anim is None:
            return 0
        return max(1, anim.width * anim.height * 4)

    def stats(self, protected_frames=frozenset()) -> dict:
        frames = sum(len(a._frames) for a in self._pool.values())
        total = self.total_bytes()
        # §6.2：显示帧 floor 显式诊断（不隐式无限超额）
        floor = sum(self._frame_size(k) for k in protected_frames
                    if self._pool.get(k[0]) is not None
                    and k[1] in self._pool[k[0]]._frames)
        return {"cache_bytes": total,
                "cache_budget": self.max_bytes,
                "cache_anims": len(self._pool),
                "cache_frames": frames,
                "cache_required_floor_bytes": floor,
                "cache_over_budget_due_to_displayed_frames":
                    bool(total > self.max_bytes)}


class AnimationCursor:
    """一只 Pet 的播放光标：只存状态，不存解码数据（v4plan §11.2）。"""

    def __init__(self, view_id: str):
        self.view_id = view_id
        self.state = ""
        self.path = ""
        self.frame_index = 0
        self.speed = 1.0
        self.static = False
        self.paused = False
        self.repeat_left = 0
        self.next_due = 0.0
        self.base_delay = 83
        self.frames = 1
        self.size = (0, 0)
        self.on_done = None
        self.dirty = True   # 需要重绘
        # v4.3 §6.2：实际显示中的帧（保护集合来源）。只有 PetView
        # 真正把新 PhotoImage 交给画布时才更新；skin/path 切换但新帧
        # 尚未解码时继续指向旧画面的 FrameKey。
        self.displayed_frame_key: FrameKey | None = None

    def play(self, path: str, state: str, meta, repeat: int = 0,
             force: bool = False):
        if path == self.path and state == self.state and not force and repeat == 0:
            return
        self.path = path
        self.state = state
        self.frame_index = 0
        # base_delay 的单位是毫秒（meta delay_ms）
        self.base_delay = max(1, int(getattr(meta, "base_delay", 83)))
        self.frames = max(1, int(getattr(meta, "n", 1)))
        self.size = (getattr(meta, "width", 0), getattr(meta, "height", 0))
        loop = bool(getattr(meta, "loop", True))
        self.repeat_left = repeat if repeat > 0 else (0 if loop else 1)
        self.next_due = time.monotonic()
        self.dirty = True

    def advance(self, now: float) -> bool:
        """推进一帧；返回是否仍需继续调度。设置 dirty 供重绘。"""
        self.frame_index += 1
        if self.frame_index >= self.frames:
            if self.repeat_left > 1:
                self.repeat_left -= 1
                self.frame_index = 0
            elif self.repeat_left == 0:      # 循环动画
                self.frame_index = 0
            else:                            # 播完停在最后一帧
                self.frame_index = self.frames - 1
                self.next_due = 0.0
                self.dirty = True
                return False
        self.dirty = True
        self.next_due = now + self.frame_delay()
        return True

    def frame_delay(self) -> float:
        """帧间隔（秒）：base_delay 是毫秒，next_due 是 monotonic 秒。"""
        return max(0.02, self.base_delay / 1000.0 / max(0.1, self.speed))


class AnimationScheduler:
    """单调度器：一个 root.after 管理所有 cursor（v4plan §11.3）。

    只保留最早 due 的定时器；到期时批量推进所有 due 光标并回调。
    hidden Pet（paused）不推进。注册/注销不产生定时器堆积。

    v4.3 §6.3 冷帧 decode queue：frame_image() cache miss 时入队
    （CURRENT），返回 None——PetView 保持上一张图不闪白；每个 Tk
    idle slice 最多解码 1 帧（MAX_COLD_DECODE_PER_SLICE），解码完成
    后只通知等待中的 view 重绘。队列有界（≤MAX_DECODE_QUEUE），
    PREFETCH 在队列拥挤或存在 CURRENT 时让路。
    """

    def __init__(self, root: tk.Misc, cache: SharedAnimationCache):
        self.root = root
        self.cache = cache
        self._cursors: dict[str, AnimationCursor] = {}
        self._callbacks: dict[str, object] = {}
        self._after_id = None
        self._due_target = 0.0
        # 冷帧解码队列（v4.3 §6.3）：FrameKey → request（去重）
        self._decode_queue: "OrderedDict[FrameKey, FrameDecodeRequest]" = \
            OrderedDict()
        self._decode_after = None
        self.cold_decode_count = 0   # §19.1 生产统计（只计数）

    def register(self, cursor: AnimationCursor, on_frame) -> None:
        self._cursors[cursor.view_id] = cursor
        self._callbacks[cursor.view_id] = on_frame
        self._schedule()

    def unregister(self, view_id: str) -> None:
        self._cursors.pop(view_id, None)
        self._callbacks.pop(view_id, None)
        for key in list(self._decode_queue):
            req = self._decode_queue[key]
            req.waiting_views.discard(view_id)
            if not req.waiting_views:
                self._decode_queue.pop(key, None)
        if not self._cursors:
            self._cancel()

    def cursor(self, view_id: str) -> AnimationCursor | None:
        return self._cursors.get(view_id)

    def cursors(self) -> dict[str, AnimationCursor]:
        return dict(self._cursors)

    def active_paths(self) -> set[str]:
        """cache keep set 从 cursors 派生（v4.2.3 §11）。

        v4.3 起帧级保护以 protected_frames() 为准；本方法仍用于
        metadata/皮肤目录 prune 的路径集合。
        """
        return {c.path for c in self._cursors.values() if c.path}

    def protected_frames(self) -> set[FrameKey]:
        """实际显示中的帧（v4.3 §6.2，≤8）——cache 逐出保护集合。"""
        return {c.displayed_frame_key for c in self._cursors.values()
                if c.displayed_frame_key is not None}

    def cursor_frame_key(self, cursor: AnimationCursor) -> FrameKey:
        return (cursor.path, min(cursor.frame_index, cursor.frames - 1))

    def frame_image(self, cursor: AnimationCursor):
        """v4.3 §6.3：cache hit → 立即返回；miss → 入队 CURRENT，
        返回 None（调用方保持旧图，不 busy-wait、不 nested update）。"""
        if not cursor.path:
            return None
        key = self.cursor_frame_key(cursor)
        img = self.cache.lookup_frame(key)
        if img is not None:
            self._maybe_prefetch(cursor, key)
            return img
        self._enqueue_decode(key, DECODE_CURRENT, cursor.view_id)
        return None

    # ------------------------------------------------------------ decode 队列
    def _maybe_prefetch(self, cursor: AnimationCursor, hit_key: FrameKey):
        """当前帧 ready 后前瞻下一帧（PREFETCH 让路规则见 §6.3）。"""
        if len(self._decode_queue) >= 8:
            return
        for req in self._decode_queue.values():
            if req.priority == DECODE_CURRENT:
                return
        next_index = (hit_key[1] + 1) % max(1, cursor.frames)
        next_key = (cursor.path, next_index)
        if self.cache.lookup_frame(next_key) is not None:
            return   # 已在缓存：不占队列
        self._enqueue_decode(next_key, DECODE_PREFETCH, cursor.view_id)

    def _enqueue_decode(self, key: FrameKey, priority: int, view_id: str):
        req = self._decode_queue.get(key)
        if req is not None:
            req.waiting_views.add(view_id)
            if priority < req.priority:
                req.priority = priority
            return
        if len(self._decode_queue) >= MAX_DECODE_QUEUE:
            if priority == DECODE_PREFETCH:
                return
            for k, r in self._decode_queue.items():
                if r.priority == DECODE_PREFETCH:
                    del self._decode_queue[k]
                    break
            else:
                return   # 队列全是 CURRENT：保最早请求，本请求丢弃
        self._decode_queue[key] = FrameDecodeRequest(
            key=key, priority=priority, waiting_views={view_id})
        self._schedule_decode_slice()

    def _schedule_decode_slice(self):
        if self._decode_after is None:
            self._decode_after = self.root.after_idle(self._decode_slice)

    def _decode_slice(self):
        """每个 Tk idle slice 最多解码 1 帧冷帧（AC43-ANIM-03）。"""
        self._decode_after = None
        if not self._decode_queue:
            return
        # CURRENT 优先，同级 FIFO
        key = next((k for k, r in self._decode_queue.items()
                    if r.priority == DECODE_CURRENT), None)
        if key is None:
            key = next(iter(self._decode_queue))
        req = self._decode_queue.pop(key)
        protected = self.protected_frames()
        img = self.cache.decode_frame(key, master=self.root,
                                      protected_frames=protected)
        if img is not None:
            self.cold_decode_count += 1
            if req.priority == DECODE_CURRENT:
                for view_id in sorted(req.waiting_views):
                    cb = self._callbacks.get(view_id)
                    if cb is not None:
                        try:
                            cb(view_id)
                        except Exception:
                            pass
        # metadata pool 收敛（pinned = 显示中 + 队列中的路径）
        pinned = {k[0] for k in self.protected_frames()}
        pinned |= {k[0] for k in self._decode_queue}
        self.cache.evict_metadata(pinned)
        if self._decode_queue:
            self._schedule_decode_slice()

    def decode_queue_len(self) -> int:
        return len(self._decode_queue)

    def frame_size(self, cursor: AnimationCursor) -> tuple[int, int]:
        return cursor.size

    def kick(self, view_id: str):
        """外部状态变化（play/pause/speed）后立即重排调度。"""
        cursor = self._cursors.get(view_id)
        if cursor is not None and not cursor.paused and not cursor.static:
            cursor.next_due = time.monotonic()
        self._schedule()

    def stop(self):
        self._cancel()
        if self._decode_after is not None:
            try:
                self.root.after_cancel(self._decode_after)
            except Exception:
                pass
            self._decode_after = None
        self._decode_queue.clear()

    # ------------------------------------------------------------ 内部
    def _cancel(self):
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        self._due_target = 0.0

    def _schedule(self):
        due = [c.next_due for c in self._cursors.values()
               if not c.paused and not c.static and c.next_due > 0]
        if not due:
            self._cancel()
            return
        target = min(due)
        if self._after_id is not None and target >= self._due_target:
            return   # 已有更早或同时的定时器在等
        self._cancel()
        now = time.monotonic()
        delay_ms = max(10, int((target - now) * 1000))
        self._due_target = target
        self._after_id = self.root.after(delay_ms, self._on_due)

    def _on_due(self):
        self._after_id = None
        now = time.monotonic()
        for view_id, cursor in list(self._cursors.items()):
            if cursor.paused or cursor.static or cursor.next_due <= 0:
                continue
            if now + 0.005 < cursor.next_due:
                continue
            if cursor.advance(now):
                pass
            cb = self._callbacks.get(view_id)
            if cb is not None:
                try:
                    cb(view_id)
                except Exception:
                    pass
        self._schedule()
