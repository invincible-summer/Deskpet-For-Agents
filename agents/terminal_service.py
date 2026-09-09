"""WindowsTerminalService：观察 + 双解析 + window-only 激活的 facade
（v4.1.3 §8）。

内部组合 UiaBackend / TerminalObserver / TerminalWindowResolver /
TerminalObservationResolver。Monitor 只面对本模块，不再分别管理终端
生命周期，也不接触 UIA backend 的任何 control 级方法。

window-only 激活事务（plan §6.3，顺序固定、fail-closed、不依赖 UIA）：
  1. 再确认 exact agent_key 仍 live；否则 AGENT_GONE
  2. TerminalWindowBinding 存在且有 window；否则 NO_BINDING
  3. 校验 WindowIdentity（IsWindow + PID + create_time + class）
  4. 失效时只允许一次 topology refresh + re-resolve；仍失败 STALE_WINDOW
  5. restore_window（最小化时恢复）
  6. try_set_foreground；OS 拒绝 → FlashWindowEx + FOREGROUND_DENIED
     （不绕过系统 foreground policy，绝不输入注入）

绝不做：UIA Tab 切换、键盘/快捷键模拟、剪贴板注入——Windows
Terminal 没有稳定的公开"按 WT_SESSION 激活既有标签页"接口
（microsoft/terminal#19783，closed/not_planned）。
"""
from __future__ import annotations

import time
from typing import Callable

from actions import winkeys
from .models import (
    ActivationCode,
    ActivationResult,
    TerminalWindowBinding,
)
from .terminal_resolver import (
    TerminalObservationResolver,
    TerminalWindowResolver,
)
from .terminal_uia import TerminalObserver


