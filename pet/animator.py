"""动画播放器：tkinter 原生 GIF 逐帧惰性解码 + LRU 缓存（内存友好）。"""
import os
import tkinter as tk
from collections import OrderedDict

MAX_CACHED_ANIMS = 2


class Animation:
    def __init__(self, path: str, meta: dict):
        self.path = path
        self.n = int(meta.get("frames", 1))
        self.base_delay = int(meta.get("delay_ms", 83))
        self.loop = bool(meta.get("loop", True))
        self.width = int(meta.get("width", 0))
        self.height = int(meta.get("height", 0))
        self._frames: dict[int, tk.PhotoImage] = {}

    def frame(self, i: int) -> tk.PhotoImage:
        img = self._frames.get(i)
        if img is None:
            try:
                img = tk.PhotoImage(file=self.path, format=f"gif -index {i}")
            except tk.TclError:
                # Pillow 保存时会合并相邻相同帧，实际帧数可能比 meta 少
                self.n = max(1, i)
                img = self._frames.get(0)
                if img is None:
                    img = tk.PhotoImage(file=self.path, format="gif -index 0")
                    self._frames[0] = img
                return img
            self._frames[i] = img
        return img

    def free(self):
        self._frames.clear()


class Animator:
    """负责当前动画的帧推进。静态模式只显示第 0 帧。"""

    def __init__(self, root: tk.Misc):
        self.root = root
        self.speed = 1.0
        self.static = False
        self.current: Animation | None = None
        self._name = ""
        self._idx = 0
        self._repeats_left = 0      # 非循环动画剩余播放次数
        self._on_done = None
        self._after_id = None
        self._pool: OrderedDict[str, Animation] = OrderedDict()
        self._paths: dict[str, str] = {}
        self._tick_cb = None        # 帧更新回调

    def bind_tick(self, cb):
        self._tick_cb = cb

    def load_pool(self, names: dict[str, str]):
        """把皮肤的全部动画加入池（只记路径，不解码）。"""
        self._paths = dict(names)
        self._name = ""   # 允许同名动画立即切换（换肤/换尺寸）

    def play(self, name: str, repeat: int = 0, on_done=None, force: bool = False):
        """repeat=0 表示按 meta 的循环属性；>0 表示强制播放 N 遍后回调 on_done。"""
        if name == self._name and not force and repeat == 0:
            return
        path = self._paths.get(name)
        if not path or not os.path.isfile(path):
            return
        anim = self._pool_get(path)
        self._pool_touch(path, anim)
        self.current = anim
        self._name = name
        self._idx = 0
        self._on_done = on_done
        self._repeats_left = (repeat - 1) if repeat > 0 else (0 if anim.loop else -1)
        if self._after_id:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self._schedule()
        if self._tick_cb:
            self._tick_cb()

    def frame_image(self) -> tk.PhotoImage | None:
        if not self.current:
            return None
        return self.current.frame(min(self._idx, self.current.n - 1))

    def frame_size(self) -> tuple[int, int]:
        if self.current:
            return self.current.width, self.current.height
        return 0, 0

    def _pool_get(self, path: str) -> Animation:
        for a in self._pool.values():
            if a.path == path:
                return a
        import json
        with open(path + ".json", encoding="utf-8") as f:
            meta = json.load(f)
        return Animation(path, meta)

    def _pool_touch(self, path: str, anim: Animation):
        self._pool[path] = anim
        self._pool.move_to_end(path)
        while len(self._pool) > MAX_CACHED_ANIMS:
            _, old = self._pool.popitem(last=False)
            if old is not self.current:
                old.free()

    def prune(self):
        """释放不在当前皮肤路径集中的动画（换肤/换尺寸后调用）。"""
        alive = set(self._paths.values())
        for path in list(self._pool):
            anim = self._pool.pop(path)
            if anim is not self.current:
                anim.free()

    def free_all(self):
        for a in self._pool.values():
            a.free()
        self._pool.clear()
        self.current = None
        self._name = ""

    def stop(self):
        """退出前调用：取消帧定时器。"""
        if self._after_id:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _schedule(self):
        if self.static or not self.current:
            return
        delay = max(20, int(self.current.base_delay / self.speed))
        self._after_id = self.root.after(delay, self._advance)

    def _advance(self):
        self._after_id = None
        if not self.current:
            return
        self._idx += 1
        if self._idx >= self.current.n:
            if self._repeats_left > 0:
                self._repeats_left -= 1
                self._idx = 0
            elif self._repeats_left == 0:      # 循环动画
                self._idx = 0
            else:                               # 非循环动画播完：释放帧缓存再回调
                self._idx = self.current.n - 1
                finished, self.current = self.current, None
                finished.free()
                self._pool = OrderedDict(
                    (k, v) for k, v in self._pool.items() if v is not finished)
                cb, self._on_done = self._on_done, None
                if self._tick_cb:
                    self._tick_cb()
                if cb:
                    cb()
                return                          # 无论有无回调都必须停在这里
        if self._tick_cb:
            self._tick_cb()
        self._schedule()

    def set_speed(self, speed: float):
        self.speed = max(0.1, float(speed))

    def set_static(self, static: bool):
        self.static = bool(static)
        if self.current is None:
            return
        if self.static:
            if self._after_id:
                self.root.after_cancel(self._after_id)
                self._after_id = None
            self._idx = 0
            if self._tick_cb:
                self._tick_cb()
        elif not self._after_id:
            self._schedule()
