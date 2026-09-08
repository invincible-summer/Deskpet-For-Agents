"""Windows Terminal UI Automation 被观察层（plan.md §14-§25）。

架构：
  * UIA 调用全部在独立 MTA 线程（comtypes COINIT_MULTITHREADED），
    不放 Tk UI 线程，不拥有窗口；事件 handler 的添加/删除也在该线程
    （MS UIA threading guidance：同一非 UI/MTA 线程完成 add/remove）。
  * 事件驱动：pane 级 UIA Notification（2022 起携带实际新增文本 payload）
    + pane 级 TextChanged（debounce 后的有界审批 fallback）
    + 窗口级 StructureChanged（pane 开合 → 立即重发现，20s safety 仅为兜底）。
    不做 200ms 全树扫描。
  * 只读当前可见区域：TextPattern.GetVisibleRanges()，不读 scrollback。
  * 内存边界（plan §18）：delta ≤2048 / ring ≤8192 / visible ≤4096 /
    最多 16 个 pane / 事件队列 ≤256 / UIA 命令队列 ≤32（满即失败不排队）/
    可见读取全局 ≤6/s、单 pane ≤2/s（等待复检 0.75s 独立通道）。
  * 终端文本是不可信数据：只做字符串匹配与状态归类，绝不执行、绝不
    写日志/配置/诊断文件。
  * UIA 审批观察带 TTL（1.5s）：等待期间每 0.75~1s 重读可见区域续期或
    清除，scrollback 里的旧审批文案不会造成永久 WAITING。
  * 订阅生命周期：pane/窗口订阅记录在册，pane 消失时在 MTA 线程正确
    Remove*EventHandler 并释放强引用，长期 pane churn 不积累 COM handler。
"""
from __future__ import annotations

import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, replace as dataclasses_replace
from typing import Callable

from .matching import best_effort_scores, mutual_unique_matches
from .models import (
    AgentKind,
    BindingConfidence,
    BindingOrigin,
    Confidence,
    EvidenceSource,
    Observation,
    Phase,
    Status,
    TabInfo,
    TerminalBinding,
    TerminalLocation,
    WindowIdentity,
)

# ------------------------------------------------------------- 常量（代码级安全上限）

DELTA_MAX = 2048          # 单条事件 delta 上限
RING_MAX = 8192           # 每 pane ring buffer 字符上限
VISIBLE_MAX = 4096        # 可见快照字符上限
MAX_PANES = 16            # 最多监听的 pane 数
EVENT_QUEUE_MAX = 256     # 事件队列上限（满时丢最旧）
UIA_CALL_QUEUE_MAX = 32   # UIA 命令队列上限（满时立即失败，不排队积压）
APPROVAL_TTL = 1.5        # 审批观察有效期（秒）
RECHECK_SEC = 0.75        # 等待期间的可见区域复检间隔
REDISCOVER_SEC = 20.0     # pane 重新发现周期（safety refresh；正常由
                          # StructureChanged 立即触发）
TEXT_CHANGED_DEBOUNCE = 0.15        # TextChanged → 可见读取的 debounce
PANE_VISIBLE_READ_MIN_INTERVAL = 0.5   # 单 pane 可见读取最小间隔
GLOBAL_VISIBLE_READ_LIMIT = 6         # 全局可见读取预算（次/秒）
WEAK_TRIGGER_RE = re.compile(
    r"(would you like|do you want|approve|approval|permission|permissions|"
    r"proceed|don't ask|tell (?:codex|claude|kimi)|waiting for|confirm|"
    r"\(esc\)|\(y/n\)|yes/no)", re.IGNORECASE)

# UIA 事件/属性常量（头文件 #define，不在 typelib）
UIA_TEXT_TEXTCHANGED_EVENT = 20015
UIA_SELECTION_ITEM_SELECTED_EVENT = 20012   # UIA_SelectionItem_ElementSelectedEventId
UIA_TEXT_PATTERN_ID = 10014
UIA_SELECTION_ITEM_PATTERN_ID = 10010
UIA_CLASSNAME_PROPERTY_ID = 30012
UIA_CONTROL_TYPE_PROPERTY_ID = 30003
UIA_TAB_ITEM_CONTROL_TYPE_ID = 50019
TREE_SCOPE_ELEMENT = 1
TREE_SCOPE_DESCENDANTS = 4
WT_WINDOW_CLASS = "CASCADIA_HOSTING_WINDOW_CLASS"


@dataclass
class PaneInfo:
    """一个 TermControl pane 的稳定身份（runtime id 只在生命周期内有效）。

    tab_id 关联该 pane 所属 Tab（仅 selected Tab 的 TermControl 会
    attach 到 XAML root，可被遍历）。
    """
    pane_id: tuple            # (hwnd, runtime_id tuple)
    hwnd: int
    window_pid: int
    title: str
    window_class: str = WT_WINDOW_CLASS
    tab_id: tuple = ()        # 所属 Tab 的 (hwnd, runtime_id)（未知为 ()）
    window_created: float = 0.0

    def as_key(self) -> tuple:
        return self.pane_id


@dataclass
class TerminalLayout:
    """一轮 Windows Terminal topology 发现结果（v4plan §5.3）。

    只有当前 selected Tab 的 TermControl 会出现在 XAML root 里
    （Windows Terminal TabManagement.cpp：选中变化才 attach terminal
    control），因此 panes 只覆盖 selected tabs——绝不后台切换 Tab 补全。
    """
    windows: dict[int, object]        # hwnd → WindowIdentity
    tabs: dict[tuple, TabInfo]        # tab_id → TabInfo
    panes: dict[tuple, PaneInfo]      # pane_id → PaneInfo（selected tabs）
    selected_tabs: dict[int, tuple]   # hwnd → 当前 selected tab_id

    def __bool__(self) -> bool:
        return bool(self.windows)


