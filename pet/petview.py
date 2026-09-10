"""PetView / PetViewManager：一个 Tk interpreter，N 个 Toplevel 桌宠（v4plan §8）。

  * PetApp.root 是隐藏的 controller root，不是宠物；
  * 每只宠物 = 一个 PetView（Toplevel + 独立 cursor + 独立 bubble）；
  * SharedAnimationCache / AnimationScheduler / SkinBuildManager 全进程
    一份，由 manager 统一持有——Pet 数量增加不复制缓存/定时器；
  * 每 slot 的外观经 ResolvedViewConfig 以 slot appearance 覆盖全局
    （skin=null = 继承全局，v4.3 §7.1；override 只在 FLEET 生效 §7.6）。
"""
from __future__ import annotations

import copy
import time

from actions import winkeys
from agents.models import Status
from agents.summarize import shorten

from . import skins
from .animator import AnimationCursor, SharedAnimationCache, AnimationScheduler
from .bubble import SingleAgentBubbleRenderer
from .labels import status_text
from .petwindow import MAGIC, PetWindow
from .presentation import PresentationMode, PresentationState

MARGIN = 8
GAP = 4
# 聚合叠层气泡（v4.1.4）：同一桌宠上把每张 Agent 卡片叠成一小摞——
# 最底一张带指向桌宠的尾巴，上方卡片无尾巴、只留小间隔；每张卡
# 携带自己的 agent_key（双击该气泡 = 唤起该 Agent 的终端窗口）。
STACK_GAP = 6   # 叠层卡片间隔（逻辑像素，随 scale/DPI 缩放）


class ResolvedViewConfig:
    """slot appearance override 解析（v4.3 §7.3）。

    只缓存一个小的 appearance deepcopy（避免创建时的 stale slot
    snapshot）；运行期更新一律经 PetViewManager.refresh_slot_config()
    显式刷新。slot=None 表示该 view 当前不应用 slot override
    （SINGLE/AGGREGATE 的 pet-1 使用全局 skin，§7.6）。
    """

    def __init__(self, global_config, slot_id: str, slot: dict | None = None):
        self.global_config = global_config
        self.slot_id = str(slot_id)
        self._appearance = self._extract_appearance(slot)

    @staticmethod
    def _extract_appearance(slot: dict | None) -> dict:
        appearance = (slot or {}).get("appearance")
        return copy.deepcopy(appearance) if isinstance(appearance, dict) else {}

    def refresh_slot(self, slot: dict | None) -> None:
        """运行期刷新 slot appearance（deepcopy 小对象）。"""
        self._appearance = self._extract_appearance(slot)

    def _slot_override(self, path: str):
        parts = path.split(".")
        node = self._appearance
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        leaf = parts[-1]
        if isinstance(node, dict) and leaf in node and node[leaf] is not None:
            return node[leaf]
        return None

    def overrides(self, path: str) -> bool:
        """该 path 当前是否有 slot override 生效。"""
        return self._slot_override(path) is not None

    def get(self, path, default=None):
        override = self._slot_override(path)
        if override is not None:
            return override
        try:
            return self.global_config.get(path, default)
        except Exception:
            return default


