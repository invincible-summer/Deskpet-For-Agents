"""Windows Terminal UI Automation 被观察层（plan.md §14-§25）。

架构：
  * UIA 调用全部在独立 MTA 线程（comtypes COINIT_MULTITHREADED），
    不放 Tk UI 线程，不拥有窗口；事件 handler 的添加/删除也在该线程。
  * 事件驱动：订阅 Windows Terminal TermControl 的 UIA Notification
    （2022 起携带实际新增文本 payload）+ TextChanged fallback +
    窗口级 StructureChanged（pane 开合 → 重发现）。不做 200ms 全树扫描。
  * 只读当前可见区域：TextPattern.GetVisibleRanges()，不读 scrollback。
  * 内存边界（plan §18）：delta ≤2048 / ring ≤8192 / visible ≤4096 /
    最多 16 个 pane / 事件队列 ≤256（满时丢最旧，绝不阻塞 UIA callback）。
  * 终端文本是不可信数据：只做字符串匹配与状态归类，绝不执行、绝不
    写日志/配置/诊断文件。
  * UIA 审批观察带 TTL（1.5s）：等待期间每 0.75~1s 重读可见区域续期或
    清除，scrollback 里的旧审批文案不会造成永久 WAITING。
"""
from __future__ import annotations

import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .models import (
    AgentKind,
    BindingConfidence,
    Confidence,
    EvidenceSource,
    Observation,
    Phase,
    Status,
    TerminalBinding,
)

# ------------------------------------------------------------- 常量（代码级安全上限）

DELTA_MAX = 2048          # 单条事件 delta 上限
RING_MAX = 8192           # 每 pane ring buffer 字符上限
VISIBLE_MAX = 4096        # 可见快照字符上限
MAX_PANES = 16            # 最多监听的 pane 数
EVENT_QUEUE_MAX = 256     # 事件队列上限（满时丢最旧）
APPROVAL_TTL = 1.5        # 审批观察有效期（秒）
RECHECK_SEC = 0.75        # 等待期间的可见区域复检间隔
REDISCOVER_SEC = 20.0     # pane 重新发现周期（safety refresh）
WEAK_TRIGGER_RE = re.compile(
    r"(would you like|do you want|approve|approval|permission|permissions|"
    r"proceed|don't ask|tell (?:codex|claude|kimi)|waiting for|confirm|"
    r"\(esc\)|\(y/n\)|yes/no)", re.IGNORECASE)


@dataclass
class PaneInfo:
    """一个 TermControl pane 的稳定身份（runtime id 只在生命周期内有效）。"""
    pane_id: tuple            # (hwnd, runtime_id tuple)
    hwnd: int
    window_pid: int
    title: str
    window_class: str = "CASCADIA_HOSTING_WINDOW_CLASS"

    def as_key(self) -> tuple:
        return self.pane_id


@dataclass
class TerminalEvent:
    pane_id: tuple
    kind: str        # notification / activity / structure
    text: str = ""   # notification 的 delta（有界）
    ts: float = 0.0


# ------------------------------------------------------------- Backend 协议

class TerminalBackend:
    """UIA 后端抽象；FakeBackend 用于无 Windows Terminal 的测试。"""

    available: bool = False

    def start(self) -> bool:
        return False

    def stop(self):
        pass

    def discover_panes(self) -> list[PaneInfo]:
        return []

    def read_visible(self, pane_id: tuple) -> str:
        return ""

    def focused_pane(self) -> PaneInfo | None:
        return None