@dataclass
class TerminalEvent:
    pane_id: tuple
    kind: str        # notification / activity / structure / tab-selected
    text: str = ""   # notification 的 delta（有界）
    ts: float = 0.0


# ------------------------------------------------------------- 订阅生命周期

class SubscriptionTracker:
    """纯数据订阅账本：sync() 得出需新增/移除的键，不直接碰 COM。

    与 UiaBackend 解耦以便对 pane churn 做确定性测试。
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
class PaneSubscription:
    """单个 pane 的订阅记录：哪个 handler 注册了哪类事件。"""
    element: object
    handler: object
    notification_registered: bool = False
    text_changed_registered: bool = False


@dataclass
class WindowSubscription:
    """Windows Terminal 顶层窗口的事件订阅记录。

    StructureChanged（pane 开合）+ ElementSelected（用户自然切 Tab，
    20012）都在窗口 root 上按 TREE_SCOPE_DESCENDANTS 订阅。
    """
    root: object
    handler: object
    registered: bool = False
    selected_registered: bool = False


# ------------------------------------------------------------- Backend 协议

class TerminalBackend:
    """UIA 后端抽象；FakeBackend 用于无 Windows Terminal 的测试。"""

    available: bool = False

    def start(self) -> bool:
        return False

    def stop(self):
        pass

    def discover_layout(self) -> "TerminalLayout | None":
        """topology 发现（必须实现；返回 None 表示本轮失败/不可用）。"""
        return None

    def selected_tab(self, hwnd: int) -> TabInfo | None:
        return None

    def focused_location(self) -> TerminalLocation | None:
        return None

    def select_tab(self, tab_id: tuple) -> bool:
        return False

    def focus_pane(self, pane_id: tuple) -> bool:
        return False

    def read_visible(self, pane_id: tuple) -> str:
        return ""

    def focused_pane(self) -> PaneInfo | None:
        return None

    def sync_pane_subscriptions(self, panes: list[PaneInfo]):
        """默认实现为空；真实后端在唯一 MTA 线程完成 add/remove。"""


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
        pane 开合立即触发重发现，不等 20s safety。
    """
    available = False

    def __init__(self):
        self._queue: "queue.Queue[UiaCall]" = queue.Queue(maxsize=UIA_CALL_QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self.uia = None
        self.lib = None
        self._handler_cls = None
        self._pane_elements: dict[tuple, object] = {}
        self._pane_subscriptions: dict[tuple, PaneSubscription] = {}
        self._window_subscriptions: dict[int, WindowSubscription] = {}
        self._pane_tracker = SubscriptionTracker()
        self._window_tracker = SubscriptionTracker()
        self.event_sink: Callable[[TerminalEvent], None] | None = None
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
                "uia_pane_subs": len(self._pane_subscriptions),
                "uia_window_subs": len(self._window_subscriptions)}

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
                    backend._emit(backend._pane_of(sender), "notification", text)
                except Exception:
                    backend.errors += 1

            def IUIAutomationEventHandler_HandleAutomationEvent(self, sender, eventId):
                try:
                    eid = int(eventId)
                    if eid == UIA_TEXT_TEXTCHANGED_EVENT:
                        backend._emit(backend._pane_of(sender), "activity", "")
                    elif eid == UIA_SELECTION_ITEM_SELECTED_EVENT:
                        # 用户（或 Select()）切换了 Tab：只发 topology 脏
                        # 信号，不读任何终端文本。
                        backend._emit(None, "tab-selected", "")
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
            for pane_id, sub in list(self._pane_subscriptions.items()):
                self._remove_pane_subscription(sub)
                self._pane_subscriptions.pop(pane_id, None)
            for hwnd, sub in list(self._window_subscriptions.items()):
                self._remove_window_subscription(sub)
                self._window_subscriptions.pop(hwnd, None)
            self._pane_tracker.active.clear()
            self._window_tracker.active.clear()
            self._pane_elements.clear()
        except Exception:
            pass
        self.available = False

    def _pane_of(self, sender) -> tuple | None:
        try:
            rid = tuple(int(x) for x in sender.GetRuntimeId())
        except Exception:
            return None
        for pane_id, _el in self._pane_elements.items():
            if pane_id[1] == rid:
                return pane_id
        return None

    def _emit(self, pane_id: tuple | None, kind: str, text: str):
        sink = self.event_sink
        if sink is None:
            return
        sink(TerminalEvent(pane_id=pane_id or (), kind=kind, text=text,
                           ts=time.time()))

    # ---- 对外（任意线程调用，封送到 MTA 线程执行） ----
    def start(self) -> bool:
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

    def stop(self):
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=3.0)

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

    def _find_tab_items(self, root):
        return self._find_elements(root, UIA_CONTROL_TYPE_PROPERTY_ID,
                                   UIA_TAB_ITEM_CONTROL_TYPE_ID)

    @staticmethod
    def _runtime_id(el) -> tuple | None:
        try:
            return tuple(int(x) for x in el.GetRuntimeId())
        except Exception:
            return None

    def discover_layout(self) -> TerminalLayout:
        """发现全部 WT 窗口的 Window/Tab 拓扑 + selected Tab 的 pane。

        物理限制（TabManagement.cpp）：只有 selected Tab 的 TermControl
        attach 在 XAML root；background Tab 只有 TabItem。绝不后台切换
        Tab 补全 pane——用户自然切换时经 20012 事件学习。
        """
        from actions import winkeys

        def do():
            layout = TerminalLayout(windows={}, tabs={}, panes={},
                                    selected_tabs={})
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
                index_hint = 0
                for el in self._find_tab_items(root):
                    rid = self._runtime_id(el)
                    if rid is None:
                        continue
                    selected = False
                    name = ""
                    try:
                        name = str(el.CurrentName or "")
                        punk = el.GetCurrentPattern(UIA_SELECTION_ITEM_PATTERN_ID)
                        if punk:
                            sip = punk.QueryInterface(
                                self.lib.IUIAutomationSelectionItemPattern)
                            selected = bool(sip.CurrentIsSelected)
                    except Exception:
                        selected = False
                    tab_id = (hwnd, rid)
                    layout.tabs[tab_id] = TabInfo(
                        tab_id=tab_id, hwnd=hwnd, window_pid=identity.pid,
                        title=name, index_hint=index_hint,
                        selected=selected, last_seen=time.time())
                    if selected:
                        layout.selected_tabs[hwnd] = tab_id
                    index_hint += 1
                # 当前 selected Tab 的 TermControl（background tab 没有）
                selected_tab = layout.selected_tabs.get(hwnd, ())
                selected_title = (layout.tabs.get(selected_tab).title
                                  if selected_tab in layout.tabs else "")
                for el in self._find_term_controls(root)[:MAX_PANES]:
                    rid = self._runtime_id(el)
                    if rid is None:
                        continue
                    try:
                        name = str(el.CurrentName or "")
                    except Exception:
                        name = ""
                    if not name:
                        # TermControl Name 为空时退回 Tab 标题（评分证据）
                        name = selected_title
                    pane_id = (hwnd, rid)
                    self._pane_elements[pane_id] = el
                    layout.panes[pane_id] = PaneInfo(
                        pane_id=pane_id, hwnd=hwnd, window_pid=identity.pid,
                        title=name, tab_id=selected_tab,
                        window_created=identity.process_created)
                    if len(layout.panes) >= MAX_PANES:
                        break
            # 清理已消失 pane 的元素缓存
            live = set(layout.panes)
            for pane_id in list(self._pane_elements):
                if pane_id not in live:
                    self._pane_elements.pop(pane_id, None)
            return layout
        return self._submit(do) or TerminalLayout(windows={}, tabs={},
                                                  panes={}, selected_tabs={})

    def _resolve_element(self, pane_id: tuple):
        """按 pane_id 找 UIA element；缓存缺失时从窗口树重新解析。"""
        el = self._pane_elements.get(pane_id)
        if el is not None:
            return el
        if not pane_id:
            return None
        hwnd = pane_id[0]

        def find():
            try:
                root = self.uia.ElementFromHandle(hwnd)
            except Exception:
                return None
            for cand in self._find_term_controls(root):
                try:
                    if (hwnd, tuple(int(x) for x in cand.GetRuntimeId())) == pane_id:
                        self._pane_elements[pane_id] = cand
                        return cand
                except Exception:
                    continue
            return None
        self._submit(find, timeout=1.5)
        return self._pane_elements.get(pane_id)

    def read_visible(self, pane_id: tuple) -> str:
        def do():
            el = self._pane_elements.get(pane_id)
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
        if self._resolve_element(pane_id) is None:
            return ""
        return self._submit(do) or ""

    # ---- 订阅生命周期（唯一 MTA 线程内完成 add/remove） ----
    def sync_pane_subscriptions(self, panes: list[PaneInfo]):
        """按当前 pane 集合同步订阅：新增订阅、消失退订，绝不积累。"""
        def do():
            wanted = {p.pane_id for p in panes}
            to_add, to_remove = self._pane_tracker.sync(wanted)
            for pane_id in to_remove:
                sub = self._pane_subscriptions.pop(pane_id, None)
                if sub is not None:
                    self._remove_pane_subscription(sub)
            pane_by_id = {p.pane_id: p for p in panes}
            for pane_id in to_add:
                self._subscribe_pane(pane_by_id[pane_id])
            # 窗口级 StructureChanged：pane 开合 → 立即触发重发现
            self._sync_window_subscriptions({p.hwnd for p in panes})
        self._submit(do)

    def _subscribe_pane(self, pane: PaneInfo):
        try:
            root = self.uia.ElementFromHandle(pane.hwnd)
            for el in self._find_term_controls(root):
                try:
                    rid = tuple(int(x) for x in el.GetRuntimeId())
                except Exception:
                    continue
                if (pane.hwnd, rid) != pane.pane_id:
                    continue
                self._pane_elements[pane.pane_id] = el
                handler = self._handler_cls()
                sub = PaneSubscription(element=el, handler=handler)
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
                    self._pane_subscriptions[pane.pane_id] = sub
                else:
                    # 两类事件都注册失败：不标记为已订阅，下轮可重试
                    self._pane_tracker.discard(pane.pane_id)
                return
        except Exception:
            self.errors += 1
            self._pane_tracker.discard(pane.pane_id)

    def _remove_pane_subscription(self, sub: PaneSubscription):
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
                # StructureChanged：pane 开合 → 重发现
                try:
                    self.uia.AddStructureChangedEventHandler(
                        root, TREE_SCOPE_DESCENDANTS, None, handler)
                    sub.registered = True
                except Exception:
                    self.errors += 1
                # ElementSelected(20012)：用户自然切 Tab → topology 学习
                try:
                    self.uia.AddAutomationEventHandler(
                        UIA_SELECTION_ITEM_SELECTED_EVENT, root,
                        TREE_SCOPE_DESCENDANTS, None, handler)
                    sub.selected_registered = True
                except Exception:
                    self.errors += 1
                if sub.registered or sub.selected_registered:
                    self._window_subscriptions[hwnd] = sub
                else:
                    self._window_tracker.discard(hwnd)
            except Exception:
                self._window_tracker.discard(hwnd)
                self.errors += 1

    def _remove_window_subscription(self, sub: WindowSubscription):
        if sub.registered:
            try:
                self.uia.RemoveStructureChangedEventHandler(sub.root, sub.handler)
            except Exception:
                pass
        if sub.selected_registered:
            try:
                self.uia.RemoveAutomationEventHandler(
                    UIA_SELECTION_ITEM_SELECTED_EVENT, sub.root, sub.handler)
            except Exception:
                pass

    def focused_pane(self) -> PaneInfo | None:
        def do():
            try:
                el = self.uia.GetFocusedElement()
            except Exception:
                return None
            for _ in range(12):
                try:
                    cls = str(el.CurrentClassName or "")
                except Exception:
                    return None
                if cls == "TermControl":
                    try:
                        rid = tuple(int(x) for x in el.GetRuntimeId())
                        hwnd = int(el.CurrentNativeWindowHandle or 0)
                    except Exception:
                        return None
                    if hwnd == 0:
                        hwnd = self._hwnd_of_element(el)
                    return PaneInfo(pane_id=(hwnd, rid), hwnd=hwnd,
                                    window_pid=0, title="")
                try:
                    parent = self.uia.TreeWalker.GetParentElement(el)
                except Exception:
                    return None
                if parent is None:
                    return None
                el = parent
            return None
        return self._submit(do)

    # ---- Tab/Pane 激活原语（MTA 内执行） ----
    def _tab_element(self, tab_id: tuple):
        """按 (hwnd, runtime_id) 精确解析 TabItem element；找不到 None。"""
        if not tab_id:
            return None
        hwnd = int(tab_id[0])
        try:
            root = self.uia.ElementFromHandle(hwnd)
        except Exception:
            return None
        for el in self._find_tab_items(root):
            rid = self._runtime_id(el)
            if rid is not None and (hwnd, rid) == tuple(tab_id):
                return el
        return None

    def select_tab(self, tab_id: tuple) -> bool:
        """exact TabItem → SelectionItemPattern.Select()（官方语义：
        清除其他选择并选中该元素）。不用 title/index 猜，不发送键盘。"""
        def do():
            el = self._tab_element(tab_id)
            if el is None:
                return False
            try:
                punk = el.GetCurrentPattern(UIA_SELECTION_ITEM_PATTERN_ID)
                if not punk:
                    return False
                sip = punk.QueryInterface(
                    self.lib.IUIAutomationSelectionItemPattern)
                sip.Select()
                return True
            except Exception:
                self.errors += 1
                return False
        return bool(self._submit(do, timeout=3.0))

    def focus_pane(self, pane_id: tuple) -> bool:
        """Pane TermControl → UIA SetFocus（键盘焦点给该 element）。"""
        def do():
            el = self._pane_elements.get(tuple(pane_id))
            if el is None:
                return False
            try:
                el.SetFocus()
                return True
            except Exception:
                self.errors += 1
                return False
        if self._resolve_element(tuple(pane_id)) is None:
            return False
        return bool(self._submit(do))

    def focused_location(self) -> TerminalLocation | None:
        """当前键盘焦点所在的 (Window, Tab, Pane)——手工绑定入口。

        从 GetFocusedElement 向上走：TermControl（pane）与 TabItem
        （tab）都是 UIA 树祖先；窗口经 NativeWindowHandle/TreeWalker
        归到 WT 顶层窗口。
        """
        from actions import winkeys

        def do():
            try:
                el = self.uia.GetFocusedElement()
            except Exception:
                return None
            pane_rid = None
            tab_rid = None
            hwnd = 0
            for _ in range(16):
                try:
                    cls = str(el.CurrentClassName or "")
                except Exception:
                    return None
                if cls == "TermControl" and pane_rid is None:
                    pane_rid = self._runtime_id(el)
                    hwnd = int(el.CurrentNativeWindowHandle or 0) or hwnd
                elif cls == "TabItem" and tab_rid is None:
                    tab_rid = self._runtime_id(el)
                    hwnd = int(el.CurrentNativeWindowHandle or 0) or hwnd
                elif cls == WT_WINDOW_CLASS:
                    hwnd = int(el.CurrentNativeWindowHandle or 0) or hwnd
                    break
                try:
                    parent = self.uia.TreeWalker.GetParentElement(el)
                except Exception:
                    break
                if parent is None:
                    break
                el = parent
            if hwnd == 0:
                hwnd = self._hwnd_of_element(el)
            if not hwnd:
                return None
            identity = winkeys.window_identity(hwnd)
            if identity is None:
                return None
            return TerminalLocation(
                window=identity,
                tab_id=(hwnd, tab_rid) if tab_rid else None,
                pane_id=(hwnd, pane_rid) if pane_rid else None)
        return self._submit(do)

    def selected_tab(self, hwnd: int) -> TabInfo | None:
        """某 WT 窗口当前 selected 的 Tab（Select 后验证用）。"""
        def do():
            try:
                root = self.uia.ElementFromHandle(int(hwnd))
            except Exception:
                return None
            for el in self._find_tab_items(root):
                try:
                    punk = el.GetCurrentPattern(UIA_SELECTION_ITEM_PATTERN_ID)
                    if not punk:
                        continue
                    sip = punk.QueryInterface(
                        self.lib.IUIAutomationSelectionItemPattern)
                    if bool(sip.CurrentIsSelected):
                        rid = self._runtime_id(el)
                        if rid is None:
                            return None
                        name = str(el.CurrentName or "")
                        return TabInfo(tab_id=(int(hwnd), rid), hwnd=int(hwnd),
                                       window_pid=0, title=name, index_hint=-1,
                                       selected=True, last_seen=time.time())
                except Exception:
                    continue
            return None
        return self._submit(do)

    def _hwnd_of_element(self, el) -> int:
        try:
            for _ in range(12):
                hwnd = int(el.CurrentNativeWindowHandle or 0)
                if hwnd:
                    return hwnd
                el = self.uia.TreeWalker.GetParentElement(el)
        except Exception:
            pass
        return 0


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

