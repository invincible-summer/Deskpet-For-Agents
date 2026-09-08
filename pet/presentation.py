"""PresentationController：并发呈现的事实层（v4plan §6/§7）。

Monitor 不再负责"UI 当前是谁"；本模块只消费 `get_targets()` 产出
呈现事实：focused_key（用户选择）与 attention_key（最需注意）分离、
Aggregate 卡片选择、Fleet slot 绑定。不 import Tk——纯逻辑可测。

设计合同：
  * focused_key：用户最近明确选择的 Agent；runtime only；不因另一个
    普通 WORKING 改变；Agent exit 后清空；
  * attention_key：当前最需要引起注意的 Agent（WAITING > INPUT >
    ERROR > WORKING > DONE > IDLE > UNKNOWN），WAITING 可在视觉上
    高亮，但不偷偷改 focused_key；
  * WAITING/INPUT/ERROR 必须抢占 DONE special 动画（§6.5）；
  * max_targets 只是展示上限，超出的 Agent 仍被 Monitor 正常监听；
  * Fleet selector 模糊时（>1 candidate）不猜，slot 保持 vacant。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum

from agents.models import AgentKind, AgentTarget, Status


class PresentationMode(str, Enum):
    SINGLE = "single"
    AGGREGATE = "aggregate"
    FLEET = "fleet"


# 注意力优先级（数值越小越优先；v4plan §6.4）
_ATTENTION_PRIORITY = {
    Status.WAITING: 0,
    Status.INPUT: 1,
    Status.ERROR: 2,
    Status.WORKING: 3,
    Status.DONE: 4,
    Status.IDLE: 5,
    Status.UNKNOWN: 6,
}

VALID_MODES = ("aggregate", "fleet")
MAX_TARGETS_MIN, MAX_TARGETS_MAX = 1, 8


@dataclass
class AgentCardModel:
    """一张精确 Agent 卡片（携带 exact agent_key，v4plan §7.2）。"""
    agent_key: str
    title: str
    status: str
    text: str
    footer: str
    accent: str
    attention: bool = False


@dataclass
class PresentationState:
    """一轮 reconcile 的呈现事实快照。"""
    mode: PresentationMode
    focused_key: str = ""
    attention_key: str = ""
    cards: tuple[AgentCardModel, ...] = ()
    overflow_count: int = 0
    slot_keys: dict[str, str] = field(default_factory=dict)   # fleet: slot→key
    slot_vacant_reason: dict[str, str] = field(default_factory=dict)

    def signature(self) -> tuple:
        """卡片/绑定内容的确定性指纹：不变则 UI 不重建。"""
        return (self.mode, self.focused_key, self.attention_key,
                tuple((c.agent_key, c.title, c.status, c.text, c.footer,
                       c.accent, c.attention) for c in self.cards),
                self.overflow_count, tuple(sorted(self.slot_keys.items())),
                tuple(sorted(self.slot_vacant_reason.items())))


@dataclass
class AgentSelector:
    """语义 selector：只用于重启后"尝试重新认领"（v4plan §8.4）。

    workspace_fp 是 normalized cwd 的 SHA-256 截断（不含原始路径）；
    label 只作 UI。绝不持久化 exact key/PID/HWND/RuntimeId。
    """
    kind: str | None = None
    source: str | None = None
    workspace_fp: str | None = None
    label: str = ""

    @staticmethod
    def workspace_fingerprint(cwd: str) -> str:
        normalized = str(cwd or "").strip().replace("\\", "/").rstrip("/").lower()
        if not normalized:
            return ""
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]

    def matches(self, target: AgentTarget) -> bool:
        inst = target.instance
        if self.kind and inst.kind.value != self.kind:
            return False
        if self.source and inst.source != self.source:
            return False
        if self.workspace_fp and AgentSelector.workspace_fingerprint(
                inst.cwd) != self.workspace_fp:
            return False
        return True


def _kind_value(kind) -> str:
    return kind.value if isinstance(kind, AgentKind) else str(kind)


class PresentationController:
    """呈现控制器：消费 targets，产出 PresentationState。

    不持有 Monitor；不持久化任何 runtime key（include/exclude/slot
    绑定都是运行期状态，重启后靠语义 selector 重新认领）。
    """

    def __init__(self, config=None):
        self.config = config
        self._focused_key = ""
        self._included_keys: set[str] = set()
        self._excluded_keys: set[str] = set()
        # fleet 运行期 slot → agent_key
        self._slot_bindings: dict[str, str] = {}
        self._slot_vacant_reason: dict[str, str] = {}
        self._slot_auto: set[str] = set()   # 自动分配（非用户手动绑定）
        # DONE special 动画状态（§6.5）
        self._special_until = 0.0
        self._done_seen: dict[str, float] = {}
        self._last_signature: tuple = ()

    # ------------------------------------------------------------ 配置读取
    def _cfg(self, path, default):
        try:
            return self.config.get(path, default)
        except Exception:
            return default

    @property
    def concurrent_enabled(self) -> bool:
        return bool(self._cfg("presentation.concurrent.enabled", False))

    @property
    def mode(self) -> PresentationMode:
        if not self.concurrent_enabled:
            return PresentationMode.SINGLE
        raw = str(self._cfg("presentation.concurrent.mode", "aggregate"))
        if raw == "fleet":
            return PresentationMode.FLEET
        return PresentationMode.AGGREGATE

    def max_targets(self) -> int:
        try:
            value = int(self._cfg("presentation.concurrent.max_targets", 3))
        except (TypeError, ValueError):
            value = 3
        return max(MAX_TARGETS_MIN, min(MAX_TARGETS_MAX, value))

    def eligible_kinds(self) -> set[str]:
        raw = self._cfg("presentation.concurrent.eligible_kinds", None)
        if isinstance(raw, dict):
            return {str(k) for k, v in raw.items() if v}
        return {k.value for k in AgentKind}

    def slot_ids(self) -> list[str]:
        """Fleet slot 列表：配置 slots 优先，不足 max_targets 时自动补齐
        pet-N（没有绑定的 slot 不会产生桌宠窗口）。"""
        slots = self._cfg("presentation.concurrent.slots", [])
        ids: list[str] = []
        if isinstance(slots, list):
            ids = [str(s.get("id", "")) for s in slots
                   if isinstance(s, dict) and s.get("id")]
        want = self.max_targets()
        i = 1
        while len(ids) < want:
            cand = f"pet-{i}"
            if cand not in ids:
                ids.append(cand)
            i += 1
            if i > want + 16:
                break
        return ids[:max(want, 1)] if want <= len(ids) else ids

    def slot_selector(self, slot_id: str) -> AgentSelector | None:
        slots = self._cfg("presentation.concurrent.slots", [])
        if not isinstance(slots, list):
            return None
        for slot in slots:
            if isinstance(slot, dict) and slot.get("id") == slot_id:
                sel = slot.get("selector")
                if isinstance(sel, dict):
                    return AgentSelector(
                        kind=sel.get("kind"),
                        source=sel.get("source"),
                        workspace_fp=sel.get("workspace_fp"),
                        label=sel.get("label", ""))
        return None

    # ------------------------------------------------------------ focused
    @property
    def focused_key(self) -> str:
        return self._focused_key

    def set_focus(self, agent_key: str | None) -> None:
        """用户明确选择；None/空 = 清除。不持久化。"""
        self._focused_key = str(agent_key or "")

    # ------------------------------------------------------------ include
    def set_instance_included(self, agent_key: str, included: bool) -> None:
        """运行期手动加入/移出并发展示（runtime state，不写配置）。"""
        if included:
            self._included_keys.add(agent_key)
            self._excluded_keys.discard(agent_key)
        else:
            self._excluded_keys.add(agent_key)
            self._included_keys.discard(agent_key)

    def instance_included(self, agent_key: str) -> bool | None:
        if agent_key in self._included_keys:
            return True
        if agent_key in self._excluded_keys:
            return False
        return None

    def _prune_runtime_keys(self, live_keys: set[str]) -> None:
        """Agent 退出：include/exclude/slot 绑定立即失效。"""
        self._included_keys &= live_keys
        self._excluded_keys &= live_keys
        for slot_id in list(self._slot_bindings):
            if self._slot_bindings[slot_id] not in live_keys:
                self._slot_bindings.pop(slot_id, None)
                self._slot_auto.discard(slot_id)
                self._slot_vacant_reason[slot_id] = "agent-exited"
        if self._focused_key and self._focused_key not in live_keys:
            self._focused_key = ""
        for key in list(self._done_seen):
            if key not in live_keys:
                self._done_seen.pop(key, None)

    # ------------------------------------------------------------ slots
    def bind_slot(self, slot_id: str, agent_key: str) -> bool:
        """手动绑定 exact Agent 到 slot；同 key 已被其他 slot 绑定 → 拒绝。"""
        for other, key in self._slot_bindings.items():
            if key == agent_key and other != slot_id:
                return False
        self._slot_bindings[slot_id] = agent_key
        self._slot_auto.discard(slot_id)
        self._slot_vacant_reason.pop(slot_id, None)
        return True

    def unbind_slot(self, slot_id: str) -> None:
        self._slot_bindings.pop(slot_id, None)
        self._slot_auto.discard(slot_id)

    def slot_binding(self, slot_id: str) -> str:
        return self._slot_bindings.get(slot_id, "")

    def is_auto_bound(self, slot_id: str) -> bool:
        return slot_id in self._slot_auto

    def _reclaim_slots(self, targets: dict[str, AgentTarget]) -> None:
        """重启后语义 selector 认领：0→vacant；1→自动绑；>1→不猜（§8.4）。

        唯一命中属于自动认领，必须标记 _slot_auto——否则 UI 会把
        自动 reclaim 误显示成"手动绑定"（v4.1.1 §12.1）。
        """
        for slot_id in self.slot_ids():
            if slot_id in self._slot_bindings:
                continue
            selector = self.slot_selector(slot_id)
            if selector is None:
                continue
            candidates = [t for t in targets.values() if selector.matches(t)]
            taken = set(self._slot_bindings.values())
            candidates = [t for t in candidates if t.key not in taken]
            if len(candidates) == 1:
                self._slot_bindings[slot_id] = candidates[0].key
                self._slot_auto.add(slot_id)
                self._slot_vacant_reason.pop(slot_id, None)
            elif len(candidates) > 1:
                self._slot_vacant_reason[slot_id] = "ambiguous-selector"

    def _auto_bind_slots(self, targets: dict[str, AgentTarget]) -> None:
        """自动分配：把未占用的 slot 填上候选 Agent（注意力优先）。

        先释放"已不是候选"的绑定（Agent 被移出并发 / 类型不再 eligible），
        再按注意力顺序补空位。用户手动绑定优先于自动分配，但同样遵守
        候选集（移出并发的 Agent 不保留桌宠）。Agent 退出释放 slot 后，
        下一轮 reconcile 自动补位。
        """
        candidates = self.candidate_keys(targets)
        cand_set = set(candidates)
        for slot_id in list(self._slot_bindings):
            key = self._slot_bindings[slot_id]
            if key not in cand_set:
                self._slot_bindings.pop(slot_id, None)
                self._slot_auto.discard(slot_id)
                if key in targets:
                    self._slot_vacant_reason[slot_id] = "agent-excluded"
        bound = set(self._slot_bindings.values())
        for slot_id in self.slot_ids():
            if slot_id in self._slot_bindings:
                continue
            pick = ""
            for key in candidates:
                if key not in bound:
                    pick = key
                    break
            if not pick:
                break
            self._slot_bindings[slot_id] = pick
            self._slot_auto.add(slot_id)
            self._slot_vacant_reason.pop(slot_id, None)
            bound.add(pick)

    # ------------------------------------------------------------ attention
    def _attention_rank(self, key: str, target: AgentTarget):
        snap = target.snapshot
        status = snap.status if snap is not None else Status.UNKNOWN
        focused_bias = 0 if key == self._focused_key else 1
        return (_ATTENTION_PRIORITY.get(status, 9), focused_bias,
                -float(getattr(target.instance, "started_at", 0.0) or 0.0),
                key)

    def attention_key(self, targets: dict[str, AgentTarget]) -> str:
        if not targets:
            return ""
        return sorted(targets, key=lambda k: self._attention_rank(k, targets[k]))[0]

    # ------------------------------------------------------------ 主入口
    def candidate_keys(self, targets: dict[str, AgentTarget]) -> list[str]:
        """eligible + included - excluded，按 focused → attention → 稳定序。"""
        eligible = self.eligible_kinds()
        candidates = []
        for key, target in targets.items():
            included = self.instance_included(key)
            if included is True:
                candidates.append(key)
                continue
            if included is False:
                continue
            if _kind_value(target.instance.kind) in eligible:
                candidates.append(key)
        candidates.sort(key=lambda k: self._attention_rank(k, targets[k]))
        if self._focused_key and self._focused_key in candidates:
            candidates.remove(self._focused_key)
            candidates.insert(0, self._focused_key)
        return candidates

    def reconcile(self, targets: dict[str, AgentTarget],
                  now: float) -> PresentationState:
        """每轮 UI tick 调用：产出呈现事实（不做任何 I/O）。"""
        self._prune_runtime_keys(set(targets))
        state = PresentationState(mode=self.mode)
        state.focused_key = self._focused_key
        state.attention_key = self.attention_key(targets)

        if state.mode is PresentationMode.SINGLE:
            # 单目标兼容：focused 优先，否则 attention
            pick = self._focused_key if self._focused_key in targets else ""
            if not pick:
                pick = state.attention_key
            state.focused_key = pick
            self._focused_key = pick
        elif state.mode is PresentationMode.AGGREGATE:
            candidates = self.candidate_keys(targets)
            cap = self.max_targets()
            selected = candidates[:cap]
            state.overflow_count = max(0, len(candidates) - len(selected))
            state.cards = tuple(
                self._card(targets[k], state.attention_key) for k in selected)
            # 气泡与单个监听一致：显示 focused（无则 attention）的单卡
            pick = self._focused_key if self._focused_key in selected else ""
            if not pick:
                pick = (state.attention_key
                        if state.attention_key in selected else "")
            state.focused_key = pick
        else:   # FLEET
            self._reclaim_slots(targets)
            self._auto_bind_slots(targets)
            for slot_id in self.slot_ids():
                key = self._slot_bindings.get(slot_id, "")
                if key:
                    state.slot_keys[slot_id] = key
                elif self._slot_vacant_reason.get(slot_id):
                    state.slot_vacant_reason[slot_id] = \
                        self._slot_vacant_reason[slot_id]
        self._last_signature = state.signature()
        return state

    def _card(self, target: AgentTarget, attention_key: str) -> AgentCardModel:
        from .labels import status_text
        snap = target.snapshot
        kind_label = snap.kind.label if snap is not None else "Agent"
        project = target.instance.project or target.instance.source
        accent = "#a06b38" if snap.status in (Status.WAITING, Status.INPUT) \
            else "#487f73" if snap.status == Status.DONE \
            else "#a06060" if snap.status == Status.ERROR \
            else "#487f73"
        if snap.status in (Status.WAITING, Status.INPUT):
            text = snap.waiting_detail or snap.summary or "等待处理"
        else:
            text = snap.summary or snap.goal or "等待新的任务"
        if target.instance.distro:
            footer = f"WSL {target.instance.distro}"
        else:
            footer = "Windows"
        if snap.stale:
            footer += " · 状态可能延迟"
        return AgentCardModel(
            agent_key=target.key,
            title=f"{kind_label} · {project}",
            status=status_text(snap),
            text=text,
            footer=footer,
            accent=accent,
            attention=(target.key == attention_key
                       and snap.status in (Status.WAITING, Status.INPUT,
                                          Status.ERROR)))

    # ------------------------------------------------------------ 动画状态（§6.5）
    def animation_state(self, state: PresentationState,
                        targets: dict[str, AgentTarget],
                        now: float) -> tuple[str, int]:
        """single/aggregate 模式：卡片 + focused 集合的动画状态。"""
        keys = [c.agent_key for c in state.cards] or (
            [state.focused_key] if state.focused_key else list(targets))
        return self.animation_state_for(keys, targets, now)

    def animation_state_for(self, keys: list[str],
                            targets: dict[str, AgentTarget],
                            now: float) -> tuple[str, int]:
        """按给定 key 集合计算动画状态（fleet per-slot 用）。

        WAITING/INPUT→die；ERROR→die；WORKING→walk；DONE（无更高注意）
        →special×3；否则 sleep。special 期间新的 WAITING/INPUT/ERROR
        立即抢占（v4plan §6.5 修正）。
        """
        active = [targets[k] for k in keys if k in targets]
        if not active:
            return "sleep", 0

        def worst(target: AgentTarget) -> int:
            status = target.snapshot.status if target.snapshot else Status.UNKNOWN
            return _ATTENTION_PRIORITY.get(status, 9)

        top = min(active, key=worst)
        status = top.snapshot.status if top.snapshot else Status.UNKNOWN
        if status in (Status.WAITING, Status.INPUT, Status.ERROR):
            self._special_until = 0.0    # 抢占 special
            return "die", 0
        if status == Status.WORKING:
            return "walk", 0
        if status == Status.DONE:
            in_special = now < self._special_until
            if not in_special:
                fresh = now - (top.snapshot.ts or 0) < 10
                if now - self._done_seen.get(top.key, 0) > 12 and fresh:
                    self._done_seen[top.key] = now
                    self._special_until = now + 12
                    return "special", 3
            return "walk", 0
        return "sleep", 0

    @property
    def special_until(self) -> float:
        return self._special_until