class UiaBackend(TerminalBackend):
    """comtypes 实现：独立 MTA 线程，所有 UIA 调用经该线程执行。

    依据（2026-09 查证）：
      * CUIAutomation8 CLSID {E22AD333-...}，请求 IUIAutomation5 以获得
        AddNotificationEventHandler（Win10 1709+）。
      * TermControl 类名固定（"IMPORTANT: Do NOT change"），ControlType
        = Text，仅支持 TextPattern。
      * 事件常量是头文件 #define，不在 typelib：TextPattern=10014、
        ClassNameProperty=30012、TextChanged=20015。
      * COM callback 用 comtypes.COMObject 子类（NVDA 模式）。
    """
    available = False

    def __init__(self):
        self._queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self.uia = None
        self.lib = None
        self._handler_cls = None
        self._handlers: list[object] = []       # 强引用，防 GC access violation
        self._pane_elements: dict[tuple, object] = {}
        self._subscribed: set[tuple] = set()
        self._handler_pairs: list[tuple[object, object]] = []   # (handler, element)
        self.event_sink: Callable[[TerminalEvent], None] | None = None
        self.calls = 0
        self.errors = 0

    # ---- MTA 线程 ----
    def _run(self):
        import comtypes
        import comtypes.client
        from comtypes import COMObject
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            return
        try:
            lib = comtypes.client.GetModule("UIAutomationCore.dll")
            uia = comtypes.client.CreateObject(
                lib.CUIAutomation8, interface=lib.IUIAutomation5)
        except Exception:
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
                    if int(eventId) == 20015:   # UIA_Text_TextChangedEventId
                        backend._emit(backend._pane_of(sender), "activity", "")
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
                    fn = self._queue.get(timeout=0.25)
                except queue.Empty:
                    continue
                try:
                    fn()
                except Exception:
                    self.errors += 1
        finally:
            self._teardown()
            comtypes.CoUninitialize()

    def _teardown(self):
        # handler 的移除与添加在同一 MTA 线程（MS threading guidance）。
        try:
            for handler, element in list(self._handler_pairs):
                try:
                    self.uia.RemoveAutomationEventHandler(20015, element, handler)
                except Exception:
                    pass
                try:
                    self.uia.RemoveNotificationEventHandler(element, handler)
                except Exception:
                    pass
            self._handlers.clear()
            self._handler_pairs.clear()
            self._pane_elements.clear()
            self._subscribed.clear()
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
        except Exception:
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="deskpet-uia-mta", daemon=True)
        self._thread.start()
        self._ready.wait(8.0)   # 首次生成 typelib 包装可能需要数秒
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
        done = threading.Event()
        result: list = []

        def run():
            result.append(fn())
            done.set()

        self._queue.put(run)
        if not done.wait(timeout):
            self.errors += 1
            return None
        return result[0] if result else None

    # ---- 具体操作 ----
    def _find_term_controls(self, root):
        cond = self.uia.CreatePropertyCondition(30012, "TermControl")
        found = root.FindAll(4, cond)   # TreeScope_Descendants
        out = []
        for i in range(found.Length):
            out.append(found.GetElement(i))
        return out

    def discover_panes(self) -> list[PaneInfo]:
        def do():
            from actions import winkeys
            panes = []
            for hwnd, wpid, _title, cls in winkeys.enum_windows():
                if cls != "CASCADIA_HOSTING_WINDOW_CLASS":
                    continue
                try:
                    root = self.uia.ElementFromHandle(hwnd)
                except Exception:
                    continue
                for el in self._find_term_controls(root)[:MAX_PANES]:
                    try:
                        rid = tuple(int(x) for x in el.GetRuntimeId())
                        name = str(el.CurrentName or "")
                    except Exception:
                        continue
                    pane_id = (int(hwnd), rid)
                    # discover 时同步缓存 element，read_visible 无需订阅即可用
                    self._pane_elements[pane_id] = el
                    panes.append(PaneInfo(
                        pane_id=pane_id, hwnd=int(hwnd),
                        window_pid=int(wpid), title=name))
                    if len(panes) >= MAX_PANES:
                        return panes
            # 清理已消失 pane 的元素缓存
            live = {p.pane_id for p in panes}
            for pane_id in list(self._pane_elements):
                if pane_id not in live:
                    self._pane_elements.pop(pane_id, None)
            return panes
        return self._submit(do) or []

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
                punk = el.GetCurrentPattern(10014)   # UIA_TextPatternId
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
        self.calls += 1
        if self._resolve_element(pane_id) is None:
            return ""
        return self._submit(do) or ""

    def subscribe_panes(self, panes: list[PaneInfo]):
        """在 MTA 线程订阅 pane 事件；element 缓存由 discover/read 维护。"""
        def do():
            wanted = {p.pane_id for p in panes}
            for pane in panes:
                if pane.pane_id in self._subscribed:
                    continue
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
                        self._handlers.append(handler)
                        # Notification event（携带新增文本）
                        try:
                            self.uia.AddNotificationEventHandler(
                                el, 1, None, handler)   # TreeScope_Element
                        except Exception:
                            pass
                        # TextChanged fallback（无 payload，仅活动信号）
                        try:
                            self.uia.AddAutomationEventHandler(
                                20015, el, 1, None, handler)
                            self._handler_pairs.append((handler, el))
                        except Exception:
                            pass
                        self._subscribed.add(pane.pane_id)
                        break
                except Exception:
                    self.errors += 1
            # 移除不再存在 pane 的订阅标记（handler 由线程退出时统一清理）
            self._subscribed &= wanted
        self._submit(do)

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
        self.observations: dict[tuple, Observation] = {}
        self.activity: dict[tuple, float] = {}
        self.rings: dict[tuple, deque] = {}
        self._ring_len: dict[tuple, int] = {}
        self._events: deque = deque(maxlen=EVENT_QUEUE_MAX)  # UIA 线程 → 核心的有界队列
        self._event_q_lock = threading.Lock()
        self._waiting_recheck: dict[tuple, float] = {}
        self._last_discover = 0.0
        self._structure_dirty = False
        self._started = False
        # 诊断计数（不含任何终端文本）
        self.stats = {"events": 0, "dropped": 0, "visible_reads": 0,
                      "rediscoveries": 0, "triggers": 0}
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
            panes = self.backend.discover_panes()
        except Exception:
            panes = []
        new_map = {}
        for pane in panes[:MAX_PANES]:
            new_map[pane.pane_id] = pane
        if new_map != self.panes:
            self.panes = new_map
            subscribe = getattr(self.backend, "subscribe_panes", None)
            if subscribe is not None:
                try:
                    subscribe(list(new_map.values()))
                except Exception:
                    pass
            # 消失 pane 的状态一并清理
            for pane_id in list(self.observations):
                if pane_id not in new_map:
                    self.observations.pop(pane_id, None)
            for table in (self.activity, self._waiting_recheck, self.rings,
                          self._ring_len):
                for pane_id in list(table):
                    if pane_id not in new_map:
                        table.pop(pane_id, None)

    # ---- 事件处理（monitor core 线程调用） ----
    def poll(self, now: float):
        if not self._started:
            return
        if self._structure_dirty:
            self._structure_dirty = False
            self.refresh_panes(force=True)
        else:
            self.refresh_panes()

        with self._event_q_lock:
            events = list(self._events)
            self._events.clear()
        for ev in events:
            self.stats["events"] += 1
            if ev.kind == "structure":
                self._structure_dirty = True
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

        # 等待期间的 TTL 复检（plan §21）
        for pane_id in list(self._waiting_recheck):
            if pane_id not in self.panes:
                self._waiting_recheck.pop(pane_id, None)
                self.observations.pop(pane_id, None)
                continue
            if now >= self._waiting_recheck[pane_id]:
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
        """绑定 pane 的活动证据（Claude transcript 回归时的 WORKING 补充）。"""
        last = self.activity.get(pane_id, 0.0)
        if not last or now - last > grace:
            return None
        return Observation(
            source=EvidenceSource.TERMINAL, timestamp=last,
            status=Status.WORKING, phase=Phase.NONE,
            confidence=Confidence.MEDIUM,
            summary="终端活动",
            expires_at=last + grace,
        )

    def waiting_observation(self, pane_id: tuple) -> Observation | None:
        return self.observations.get(pane_id)

    def manual_bind_focused(self) -> PaneInfo | None:
        """高级修复入口：取当前焦点所在 TermControl pane。"""
        try:
            return self.backend.focused_pane()
        except Exception:
            return None


