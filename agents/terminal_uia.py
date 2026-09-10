"""Windows Terminal UI Automation 被观察层（v4.1.1 plan §7）。

架构：
  * UIA 调用全部在独立 MTA 线程（comtypes COINIT_MULTITHREADED），
    不放 Tk UI 线程，不拥有窗口；事件 handler 的添加/删除也在该线程
    （MS UIA threading guidance：同一非 UI/MTA 线程完成 add/remove）。
  * 事件驱动：TermControl 级 UIA Notification（2022 起携带实际新增文本
    payload）+ TermControl 级 TextChanged（debounce 后的有界审批 fallback）
    + 窗口级 StructureChanged（control 开/关、split 变化、active control
    attach/detach → 立即重发现，20s safety 仅为兜底）。
    不做 200ms 全树扫描。
  * 只读当前可见区域：TextPattern.GetVisibleRanges()，不读 scrollback。
  * 内存边界（plan §7.3）：delta ≤2048 / ring ≤8192 / visible ≤4096 /
    最多 16 个 control / 事件队列 ≤256 / UIA 命令队列 ≤32（满即失败
    不排队）/ 可见读取全局 ≤6/s、单 control ≥0.5s 间隔
    （等待复检 0.75s 独立通道）。
  * 终端文本是不可信数据：只做字符串匹配与状态归类，绝不执行、绝不
    写日志/配置/诊断文件。
  * UIA 审批观察带 TTL（1.5s）：等待期间每 0.75~1s 重读可见区域续期或
    清除，scrollback 里的旧审批文案不会造成永久 WAITING。
  * 订阅生命周期：control/窗口订阅记录在册，control 消失时在 MTA 线程
    正确 Remove*EventHandler 并释放强引用，长期 churn 不积累 COM handler。

v4.1.1 收敛（plan §7.2）：本模块只做被动观察。Tab topology（TabItem
枚举、SelectionItem 选择模式、标签切换/验证与 pane 焦点控制）已全部
删除——Windows Terminal 没有稳定的公开"按 WT_SESSION 激活既有标签页"
接口（microsoft/terminal#19783，closed/not_planned），UIA Tab 选择是
fragile workaround，不再作为 DeskPet 的产品级承诺。
ObservedTerminalControl 是 UIA observer 的内部 control descriptor，
不是用户可绑定 Terminal Pane，不参与 window activation、不持久化
（plan §4.3）。
"""
from __future__ import annotations

import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable

from .matching import best_effort_scores, mutual_unique_matches
from .models import (
    AgentKind,
    Confidence,
    EvidenceSource,
    Observation,
    Phase,
    Status,
    WindowIdentity,
)

# ------------------------------------------------------------- 常量（代码级安全上限）

DELTA_MAX = 2048          # 单条事件 delta 上限
RING_MAX = 8192           # 每 control ring buffer 字符上限
VISIBLE_MAX = 4096        # 可见快照字符上限
MAX_CONTROLS = 16         # 最多监听的 TermControl 数
EVENT_QUEUE_MAX = 256     # 事件队列上限（满时丢最旧）
UIA_CALL_QUEUE_MAX = 32   # UIA 命令队列上限（满时立即失败，不排队积压）
APPROVAL_TTL = 1.5        # 审批观察有效期（秒）
RECHECK_SEC = 0.75        # 等待期间的可见区域复检间隔
REDISCOVER_SEC = 20.0     # control 重新发现周期（safety refresh；正常由
                          # StructureChanged 立即触发）
TEXT_CHANGED_DEBOUNCE = 0.15        # TextChanged → 可见读取的 debounce
CONTROL_VISIBLE_READ_MIN_INTERVAL = 0.5   # 单 control 可见读取最小间隔
GLOBAL_VISIBLE_READ_LIMIT = 6         # 全局可见读取预算（次/秒）
# 可见读取 broker（v4.2.3 §5.1）：所有生产读取经统一入口调度；
# 每次 poll 最多 3 次真实 read，避免一次 poll 被同步 UIA 调用拖长。
MAX_VISIBLE_READS_PER_POLL = 3
# 订阅瞬时失败重试（v4.2.3 §5.2）：topology 不变时对当前 controls
# 重试一次 sync；健康状态下零额外 UIA command。
SUBSCRIPTION_RETRY_SEC = 2.0
# 屏幕摘要通道（v4.1.4）：窗口候选评分用的每 control 最近一次可见读取。
# 只存内存、绝不持久化/展示/写日志；事件驱动的读取顺带更新，
# 缺失/过期才补读，且与审批通道共享同一套读取预算。
SCREEN_DIGEST_STALE_SEC = 30.0       # 无事件时的最长可信时间
SCREEN_DIGEST_MIN_INTERVAL = 2.0     # 单 control 摘要补读最小间隔
SCREEN_DIGEST_PER_POLL = 2           # 每次 poll 最多补读的 control 数
WEAK_TRIGGER_RE = re.compile(
    r"(would you like|do you want|approve|approval|permission|permissions|"
    r"proceed|don't ask|tell (?:codex|claude|kimi)|waiting for|confirm|"
    r"\(esc\)|\(y/n\)|yes/no)", re.IGNORECASE)

# UIA 事件/属性常量（头文件 #define，不在 typelib）
UIA_TEXT_TEXTCHANGED_EVENT = 20015
UIA_TEXT_PATTERN_ID = 10014
UIA_CLASSNAME_PROPERTY_ID = 30012
TREE_SCOPE_ELEMENT = 1
TREE_SCOPE_DESCENDANTS = 4
WT_WINDOW_CLASS = "CASCADIA_HOSTING_WINDOW_CLASS"


@dataclass(frozen=True)
class ObservedTerminalControl:
    """UIA observer 内部的一个 TermControl descriptor（v4.1.1 §4.3）。

    control_id 是 (hwnd, runtime_id) 的短生命周期 UIA 句柄：仅内存、
    仅 UIA thread / observer 使用、RuntimeId 失效后即废弃。它不是用户
    可绑定的 Terminal Pane，不参与 window activation，不持久化，
    不在 Dashboard 普通诊断展示。
    """
    control_id: tuple          # (hwnd, runtime_id tuple)
    hwnd: int
    window_pid: int
    title: str                 # TermControl 的 UIA Name（观察证据）
    window_class: str = WT_WINDOW_CLASS


