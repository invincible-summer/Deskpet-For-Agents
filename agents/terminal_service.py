"""WindowsTerminalService：观察、解析、激活的统一 facade（v4plan §5.8）。

内部组合 UiaBackend / TerminalObserver / TerminalResolver /
TerminalActivator，外部（Monitor）不再分别管理两套终端生命周期。

exact activation 事务（v4plan §5.9，顺序固定、fail-closed）：
  1. 再确认 exact agent_key 仍 live；否则 AGENT_GONE
  2. binding 必须存在；否则 NO_BINDING
  3. CONFIRMED/HIGH 才允许 exact activate；否则 AMBIGUOUS
  4. 校验 WindowIdentity（IsWindow + PID + create_time + class）
  5. 失效时只允许一次 topology refresh/re-resolve；仍失败 STALE_WINDOW
  6. 在 exact HWND 下枚举 TabItem，用 tab_id 定位（绝不 title/index 猜）
  7. SelectionItemPattern.Select()
  8. 验证目标 Tab selected
  9. 重新发现该 Tab 的 TermControl(s)
 10. pane revalidation：stored pane 存在→exact；缺失且唯一→sole-pane
     safe rebind；缺失且多个→STALE_PANE（不猜）
 11. restore + SetForegroundWindow；foreground 成功才 pane SetFocus()
 12. OS 拒绝 foreground → FlashWindowEx + FOREGROUND_DENIED（不绕过）
"""
from __future__ import annotations

import time
from typing import Callable

from actions import winkeys
from .models import (
    ActivationCode,
    ActivationResult,
    AgentTarget,
    BindingConfidence,
    TerminalBinding,
    TerminalLocation,
)
from .terminal_uia import TerminalObserver, TerminalResolver


class TerminalActivator:
    """exact Window → Tab → Pane 激活事务（每次调用完整走一遍校验）。"""

    def __init__(self, service: "WindowsTerminalService"):
        self._service = service

    def activate(self, target: AgentTarget,
                 is_agent_live: Callable[[str], bool]) -> ActivationResult:
        # 1. Agent 生命周期复核（get_target 之后仍可能退出）
        if not is_agent_live(target.key):
            return ActivationResult(ActivationCode.AGENT_GONE)
        observer = self._service.observer
        backend = self._service.backend
        if observer is None or backend is None or not backend.available:
            return ActivationResult(ActivationCode.UIA_UNAVAILABLE,
                                    detail="uia backend unavailable")

        # 2/3. 绑定与置信度门槛
        binding = self._service.current_binding(target.key)
        if binding is None or not binding.hwnd:
            return ActivationResult(ActivationCode.NO_BINDING)
        if binding.confidence not in (BindingConfidence.CONFIRMED,
                                      BindingConfidence.HIGH):
            return ActivationResult(ActivationCode.AMBIGUOUS,
                                    detail=f"binding={binding.confidence.value}")

        result = self._activate_with_binding(target, binding, refresh=False)
        if result.code == ActivationCode.OK:
            return result
        if result.code in (ActivationCode.STALE_WINDOW, ActivationCode.STALE_TAB):
            # 5. 任一身份失效只允许一次 refresh/re-resolve
            refreshed = self._service.refresh_topology()
            if refreshed:
                if not is_agent_live(target.key):
                    return ActivationResult(ActivationCode.AGENT_GONE)
                binding2 = self._service.current_binding(target.key)
                if binding2 is None or not binding2.hwnd:
                    return ActivationResult(ActivationCode.NO_BINDING,
                                            repaired=True)
                if binding2.confidence not in (BindingConfidence.CONFIRMED,
                                               BindingConfidence.HIGH):
                    return ActivationResult(ActivationCode.AMBIGUOUS,
                                            repaired=True,
                                            detail=f"binding={binding2.confidence.value}")
                retry = self._activate_with_binding(target, binding2,
                                                    refresh=True)
                return ActivationResult(retry.code, repaired=True,
                                        detail=retry.detail)
        return result

    def _activate_with_binding(self, target: AgentTarget,
                               binding: TerminalBinding,
                               refresh: bool) -> ActivationResult:
        backend = self._service.backend
        # 4. WindowIdentity 校验（HWND/PID 复用防御）
        identity = binding.window_identity()
        if not winkeys.validate_window(identity):
            return ActivationResult(ActivationCode.STALE_WINDOW)
        hwnd = binding.hwnd

        # 6. exact Tab 定位（无 tab_id 时只做 Window+sole-pane 检查）
        tab_id = binding.tab_id
        if tab_id:
            layout = self._service.layout()
            tab = layout.tabs.get(tab_id) if layout is not None else None
            if tab is None or tab.hwnd != hwnd:
                return ActivationResult(ActivationCode.STALE_TAB)
            # 7. SelectionItemPattern.Select()（官方语义：清除其他选择）
            if not backend.select_tab(tab_id):
                return ActivationResult(ActivationCode.UIA_UNAVAILABLE,
                                        detail="select failed")
            # 8. 验证目标 Tab 已 selected
            selected = backend.selected_tab(hwnd)
            if selected is None or selected.tab_id != tab_id:
                return ActivationResult(ActivationCode.STALE_TAB,
                                        detail="tab not selected after select")

        # 9. 重新发现该 Tab 当前的 TermControl(s)（Select 后才 attach）
        layout = self._service.refresh_panes_now()
        panes = [p for p in (layout.panes.values() if layout else [])
                 if p.hwnd == hwnd and (not tab_id or p.tab_id == tab_id)]
        if not panes:
            return ActivationResult(ActivationCode.STALE_PANE,
                                    detail="no live term controls")

        # 10. pane revalidation
        pane = None
        if binding.pane_id is not None:
            pane = next((p for p in panes if p.pane_id == binding.pane_id),
                        None)
        if pane is None:
            if len(panes) == 1:
                # sole-pane safe rebind（v4plan §5.6）：唯一 pane 才允许
                pane = panes[0]
                self._service.note_pane_rebind(target.key, pane)
            else:
                return ActivationResult(
                    ActivationCode.STALE_PANE,
                    detail=f"{len(panes)} live panes, stored pane gone")

        # 11. restore + foreground（OS policy 决定成败，不绕过）
        winkeys.restore_window(hwnd)
        if not winkeys.try_set_foreground(hwnd):
            # 12. Tab 已选对，但前台被拒：Flash 提醒
            winkeys.flash_window(hwnd)
            return ActivationResult(ActivationCode.FOREGROUND_DENIED)
        # foreground 成功后 pane SetFocus
        backend.focus_pane(pane.pane_id)
        return ActivationResult(ActivationCode.OK)