# ------------------------------------------------------------- 终端绑定解析

class TerminalResolver:
    """Agent ↔ Windows Terminal pane 的置信度绑定（plan §22-§25）。

    Windows 原生 Agent：PID 祖先链 → 唯一窗口 → CONFIRMED。
    WSL Agent：WT_SESSION 没有官方 pane 查询接口，只能用标题/cwd/
    distro 评分：唯一强候选 → HIGH；并列 → AMBIGUOUS；无 → NONE。
    只有 CONFIRMED/HIGH 绑定才允许把终端审批观察归属到该 Agent。
    """

    def __init__(self, enum_windows=None, ancestor_pids=None):
        self._enum_windows = enum_windows
        self._ancestor_pids = ancestor_pids
        self._manual: dict[str, tuple] = {}   # key → pane_id（运行期）
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

    def set_manual_binding(self, key: str, pane_id: tuple | None):
        if pane_id is None:
            self._manual.pop(key, None)
        else:
            self._manual[key] = pane_id

    def resolve(self, instances: list, panes: dict[tuple, PaneInfo],
                now: float) -> dict[str, TerminalBinding]:
        wins = self._windows()
        by_hwnd: dict[int, tuple[int, str, str]] = {}
        wt_hwnds: set[int] = set()
        for hwnd, wpid, title, cls in wins:
            by_hwnd[int(hwnd)] = (int(wpid), title, cls)
            if cls == "CASCADIA_HOSTING_WINDOW_CLASS":
                wt_hwnds.add(int(hwnd))
        panes_by_hwnd: dict[int, list[PaneInfo]] = {}
        for pane in panes.values():
            panes_by_hwnd.setdefault(pane.hwnd, []).append(pane)

        out: dict[str, TerminalBinding] = {}
        scored_pairs: list[tuple[int, str, tuple]] = []

        for inst in instances:
            key = inst.key
            manual = self._manual.get(key)
            if manual is not None and manual in panes:
                pane = panes[manual]
                wpid, _title, cls = by_hwnd.get(pane.hwnd, (0, "", ""))
                out[key] = TerminalBinding(
                    provider="windows-terminal", hwnd=pane.hwnd,
                    window_pid=wpid or pane.window_pid,
                    window_created=0.0,
                    window_class=cls or pane.window_class, title=pane.title,
                    pane_id=pane.pane_id,
                    confidence=BindingConfidence.CONFIRMED,
                    observable=True, last_seen=now)
                continue
            source = str(getattr(inst, "source", ""))
            if source == "windows" and inst.pid:
                parents = self._ancestors(inst.pid)
                matches = list(dict.fromkeys(
                    int(hwnd) for hwnd, wpid, _t, _c in wins if int(wpid) in parents))
                if len(matches) == 1:
                    hwnd = matches[0]
                    wpid, title, cls = by_hwnd.get(hwnd, (0, "", ""))
                    pane_list = panes_by_hwnd.get(hwnd, [])
                    if len(pane_list) == 1:
                        pane = pane_list[0]
                        out[key] = TerminalBinding(
                            provider="windows-terminal", hwnd=hwnd,
                            window_pid=wpid, window_created=0.0,
                            window_class=cls, title=pane.title,
                            pane_id=pane.pane_id,
                            confidence=BindingConfidence.CONFIRMED,
                            observable=True, last_seen=now)
                    else:
                        # 窗口唯一但 pane 不唯一：窗口可唤起，审批不可归属
                        out[key] = TerminalBinding(
                            provider="windows-terminal", hwnd=hwnd,
                            window_pid=wpid, window_created=0.0,
                            window_class=cls, title=title,
                            confidence=BindingConfidence.AMBIGUOUS,
                            observable=len(pane_list) > 0, last_seen=now)
                    continue
                if len(matches) > 1:
                    out[key] = TerminalBinding(
                        confidence=BindingConfidence.AMBIGUOUS, last_seen=now)
                    continue
                # 祖先链找不到窗口（非 WT 宿主或已退出）→ 落到打分路径再试
            for pane in panes.values():
                score = self._score_pane(inst, pane)
                if score > 0:
                    scored_pairs.append((score, key, pane.pane_id))
        # 贪心分配
        scored_pairs.sort(key=lambda x: (-x[0], x[1]))
        used_panes: set[tuple] = set()
        best_for_agent: dict[str, tuple[int, tuple]] = {}
        second_for_agent: dict[str, int] = {}
        for score, key, pane_id in scored_pairs:
            if key in best_for_agent:
                second_for_agent[key] = max(second_for_agent.get(key, 0), score)
                continue
            if pane_id in used_panes:
                continue
            best_for_agent[key] = (score, pane_id)
            used_panes.add(pane_id)

        total_panes = len(panes)
        for inst in instances:
            key = inst.key
            if key in out:
                continue
            best = best_for_agent.get(key)
            if best is None:
                out[key] = self._fallback_binding(inst, panes, panes_by_hwnd,
                                                  wt_hwnds, now)
                continue
            score, pane_id = best
            pane = panes[pane_id]
            second = second_for_agent.get(key, 0)
            unique_strong = score >= 3 and score > second
            only_pane = total_panes == 1 and score >= 1
            confidence = (BindingConfidence.HIGH
                          if (unique_strong or only_pane)
                          else BindingConfidence.AMBIGUOUS)
            wpid, title, cls = by_hwnd.get(pane.hwnd, (0, "", ""))
            out[key] = TerminalBinding(
                provider="windows-terminal", hwnd=pane.hwnd,
                window_pid=wpid or pane.window_pid, window_created=0.0,
                window_class=cls or pane.window_class,
                title=pane.title or title,
                pane_id=pane_id, confidence=confidence,
                observable=True, last_seen=now)
        return out

    def _score_pane(self, inst, pane: PaneInfo) -> int:
        title = _squash(pane.title)
        if not title:
            return 0
        score = 0
        comm = str(getattr(inst, "kind", None).value if getattr(inst, "kind", None) else "")
        if comm and comm in title:
            score += 3
        cwd = str(getattr(inst, "cwd", "") or "").rstrip("/")
        if cwd:
            base = cwd.rsplit("/", 1)[-1].lower()
            if base and base in title:
                score += 2
            # user@host:~/path 形态
            user = str(getattr(inst, "user", "") or "")
            if user and title.startswith(user + "@"):
                score += 1
            elif user and (user + "@") in title:
                score += 1
        distro = str(getattr(inst, "distro", "") or "").lower()
        if distro and distro in title:
            score += 1
        return score

    def _fallback_binding(self, inst, panes, panes_by_hwnd, wt_hwnds, now):
        # 唯一 Windows Terminal 窗口时可尽力唤起，但审批观察不归属
        if len(wt_hwnds) == 1:
            hwnd = next(iter(wt_hwnds))
            pane_list = panes_by_hwnd.get(hwnd, [])
            pane = pane_list[0] if len(pane_list) == 1 else None
            return TerminalBinding(
                provider="windows-terminal", hwnd=hwnd,
                window_created=0.0,
                window_class="CASCADIA_HOSTING_WINDOW_CLASS",
                title=pane.title if pane else "",
                pane_id=pane.pane_id if pane else None,
                confidence=BindingConfidence.NONE,
                observable=pane is not None, last_seen=now)
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