@dataclass
class TerminalLayout:
    """一轮 Windows Terminal 观察拓扑发现结果（v4.1.1：仅窗口 + control）。

    只有当前 selected Tab 的 TermControl 会出现在 XAML root 里
    （Windows Terminal TabManagement.cpp：选中变化才 attach terminal
    control），因此 controls 只覆盖当前可观察的 TermControl——
    绝不后台切换 Tab 补全。
    """
    windows: dict[int, WindowIdentity]                # hwnd → identity
    controls: dict[tuple, ObservedTerminalControl]    # control_id → control

    def __bool__(self) -> bool:
        return bool(self.windows)


@dataclass
class TerminalEvent:
    control_id: tuple
    kind: str        # notification / activity / structure
    text: str = ""   # notification 的 delta（有界）
    ts: float = 0.0


# ------------------------------------------------------------- 订阅生命周期

class SubscriptionTracker:
    """纯数据订阅账本：sync() 得出需新增/移除的键，不直接碰 COM。

    与 UiaBackend 解耦以便对 control churn 做确定性测试。
    """

    def __init__(self):
        self.active: set = set()

    def sync(self, wanted: set) -> tuple[list, list]:
        to_add = [k for k in wanted if k not in self.active]
        to_remove = [k for k in self.active if k not in wanted]
        self.active = set(wanted)
        return to_add, to_remove

    def discard(self, key) -> None:
        self.active.discard(key)


@dataclass
class ControlSubscription:
    """单个 TermControl 的订阅记录：哪个 handler 注册了哪类事件。"""
    element: object
    handler: object
    notification_registered: bool = False
    text_changed_registered: bool = False


@dataclass
class WindowSubscription:
    """Windows Terminal 顶层窗口的事件订阅记录。

    只订阅 StructureChanged（control 开合/attach/detach → 立即重发现），
    按 TREE_SCOPE_DESCENDANTS 在窗口 root 上注册。
    """
    root: object
    handler: object
    registered: bool = False


# ------------------------------------------------------------- Backend 协议

class TerminalBackend:
    """UIA 后端抽象；FakeBackend 用于无 Windows Terminal 的测试。"""

    available: bool = False

    def start(self) -> bool:
        return False

    def stop(self):
        pass

    def discover_layout(self) -> "TerminalLayout | None":
        """观察拓扑发现（必须实现；返回 None 表示本轮失败/不可用）。"""
        return None

    def read_visible(self, control_id: tuple) -> str:
        return ""

    def sync_control_subscriptions(self, controls: list):
        """默认实现为空；真实后端在唯一 MTA 线程完成 add/remove。"""

    def subscription_retry_needed(self) -> bool:
        """订阅是否存在瞬时失败、需要 topology 不变时的重试。

        v4.2.3 §5.2：默认 False（健康/不可重试）。真实后端在全部
        期望订阅覆盖后自动清除。
        """
        return False


class UiaCall:
    """一次封送到 MTA 线程的 UIA 调用；支持超时取消与异常回传。"""

    __slots__ = ("fn", "done", "result", "error", "cancelled")

    def __init__(self, fn: Callable):
        self.fn = fn
        self.done = threading.Event()
        self.result = None
        self.error: Exception | None = None
        self.cancelled = False


