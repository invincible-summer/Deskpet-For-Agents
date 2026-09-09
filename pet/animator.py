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
"""
import json
import os
import time
import tkinter as tk
from collections import OrderedDict

MAX_CACHED_ANIMS = 2   # 每个 skin 尺寸组合的 Animation 对象上限（全局）


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
    """进程级共享动画缓存：一条全局 byte 预算（v4plan §11.2）。

    多个 Pet 请求同一 (skin,height) 的同一帧 → 返回同一个
    PhotoImage 对象；预算不足时按 LRU 逐出（正在显示的帧除外——由
    调用方在 advance 后传入 keep 集合）。
    """

    def __init__(self, max_bytes: int = 48 * 1024 * 1024):
        self.max_bytes = int(max_bytes)
        self._pool: OrderedDict[str, Animation] = OrderedDict()

    def animation(self, path: str) -> Animation | None:
        anim = self._pool.get(path)
        if anim is not None:
            self._pool.move_to_end(path)
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
        self._enforce_anims()
        return anim

    def frame(self, path: str, index: int, keep_paths: set[str] = frozenset(),
              master=None):
        """取帧并维持全局字节预算。keep_paths 中的动画不做帧逐出。"""
        anim = self.animation(path)
        if anim is None:
            return None
        img = anim.frame(index, master=master)
        self._enforce_bytes(keep_paths)
        return img

    def total_bytes(self) -> int:
        return sum(a.frame_bytes() for a in self._pool.values())

    def _enforce_anims(self):
        while len(self._pool) > MAX_CACHED_ANIMS * 8:
            self._pool.popitem(last=False)

    def _enforce_bytes(self, keep_paths: set[str]):
        total = self.total_bytes()
        for path in list(self._pool):
            if total <= self.max_bytes:
                return
            anim = self._pool[path]
            if path in keep_paths:
                continue
            for index in list(anim._frames):
                if total <= self.max_bytes:
                    return
                size = max(1, anim.width * anim.height * 4)
                del anim._frames[index]
                total -= size
            # 帧清空的动画对象保留（元数据很便宜，避免重复读 json）

    def prune(self, alive_paths: set[str], keep_paths: set[str] = frozenset()):
        """释放不在当前皮肤路径集中的动画（换肤/换尺寸后调用）。"""
        for path in list(self._pool):
            if path not in alive_paths:
                anim = self._pool.pop(path)
                if path not in keep_paths:
                    anim.free()

    def free_all(self):
        for a in self._pool.values():
            a.free()
        self._pool.clear()

    def stats(self) -> dict:
        frames = sum(len(a._frames) for a in self._pool.values())
        return {"cache_bytes": self.total_bytes(),
                "cache_budget": self.max_bytes,
                "cache_anims": len(self._pool),
                "cache_frames": frames}


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
    """

    def __init__(self, root: tk.Misc, cache: SharedAnimationCache):
        self.root = root
        self.cache = cache
        self._cursors: dict[str, AnimationCursor] = {}
        self._callbacks: dict[str, object] = {}
        self._after_id = None
        self._due_target = 0.0

    def register(self, cursor: AnimationCursor, on_frame) -> None:
        self._cursors[cursor.view_id] = cursor
        self._callbacks[cursor.view_id] = on_frame
        self._schedule()

    def unregister(self, view_id: str) -> None:
        self._cursors.pop(view_id, None)
        self._callbacks.pop(view_id, None)
        if not self._cursors:
            self._cancel()

    def cursor(self, view_id: str) -> AnimationCursor | None:
        return self._cursors.get(view_id)

    def cursors(self) -> dict[str, AnimationCursor]:
        return dict(self._cursors)

    def active_paths(self) -> set[str]:
        """cache keep set 从 cursors 派生（v4.2.3 §11）。

        替代只增不减的 mutable `_active_paths`：walk→attack→die 状态
        切换后旧 path 不再进入 keep 集，历史动画可被预算正常逐出。
        Fleet ≤8 个 view，O(≤8) 可忽略。
        """
        return {c.path for c in self._cursors.values() if c.path}

    def frame_image(self, cursor: AnimationCursor):
        return self.cache.frame(cursor.path,
                                min(cursor.frame_index, cursor.frames - 1),
                                keep_paths=self.active_paths(),
                                master=self.root)

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