class TerminalObserver:
    """事件驱动的终端观察器（逻辑与 backend 解耦，可注入 FakeBackend）。"""

    def __init__(self, backend: TerminalBackend,
                 recognizers=DEFAULT_RECOGNIZERS, cfg: dict | None = None):
        self.backend = backend
        self.recognizers = tuple(recognizers)
        self.cfg = dict(cfg or {})
        self.enabled = bool(self.cfg.get("terminal_observer", True))
        self.panes: dict[tuple, PaneInfo] = {}
        self.layout: TerminalLayout | None = None
        self.observations: dict[tuple, Observation] = {}
        self.activity: dict[tuple, float] = {}
        self.rings: dict[tuple, deque] = {}
        self._ring_len: dict[tuple, int] = {}
        self._events: deque = deque(maxlen=EVENT_QUEUE_MAX)  # UIA 线程 → 核心的有界队列
        self._event_q_lock = threading.Lock()
        self._waiting_recheck: dict[tuple, float] = {}
        # TextChanged debounce 审批 fallback（有界读取）
        self._dirty_panes: dict[tuple, float] = {}
        self._last_visible_read: dict[tuple, float] = {}
        self._visible_read_times: deque = deque()   # 全局预算滑动窗口
        self._last_discover = 0.0
        self._structure_dirty = False
        self._tab_dirty = False
        self._started = False
        # 诊断计数（不含任何终端文本）
        self.stats = {"events": 0, "dropped": 0, "visible_reads": 0,
                      "rediscoveries": 0, "triggers": 0,
                      "text_fallback_reads": 0, "tab_events": 0}
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
            self.refresh_panes(force=True)
        return ok

    def stop(self):
        try:
            self.backend.stop()
        except Exception:
            pass
        self._started = False

    # ---- pane 发现 ----
    def refresh_panes(self, force: bool = False):
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
        for pane in list(layout.panes.values())[:MAX_PANES]:
            new_map[pane.pane_id] = pane
        if new_map != self.panes or self._tab_dirty:
            self._tab_dirty = False
            self.panes = new_map
            sync = getattr(self.backend, "sync_pane_subscriptions", None)
            if sync is not None:
                try:
                    sync(list(new_map.values()))
                except Exception:
                    pass
            # 消失 pane 的状态一并清理
            for pane_id in list(self.observations):
                if pane_id not in new_map:
                    self.observations.pop(pane_id, None)
            for table in (self.activity, self._waiting_recheck, self.rings,
                          self._ring_len, self._dirty_panes,
                          self._last_visible_read):
                for pane_id in list(table):
                    if pane_id not in new_map:
                        table.pop(pane_id, None)

    def topology_signature(self) -> tuple:
        """实例无关的 topology 指纹：pane/tab/selected 变化时让上层重解析。"""
        layout = self.layout
        if layout is None:
            return ()
        return (tuple(sorted(layout.panes)),
                tuple(sorted(t.tab_id for t in layout.tabs.values())),
                tuple(sorted(layout.selected_tabs.items())))

    # ---- 事件处理（monitor core 线程调用） ----
    def poll(self, now: float):
        if not self._started:
            return

        with self._event_q_lock:
            events = list(self._events)
            self._events.clear()
        for ev in events:
            self.stats["events"] += 1
            if ev.kind == "structure":
                self._structure_dirty = True
                continue
            if ev.kind == "tab-selected":
                # 用户（或 Select()）自然切 Tab：topology 需要重学习
                self.stats["tab_events"] += 1
                self._tab_dirty = True
                continue
            pane_id = ev.pane_id
            if pane_id not in self.panes:
                # 未知 pane（新开/未订阅）：标记发现需求
                self._structure_dirty = True
                continue
            if ev.kind == "notification" and ev.text:
                self._push_delta(pane_id, ev.text)
                self.activity[pane_id] = ev.ts or now
                if WEAK_TRIGGER_RE.search(ev.text):
                    self.stats["triggers"] += 1
                    self._inspect_visible(pane_id, now, delta=ev.text)
            elif ev.kind in ("notification", "activity"):
                self.activity[pane_id] = ev.ts or now
                # TextChanged fallback：Notification 不携带审批文案时，
                # 有界 debounce 后读一次可见区域（不是持续全树扫描）
                if pane_id not in self._waiting_recheck:
                    deadline = (ev.ts or now) + TEXT_CHANGED_DEBOUNCE
                    current = self._dirty_panes.get(pane_id)
                    self._dirty_panes[pane_id] = (min(current, deadline)
                                                  if current else deadline)

        # 事件处理完毕后再判定重发现：structure/tab 事件本轮立即生效
        if self._structure_dirty or self._tab_dirty:
            self._structure_dirty = False
            self.refresh_panes(force=True)
        else:
            self.refresh_panes()

        # 等待期间的 TTL 复检（plan §21）
        for pane_id in list(self._waiting_recheck):
            if pane_id not in self.panes:
                self._waiting_recheck.pop(pane_id, None)
                self.observations.pop(pane_id, None)
                continue
            if now >= self._waiting_recheck[pane_id]:
                self._inspect_visible(pane_id, now)

        self._drain_dirty(now)

    def _drain_dirty(self, now: float):
        """处理 TextChanged debounce 到期的可见读取（多重限流）。"""
        if not self._dirty_panes:
            return
        while self._visible_read_times and now - self._visible_read_times[0] > 1.0:
            self._visible_read_times.popleft()
        for pane_id in list(self._dirty_panes):
            if pane_id not in self.panes:
                self._dirty_panes.pop(pane_id, None)
                continue
            if now < self._dirty_panes[pane_id]:
                continue
            if now - self._last_visible_read.get(pane_id, 0.0) < PANE_VISIBLE_READ_MIN_INTERVAL:
                # 单 pane 限流：推迟 deadline
                self._dirty_panes[pane_id] = now + TEXT_CHANGED_DEBOUNCE
                continue
            if len(self._visible_read_times) >= GLOBAL_VISIBLE_READ_LIMIT:
                # 全局预算耗尽：推迟到下一轮 poll
                self._dirty_panes[pane_id] = now + TEXT_CHANGED_DEBOUNCE
                continue
            self._dirty_panes.pop(pane_id, None)
            self.stats["text_fallback_reads"] += 1
            self._inspect_visible(pane_id, now)

    def _push_delta(self, pane_id: tuple, text: str):
        ring = self.rings.setdefault(pane_id, deque())
        total = self._ring_len.setdefault(pane_id, 0)
        text = text[:DELTA_MAX]
        ring.append(text)
        total += len(text)
        while ring and total > RING_MAX:
            dropped = ring.popleft()
            total -= len(dropped)
        self._ring_len[pane_id] = total

    def _ring_text(self, pane_id: tuple) -> str:
        return "".join(self.rings.get(pane_id, deque()))[:RING_MAX]

    def _inspect_visible(self, pane_id: tuple, now: float, delta: str = ""):
        self.stats["visible_reads"] += 1
        self._last_visible_read[pane_id] = now
        self._visible_read_times.append(now)
        try:
            visible = self.backend.read_visible(pane_id)
        except Exception:
            visible = ""
        obs_list: list[Observation] = []
        for recognizer in self.recognizers:
            try:
                obs_list += recognizer.inspect(visible, delta or self._ring_text(pane_id), now)
            except Exception:
                continue
        waiting = next((o for o in obs_list if o.status == Status.WAITING), None)
        if waiting is not None:
            self.observations[pane_id] = waiting
            self._waiting_recheck[pane_id] = now + RECHECK_SEC
        else:
            # 可见区域已无审批 UI → 清除 WAITING（不残留 scrollback 文案）
            old = self.observations.get(pane_id)
            self.observations.pop(pane_id, None)
            self._waiting_recheck.pop(pane_id, None)
            if old is None and visible:
                self.activity[pane_id] = now

    def pane_activity_observation(self, pane_id: tuple, now: float,
                                  grace: float) -> Observation | None:
        """绑定 pane 的泛化活动证据（agent_kind=None，仅 fallback 语义）。"""
        last = self.activity.get(pane_id, 0.0)
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

    def waiting_observation(self, pane_id: tuple) -> Observation | None:
        return self.observations.get(pane_id)