class PetView:
    """一只桌宠：Toplevel 窗口 + 光标 + 气泡渲染（不拥有缓存）。"""

    def __init__(self, view_id: str, master, view_config,
                 scheduler: AnimationScheduler,
                 on_activate, on_context_menu, on_interact, on_moved,
                 on_double_vacant=None):
        self.view_id = view_id
        self.view_config = view_config
        self.scheduler = scheduler
        self._on_activate = on_activate
        self._on_double_vacant = on_double_vacant

        self.window = PetWindow(master, view_config)
        self.bubble = SingleAgentBubbleRenderer(self.window.canvas, view_config)
        # 聚合叠层：主卡（self.bubble）之上的额外卡片（无尾巴），
        # 顺序 = 距主卡由近到远
        self.stack_bubbles: list[SingleAgentBubbleRenderer] = []
        self.cursor = AnimationCursor(view_id)
        self.agent_key = ""
        self.hidden = False

        self.window.bind_hit(self._hit)
        self.window.on_bubble_double = self._on_hit_tag
        self.window.on_body_double = self._on_body_double
        # DP43-R14：context request 显式闭包携带本 view——生产路径
        # 永远知道是哪只桌宠右键，不依赖任何隐式当前菜单上下文
        self.window.on_context_menu = (
            lambda x_root, y_root: on_context_menu(self, x_root, y_root))
        self.window.on_moved = lambda: on_moved(self)
        self.scheduler.register(self.cursor, self._on_frame)
        self._on_interact_cb = on_interact
        # Aggregate 模式下 body 双击只互动不激活（v4plan §7.1）
        self.body_activates = True

        # 锚点 = 桌宠底部中心（屏幕坐标）
        pos = view_config.get("pet_pos")
        self.anchor: tuple[int, int] | None = (
            tuple(pos) if pos and len(pos) == 2 else None)
        self._win_size: tuple[int, int] | None = None
        self._pet_item = None
        self._pet_image = None
        # v4.3 §6.3：当前显示帧的 FrameKey（与 _pet_image 对应）；
        # 与 cursor 请求 key 相同 → 沿用当前图，不再次向 cache 请求
        self._pet_frame_key = None
        # v4.3 §5.1 dirty-view：visual_dirty=需要重画；layout_dirty=
        # 结构变化（叠层数量/动画尺寸/皮肤就绪），redraw_dirty 两者一并清
        self.visual_dirty = True
        self.layout_dirty = True
        self._request_render = None   # manager 注入的 request 回调
        self._dpi = 0
        self._skin_paths: dict[str, str] = {}
        self._build_key = None
        # v4.3.1 DP43-R01：close 幂等标记（第二次调用直接返回）
        self._closed = False
        self._state = "sleep"
        # v4.3 §17 皮肤运行态（仅内存，不进 config）
        self.skin_requested_name = ""
        self.skin_runtime_name = ""
        self.skin_build_state = "ready"   # ready|queued|building|error|fallback
        self.skin_build_error = ""

    # ------------------------------------------------------------ 皮肤
    def dpi(self) -> int:
        if not self._dpi:
            try:
                self._dpi = winkeys.dpi_for_window(
                    int(self.window.root.winfo_id()))
            except Exception:
                self._dpi = 96
        return self._dpi or 96

    def invalidate_dpi(self):
        self._dpi = 0

    def gif_height(self) -> int:
        scale = float(self.view_config.get("scale", 1.0) or 1.0)
        dpi = self.dpi() / 96
        return max(96, min(960, int(round(240 * scale * dpi))))

    def desired_build_key(self):
        # 最终 fallback 是程序化 builtin-cat（v4.2.3 §10.4），
        # 不再暗含 amiya。
        skin = self.view_config.get("skin", skins.BUILTIN_SKIN)
        fps = int(self.view_config.get("convert.fps", 12) or 12)
        return (str(skin), self.gif_height(), fps)

    def load_skin(self, build_manager):
        """请求皮肤（去重 build）；有缓存立即就绪。

        v4.3 §6.5：请求携带 view_id（waiter set）；换皮时撤销对旧
        build key 的等待。§7.7：config 指定的皮肤缺失时 runtime 回退
        builtin-cat（config 字符串原样保留，UI 明示），绝不无图/异常。
        """
        key = self.desired_build_key()
        skin, height, fps = key
        self.skin_requested_name = skin
        if skin not in skins.list_skins():
            # missing skin fail-safe：runtime fallback builtin-cat
            if self.skin_runtime_name != skins.BUILTIN_SKIN:
                self.skin_runtime_name = skins.BUILTIN_SKIN
                self.skin_build_state = "fallback"
                self.skin_build_error = f"皮肤缺失：{skin}"
            skin = skins.BUILTIN_SKIN
            key = (skin, height, fps)
        else:
            self.skin_runtime_name = skin
            self.skin_build_error = ""
        if self._build_key is not None and self._build_key != key:
            build_manager.forget(self.view_id, self._build_key)
        self._build_key = key
        # v4.3.1 DP43-R04/§12：ready 判定走 manager 内存 index
        # （已验证 manifest 的唯一 cache 真值）；Tk 路径不再做
        # built_gifs_any 的 CACHE_DIR listdir。就绪回退用同 skin 同
        # fps 的就近高度（同样来自 index，无 I/O）。
        paths = build_manager.ready_paths(skin, height, fps)
        if paths:
            self._skin_ready(paths)
            if self.skin_build_state != "fallback":
                self.skin_build_state = "ready"
            return
        alt = build_manager.nearest_ready_cache(skin, height, fps)
        if alt:
            self._skin_ready(alt)
        if self.skin_build_state != "fallback":
            self.skin_build_state = "queued"
        build_manager.request(self.view_id, skin, height, fps)

    def request_skin_rebuild(self, build_manager):
        """请求重建当前皮肤的 cache（v4.3.1 DP43-R06：Tk 只做 O(1)
        排队，重建在 skin lane 的 staging 事务中进行）。"""
        key = self._build_key or self.desired_build_key()
        build_manager.request_rebuild(self.view_id, *key)

    def build_result(self, key, kind, payload, build_manager):
        if self._closed:
            return   # 已关闭的 view 不能被迟到的 build 结果复活（DP43-R01）
        if key != self._build_key:
            return
        if kind == "ok":
            if self.skin_build_state != "fallback":
                self.skin_build_state = "ready"
                self.skin_build_error = ""
            self._skin_ready(payload)
        elif kind == "err":
            # v4.3 §17：构建失败保留旧画面，继续运行
            if self.skin_build_state != "fallback":
                self.skin_build_state = "error"
                self.skin_build_error = str(payload)[:200]

    def _skin_ready(self, paths: dict[str, str]):
        self._skin_paths = dict(paths)
        self.cursor.speed = float(self.view_config.get("speed", 1.0) or 1.0)
        self.cursor.static = not bool(self.view_config.get("animated", True))
        state = self._state if self._state in self._skin_paths else "sleep"
        self._play(state, force=True)

    def _anim_meta(self, state: str):
        path = self._skin_paths.get(state) or ""
        anim = self.scheduler.cache.animation(path)
        return anim

    def _play(self, state: str, repeat: int = 0, force: bool = False):
        self._state = state
        meta = self._anim_meta(state)
        if meta is None:
            return
        self.cursor.play(meta.path, state, meta, repeat=repeat, force=force)
        self.scheduler.kick(self.view_id)
        # 与 V3 对齐：切换动画立即显示第 0 帧——经 render flush（同
        # event-loop 的 after_idle）合并执行（v4.3 §5.1，不再直接 redraw）
        self.mark_dirty(layout=True)

    def set_speed(self, speed: float):
        self.cursor.speed = max(0.1, float(speed))
        self.scheduler.kick(self.view_id)

    def set_animated(self, animated: bool):
        self.cursor.static = not animated
        if self.cursor.static:
            self.cursor.frame_index = 0   # 静态模式显示第 0 帧（V3 行为）
        self.scheduler.kick(self.view_id)
        self.mark_dirty()

    def apply_animation(self, state: str, repeat: int = 0):
        if state != self._state or self.cursor.repeat_left == 0:
            self._play(state, repeat=repeat,
                       force=self.cursor.path == "")

    # ------------------------------------------------------------ Agent 绑定
    def set_agent(self, key: str):
        self.agent_key = key

    # ------------------------------------------------------------ 绘制
    def _on_frame(self, _view_id):
        # v4.3 §4.5：scheduler due 只标记 due view dirty，不做全局重绘
        self.mark_dirty()

    def mark_dirty(self, *, layout: bool = False):
        """标记本 view 需要重画（v4.3 §5.1）。

        只设置 bool 并把 view_id 送入 coordinator 的 bounded set；
        多次调用天然 coalesce（coordinator 只保留一个 render
        after_idle）。绝不在此调用 redraw()/update()/update_idletasks()。
        """
        self.visual_dirty = True
        if layout:
            self.layout_dirty = True
        cb = self._request_render
        if cb is not None:
            cb(self.view_id)

    def _hit(self, x, y):
        tag = self.bubble.hit_button(x, y)
        if tag:
            return tag
        for renderer in self.stack_bubbles:
            tag = renderer.hit_button(x, y)
            if tag:
                return tag
        return None

    def _on_hit_tag(self, tag):
        """气泡双击：('activate', exact_agent_key) → 精确激活（绘制时
        固化的 key；现场绝不重新读 attention/focused，§12）。"""
        if isinstance(tag, tuple) and len(tag) == 2 and tag[0] == "activate":
            self._on_activate(tag[1])

    def _on_body_double(self):
        """双击 body（§13 三模式矩阵）：SINGLE/FLEET（绑定 Agent）→
        exact 激活；AGGREGATE → 只互动，绝不激活；空 slot → picker。"""
        if not self.body_activates:
            if self._on_interact_cb:
                self._on_interact_cb()
            return

        if self.agent_key:
            self._on_activate(self.agent_key)
        elif self._on_double_vacant:
            self._on_double_vacant(self)

    def set_body_activation(self, allowed: bool):
        self.body_activates = bool(allowed)

    def set_single_model(self, target) -> bool:
        """填充单卡气泡模型；返回显示内容是否变化（v4.3 §5 dirty 检测）。"""
        return self._fill_model(self.bubble.model, target)

    def set_stacked_models(self, primary, stack) -> bool:
        """聚合叠层：主卡（最下，带尾巴）+ 上方无尾巴叠卡（v4.1.4）。

        primary = cards[0]（focused/attention 优先），stack = 其余卡，
        顺序 = 距主卡由近到远。卡片数量收缩时销毁多余渲染器条目。
        """
        changed = self._fill_model(self.bubble.model, primary)
        if len(self.stack_bubbles) != len(stack):
            changed = True   # 叠层数量变化 → 结构（layout）变化
        while len(self.stack_bubbles) < len(stack):
            self.stack_bubbles.append(SingleAgentBubbleRenderer(
                self.window.canvas, self.view_config))
        for renderer, target in zip(self.stack_bubbles, stack):
            if self._fill_model(renderer.model, target):
                changed = True
        for renderer in self.stack_bubbles[len(stack):]:
            renderer.model.visible = False
            renderer.destroy_items()
        del self.stack_bubbles[len(stack):]
        return changed

    def clear_stack(self):
        """离开聚合多卡状态：收回叠层（canvas 条目一并删除）。"""
        if not self.stack_bubbles:
            return
        for renderer in self.stack_bubbles:
            renderer.destroy_items()
        self.stack_bubbles = []
        self.mark_dirty(layout=True)

    def _fill_model(self, m, target) -> bool:
        if target is None:
            changed = m.visible or m.agent_key != ""
            m.visible = False
            m.agent_key = ""
            return changed
        before = (m.visible, m.agent_key, m.status, m.text, m.footer,
                  m.accent)
        # 全量重写正文 = 恢复 canonical 内容 → 清除 toast 覆盖标记
        m.toast_applied = False
        m.pre_toast_text = None
        m.agent_key = target.key
        if not bool(self.view_config.get("bubble.enabled", True)):
            m.visible = False
            return (m.visible, m.agent_key, m.status, m.text, m.footer,
                    m.accent) != before
        m.visible = True
        snap = target.snapshot
        head = snap.kind.label
        from .labels import mode_text, phase_text
        if snap.status == Status.WORKING:
            label = " · ".join(x for x in (head, mode_text(snap),
                                           phase_text(snap) or "处理中") if x)
        else:
            label = " · ".join(x for x in (head, status_text(snap)) if x)
        m.status = label
        if snap.status in (Status.WAITING, Status.INPUT):
            m.text = snap.waiting_detail or snap.summary or "等待处理"
            m.footer = "请在终端处理"
            m.accent = "#a06b38"
        else:
            m.text = shorten(snap.summary or "等待新的任务", 160)
            if snap.goal:
                m.footer = "目标 · " + shorten(snap.goal, 60)
            else:
                inst = target.instance
                if inst.distro:
                    m.footer = f"DeskPet · WSL {inst.distro}"
                else:
                    m.footer = "Windows"
            m.accent = "#487f73" if snap.status == Status.DONE else (
                "#a06060" if snap.status == Status.ERROR else "#487f73")
        if snap.stale:
            m.footer += " · 状态可能延迟"
        return (m.visible, m.agent_key, m.status, m.text, m.footer,
                m.accent) != before

    def hide(self):
        self.hidden = True
        self.cursor.paused = True
        self.scheduler.kick(self.view_id)   # 重排：paused 不占定时器
        self.window.hide()

    def show(self):
        self.hidden = False
        self.cursor.paused = False
        self.scheduler.kick(self.view_id)
        self.window.show()
        self.mark_dirty()

    def release_images(self):
        """释放显示中的 PhotoImage 引用（v4.2.1 CI 崩溃修复）。

        必须在所属 interpreter 销毁前、主线程调用：否则引用环会把
        PhotoImage 拖到之后由任意触发 GC 的工作线程回收，__del__ 异
        线程触碰 Tcl → "Tcl_AsyncDelete: async handler deleted by
        the wrong thread" 进程中止（windows-latest CI 曾命中）。
        """
        self._pet_image = None

    def close(self, build_manager=None):
        """回收 view（DP43-R01：幂等 + forget 恰好一次）。

        第二次调用直接返回；forget(view_id, key) 只执行一次（旧实现
        第二次 forget 少传 view_id 参数，active build key 下必抛
        TypeError）。清理顺序：撤销 build 等待 → 注销 scheduler →
        释放 PhotoImage → 销毁窗口；任一步失败不阻断后续清理。
        """
        if self._closed:
            return
        self._closed = True
        key = self._build_key
        self._build_key = None
        try:
            if build_manager is not None and key is not None:
                build_manager.forget(self.view_id, key)
        finally:
            try:
                self.scheduler.unregister(self.view_id)
            finally:
                try:
                    self.release_images()
                finally:
                    try:
                        self.window.root.destroy()
                    except Exception:
                        pass

    # ------------------------------------------------------------ 几何
    def _ensure_window(self, w: int, h: int):
        if self._win_size == (w, h) or self.anchor is None:
            if self._win_size != (w, h):
                self._win_size = (w, h)
                self.window.apply_geometry(
                    w, h, self.anchor[0] - w // 2, self.anchor[1] - h)
            return
        self._win_size = (w, h)
        self.window.apply_geometry(w, h,
                                   self.anchor[0] - w // 2,
                                   self.anchor[1] - h)

    def redraw(self):
        if self.hidden or self.window.dragging:
            return
        c = self.window.canvas
        pw, ph = self.cursor.size
        bw, bh = self.bubble.layout()
        for renderer in self.stack_bubbles:
            renderer.layout()
        n_stack = sum(1 for b in self.stack_bubbles if b.model.visible)
        visible = self.bubble.model.visible
        n = n_stack + 1 if visible else 0
        scale = float(self.view_config.get("scale", 1) or 1)
        stack_gap = max(2, round(STACK_GAP * scale * self.dpi() / 96))
        block_h = n * bh + (n - 1) * stack_gap if n else 0
        if pw <= 0:
            if n:
                self._ensure_window(bw + MARGIN * 2,
                                    block_h + MARGIN * 2)
                self._draw_bubble_stack(win_w=bw + MARGIN * 2, bw=bw,
                                        block_h=block_h, stack_gap=stack_gap,
                                        pet_top=block_h + MARGIN * 2 - 2)
            return
        win_w = max(pw, bw if n else 0) + MARGIN * 2
        gap = max(2, round(GAP * scale * self.dpi() / 96))
        win_h = (block_h + gap if n else 0) + ph + MARGIN
        self._ensure_window(win_w, win_h)
        # v4.3 §6.3：请求 key 与当前显示 key 相同 → 沿用 _pet_image；
        # cache miss → scheduler 入队 CURRENT 并返回 None，本 view 保持
        # 旧图（不闪白、不 busy-wait、不 nested update，§6.4），气泡/
        # geometry 照常绘制，解码完成后由 scheduler 回调再触发重绘。
        img = None
        if self.cursor.path:
            key = self.scheduler.cursor_frame_key(self.cursor)
            if key == self._pet_frame_key and self._pet_image is not None:
                img = self._pet_image
            else:
                img = self.scheduler.frame_image(self.cursor)
                if img is not None:
                    self._pet_frame_key = key
                    # §6.2：只有真正替换 _pet_image 时才更新保护集合
                    self.cursor.displayed_frame_key = key
        if img is not None:
            if self._pet_item is None:
                self._pet_item = c.create_image(
                    (win_w - pw) // 2, win_h - ph, image=img, anchor="nw")
            else:
                c.coords(self._pet_item, (win_w - pw) // 2, win_h - ph)
                if self._pet_image is not img:
                    c.itemconfigure(self._pet_item, image=img)
            self._pet_image = img
        # 尾巴尖端 = 桌宠顶部（V3 语义 win_h-ph-2），不再伸到窗口底
        self._draw_bubble_stack(win_w=win_w, bw=bw, block_h=block_h,
                                stack_gap=stack_gap, pet_top=win_h - ph - 2)

    def _draw_bubble_stack(self, win_w, bw, block_h, stack_gap, pet_top):
        """气泡块自窗口顶部 y=0 起：主卡（带尾巴）在块底，叠卡向上。

        不可见的渲染器也必须调 draw——draw 开头会删除旧 canvas 条目，
        跳过调用会让隐藏的卡片残留在画面上。
        """
        ox = (win_w - bw) // 2
        pet_cx = win_w // 2
        self.bubble.draw(ox, block_h - self.bubble.h, pet_cx, pet_top)
        y = block_h - self.bubble.h
        for renderer in self.stack_bubbles:
            if renderer.model.visible:
                y -= renderer.h + stack_gap
            renderer.draw(ox, y, pet_cx, pet_top, draw_tail=False)

    def anchor_from_window(self):
        w, h = self._win_size or (self.window.canvas.winfo_width(),
                                  self.window.canvas.winfo_height())
        return self.window.anchor_from_window(w, h)


class PetViewManager:
    """创建/回收 PetView；持有全进程共享的 cache/scheduler/build。"""

    def __init__(self, root, config, presentation, build_manager=None):
        import tkinter as tk  # noqa: F401
        self.root = root
        self.config = config
        self.presentation = presentation
        self.cache = SharedAnimationCache(
            max_bytes=int(config.get("animation_cache_mb", 48) or 48)
            * 1024 * 1024)
        self.scheduler = AnimationScheduler(root, self.cache)
        self.build_manager = build_manager if build_manager is not None else (
            __import__("pet.skins", fromlist=["SkinBuildManager"]).SkinBuildManager())
        self.views: dict[str, PetView] = {}
        self._hooks = None   # app 提供 activate/menu/interact/moved 回调
        # v4.3 §5.1：PetView.mark_dirty → UiCoordinator.request_view 的
        # 注入点（None 时 mark_dirty 只设置 bool，供测试直接 redraw）
        self._render_request = None
        self._last_sync_mode = None
        # v4.3 §7.5/§13.3：scale 快速跨 step 只为最终稳定值 build
        # （350ms debounce；SkinBuildManager 同 key 去重兜底）
        self._build_debounce_after = None
        self._build_debounce_views: set[str] = set()
        # v4.3 §18.2：用户显式"隐藏全部桌宠"的运行期 override。
        # 不持久化；进程重启固定 False（下次启动至少一宠重新可见）。
        self.user_hidden = False

    def set_hooks(self, on_activate, on_context_menu, on_interact,
                  on_moved, on_double_vacant=None):
        self._hooks = (on_activate, on_context_menu, on_interact,
                       on_moved, on_double_vacant)

    def set_render_requester(self, cb) -> None:
        """注入 mark_dirty → coordinator.request_view 通路（v4.3 §5.1）。

        App 在 UiCoordinator 就绪后调用一次；已存在的 view 一并接上。
        """
        self._render_request = cb
        for view in self.views.values():
            view._request_render = cb

    # ------------------------------------------------------------ slot 配置
    def _find_slot(self, slot_id: str) -> dict | None:
        slots = self.config.get("presentation.concurrent.slots", []) or []
        for item in slots:
            if isinstance(item, dict) and item.get("id") == slot_id:
                return item
        return None

    def _slot_config(self, slot_id: str):
        # v4.3 §7.6：slot appearance override 只在 FLEET 生效；
        # SINGLE/AGGREGATE 的 pet-1 使用全局外观（未配置 slot 时同样
        # 隐式继承全局 pet_pos 等根键）。
        fleet_active = (self.presentation is not None
                        and self.presentation.mode is PresentationMode.FLEET)
        slot = self._find_slot(slot_id) if fleet_active else None
        return ResolvedViewConfig(self.config, slot_id, slot)

    def ensure_view(self, slot_id: str):
        if slot_id in self.views:
            return self.views[slot_id]
        view_config = self._slot_config(slot_id)
        (on_activate, on_context_menu, on_interact, on_moved,
         on_double_vacant) = \
            self._hooks or (lambda k: None, lambda v, x, y: None,
                            lambda: None, lambda v: None, None)
        view = PetView(slot_id, self.root, view_config, self.scheduler,
                       on_activate, on_context_menu, on_interact,
                       on_moved, on_double_vacant)
        if self._render_request is not None:
            view._request_render = self._render_request
        if view.anchor is None:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            view.anchor = (sw - 300 - 40 * len(self.views),
                           sh - 240)
        self.views[slot_id] = view
        view.load_skin(self.build_manager)
        if self.user_hidden:
            # v4.3 §18.2：显式隐藏期间新 view 也保持隐藏
            view.hide()
        return view

    def remove_view(self, slot_id: str):
        view = self.views.pop(slot_id, None)
        if view is not None:
            view.close(self.build_manager)

    # ------------------------------------------------------------ 外观定向更新
    def refresh_slot_config(self, slot_id: str) -> None:
        """运行期刷新某个 view 的 slot appearance 解析（v4.3 §7.3）。

        mode 切换（Fleet↔Aggregate）后必须调用：SINGLE/AGGREGATE 的
        pet-1 不应用 slot override，FLEET 恢复。
        """
        view = self.views.get(slot_id)
        if view is None:
            return
        if self.presentation.mode is PresentationMode.FLEET:
            view.view_config.refresh_slot(self._find_slot(slot_id))
        else:
            view.view_config.refresh_slot(None)

    def refresh_all_slot_configs(self) -> None:
        for slot_id in list(self.views):
            self.refresh_slot_config(slot_id)

    def slot_skin_overridden(self, slot_id: str) -> bool:
        view = self.views.get(slot_id)
        return bool(view and view.view_config.overrides("skin"))

    def apply_appearance_change(self, *, scope: str,
                                changed_paths: set[str]) -> set[str]:
        """定向应用一次外观修改（v4.3 §7.5 行为矩阵）。

        scope="global" 或 slot_id；返回需要 redraw 的 view id 集合。
        skin：旧画面保持，按 build dedup 请求新 build；scale：layout
        立即变 + 新尺寸 build；speed/animated：cursor 立即变；
        bubble.*：invalidate + redraw；force_state：重新计算动画决策。
        """
        affected: set[str] = set()
        # 先刷新 resolved config（global 修改影响所有继承者）
        if scope == "global":
            self.refresh_all_slot_configs()
        else:
            self.refresh_slot_config(scope)

        views = dict(self.views)
        if scope != "global":
            view = views.get(scope)
            views = {scope: view} if view is not None else {}

        if "skin" in changed_paths:
            for slot_id, view in views.items():
                if scope == "global" and self.slot_skin_overridden(slot_id):
                    continue   # slot override 不跟随全局（§7.6）
                view.load_skin(self.build_manager)
                affected.add(slot_id)
        if "scale" in changed_paths:
            for slot_id, view in views.items():
                view.invalidate_dpi()
                view.bubble.invalidate()
                view._win_size = None
                affected.add(slot_id)
            # 布局立即变；最终尺寸 build 走 350ms debounce，
            # 快速滑过多 step 不排队构建中间尺寸。
            self._request_deferred_build(set(affected))
        if "speed" in changed_paths:
            for slot_id, view in views.items():
                view.set_speed(float(view.view_config.get("speed", 1.0) or 1.0))
                affected.add(slot_id)
        if "animated" in changed_paths:
            for slot_id, view in views.items():
                view.set_animated(bool(view.view_config.get("animated", True)))
                affected.add(slot_id)
        if any(p == "force_state" or p.startswith("bubble.") for p in
               changed_paths):
            for slot_id, view in views.items():
                view.bubble.invalidate()
                affected.add(slot_id)
        # 外观定向更新后统一 mark dirty（v4.3 §4.4 B：APPEARANCE/SKIN
        # 已定向应用，这里只负责把受影响 view 送入 render 队列）
        for slot_id in affected:
            view = self.views.get(slot_id)
            if view is not None:
                view.mark_dirty(layout=True)
        return affected

    # ------------------------------------------------------------ build debounce
    def _request_deferred_build(self, view_ids: set[str],
                                delay_ms: int = 350) -> None:
        """合并短时间内的最终尺寸 build 请求（v4.3 §13.3）。"""
        self._build_debounce_views |= set(view_ids)
        if self._build_debounce_after is not None:
            try:
                self.root.after_cancel(self._build_debounce_after)
            except Exception:
                pass
        self._build_debounce_after = self.root.after(
            delay_ms, self._flush_deferred_build)

    def _flush_deferred_build(self) -> None:
        self._build_debounce_after = None
        view_ids, self._build_debounce_views = self._build_debounce_views, set()
        for slot_id in sorted(view_ids):
            view = self.views.get(slot_id)
            if view is not None:
                view.load_skin(self.build_manager)

    def cancel_deferred_build(self) -> None:
        if self._build_debounce_after is not None:
            try:
                self.root.after_cancel(self._build_debounce_after)
            except Exception:
                pass
            self._build_debounce_after = None
        self._build_debounce_views = set()

    # ------------------------------------------------------------ 每轮同步
    def sync(self, state: PresentationState,
             targets: dict, now: float, force_state: str = ""):
        """按呈现事实创建/回收/更新 views（v4.3 §18.2 至少一宠）。

        * SINGLE/AGGREGATE：恒为 pet-1 一个 view（0 Agent 也保留，
          idle/sleep 形态、气泡隐藏）；
        * FLEET：只有绑定了 Agent 的 slot 才有桌宠；0 bound 时保留
          pet-1 idle fallback（不代表 fake Agent：agent_key 空、不占
          Monitor target、不进 Agent 计数）；fallback 在第一个 bound
          slot 出现后被复用或原子替换；
        * create desired 先于 remove obsolete，任何 reconcile 中可见
          view 数不降为 0（用户显式隐藏除外）。
        """
        if state.mode is PresentationMode.FLEET:
            desired = set(state.slot_keys) or {"pet-1"}   # zero-agent fallback
        else:
            desired = {"pet-1"}
        # v4.3 §7.6：mode 切换后刷新所有 view 的 slot override 解析
        if state.mode is not self._last_sync_mode:
            self._last_sync_mode = state.mode
            self.refresh_all_slot_configs()
        for slot_id in sorted(desired):
            if slot_id not in self.views:
                self.ensure_view(slot_id)
        for slot_id in list(self.views):
            if slot_id not in desired:
                self.remove_view(slot_id)

        if state.mode is PresentationMode.FLEET:
            for slot_id, key in sorted(state.slot_keys.items()):
                view = self.views.get(slot_id)
                if view is None:
                    continue
                view.set_agent(key)
                view.set_body_activation(True)   # fleet：双击 = 激活绑定 Agent
                # v4.3 §5：只 dirty 显示内容真正变化的 view
                if view.set_single_model(targets.get(key)):
                    view.mark_dirty()
            if not state.slot_keys:
                # 0-bound fallback：idle 形态、无气泡、不激活终端
                view = self.views.get("pet-1")
                if view is not None:
                    view.set_agent("")
                    view.set_body_activation(False)
                    view.clear_stack()
                    if view.set_single_model(None):
                        view.mark_dirty()
        else:
            view = self.views["pet-1"]
            view.set_body_activation(
                state.mode is not PresentationMode.AGGREGATE)
            if (state.mode is PresentationMode.AGGREGATE
                    and len(state.cards) > 1):
                keys = [c.agent_key for c in state.cards]
                view.set_agent(keys[0])
                # 任一 displayed card 变化 → dirty pet-1（AC43-UI-06）
                if view.set_stacked_models(
                        targets.get(keys[0]),
                        [targets.get(k) for k in keys[1:]]):
                    view.mark_dirty(layout=True)
            else:
                view.clear_stack()
                key = state.focused_key
                view.set_agent(key)
                if view.set_single_model(
                        targets.get(key) if key else None):
                    view.mark_dirty()

    def apply_animation(self, state: PresentationState,
                        targets: dict, now: float, force_state: str = ""):
        """每只 Pet 独立动画状态（fleet per-slot；single/aggregate 共用）。"""
        if force_state in ("walk", "attack", "die", "special", "sleep"):
            for view in self.views.values():
                view.apply_animation(force_state,
                                      3 if force_state == "special" else 0)
            return
        if state.mode is PresentationMode.FLEET:
            for slot_id, view in self.views.items():
                key = view.agent_key
                target = targets.get(key) if key else None
                name, repeat = self.presentation.animation_state_for(
                    [key] if key else [], targets, now)
                view.apply_animation(name, repeat)
        else:
            name, repeat = self.presentation.animation_state_for(
                [c.agent_key for c in state.cards] or
                ([state.focused_key] if state.focused_key else []),
                targets, now)
            for view in self.views.values():
                view.apply_animation(name, repeat)

    def redraw_dirty(self, view_ids: set[str] | None = None):
        """只重画 dirty 的 view（v4.3 §5.2）。

        view_ids=None → 所有 visual_dirty 的 view。集合天然 ≤8。
        redraw_all() 只保留为测试/显式全局 reset 工具。
        """
        if view_ids is None:
            view_ids = {vid for vid, v in self.views.items()
                        if v.visual_dirty}
        for view_id in sorted(view_ids):   # stable order
            view = self.views.get(view_id)
            if view is not None:
                view.redraw()
                view.visual_dirty = False
                view.layout_dirty = False

    def redraw_all(self):
        for view in self.views.values():
            view.redraw()
            view.visual_dirty = False
            view.layout_dirty = False

    def hide_all(self):
        # v4.3 §18.2：用户显式隐藏是运行期 override（不持久化）
        self.user_hidden = True
        for view in self.views.values():
            view.hide()

    def show_all(self):
        self.user_hidden = False
        for view in self.views.values():
            view.show()

    def reassert_visible_windows(self):
        """对逻辑可见（hidden=False）的桌宠做一次 no-activate Z-order
        重声明（v4.1.3 §16）。必须用 view.hidden 表示用户逻辑意图，
        不用 winfo_viewable() 决定是否重新显示。"""
        for view in self.views.values():
            if view.hidden:
                continue
            try:
                view.window.reassert_z_order()
            except Exception:
                pass

    def any_visible(self) -> bool:
        return any(not v.hidden for v in self.views.values())

    def poll_skin_builds(self) -> list:
        """收割 build 结果并应用到等待的 view；返回 [(key,kind,payload)]。"""
        results = self.build_manager.poll_results()
        for key, kind, payload in results:
            for view in self.views.values():
                view.build_result(key, kind, payload, self.build_manager)
        return results

    def stats(self) -> dict:
        out = self.cache.stats(
            protected_frames=self.scheduler.protected_frames())
        out.update({
            "pet_views": len(self.views),
            "skin_build_pending": self.build_manager.pending_count(),
            "cold_frame_decodes": self.scheduler.cold_decode_count,
            "cold_frame_decode_queue": self.scheduler.decode_queue_len(),
        })
        return out

    def stop(self):
        self.scheduler.stop()
        self.cancel_deferred_build()
        # v4.3.1 DP43-R05：封口 skin lane（取消 active converter 树、
        # 拒绝新 job、有界等待 worker）——退出时不遗留 converter/ffmpeg
        self.build_manager.stop()
        # 先于 root.destroy() 在主线程释放全部 PhotoImage（防异线程 GC
        # 触碰 Tcl；v4.2.1 CI 崩溃修复），再清缓存帧
        for view in self.views.values():
            view.release_images()
        self.cache.free_all()
