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

    def stop(self):
        self._stopped = True   # 终态：后续 start() 直接拒绝
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
        """按当前 control 集合同步订阅：新增订阅、消失退订，绝不积累。"""
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
        self._submit(do)

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
                    # 两类事件都注册失败：不标记为已订阅，下轮可重试
                    self._control_tracker.discard(control.control_id)
                return
        except Exception:
            self.errors += 1
            self._control_tracker.discard(control.control_id)

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
                if sub.registered:
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
        self._last_discover = 0.0
        self._structure_dirty = False
        self._started = False
        # 诊断计数（不含任何终端文本）
        self.stats = {"events": 0, "dropped": 0, "visible_reads": 0,
                      "rediscoveries": 0, "triggers": 0,
                      "text_fallback_reads": 0}
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

    def stop(self):
        try:
            self.backend.stop()
        except Exception:
            pass
        self._started = False

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
                          self._last_visible_read):
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
                    self._inspect_visible(control_id, now, delta=ev.text)
            elif ev.kind in ("notification", "activity"):
                self.activity[control_id] = ev.ts or now
                # TextChanged fallback：Notification 不携带审批文案时，
                # 有界 debounce 后读一次可见区域（不是持续全树扫描）
                if control_id not in self._waiting_recheck:
                    deadline = (ev.ts or now) + TEXT_CHANGED_DEBOUNCE
                    current = self._dirty_controls.get(control_id)
                    self._dirty_controls[control_id] = (min(current, deadline)
                                                        if current else deadline)

        # 事件处理完毕后再判定重发现：structure 事件本轮立即生效
        if self._structure_dirty:
            self._structure_dirty = False
            self.refresh_controls(force=True)
        else:
            self.refresh_controls()

        # 等待期间的 TTL 复检（plan §21）
        for control_id in list(self._waiting_recheck):
            if control_id not in self.controls:
                self._waiting_recheck.pop(control_id, None)
                self.observations.pop(control_id, None)
                continue
            if now >= self._waiting_recheck[control_id]:
                self._inspect_visible(control_id, now)

        self._drain_dirty(now)

    def _drain_dirty(self, now: float):
        """处理 TextChanged debounce 到期的可见读取（多重限流）。"""
        if not self._dirty_controls:
            return
        while self._visible_read_times and now - self._visible_read_times[0] > 1.0:
            self._visible_read_times.popleft()
        for control_id in list(self._dirty_controls):
            if control_id not in self.controls:
                self._dirty_controls.pop(control_id, None)
                continue
            if now < self._dirty_controls[control_id]:
                continue
            if now - self._last_visible_read.get(control_id, 0.0) < CONTROL_VISIBLE_READ_MIN_INTERVAL:
                # 单 control 限流：推迟 deadline
                self._dirty_controls[control_id] = now + TEXT_CHANGED_DEBOUNCE
                continue
            if len(self._visible_read_times) >= GLOBAL_VISIBLE_READ_LIMIT:
                # 全局预算耗尽：推迟到下一轮 poll
                self._dirty_controls[control_id] = now + TEXT_CHANGED_DEBOUNCE
                continue
            self._dirty_controls.pop(control_id, None)
            self.stats["text_fallback_reads"] += 1
            self._inspect_visible(control_id, now)

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

    def _inspect_visible(self, control_id: tuple, now: float, delta: str = ""):
        self.stats["visible_reads"] += 1
        self._last_visible_read[control_id] = now
        self._visible_read_times.append(now)
        try:
            visible = self.backend.read_visible(control_id)
        except Exception:
            visible = ""
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