class WindowsTerminalService:
    """持有同一个 UIA backend 的观察/解析/激活 facade。"""

    def __init__(self, observer: TerminalObserver | None,
                 window_resolver: TerminalWindowResolver | None = None,
                 observation_resolver: TerminalObservationResolver | None = None,
                 cfg: dict | None = None):
        self.observer = observer
        self.window_resolver = window_resolver or TerminalWindowResolver()
        self.observation_resolver = (
            observation_resolver or TerminalObservationResolver())
        self.cfg = dict(cfg or {})
        self.failed = observer is None
        self._stopped = False   # stop 终态：异步 boot 晚到的 start 拒绝
        # 绑定结果缓存（Monitor 线程写入，activate 时读取）
        self._window_bindings: dict[str, TerminalWindowBinding] = {}
        self._observation_bindings: dict = {}
        # 最近一次 resolve 的实例集合（activate 内 re-resolve 用）
        self._last_instances: list = []

    # ------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        if self._stopped:
            return False
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
        self._stopped = True
        if self.observer is not None:
            try:
                self.observer.stop()
            except Exception:
                pass

    @property
    def backend(self):
        return self.observer.backend if self.observer is not None else None

    def available(self) -> bool:
        """UIA 观察是否就绪（§8.4：startup 失败只作历史诊断，不永久
        gate 后续 ready 的 backend）。"""
        backend = self.backend
        return bool(self.observer is not None
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

    def observed_controls(self) -> dict:
        return self.observer.controls if self.observer is not None else {}

    def layout(self):
        return self.observer.layout if self.observer is not None else None

    def refresh_observed_controls(self, force: bool = False) -> bool:
        """一次观察拓扑重发现（HWND 失效/手工 rescan 场景）。"""
        if self.observer is None:
            return False
        try:
            self.observer.refresh_controls(force=force)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------ 解析
    def resolve(self, instances: list, now: float) -> tuple:
        """双表解析：window binding + observation binding。

        返回 (window_bindings, observation_bindings)；失败返回空表
        （fail-closed，不抛出——Monitor 主循环不能被 UIA/Win32 异常打断）。
        """
        controls = self.observed_controls()
        screens = (self.observer.screen_texts()
                   if self.observer is not None else {})
        try:
            # Window 目录来自 enum_windows，不依赖 UIA layout（§8.1）；
            # UIA controls 只是 v3 WSL 标题 hint + 屏幕摘要证据。
            window_bindings = self.window_resolver.resolve(
                list(instances), controls, now, screens=screens)
        except Exception:
            window_bindings = {}
        try:
            observation_bindings = self.observation_resolver.resolve(
                list(instances), controls, window_bindings, now,
                layout=self.layout())
        except Exception:
            observation_bindings = {}
        self._last_instances = list(instances)
        self._window_bindings = window_bindings
        self._observation_bindings = observation_bindings
        return window_bindings, observation_bindings

    def current_window_binding(
            self, agent_key: str) -> TerminalWindowBinding | None:
        return self._window_bindings.get(agent_key)

    def observation_binding(self, agent_key: str):
        return self._observation_bindings.get(agent_key)

    # ------------------------------------------------------------ 观察 API
    def waiting_observation(self, control_id: tuple):
        if self.observer is None:
            return None
        return self.observer.waiting_observation(control_id)

    def activity_observation(self, control_id: tuple, now: float,
                             grace: float):
        if self.observer is None:
            return None
        return self.observer.control_activity_observation(
            control_id, now, grace)

    # ------------------------------------------------------------ 用户显式 action
    def activate(self, agent_key: str, *,
                 is_agent_live: Callable[[str], bool]) -> ActivationResult:
        """window-only 激活事务（§8.2；refresh 最多一次）。

        不看 confidence：CONFIRMED/HIGH/AMBIGUOUS/NONE 只要
        binding.window 存在，都走同一条 restore + foreground 链
        （wakeability 只由 window 决定，v4.1.3 §3.1）。
        """
        if not is_agent_live(agent_key):
            return ActivationResult(ActivationCode.AGENT_GONE)

        binding = self._window_bindings.get(agent_key)
        if binding is None or binding.window is None:
            return ActivationResult(
                ActivationCode.NO_BINDING,
                detail=binding.reason if binding is not None else "")

        if not winkeys.validate_window(binding.window):
            # 4. 只允许一次 refresh + re-resolve（不循环，§8.3）
            self.window_resolver.invalidate_window_cache()
            self.refresh_observed_controls(force=True)
            self._resolve_once()
            if not is_agent_live(agent_key):
                return ActivationResult(ActivationCode.AGENT_GONE)
            binding = self._window_bindings.get(agent_key)
            if binding is None or binding.window is None:
                return ActivationResult(
                    ActivationCode.NO_BINDING, repaired=True,
                    detail=binding.reason if binding is not None else "")
            if not winkeys.validate_window(binding.window):
                return ActivationResult(ActivationCode.STALE_WINDOW,
                                        repaired=True)
            # 5/6. refresh 后成功唤起：携带 repaired（v4.1.1 §19.2-C）
            hwnd = binding.hwnd
            winkeys.restore_window(hwnd)
            if winkeys.try_set_foreground(hwnd):
                return ActivationResult(ActivationCode.OK, repaired=True)
            winkeys.flash_window(hwnd)
            return ActivationResult(ActivationCode.FOREGROUND_DENIED)

        # 5/6. restore + foreground（OS policy 决定成败，不绕过）
        hwnd = binding.hwnd
        winkeys.restore_window(hwnd)
        if winkeys.try_set_foreground(hwnd):
            return ActivationResult(ActivationCode.OK)
        winkeys.flash_window(hwnd)
        return ActivationResult(ActivationCode.FOREGROUND_DENIED)

    def _resolve_once(self):
        instances = list(self._last_instances)
        if not instances:
            return
        try:
            self.resolve(
                [inst for inst in instances],
                time.time())
        except Exception:
            pass

    def drop_instance(self, agent_key: str) -> None:
        """Agent 退出级联：清运行期缓存绑定（不再有 manual path）。"""
        self._window_bindings.pop(agent_key, None)
        self._observation_bindings.pop(agent_key, None)
        self._last_instances = [inst for inst in self._last_instances
                                if getattr(inst, "key", "") != agent_key]

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
