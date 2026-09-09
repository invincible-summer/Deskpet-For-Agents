"""PetView / PetViewManager：一个 Tk interpreter，N 个 Toplevel 桌宠（v4plan §8）。

  * PetApp.root 是隐藏的 controller root，不是宠物；
  * 每只宠物 = 一个 PetView（Toplevel + 独立 cursor + 独立 bubble）；
  * SharedAnimationCache / AnimationScheduler / SkinBuildManager 全进程
    一份，由 manager 统一持有——Pet 数量增加不复制缓存/定时器；
  * 每 slot 的外观经 ResolvedViewConfig 以 slot override 覆盖全局
    （null = 继承全局，v4plan §9.3）。
"""
from __future__ import annotations

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
    """slot appearance override：null/缺失 = 继承全局（v4plan §9.3）。

    保持 `config.get(path)` 风格，现有组件无需感知 slot 存在。
    """

    _ROOT_KEYS = {"skin", "scale", "speed", "animated", "topmost",
                  "pet_pos"}

    def __init__(self, global_config, slot: dict | None):
        self.global_config = global_config
        self.slot = dict(slot or {})

    def _slot_override(self, path: str):
        appearance = self.slot.get("appearance") or {}
        parts = path.split(".")
        node = appearance
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        leaf = parts[-1]
        if isinstance(node, dict) and leaf in node and node[leaf] is not None:
            return node[leaf]
        return None

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
                 on_activate, on_menu, on_interact, on_moved,
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
        self.window.on_menu = on_menu
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
        self._dpi = 0
        self._skin_paths: dict[str, str] = {}
        self._build_key = None
        self._state = "sleep"

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
        """请求皮肤（去重 build）；有缓存立即就绪。"""
        key = self.desired_build_key()
        self._build_key = key
        skin, height, _fps = key
        paths = skins.built_gifs(skin, height)
        if paths:
            self._skin_ready(paths)
            return
        alt = skins.built_gifs_any(skin)
        if alt:
            self._skin_ready(alt)
        build_manager.request(*key)

    def build_result(self, key, kind, payload, build_manager):
        if key != self._build_key:
            return
        if kind == "ok":
            self._skin_ready(payload)

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
        self.redraw()   # 与 V3 对齐：切换动画立即显示第 0 帧

    def set_speed(self, speed: float):
        self.cursor.speed = max(0.1, float(speed))
        self.scheduler.kick(self.view_id)

    def set_animated(self, animated: bool):
        self.cursor.static = not animated
        if self.cursor.static:
            self.cursor.frame_index = 0   # 静态模式显示第 0 帧（V3 行为）
        self.scheduler.kick(self.view_id)
        self.redraw()

    def apply_animation(self, state: str, repeat: int = 0):
        if state != self._state or self.cursor.repeat_left == 0:
            self._play(state, repeat=repeat,
                       force=self.cursor.path == "")

    # ------------------------------------------------------------ Agent 绑定
    def set_agent(self, key: str):
        self.agent_key = key

    # ------------------------------------------------------------ 绘制
    def _on_frame(self, _view_id):
        self.redraw()

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

    def set_single_model(self, target):
        """填充单卡气泡模型（并发模式与单个监听完全一致的气泡）。"""
        self._fill_model(self.bubble.model, target)

    def set_stacked_models(self, primary, stack):
        """聚合叠层：主卡（最下，带尾巴）+ 上方无尾巴叠卡（v4.1.4）。

        primary = cards[0]（focused/attention 优先），stack = 其余卡，
        顺序 = 距主卡由近到远。卡片数量收缩时销毁多余渲染器条目。
        """
        self._fill_model(self.bubble.model, primary)
        while len(self.stack_bubbles) < len(stack):
            self.stack_bubbles.append(SingleAgentBubbleRenderer(
                self.window.canvas, self.view_config))
        for renderer, target in zip(self.stack_bubbles, stack):
            self._fill_model(renderer.model, target)
        for renderer in self.stack_bubbles[len(stack):]:
            renderer.model.visible = False
            renderer.destroy_items()
        del self.stack_bubbles[len(stack):]

    def clear_stack(self):
        """离开聚合多卡状态：收回叠层（canvas 条目一并删除）。"""
        for renderer in self.stack_bubbles:
            renderer.destroy_items()
        self.stack_bubbles = []

    def _fill_model(self, m, target):
        if target is None:
            m.visible = False
            m.agent_key = ""
            return
        m.agent_key = target.key
        if not bool(self.view_config.get("bubble.enabled", True)):
            m.visible = False
            return
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

    def release_images(self):
        """释放显示中的 PhotoImage 引用（v4.2.1 CI 崩溃修复）。

        必须在所属 interpreter 销毁前、主线程调用：否则引用环会把
        PhotoImage 拖到之后由任意触发 GC 的工作线程回收，__del__ 异
        线程触碰 Tcl → "Tcl_AsyncDelete: async handler deleted by
        the wrong thread" 进程中止（windows-latest CI 曾命中）。
        """
        self._pet_image = None

    def close(self, build_manager=None):
        self.release_images()
        self.scheduler.unregister(self.view_id)
        if build_manager is not None and self._build_key is not None:
            build_manager.forget(self._build_key)
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
        img = self.scheduler.frame_image(self.cursor)
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

    def set_hooks(self, on_activate, on_menu, on_interact, on_moved,
                  on_double_vacant=None):
        self._hooks = (on_activate, on_menu, on_interact, on_moved,
                       on_double_vacant)

    # ------------------------------------------------------------ slot 配置
    def _slot_config(self, slot_id: str, state: PresentationState):
        slots = self.config.get("presentation.concurrent.slots", []) or []
        slot = None
        for item in slots:
            if isinstance(item, dict) and item.get("id") == slot_id:
                slot = item
                break
        if slot is None:
            # 单目标/未配置 slot：隐式 slot 继承全局（pet_pos 等根键）
            slot = {"id": slot_id,
                    "appearance": None,
                    "placement": {"manual": False}}
        return ResolvedViewConfig(self.config, slot)

    def ensure_view(self, slot_id: str):
        if slot_id in self.views:
            return self.views[slot_id]
        state = PresentationMode.SINGLE   # 仅用于默认隐式 slot
        view_config = self._slot_config(slot_id, None)
        on_activate, on_menu, on_interact, on_moved, on_double_vacant = \
            self._hooks or (lambda k: None, lambda m: None, lambda: None,
                            lambda v: None, None)
        view = PetView(slot_id, self.root, view_config, self.scheduler,
                       on_activate, on_menu, on_interact, on_moved,
                       on_double_vacant)
        if view.anchor is None:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            view.anchor = (sw - 300 - 40 * len(self.views),
                           sh - 240)
        self.views[slot_id] = view
        view.load_skin(self.build_manager)
        return view

    def remove_view(self, slot_id: str):
        view = self.views.pop(slot_id, None)
        if view is not None:
            view.close(self.build_manager)

    # ------------------------------------------------------------ 每轮同步
    def sync(self, state: PresentationState,
             targets: dict, now: float, force_state: str = ""):
        """按呈现事实创建/回收/更新 views。

        Fleet：只有绑定了 Agent 的 slot 才有桌宠（没有绑定不显示，
        桌宠数量跟随绑定数而不是 slot 配置数）；每只桌宠的气泡与
        单个监听完全一致（单卡，无 "N Agents" 栈卡）。
        Aggregate：单宠 "pet-1"，多张候选卡叠成一摞（v4.1.4）——
        主卡（focused/attention 优先，带尾巴）最下，其余卡小间隔叠上，
        每张卡携带自己的 agent_key，双击该气泡唤起该 Agent 的终端。
        """
        if state.mode is PresentationMode.FLEET:
            live = set(state.slot_keys)
            for slot_id in list(self.views):
                if slot_id not in live:
                    self.remove_view(slot_id)
            for slot_id, key in state.slot_keys.items():
                view = self.ensure_view(slot_id)
                view.set_agent(key)
                view.set_body_activation(True)   # fleet：双击 = 激活绑定 Agent
                view.set_single_model(targets.get(key))
        else:
            if "pet-1" not in self.views:
                self.ensure_view("pet-1")
            for slot_id in list(self.views):
                if slot_id != "pet-1":
                    self.remove_view(slot_id)
            view = self.views["pet-1"]
            view.set_body_activation(
                state.mode is not PresentationMode.AGGREGATE)
            if (state.mode is PresentationMode.AGGREGATE
                    and len(state.cards) > 1):
                keys = [c.agent_key for c in state.cards]
                view.set_agent(keys[0])
                view.set_stacked_models(
                    targets.get(keys[0]),
                    [targets.get(k) for k in keys[1:]])
            else:
                view.clear_stack()
                key = state.focused_key
                view.set_agent(key)
                view.set_single_model(targets.get(key) if key else None)

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

    def redraw_all(self):
        for view in self.views.values():
            view.redraw()

    def hide_all(self):
        for view in self.views.values():
            view.hide()

    def show_all(self):
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
        out = self.cache.stats()
        out.update({
            "pet_views": len(self.views),
            "skin_build_pending": self.build_manager.pending_count(),
        })
        return out

    def stop(self):
        self.scheduler.stop()
        # 先于 root.destroy() 在主线程释放全部 PhotoImage（防异线程 GC
        # 触碰 Tcl；v4.2.1 CI 崩溃修复），再清缓存帧
        for view in self.views.values():
            view.release_images()
        self.cache.free_all()