class WindowsTerminalService:
    """持有同一个 UIA backend 的观察/解析/激活 facade。"""

    def __init__(self, observer: TerminalObserver | None,
                 resolver: TerminalResolver | None = None,
                 cfg: dict | None = None):
        self.observer = observer
        self.resolver = resolver or TerminalResolver()
        self.cfg = dict(cfg or {})
        self.activator = TerminalActivator(self)
        self.failed = observer is None
        # 绑定结果缓存（Monitor 线程写入，activate 时读取）
        self._bindings: dict[str, TerminalBinding] = {}
        self._last_refresh = 0.0

    # ------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        if self.observer is None:
            self.failed = True
            return False
        try:
            ok = self.observer.start()
        except Exception:
            ok = False
        self.failed = not ok
        return ok

    def stop(self):
        if self.observer is not None:
            try:
                self.observer.stop()
            except Exception:
                pass

    @property
    def backend(self):
        return self.observer.backend if self.observer is not None else None

    def available(self) -> bool:
        backend = self.backend
        return bool(self.observer is not None and not self.failed
                    and backend is not None and backend.available)

    def startup_error(self) -> str:
        if self.observer is None:
            return ""
        return str(getattr(self.observer.backend, "startup_error", "") or "")

    # ------------------------------------------------------------ Monitor 每轮
    def poll(self, now: float) -> None:
        if self.observer is not None:
            try:
                self.observer.poll(now)
            except Exception:
                pass

    def panes(self) -> dict:
        return self.observer.panes if self.observer is not None else {}

    def layout(self):
        return self.observer.layout if self.observer is not None else None

    def resolve(self, instances: list, now: float) -> dict[str, TerminalBinding]:
        try:
            bindings = self.resolver.resolve(
                list(instances), self.panes(), now, layout=self.layout())
        except Exception:
            bindings = {}
        self._bindings = bindings
        return bindings

    def current_binding(self, key: str) -> TerminalBinding | None:
        return self._bindings.get(key)

    def note_pane_rebind(self, key: str, pane) -> None:
        """sole-pane safe rebind 后更新运行期绑定（不落盘）。"""
        binding = self._bindings.get(key)
        if binding is not None:
            binding.pane_id = pane.pane_id
            binding.tab_id = pane.tab_id or binding.tab_id
            binding.validated_at = time.time()
        # manual location 同步 pane（tab/window 不变，只有 pane 换代）
        manual = self.resolver.manual_location(key)
        if manual is not None and pane.pane_id != manual.pane_id:
            from dataclasses import replace
            self.resolver.set_manual_location(key, replace(
                manual, pane_id=pane.pane_id))

    def refresh_topology(self) -> bool:
        """一次强制 topology 重发现 + 手工绑定重验证。"""
        if self.observer is None:
            return False
        try:
            self.observer.refresh_panes(force=True)
            return True
        except Exception:
            return False

    def refresh_panes_now(self):
        if self.observer is None:
            return None
        try:
            self.observer.refresh_panes(force=True)
        except Exception:
            pass
        return self.observer.layout

    # ------------------------------------------------------------ 观察 API
    def waiting_observation(self, binding: TerminalBinding):
        if self.observer is None or binding.pane_id is None:
            return None
        return self.observer.waiting_observation(binding.pane_id)

    def activity_observation(self, binding: TerminalBinding, now: float,
                             grace: float):
        if self.observer is None or binding.pane_id is None:
            return None
        return self.observer.pane_activity_observation(
            binding.pane_id, now, grace)

    # ------------------------------------------------------------ 用户显式 action
    def activate(self, target: AgentTarget,
                 is_agent_live: Callable[[str], bool]) -> ActivationResult:
        return self.activator.activate(target, is_agent_live)

    def bind_focused_location(self, agent_key: str) -> TerminalLocation | None:
        """一次捕获当前焦点的 (Window, Tab, Pane) 并记为 MANUAL CONFIRMED。

        只在本应用运行期有效；Agent exit / Window identity 变化立即使其
        失效（resolver._prune_manual）。失败返回 None（fail-closed）。
        """
        backend = self.backend
        if backend is None:
            return None
        try:
            location = backend.focused_location()
        except Exception:
            return None
        if location is None or location.window.hwnd == 0:
            return None
        self.resolver.set_manual_location(agent_key, location)
        return location

    def drop_instance(self, agent_key: str) -> None:
        """Agent 退出级联：清 manual binding 与缓存绑定。"""
        self.resolver.set_manual_location(agent_key, None)
        self._bindings.pop(agent_key, None)

    # ------------------------------------------------------------ 诊断
    def stats(self) -> dict:
        out = {}
        if self.observer is not None:
            out.update(self.observer.stats)
            backend = getattr(self.observer.backend, "stats", None)
            if callable(backend):
                out.update(backend())
        out["terminal_available"] = self.available()
        return out