# ------------------------------------------------------------- 终端绑定解析

class TerminalResolver:
    """Agent ↔ Windows Terminal Window/Tab/Pane 的置信度绑定（v4plan §5）。

    Windows 原生 Agent：PID 祖先链 → 唯一窗口 → CONFIRMED。
    WSL Agent：WT_SESSION 没有官方 tab/pane 查询接口，只能用标题/cwd/
    distro 评分；分配用互相唯一匹配：score>=3 且双向唯一、双侧分差 >=1
    才 HIGH。证据不足保持 AMBIGUOUS——用户可用"关联当前 Terminal 位置"
    （Window+Tab+Pane 一次捕获）修复。
    只有 CONFIRMED/HIGH 绑定才允许 exact activation 与终端审批归属。
    """

    # WSL 评分：kind in title +3 / cwd ~/ 路径匹配 +2 / user@ +1 / distro +1
    HIGH_MIN_SCORE = 3
    HIGH_MIN_MARGIN = 1

    # 标题里的 shell 路径标记：`user@host: ~/a/b/c`（大小写不敏感）
    _TITLE_PATH_RE = re.compile(r"~/[^\s:·,|]*", re.IGNORECASE)
    _KIND_WORD_RE = {
        kind.value: re.compile(rf"\b{re.escape(kind.value)}\b", re.IGNORECASE)
        for kind in AgentKind
    }

    def __init__(self, enum_windows=None, ancestor_pids=None):
        self._enum_windows = enum_windows
        self._ancestor_pids = ancestor_pids
        # key → TerminalLocation（运行期手工绑定：Window+Tab+Pane）
        self._manual: dict[str, TerminalLocation] = {}
        self._last_windows: list = []
        self._windows_ts = 0.0

    def _windows(self, force: bool = False) -> list:
        now = time.time()
        if not force and now - self._windows_ts < 3.0:
            return self._last_windows
        try:
            if self._enum_windows is not None:
                wins = self._enum_windows()
            else:
                from actions import winkeys
                wins = winkeys.enum_windows()
        except Exception:
            wins = []
        self._last_windows = wins
        self._windows_ts = now
        return wins

    def _ancestors(self, pid: int) -> set[int]:
        if self._ancestor_pids is not None:
            try:
                return self._ancestor_pids(pid)
            except Exception:
                return {pid}
        try:
            from actions import winkeys
            return winkeys._ancestor_pids(pid)
        except Exception:
            return {pid}

    # ---- 手工绑定（运行期，不落盘；Agent exit/Window 变化即失效） ----
    def set_manual_location(self, key: str, location: TerminalLocation | None):
        """关联当前 Terminal 位置（v4plan §5.10）。None = 清除。"""
        if location is None:
            self._manual.pop(key, None)
        else:
            self._manual[key] = location

    def manual_location(self, key: str) -> TerminalLocation | None:
        return self._manual.get(key)

    def _prune_manual(self, instances: list, layout: TerminalLayout | None):
        """手工绑定生命周期：Agent 退出或 WindowIdentity 失效即清理。

        pane 暂时不可见（所属 Tab 未选中）不算失效——activation 会
        先选 Tab 再做 pane revalidation。layout 为 None（UIA 降级）时
        没有窗口知识：不判死，交给 activation 时的 validate_window。
        """
        live_keys = {getattr(inst, "key", "") for inst in instances}
        windows = layout.windows if layout is not None else None
        for key in list(self._manual):
            loc = self._manual[key]
            if key not in live_keys:
                self._manual.pop(key, None)
                continue
            if windows is None:
                continue
            ident = windows.get(loc.window.hwnd)
            if ident is None or ident != loc.window:
                # 窗口消失或 HWND/PID/create_time 已变（复用）→ 失效
                self._manual.pop(key, None)

    def _binding(self, hwnd: int, windows: dict, pane, now: float,
                 confidence: BindingConfidence, origin, reason: str,
                 tab_id: tuple | None = None,
                 score: int = 0, runner_up: int = 0,
                 agent_margin: int = 0, pane_margin: int = 0) -> TerminalBinding:
        ident = windows.get(hwnd)
        return TerminalBinding(
            provider="windows-terminal",
            hwnd=hwnd,
            window_pid=(ident.pid if ident else
                        (pane.window_pid if pane else 0)),
            window_created=(ident.process_created if ident else
                            (pane.window_created if pane else 0.0)),
            window_class=(ident.window_class if ident else
                          (pane.window_class if pane else WT_WINDOW_CLASS)),
            title=(pane.title if pane else ""),
            tab_id=tab_id if tab_id is not None else (
                pane.tab_id if pane else None),
            tab_index_hint=-1,
            pane_id=(pane.pane_id if pane else None),
            origin=origin,
            confidence=confidence,
            observable=pane is not None,
            last_seen=now,
            score=score, runner_up_score=runner_up,
            agent_margin=agent_margin, pane_margin=pane_margin,
            reason=reason)

    def resolve(self, instances: list, panes: dict[tuple, PaneInfo],
                now: float,
                layout: TerminalLayout | None = None) -> dict[str, TerminalBinding]:
        self._prune_manual(instances, layout)
        windows: dict[int, WindowIdentity] = dict(
            layout.windows) if layout is not None else {}
        if not windows:
            # 无 layout（UIA 降级/测试）：退回 enum_windows 的基本信息
            for hwnd, wpid, _title, cls in self._windows():
                if cls == WT_WINDOW_CLASS:
                    windows[int(hwnd)] = WindowIdentity(
                        hwnd=int(hwnd), pid=int(wpid),
                        process_created=0.0, window_class=cls)
        wt_hwnds: set[int] = set(windows)

        # 祖先匹配用 (hwnd, pid) 平面表
        wins = [(ident.hwnd, ident.pid) for ident in windows.values()]

        panes_by_hwnd: dict[int, list[PaneInfo]] = {}
        for pane in panes.values():
            panes_by_hwnd.setdefault(pane.hwnd, []).append(pane)

        out: dict[str, TerminalBinding] = {}
        scored_instances: list = []

        for inst in instances:
            key = inst.key
            manual = self._manual.get(key)
            if manual is not None:
                ident = windows.get(manual.window.hwnd)
                if ident is not None and ident == manual.window:
                    pane = panes.get(manual.pane_id) if manual.pane_id else None
                    out[key] = self._binding(
                        manual.window.hwnd, windows, pane, now,
                        BindingConfidence.CONFIRMED, BindingOrigin.MANUAL,
                        "manual-location",
                        tab_id=manual.tab_id)
                    continue
                # 窗口失效：手工绑定已被 prune，走自动路径
            source = str(getattr(inst, "source", ""))
            if source == "windows" and inst.pid:
                parents = self._ancestors(inst.pid)
                matches = list(dict.fromkeys(
                    int(hwnd) for hwnd, wpid in wins if int(wpid) in parents))
                if len(matches) == 1:
                    hwnd = matches[0]
                    pane_list = panes_by_hwnd.get(hwnd, [])
                    if len(pane_list) == 1:
                        out[key] = self._binding(
                            hwnd, windows, pane_list[0], now,
                            BindingConfidence.CONFIRMED, BindingOrigin.AUTO,
                            "windows-ancestor")
                    else:
                        # 窗口唯一但 pane 不唯一：窗口可唤起，审批不可归属
                        out[key] = self._binding(
                            hwnd, windows, None, now,
                            BindingConfidence.AMBIGUOUS, BindingOrigin.AUTO,
                            "multi-pane-window")
                    continue
                if len(matches) > 1:
                    out[key] = TerminalBinding(
                        confidence=BindingConfidence.AMBIGUOUS, last_seen=now,
                        reason="multi-window-ancestor")
                    continue
                # 祖先链找不到窗口（非 WT 宿主或已退出）→ 落到打分路径再试
            scored_instances.append(inst)

        # 互相唯一评分分配（结果与实例顺序无关）
        left_keys = [inst.key for inst in scored_instances]
        right_keys = list(panes.keys())
        # Tab 标题是第二条证据来源：TermControl 的 UIA Name 有时停留在
        # profile 名（如 "Ubuntu"），而 TabItem 标题已带 shell 设置的
        # "user@host: ~/path"；两者取较高分（互斥唯一门槛不变）。
        tabs_by_id = {t.tab_id: t for t in (
            layout.tabs.values() if layout is not None else [])}

        def score_fn(key: str, pane_id: tuple) -> int:
            inst = inst_by_key[key]
            pane = panes[pane_id]
            best = 0
            for title in self._pane_titles(pane, tabs_by_id):
                probe = dataclasses_replace(pane, title=title)
                best = max(best, self._score_pane(inst, probe)[0])
            return best

        inst_by_key = {inst.key: inst for inst in scored_instances}
        decisions = mutual_unique_matches(
            left_keys, right_keys, score_fn,
            min_score=self.HIGH_MIN_SCORE, min_margin=self.HIGH_MIN_MARGIN)
        diagnostics = best_effort_scores(left_keys, right_keys, score_fn)

        for inst in scored_instances:
            key = inst.key
            dec = decisions.get(key)
            best_pane, best_score, runner_up = diagnostics.get(
                key, (None, 0, 0))
            if dec is not None:
                out[key] = self._binding(
                    dec.right[0], windows, panes[dec.right], now,
                    BindingConfidence.HIGH, BindingOrigin.OBSERVED,
                    self._score_reason(inst, panes[dec.right], tabs_by_id),
                    score=dec.score, runner_up=runner_up,
                    agent_margin=dec.left_margin, pane_margin=dec.right_margin)
                continue
            if best_pane is not None and best_score > 0:
                # 有正向证据但不满足互相唯一 → AMBIGUOUS（不归属审批）
                out[key] = self._binding(
                    best_pane[0], windows, panes[best_pane], now,
                    BindingConfidence.AMBIGUOUS, BindingOrigin.AUTO,
                    self._score_reason(inst, panes[best_pane], tabs_by_id)
                    + "·非唯一",
                    score=best_score, runner_up=runner_up)
                continue
            out[key] = self._fallback_binding(inst, panes, panes_by_hwnd,
                                              wt_hwnds, windows, now)
        return out

    @staticmethod
    def _cwd_tilde(cwd: str, user: str) -> str:
        """WSL cwd → `~/a/b` 形态（无法归一化时返回空）。"""
        cwd = str(cwd or "").replace("\\", "/").rstrip("/").lower()
        if not cwd or not user:
            return ""
        home = f"/home/{user.lower().strip()}"
        if cwd == home:
            return "~"
        if cwd.startswith(home + "/"):
            return "~/" + cwd[len(home) + 1:]
        return ""

    def _score_pane(self, inst, pane: PaneInfo) -> tuple[int, str]:
        """评分与依据（v4plan §5：证据可核验、误报率低）。

        * kind：词边界匹配（防 "pi" 命中 "pip"）+3；
        * cwd：标题含 `~/a/b` 路径标记时按路径比对（相等或互为前缀）+2；
          无标记时才退回 basename 出现 +2（basename 与用户名相同则不计，
          否则 "user@host" 形态的每个标题都会假命中 home 目录）；
        * user@：标题以 `<user>@` 开头 +1；
        * distro：词边界匹配 +1。
        """
        title = _squash(pane.title)
        if not title:
            return 0, ""
        score = 0
        parts: list[str] = []
        kind = getattr(inst, "kind", None)
        comm = kind.value if kind is not None else ""
        if comm:
            word_re = self._KIND_WORD_RE.get(comm)
            if word_re is not None and word_re.search(title):
                score += 3
                parts.append("kind")
        user = str(getattr(inst, "user", "") or "").strip()
        cwd = str(getattr(inst, "cwd", "") or "")
        tilde = self._cwd_tilde(cwd, user)
        title_match = self._TITLE_PATH_RE.search(title)
        home_only = tilde == "~" and re.search(r"(?:^|\s)~(?:\s|$)", title)
        if title_match is not None:
            title_path = title_match.group(0).rstrip("/").lower()
            if tilde and tilde != "~" and (
                    title_path == tilde
                    or title_path.startswith(tilde + "/")
                    or tilde.startswith(title_path + "/")):
                score += 2
                parts.append("cwd")
        elif home_only:
            score += 2
            parts.append("cwd")
        elif cwd:
            base = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
            if (base and base != user.lower()
                    and re.search(rf"(?<!\w){re.escape(base)}(?!\w)", title)):
                score += 2
                parts.append("cwd")
        if user:
            low = title.lower()
            if low.startswith(user.lower() + "@") or (user.lower() + "@") in low:
                score += 1
                parts.append("user@")
        distro = str(getattr(inst, "distro", "") or "").lower()
        if distro and re.search(rf"(?<!\w){re.escape(distro)}(?!\w)", title):
            score += 1
            parts.append("distro")
        return score, "+".join(parts)

    def _pane_titles(self, pane: PaneInfo, tabs_by_id: dict) -> list[str]:
        """pane 的证据标题集合：TermControl Name + 所属 Tab 标题。"""
        titles = []
        if pane.title:
            titles.append(pane.title)
        tab = tabs_by_id.get(pane.tab_id) if pane.tab_id else None
        if tab is not None and tab.title and tab.title not in titles:
            titles.append(tab.title)
        return titles

    def _score_reason(self, inst, pane: PaneInfo,
                      tabs_by_id: dict | None = None) -> str:
        tabs = tabs_by_id or {}
        best = ""
        for title in self._pane_titles(pane, tabs):
            if title:
                reason = self._score_pane(
                    inst, dataclasses_replace(pane, title=title))[1]
                if reason and len(reason) > len(best):
                    best = reason
        if best:
            return best
        return self._score_pane(inst, pane)[1] or "no-evidence"

    def _fallback_binding(self, inst, panes, panes_by_hwnd, wt_hwnds,
                          windows, now):
        # 唯一 Windows Terminal 窗口时可尽力唤起，但审批观察不归属
        if len(wt_hwnds) == 1:
            hwnd = next(iter(wt_hwnds))
            pane_list = panes_by_hwnd.get(hwnd, [])
            pane = pane_list[0] if len(pane_list) == 1 else None
            return self._binding(hwnd, windows, pane, now,
                                 BindingConfidence.NONE, BindingOrigin.AUTO,
                                 "single-window-fallback")
        return TerminalBinding(confidence=BindingConfidence.NONE, last_seen=now)


def make_observer(cfg: dict) -> TerminalObserver | None:
    """创建真实 UIA 观察器；comtypes 不可用时返回 None（plan §54 降级）。"""
    if not bool(cfg.get("terminal_observer", True)):
        return None
    try:
        backend = UiaBackend()
    except Exception:
        return None
    return TerminalObserver(backend, cfg=cfg)