class UiaBackend(TerminalBackend):
    """comtypes 实现：独立 MTA 线程，所有 UIA 调用经该线程执行。

    依据（2026-09 查证）：
      * CUIAutomation8 CLSID {E22AD333-...}，请求 IUIAutomation5 以获得
        AddNotificationEventHandler（Win10 1709+）。
      * TermControl 类名固定（"IMPORTANT: Do NOT change"），ControlType
        = Text，仅支持 TextPattern。
      * 事件常量是头文件 #define，不在 typelib。
      * COM callback 用 comtypes.COMObject 子类（NVDA 模式）。
      * StructureChanged 按 WT 顶层窗口 root 注册（TreeScope_Descendants），
        control 开合立即触发重发现，不等 20s safety。
    """
    available = False

    def __init__(self):
        self._queue: "queue.Queue[UiaCall]" = queue.Queue(maxsize=UIA_CALL_QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stopped = False   # stop 终态：晚到的 start 不得复活线程
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self.uia = None
        self.lib = None
        self._handler_cls = None
        self._control_elements: dict[tuple, object] = {}
        self._control_subscriptions: dict[tuple, ControlSubscription] = {}
        self._window_subscriptions: dict[int, WindowSubscription] = {}
        self._control_tracker = SubscriptionTracker()
        self._window_tracker = SubscriptionTracker()
        self.event_sink: Callable[[TerminalEvent], None] | None = None
        # 订阅瞬时失败标记（v4.2.3 §5.2）：全部期望订阅覆盖后自动清除
        self._subscription_retry = False
        # 诊断计数（不含任何终端文本）
        self.calls = 0
        self.errors = 0
        self.timeouts = 0
        self.queue_dropped = 0
        self.startup_error = ""

    def stats(self) -> dict:
        return {"uia_calls": self.calls, "uia_errors": self.errors,
                "uia_timeouts": self.timeouts,
                "uia_queue_dropped": self.queue_dropped,
                "uia_control_subs": len(self._control_subscriptions),
                "uia_window_subs": len(self._window_subscriptions),
                "uia_subscription_retry": int(self.subscription_retry_needed())}

    def subscription_retry_needed(self) -> bool:
        with self._lock:
            return self._subscription_retry

    def _mark_subscription_retry(self):
        with self._lock:
            self._subscription_retry = True

    # ---- MTA 线程 ----
    def _run(self):
        import comtypes
        import comtypes.client
        from comtypes import COMObject
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception as exc:
            self.startup_error = repr(exc)[:200]
            self._ready.set()
            return
        try:
            lib = comtypes.client.GetModule("UIAutomationCore.dll")
            uia = comtypes.client.CreateObject(
                lib.CUIAutomation8, interface=lib.IUIAutomation5)
        except Exception as exc:
            self.startup_error = repr(exc)[:200]
            self._ready.set()
            comtypes.CoUninitialize()
            return
        backend = self

        class Handlers(COMObject):
            _com_interfaces_ = [
                lib.IUIAutomationNotificationEventHandler,
                lib.IUIAutomationEventHandler,
                lib.IUIAutomationStructureChangedEventHandler,
            ]

            def IUIAutomationNotificationEventHandler_HandleNotificationEvent(
                    self, sender, notificationKind, notificationProcessing,
                    displayString, activityId):
                try:
                    text = str(displayString or "")[:DELTA_MAX]
                    backend._emit(backend._control_of(sender), "notification", text)
                except Exception:
                    backend.errors += 1

            def IUIAutomationEventHandler_HandleAutomationEvent(self, sender, eventId):
                try:
                    eid = int(eventId)
                    if eid == UIA_TEXT_TEXTCHANGED_EVENT:
                        backend._emit(backend._control_of(sender), "activity", "")
                except Exception:
                    backend.errors += 1

            def IUIAutomationStructureChangedEventHandler_HandleStructureChangedEvent(
                    self, sender, changeType, runtimeId):
                try:
                    backend._emit(None, "structure", "")
                except Exception:
                    backend.errors += 1

        self.lib, self.uia = lib, uia
        self._handler_cls = Handlers
        self.available = True
        self._ready.set()
        if self._stop.is_set():
            # 初始化期间收到 stop：直接拆线退出，不进入服务循环
            self._teardown()
            comtypes.CoUninitialize()
            return
        try:
            while not self._stop.is_set():
                try:
                    call = self._queue.get(timeout=0.25)
                except queue.Empty:
                    continue
                if call.cancelled:
                    continue
                try:
                    call.result = call.fn()
                except Exception as exc:
                    call.error = exc
                    self.errors += 1
                finally:
                    call.done.set()
        finally:
            self._teardown()
            comtypes.CoUninitialize()

    def _teardown(self):
        # handler 的移除与添加在同一 MTA 线程（MS threading guidance）。
        try:
            for control_id, sub in list(self._control_subscriptions.items()):
                self._remove_control_subscription(sub)
                self._control_subscriptions.pop(control_id, None)
            for hwnd, sub in list(self._window_subscriptions.items()):
                self._remove_window_subscription(sub)
                self._window_subscriptions.pop(hwnd, None)
            self._control_tracker.active.clear()
            self._window_tracker.active.clear()
            self._control_elements.clear()
        except Exception:
            pass
        self.available = False

    def _control_of(self, sender) -> tuple | None:
        try:
            rid = tuple(int(x) for x in sender.GetRuntimeId())
        except Exception:
            return None
        for control_id, _el in self._control_elements.items():
            if control_id[1] == rid:
                return control_id
        return None

    def _emit(self, control_id: tuple | None, kind: str, text: str):
        sink = self.event_sink
        if sink is None:
            return
        sink(TerminalEvent(control_id=control_id or (), kind=kind, text=text,
                           ts=time.time()))

    # ---- 对外（任意线程调用，封送到 MTA 线程执行） ----
    def start(self) -> bool:
        if self._stopped:
            return False   # 已停止：异步 boot 晚到也不复活（plan §18）
        if self._thread is not None:
            return self.available
        try:
            # comtypes 首次 import 时会在导入线程做一次 STA CoInitialize；
            # 必须在启动 MTA 线程之前于调用线程完成导入，否则 MTA 线程内的
            # CoInitializeEx(MULTITHREADED) 会因线程模式已定而失败
            # （WinError -2147417850）。
            import comtypes  # noqa: F401
        except Exception as exc:
            self.startup_error = repr(exc)[:200]
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="deskpet-uia-mta", daemon=True)
        self._thread.start()
        self._ready.wait(8.0)   # 首次生成 typelib 包装可能需要数秒；
        # 初始化失败时 _run 也会 set _ready，不会反复硬等 8s
        return self.available

    def request_stop(self) -> None:
        """只发停止信号：置终态 + stop event（DP43-R17：不 join）。

        _stopped 是终态标记：后续 start() 直接拒绝（plan §18）。
        """
        self._stopped = True
        self._stop.set()

    def join_for_shutdown(self, timeout: float = 3.0) -> bool:
        """有界回收 MTA 线程（timeout 来自 App 全局 deadline 的剩余量）。

        COM handler teardown 发生在 MTA owner 线程内（_run 的收尾），
        本方法只等待线程退出。
        """
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
            return not thread.is_alive()
        return True

    def stop(self, timeout: float = 3.0) -> bool:
        """兼容薄 wrapper：request_stop + bounded join（测试/旧入口）。"""
        self.request_stop()
        return self.join_for_shutdown(timeout)

    def _submit(self, fn, timeout: float = 2.0):
        if not self.available:
            return None
        call = UiaCall(fn)
        try:
            self._queue.put_nowait(call)
        except queue.Full:
            # UIA 线程卡住：立即失败，绝不排队积压
            self.queue_dropped += 1
            return None
        self.calls += 1
        if not call.done.wait(timeout):
            call.cancelled = True
            self.timeouts += 1
            return None
        return call.result

    # ---- 具体操作 ----
    def _find_elements(self, root, prop_id: int, value):
        cond = self.uia.CreatePropertyCondition(prop_id, value)
        found = root.FindAll(TREE_SCOPE_DESCENDANTS, cond)
        out = []
        for i in range(found.Length):
            out.append(found.GetElement(i))
        return out

    def _find_term_controls(self, root):
        return self._find_elements(root, UIA_CLASSNAME_PROPERTY_ID,
                                   "TermControl")

    @staticmethod
    def _runtime_id(el) -> tuple | None:
        try:
            return tuple(int(x) for x in el.GetRuntimeId())
        except Exception:
            return None

    def discover_layout(self) -> TerminalLayout:
        """发现全部 WT 窗口 + 当前可观察的 TermControl（observation-only）。

        物理限制（TabManagement.cpp）：只有 selected Tab 的 TermControl
        attach 在 XAML root；background Tab 只有 TabItem。绝不后台切换
        Tab 补全 control——StructureChanged 在用户自然切换时触发重发现。
        """
        from actions import winkeys

        def do():
            layout = TerminalLayout(windows={}, controls={})
            for hwnd, wpid, _title, cls in winkeys.enum_windows():
                if cls != WT_WINDOW_CLASS:
                    continue
                hwnd = int(hwnd)
                try:
                    root = self.uia.ElementFromHandle(hwnd)
                except Exception:
                    continue
                identity = winkeys.window_identity(hwnd)
                if identity is None:
                    continue
                layout.windows[hwnd] = identity
                for el in self._find_term_controls(root)[:MAX_CONTROLS]:
                    rid = self._runtime_id(el)
                    if rid is None:
                        continue
                    try:
                        name = str(el.CurrentName or "")
                    except Exception:
                        name = ""
                    control_id = (hwnd, rid)
                    self._control_elements[control_id] = el
                    layout.controls[control_id] = ObservedTerminalControl(
                        control_id=control_id, hwnd=hwnd,
                        window_pid=identity.pid, title=name)
                    if len(layout.controls) >= MAX_CONTROLS:
                        break
            # 清理已消失 control 的元素缓存
            live = set(layout.controls)
            for control_id in list(self._control_elements):
                if control_id not in live:
                    self._control_elements.pop(control_id, None)
            return layout
        return self._submit(do) or TerminalLayout(windows={}, controls={})

    def _resolve_element(self, control_id: tuple):
        """按 control_id 找 UIA element；缓存缺失时从窗口树重新解析。"""
        el = self._control_elements.get(control_id)
        if el is not None:
            return el
        if not control_id:
            return None
        hwnd = control_id[0]

        def find():
            try:
                root = self.uia.ElementFromHandle(hwnd)
            except Exception:
                return None
            for cand in self._find_term_controls(root):
                try:
                    if (hwnd, tuple(int(x) for x in cand.GetRuntimeId())) == control_id:
                        self._control_elements[control_id] = cand
                        return cand
                except Exception:
                    continue
            return None
        self._submit(find, timeout=1.5)
        return self._control_elements.get(control_id)

    def read_visible(self, control_id: tuple) -> str:
        def do():
            el = self._control_elements.get(control_id)
            if el is None:
                return ""
            try:
                punk = el.GetCurrentPattern(UIA_TEXT_PATTERN_ID)
                if not punk:
                    return ""
                tp = punk.QueryInterface(self.lib.IUIAutomationTextPattern)
                ranges = tp.GetVisibleRanges()
                parts = []
                for i in range(ranges.Length):
                    parts.append(str(ranges.GetElement(i).GetText(-1) or ""))
            except Exception:
                self.errors += 1
                return ""
            return "\n".join(parts)[:VISIBLE_MAX]
        if self._resolve_element(control_id) is None:
            return ""
        return self._submit(do) or ""

    # ---- 订阅生命周期（唯一 MTA 线程内完成 add/remove） ----
    def sync_control_subscriptions(self, controls: list):
        """按当前 control 集合同步订阅：新增订阅、消失退订，绝不积累。

        全部期望订阅覆盖后清除 retry 标记；任何 control/window 注册
        失败则置位，由 observer 在 topology 不变时定期重试（§5.2）。
        """
        def do():
            wanted = {c.control_id for c in controls}
            to_add, to_remove = self._control_tracker.sync(wanted)
            for control_id in to_remove:
                sub = self._control_subscriptions.pop(control_id, None)
                if sub is not None:
                    self._remove_control_subscription(sub)
            control_by_id = {c.control_id: c for c in controls}
            for control_id in to_add:
                self._subscribe_control(control_by_id[control_id])
            # 窗口级 StructureChanged：control 开合 → 立即触发重发现
            self._sync_window_subscriptions({c.hwnd for c in controls})
            # 只有 desired control/window trackers 全部覆盖才清 retry
            covered = self._subscriptions_covered(controls)
            with self._lock:
                self._subscription_retry = not covered
        self._submit(do)

    def _subscriptions_covered(self, controls: list) -> bool:
        """desired control/window 是否全部有活跃订阅（不含文本）。"""
        desired_controls = {c.control_id for c in controls}
        desired_hwnds = {c.hwnd for c in controls}
        return (all(cid in self._control_subscriptions
                    for cid in desired_controls)
                and all(hwnd in self._window_subscriptions
                        for hwnd in desired_hwnds))

    def _subscribe_control(self, control: ObservedTerminalControl):
        try:
            root = self.uia.ElementFromHandle(control.hwnd)
            for el in self._find_term_controls(root):
                try:
                    rid = tuple(int(x) for x in el.GetRuntimeId())
                except Exception:
                    continue
                if (control.hwnd, rid) != control.control_id:
                    continue
                self._control_elements[control.control_id] = el
                handler = self._handler_cls()
                sub = ControlSubscription(element=el, handler=handler)
                # Notification event（携带新增文本）
                try:
                    self.uia.AddNotificationEventHandler(
                        el, TREE_SCOPE_ELEMENT, None, handler)
                    sub.notification_registered = True
                except Exception:
                    pass
                # TextChanged（无 payload；作为 Notification 失效时的
                # 审批 fallback 触发与活动信号）
                try:
                    self.uia.AddAutomationEventHandler(
                        UIA_TEXT_TEXTCHANGED_EVENT, el,
                        TREE_SCOPE_ELEMENT, None, handler)
                    sub.text_changed_registered = True
                except Exception:
                    pass
                if sub.notification_registered or sub.text_changed_registered:
                    self._control_subscriptions[control.control_id] = sub
                else:
                    # 两类事件都注册失败：不标记为已订阅，标记重试
                    self._control_tracker.discard(control.control_id)
                    self._mark_subscription_retry()
                return
        except Exception:
            self.errors += 1
            self._control_tracker.discard(control.control_id)
            self._mark_subscription_retry()

    def _remove_control_subscription(self, sub: ControlSubscription):
        if sub.text_changed_registered:
            try:
                self.uia.RemoveAutomationEventHandler(
                    UIA_TEXT_TEXTCHANGED_EVENT, sub.element, sub.handler)
            except Exception:
                pass
        if sub.notification_registered:
            try:
                self.uia.RemoveNotificationEventHandler(sub.element, sub.handler)
            except Exception:
                pass

    def _sync_window_subscriptions(self, hwnds: set):
        to_add, to_remove = self._window_tracker.sync(set(hwnds))
        for hwnd in to_remove:
            sub = self._window_subscriptions.pop(hwnd, None)
            if sub is not None:
                self._remove_window_subscription(sub)
        for hwnd in to_add:
            try:
                root = self.uia.ElementFromHandle(hwnd)
                handler = self._handler_cls()
                sub = WindowSubscription(root=root, handler=handler)
                # StructureChanged：control 开合/attach/detach → 重发现
                try:
                    self.uia.AddStructureChangedEventHandler(
                        root, TREE_SCOPE_DESCENDANTS, None, handler)
                    sub.registered = True
                except Exception:
                    self.errors += 1
                    self._mark_subscription_retry()
                if sub.registered:
                    self._window_subscriptions[hwnd] = sub
                else:
                    self._window_tracker.discard(hwnd)
            except Exception:
                self._window_tracker.discard(hwnd)
                self._mark_subscription_retry()
                self.errors += 1

    def _remove_window_subscription(self, sub: WindowSubscription):
        if sub.registered:
            try:
                self.uia.RemoveStructureChangedEventHandler(sub.root, sub.handler)
            except Exception:
                pass


# ------------------------------------------------------------- 识别器

def _squash(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


class CodexTerminalRecognizer:
    """Codex TUI 审批 overlay（approval_overlay.rs 现行文案，2026-09）。

    要求：标题模式 + 选项结构同时出现在当前可见区域（不是 scrollback）。
    产出的 Observation 携带 agent_kind=CODEX（错误归属防护）。
    """
    KIND = AgentKind.CODEX
    HEADINGS = re.compile(
        r"would you like to (run the following command|make the following edits"
        r"|grant these permissions|send input|allow)"
        r"|do you want to approve network access"
        r"|needs your approval", re.IGNORECASE)
    OPTIONS = re.compile(
        r"yes, (just this once|proceed|and don't ask again|and allow|and provide)"
        r"|no, (and tell codex|continue without|but continue|and block)", re.IGNORECASE)

    def inspect(self, visible_text: str, delta_text: str, now: float) -> list[Observation]:
        text = _squash(visible_text)
        if not text:
            return []
        if self.HEADINGS.search(text) and self.OPTIONS.search(text):
            return [Observation(
                source=EvidenceSource.TERMINAL, timestamp=now,
                status=Status.WAITING, phase=Phase.APPROVAL,
                confidence=Confidence.HIGH,
                agent_kind=self.KIND,
                summary=self._detail(text),
                expires_at=now + APPROVAL_TTL,
            )]
        return []

    @staticmethod
    def _detail(text: str) -> str:
        if "network access" in text:
            return "网络访问需要确认"
        if "edits" in text:
            return "文件修改需要确认"
        if "permissions" in text:
            return "权限请求需要确认"
        if "send input" in text:
            return "终端输入需要确认"
        return "命令执行需要确认"


class ClaudeTerminalRecognizer:
    """Claude Code permission / plan 审批对话框。

    至少两个结构特征同时满足：permission 措辞 + Yes/No 选项结构，
    避免普通 assistant 文字里提到 "permission" 就误报。
    """
    KIND = AgentKind.CLAUDE
    WORDING = re.compile(
        r"do you want to (proceed|make this|allow|edit)"
        r"|would you like to proceed"
        r"|bash command"
        r"|wants to (edit|create|run)"
        r"|(command|file|edit|tool)s? (needs? )?(approval|permission)"
        r"|permission (request|to )", re.IGNORECASE)
    CHOICES = re.compile(
        r"yes, and don't ask again"
        r"|no, and tell claude"
        r"|\b1\.\s*yes\b.{0,40}\b2\.\s*yes\b"
        r"|\b2\.\s*no\b.{0,40}\(esc\)"
        r"|(yes|no) / (yes|no)", re.IGNORECASE)

    def inspect(self, visible_text: str, delta_text: str, now: float) -> list[Observation]:
        text = _squash(visible_text)
        if not text:
            return []
        if self.WORDING.search(text) and self.CHOICES.search(text):
            return [Observation(
                source=EvidenceSource.TERMINAL, timestamp=now,
                status=Status.WAITING, phase=Phase.APPROVAL,
                confidence=Confidence.HIGH,
                agent_kind=self.KIND,
                summary=self._detail(text),
                expires_at=now + APPROVAL_TTL,
            )]
        return []

    @staticmethod
    def _detail(text: str) -> str:
        if "edit" in text or "file" in text:
            return "文件修改需要确认"
        if "bash" in text or "command" in text or "run" in text:
            return "Bash 命令需要确认"
        return "操作需要确认"


class KimiTerminalRecognizer:
    """Kimi 兜底识别（Wire ApprovalRequest 是主来源，UIA 仅 fallback）。"""
    KIND = AgentKind.KIMI
    WORDING = re.compile(
        r"approve|approval|permission|确认|允许|批准|等待(计划|审批|确认)", re.IGNORECASE)
    CHOICES = re.compile(
        r"\b(yes|no)\b.{0,30}\b(yes|no)\b|\(esc\)|\(y/n\)|yes/no|确认|拒绝", re.IGNORECASE)

    def inspect(self, visible_text: str, delta_text: str, now: float) -> list[Observation]:
        text = _squash(visible_text)
        if not text:
            return []
        if self.WORDING.search(text) and self.CHOICES.search(text):
            return [Observation(
                source=EvidenceSource.TERMINAL, timestamp=now,
                status=Status.WAITING, phase=Phase.APPROVAL,
                confidence=Confidence.MEDIUM,
                agent_kind=self.KIND,
                summary="Kimi 等待确认",
                expires_at=now + APPROVAL_TTL,
            )]
        return []


DEFAULT_RECOGNIZERS = (
    CodexTerminalRecognizer(),
    ClaudeTerminalRecognizer(),
    KimiTerminalRecognizer(),
)


# ------------------------------------------------------------- 观察器

class VisibleReadReason(IntEnum):
    """可见读取的生产原因（v4.2.3 §5.1）；数值越小优先级越高。"""
    WAITING_RECHECK = 0     # WAITING TTL 复检（最紧急：审批语义）
    APPROVAL_TRIGGER = 1    # 弱触发 delta（Notification 审批文案）
    TEXT_FALLBACK = 2       # TextChanged debounce 到期的兜底读取
    SCREEN_DIGEST = 3       # 屏幕摘要缺失/过期补读


@dataclass
class _VisibleReadRequest:
    reason: VisibleReadReason
    delta: str = ""
    enqueued_at: float = 0.0


# _read_visible_once 的"暂时不可读"哨兵：限流/预算推迟（请求保留），
# 与 None（读取失败，请求丢弃）区分。
_READ_BUSY = object()


class TerminalObserver:
    """事件驱动的终端观察器（逻辑与 backend 解耦，可注入 FakeBackend）。

    可见读取统一 broker（v4.2.3 §5.1）：`backend.read_visible` 只有
    `_read_visible_once` 一个生产调用入口；所有通道（WAITING 复检、
    弱触发、TextChanged fallback、屏幕摘要）先登记 pending request，
    由 `_service_visible_reads` 在 poll 末按优先级统一消费，共用
    单 control ≥0.5s + 全局 ≤6/s 预算，每 poll 最多 3 次真实 read。
    """

    def __init__(self, backend: TerminalBackend,
                 recognizers=DEFAULT_RECOGNIZERS, cfg: dict | None = None):
        self.backend = backend
        self.recognizers = tuple(recognizers)
        self.cfg = dict(cfg or {})
        self.enabled = bool(self.cfg.get("terminal_observer", True))
        self.controls: dict[tuple, ObservedTerminalControl] = {}
        self.layout: TerminalLayout | None = None
        self.observations: dict[tuple, Observation] = {}
        self.activity: dict[tuple, float] = {}
        self.rings: dict[tuple, deque] = {}
        self._ring_len: dict[tuple, int] = {}
        self._events: deque = deque(maxlen=EVENT_QUEUE_MAX)  # UIA 线程 → 核心的有界队列
        self._event_q_lock = threading.Lock()
        self._waiting_recheck: dict[tuple, float] = {}
        # TextChanged debounce 审批 fallback（有界读取）
        self._dirty_controls: dict[tuple, float] = {}
        self._last_visible_read: dict[tuple, float] = {}
        self._visible_read_times: deque = deque()   # 全局预算滑动窗口
        # 统一可见读取 broker：每 control 最多 1 个 pending request
        self._pending_reads: dict[tuple, _VisibleReadRequest] = {}
        # 屏幕摘要（v4.1.4）：窗口候选评分证据，仅内存
        self._screens: dict[tuple, str] = {}
        self._screen_read_at: dict[tuple, float] = {}
        self._last_discover = 0.0
        self._structure_dirty = False
        self._started = False
        self._last_subscription_retry = 0.0
        # 诊断计数（不含任何终端文本）
        self.stats = {"events": 0, "dropped": 0, "visible_reads": 0,
                      "rediscoveries": 0, "triggers": 0,
                      "text_fallback_reads": 0, "screen_reads": 0,
                      "visible_reads_by_reason": {}, "pending_visible_reads": 0,
                      "subscription_retry_count": 0}
        backend.event_sink = self._on_event

    # ---- UIA callback 线程入口：只入队，绝不阻塞 ----
    def _on_event(self, event: TerminalEvent):
        with self._event_q_lock:
            if len(self._events) >= EVENT_QUEUE_MAX:
                self._events.popleft()
                self.stats["dropped"] += 1
            self._events.append(event)

    def start(self) -> bool:
        if not self.enabled:
            return False
        if self._started:
            return True
        ok = False
        try:
            ok = self.backend.start()
        except Exception:
            ok = False
        self._started = True
        if ok:
            self.refresh_controls(force=True)
        return ok

    def request_stop(self):
        """只发停止信号（DP43-R17：signal/join 分离）。"""
        try:
            self.backend.request_stop()
        except Exception:
            pass
        self._started = False

    def join_for_shutdown(self, timeout: float = 3.0) -> bool:
        """有界回收 backend MTA 线程（外部预算）。"""
        try:
            return self.backend.join_for_shutdown(timeout)
        except Exception:
            return True

    def stop(self):
        """兼容薄 wrapper（测试/旧入口）。"""
        self.request_stop()
        self.join_for_shutdown()

    # ---- control 发现 ----
    def refresh_controls(self, force: bool = False):
        now = time.time()
        if not force and now - self._last_discover < REDISCOVER_SEC:
            return
        self._last_discover = now
        self.stats["rediscoveries"] += 1
        try:
            layout = self.backend.discover_layout()
        except Exception:
            layout = None
        if layout is None:
            return
        self.layout = layout
        new_map = {}
        for control in list(layout.controls.values())[:MAX_CONTROLS]:
            new_map[control.control_id] = control
        if new_map != self.controls:
            self.controls = new_map
            sync = getattr(self.backend, "sync_control_subscriptions", None)
            if sync is not None:
                try:
                    sync(list(new_map.values()))
                except Exception:
                    pass
            # 消失 control 的状态一并清理
            for control_id in list(self.observations):
                if control_id not in new_map:
                    self.observations.pop(control_id, None)
            for table in (self.activity, self._waiting_recheck, self.rings,
                          self._ring_len, self._dirty_controls,
                          self._last_visible_read, self._screens,
                          self._screen_read_at, self._pending_reads):
                for control_id in list(table):
                    if control_id not in new_map:
                        table.pop(control_id, None)

    def topology_signature(self) -> tuple:
        """实例无关的观察拓扑指纹：窗口/control 变化时让上层重解析。"""
        layout = self.layout
        if layout is None:
            return ()
        return (tuple(sorted(layout.windows)),
                tuple(sorted(layout.controls)))

    # ---- 事件处理（monitor core 线程调用） ----
    def poll(self, now: float):
        if not self._started:
            return

        # 1) drain events
        with self._event_q_lock:
            events = list(self._events)
            self._events.clear()
        for ev in events:
            self.stats["events"] += 1
            if ev.kind == "structure":
                self._structure_dirty = True
                continue
            control_id = ev.control_id
            if control_id not in self.controls:
                # 未知 control（新开/未订阅）：标记发现需求
                self._structure_dirty = True
                continue
            if ev.kind == "notification" and ev.text:
                self._push_delta(control_id, ev.text)
                self.activity[control_id] = ev.ts or now
                if WEAK_TRIGGER_RE.search(ev.text):
                    self.stats["triggers"] += 1
                    self._request_visible_read(
                        control_id, now, VisibleReadReason.APPROVAL_TRIGGER,
                        delta=ev.text)
            elif ev.kind in ("notification", "activity"):
                self.activity[control_id] = ev.ts or now
                # TextChanged fallback：Notification 不携带审批文案时，
                # 有界 debounce 后读一次可见区域（不是持续全树扫描）
                if control_id not in self._waiting_recheck:
                    deadline = (ev.ts or now) + TEXT_CHANGED_DEBOUNCE
                    current = self._dirty_controls.get(control_id)
                    self._dirty_controls[control_id] = (min(current, deadline)
                                                        if current else deadline)

        # 2) 事件处理完毕后再判定重发现：structure 事件本轮立即生效
        if self._structure_dirty:
            self._structure_dirty = False
            self.refresh_controls(force=True)
        else:
            self.refresh_controls()

        # 3) 订阅瞬时失败重试（v4.2.3 §5.2）：topology 不变时只对当前
        #    controls 重试 sync；健康状态零额外 UIA command。
        self._maybe_retry_subscriptions(now)

        # 4) 请求 WAITING TTL 复检（审批通道，最高优先级）
        for control_id in list(self._waiting_recheck):
            if control_id not in self.controls:
                self._waiting_recheck.pop(control_id, None)
                self.observations.pop(control_id, None)
                continue
            if now >= self._waiting_recheck[control_id]:
                self._waiting_recheck.pop(control_id, None)
                self._request_visible_read(
                    control_id, now, VisibleReadReason.WAITING_RECHECK)

        # 5) 请求 TextChanged debounce 到期的兜底读取
        self._request_due_text_fallbacks(now)

        # 6) 请求缺失/过期的屏幕摘要补读
        self._request_stale_screen_digests(now)

        # 7) 统一消费可见读取请求（唯一真实 read 入口）
        self._service_visible_reads(now)

    # ---- 订阅重试（v4.2.3 §5.2） ----
    def _maybe_retry_subscriptions(self, now: float):
        retry = getattr(self.backend, "subscription_retry_needed", None)
        if retry is None or not retry():
            return
        if now - self._last_subscription_retry < SUBSCRIPTION_RETRY_SEC:
            return
        self._last_subscription_retry = now
        sync = getattr(self.backend, "sync_control_subscriptions", None)
        if sync is None or not self.controls:
            return
        self.stats["subscription_retry_count"] += 1
        try:
            sync(list(self.controls.values()))
        except Exception:
            pass

    # ---- 可见读取 broker（v4.2.3 §5.1） ----
    def _request_visible_read(self, control_id: tuple, now: float,
                              reason: VisibleReadReason,
                              delta: str = "") -> None:
        """登记一个可见读取请求；不直接读 backend。

        每 control 最多保留 1 个 pending request；新的高优先级请求
        可升级旧请求，同优先级保持最早入队时间（FIFO 公平）。
        """
        if control_id not in self.controls:
            return
        pending = self._pending_reads.get(control_id)
        if pending is None:
            self._pending_reads[control_id] = _VisibleReadRequest(
                reason=reason, delta=delta, enqueued_at=now)
            return
        if reason <= pending.reason:
            self._pending_reads[control_id] = _VisibleReadRequest(
                reason=reason,
                delta=delta or pending.delta,
                enqueued_at=min(now, pending.enqueued_at))

    def _request_due_text_fallbacks(self, now: float):
        """TextChanged debounce 到期的 control 转为 TEXT_FALLBACK 请求。"""
        if not self._dirty_controls:
            return
        for control_id in list(self._dirty_controls):
            if control_id not in self.controls:
                self._dirty_controls.pop(control_id, None)
                continue
            if now < self._dirty_controls[control_id]:
                continue
            self._dirty_controls.pop(control_id, None)
            self._request_visible_read(
                control_id, now, VisibleReadReason.TEXT_FALLBACK)

    def _request_stale_screen_digests(self, now: float):
        """为缺失/过期的 control 登记屏幕摘要补读请求。

        与审批通道共用同一预算（全局 ≤6/s、单 control ≥0.5s），每次
        poll 最多新登记 SCREEN_DIGEST_PER_POLL 个；事件驱动的读取顺带
        更新摘要，正常运行时这里几乎不请求。
        """
        if not self.controls:
            return
        picked = 0
        for control_id in self.controls:
            read_at = self._screen_read_at.get(control_id, 0.0)
            fresh = (control_id in self._screens
                     and now - read_at < SCREEN_DIGEST_STALE_SEC)
            if fresh or now - read_at < SCREEN_DIGEST_MIN_INTERVAL:
                continue
            self._request_visible_read(
                control_id, now, VisibleReadReason.SCREEN_DIGEST)
            picked += 1
            if picked >= SCREEN_DIGEST_PER_POLL:
                break

    def _service_visible_reads(self, now: float) -> None:
        """poll 末统一消费 pending 请求（v4.2.3 §5.1）。

        按优先级 + FIFO 顺序服务；每 poll 最多 MAX_VISIBLE_READS_PER_POLL
        次真实 read；限流/预算推迟的请求保留 pending，下一轮继续。
        """
        self.stats["pending_visible_reads"] = len(self._pending_reads)
        if not self._pending_reads:
            return
        # 全局滑窗（只记录成功读取；失败不消耗预算 token）
        while self._visible_read_times and now - self._visible_read_times[0] > 1.0:
            self._visible_read_times.popleft()
        order = sorted(self._pending_reads.items(),
                       key=lambda kv: (int(kv[1].reason), kv[1].enqueued_at))
        served = 0
        for control_id, request in order:
            if served >= MAX_VISIBLE_READS_PER_POLL:
                break
            if control_id not in self.controls:
                self._pending_reads.pop(control_id, None)
                continue
            if request.reason is VisibleReadReason.SCREEN_DIGEST:
                # 同一 control 的更高优先级读取可能已顺带更新摘要
                read_at = self._screen_read_at.get(control_id, 0.0)
                if (control_id in self._screens
                        and now - read_at < SCREEN_DIGEST_STALE_SEC):
                    self._pending_reads.pop(control_id, None)
                    continue
            result = self._read_visible_once(control_id, now)
            if result is _READ_BUSY:
                continue   # 限流/预算：保留 pending
            self._pending_reads.pop(control_id, None)
            if result is None:
                # 读取失败：不伪造 WAITING、不消耗 token；请求丢弃，
                # 由下一轮事件/复检/摘要 stale 重新驱动。
                continue
            served += 1
            reason_key = request.reason.name
            by_reason = self.stats["visible_reads_by_reason"]
            by_reason[reason_key] = by_reason.get(reason_key, 0) + 1
            if request.reason is VisibleReadReason.SCREEN_DIGEST:
                self.stats["screen_reads"] += 1
            else:
                if request.reason is VisibleReadReason.TEXT_FALLBACK:
                    self.stats["text_fallback_reads"] += 1
                self._inspect_text(control_id, result, now, request.delta)
        self.stats["pending_visible_reads"] = len(self._pending_reads)

    def _read_visible_once(self, control_id: tuple, now: float):
        """唯一调用 backend.read_visible 的生产入口（v4.2.3 §5.1）。

        统一检查：单 control ≥0.5s、全局滑窗 ≤6/s；成功后同时更新
        屏幕摘要（同一次 I/O 服务两个通道）。返回：
          str    —— 读取成功（可为空串）；
          _READ_BUSY —— 限流/预算暂时不可读（调用方保留请求）；
          None   —— 读取失败（不消耗预算 token，不更新摘要）。
        """
        if now - self._last_visible_read.get(control_id, 0.0) < \
                CONTROL_VISIBLE_READ_MIN_INTERVAL:
            return _READ_BUSY
        if len(self._visible_read_times) >= GLOBAL_VISIBLE_READ_LIMIT:
            return _READ_BUSY
        try:
            visible = self.backend.read_visible(control_id)
        except Exception:
            return None
        text = str(visible or "")[:VISIBLE_MAX]
        self._last_visible_read[control_id] = now
        self._visible_read_times.append(now)
        self.stats["visible_reads"] += 1
        # 同一次读取顺带更新屏幕摘要（复用 I/O，不额外读）
        self._screens[control_id] = text
        self._screen_read_at[control_id] = now
        return text

    def _push_delta(self, control_id: tuple, text: str):
        ring = self.rings.setdefault(control_id, deque())
        total = self._ring_len.setdefault(control_id, 0)
        text = text[:DELTA_MAX]
        ring.append(text)
        total += len(text)
        while ring and total > RING_MAX:
            dropped = ring.popleft()
            total -= len(dropped)
        self._ring_len[control_id] = total

    def _ring_text(self, control_id: tuple) -> str:
        return "".join(self.rings.get(control_id, deque()))[:RING_MAX]

    def _inspect_text(self, control_id: tuple, visible: str, now: float,
                      delta: str = ""):
        """对一次成功读取的可见文本运行识别器（不访问 backend）。"""
        obs_list: list[Observation] = []
        for recognizer in self.recognizers:
            try:
                obs_list += recognizer.inspect(visible, delta or self._ring_text(control_id), now)
            except Exception:
                continue
        waiting = next((o for o in obs_list if o.status == Status.WAITING), None)
        if waiting is not None:
            self.observations[control_id] = waiting
            self._waiting_recheck[control_id] = now + RECHECK_SEC
        else:
            # 可见区域已无审批 UI → 清除 WAITING（不残留 scrollback 文案）
            old = self.observations.get(control_id)
            self.observations.pop(control_id, None)
            self._waiting_recheck.pop(control_id, None)
            if old is None and visible:
                self.activity[control_id] = now

    # ---- 屏幕摘要（窗口候选评分证据，v4.1.4） ----
    def screen_texts(self) -> dict[tuple, str]:
        """每 control 最近一次可见屏幕文本的快照（仅内存，绝不外显）。"""
        return dict(self._screens)

    def control_activity_observation(self, control_id: tuple, now: float,
                                     grace: float) -> Observation | None:
        """归属 control 的泛化活动证据（agent_kind=None，仅 fallback 语义）。"""
        last = self.activity.get(control_id, 0.0)
        if not last or now - last > grace:
            return None
        return Observation(
            source=EvidenceSource.TERMINAL, timestamp=last,
            status=Status.WORKING, phase=Phase.NONE,
            confidence=Confidence.MEDIUM,
            agent_kind=None,
            summary="终端活动",
            expires_at=last + grace,
        )

    def waiting_observation(self, control_id: tuple) -> Observation | None:
        return self.observations.get(control_id)


def make_observer(cfg: dict) -> TerminalObserver | None:
    """创建真实 UIA 观察器；comtypes 不可用时返回 None（plan §54 降级）。"""
    if not bool(cfg.get("terminal_observer", True)):
        return None
    try:
        backend = UiaBackend()
    except Exception:
        return None
    return TerminalObserver(backend, cfg=cfg)
