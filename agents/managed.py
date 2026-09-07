"""Controlled Codex sessions backed by the Codex app-server JSON-RPC protocol.

The file watcher in :mod:`agents.codex` is intentionally read-only.  This module
is the separate, opt-in path for sessions that DeskPet starts itself.  A session
is represented by a small state object and all protocol IO happens on backend
threads.  The UI therefore only creates a key, queues work, and reads snapshots;
it never has to wait for a process or a JSON-RPC response.

Only the app-server request methods that have an explicit, typed response are
accepted here:

* ``item/commandExecution/requestApproval``
* ``item/fileChange/requestApproval``
* ``item/permissions/requestApproval``
* ``item/tool/requestUserInput`` (always answered manually)
* ``mcpServer/elicitation/request`` (always answered manually)

Unknown server requests are answered with a JSON-RPC method-not-found error.  In
particular, a quiet session never creates a guessed approval request and this
module never sends terminal keys.
"""

from __future__ import annotations

import copy
import json
import os
import queue
import shlex
import subprocess
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .summarize import classify_tool


APPROVAL_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
    }
)
USER_INPUT_METHOD = "item/tool/requestUserInput"
MCP_ELICITATION_METHOD = "mcpServer/elicitation/request"
SUPPORTED_SERVER_REQUESTS = APPROVAL_METHODS | {USER_INPUT_METHOD, MCP_ELICITATION_METHOD}
APPROVAL_DECISIONS = frozenset({"accept", "acceptForSession", "decline", "cancel"})

_MISSING = object()


def _config_get(config: Any, path: str, default: Any = None) -> Any:
    """Read dotted configuration paths from ``Config`` and plain mappings."""

    if config is None:
        return default

    # The project's Config has a dotted-path get method.  A plain dict does not,
    # so only trust a result from Config when the exact path exists.
    getter = getattr(config, "get", None)
    if getter is not None:
        try:
            value = getter(path, _MISSING)
        except TypeError:
            value = _MISSING
        if value is not _MISSING:
            return value

    node = config
    for part in path.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        else:
            return default
    return node


def _safe_copy(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _id_key(value: Any) -> tuple[str, Any]:
    """Return a type-sensitive, hashable key for a JSON-RPC request id."""

    # JSON-RPC request ids are strings or numbers.  Keep bool distinct from int
    # even though Python normally treats True as equal to 1.
    typ = type(value).__name__
    try:
        hash(value)
    except Exception:
        return typ, repr(value)
    return typ, value


def _shorten(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _error_text(value: Any) -> str:
    if isinstance(value, Mapping):
        message = value.get("message") or value.get("error") or value
        data = value.get("data")
        if data and message != value:
            message = f"{message}: {data}"
        return _shorten(message, 500)
    return _shorten(value, 500)


def _normalize_text_content(value: Any) -> str:
    """Extract visible text from a Responses-style content value."""

    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("text", "value", "content"):
            if key in value:
                result = _normalize_text_content(value[key])
                if result:
                    return result
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(
            part for item in value if (part := _normalize_text_content(item))
        )
    return ""


def _json_line(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"


@dataclass
class _Waiter:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Any = None


@dataclass
class _Pending:
    session_key: str
    wire_id: Any
    ui_id: str
    method: str
    params: dict[str, Any]
    summary: str
    state: str = "pending"
    created_at: float = field(default_factory=time.time)


@dataclass
class _Session:
    key: str
    cwd: str
    source: str
    history_limit: int
    history_item_chars: int = 20000
    live_message_limit: int = 128
    completed_item_limit: int = 512
    thread_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    title: str = ""
    goal: str = ""
    phase: str = ""
    summary: str = ""
    status: str = "connecting"
    error: str = ""
    auto: bool = False
    connection_generation: int = 0
    request_sequence: int = 0
    created_at: float = field(default_factory=time.time)
    freshness: float = field(default_factory=time.time)
    thread_started: bool = False
    resume_requested: bool = False
    active: bool = False
    history: deque[dict[str, str]] = field(default_factory=deque)
    pending: "OrderedDict[str, _Pending]" = field(default_factory=OrderedDict)
    pending_wire: dict[tuple[str, Any], _Pending] = field(default_factory=dict)
    live_messages: dict[str, dict[str, str]] = field(default_factory=dict)
    completed_items: set[str] = field(default_factory=set)
    completed_order: deque[str] = field(default_factory=deque)
    audit: deque[dict[str, Any]] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self.history = deque(maxlen=self.history_limit)
        self.audit = deque(maxlen=max(20, self.history_limit))
        self.completed_order = deque(maxlen=self.completed_item_limit)


class ManagedBackend:
    """One app-server process and its worker/reader threads.

    A backend is shared by every controlled session from one ``source``.  The
    constructor accepts factories so tests and embedders can provide an in
    memory transport without replacing the protocol logic.
    """

    def __init__(
        self,
        manager: "ManagedManager",
        source: str,
        *,
        process_factory: Callable[..., Any] | None = None,
        transport_factory: Callable[..., Any] | None = None,
        transport: Any | None = None,
    ) -> None:
        self.manager = manager
        self.source = source
        self.process_factory = process_factory
        self.transport_factory = transport_factory
        self._initial_transport = transport
        self._transport: Any | None = None
        self._process: Any | None = None
        self._reader_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._queue: queue.Queue[Any] = queue.Queue(
            maxsize=max(32, manager.queue_limit)
        )
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._opening = threading.Lock()
        self._write_lock = threading.Lock()
        self._rpc_lock = threading.Lock()
        self._waiters: dict[tuple[str, Any], _Waiter] = {}
        self._next_request_id = 0
        self._generation = 0
        self._started = False
        self._stopping = False
        self.error = ""

    @property
    def pid(self) -> int:
        process = self._process
        try:
            return int(getattr(process, "pid", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def generation(self) -> int:
        return self._generation

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name=f"deskpet-codex-{self.source}",
            daemon=True,
        )
        self._worker_thread.start()

    def enqueue(self, operation: tuple[Any, ...]) -> bool:
        if self._stop.is_set() or self._stopping:
            return False
        self.start()
        try:
            self._queue.put_nowait(operation)
            return True
        except queue.Full:
            return False

    def close(self) -> None:
        self._stopping = True
        self._stop.set()
        self._ready.clear()
        self._wake_waiters(RuntimeError("app-server stopped"))
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._close_transport()
        worker = self._worker_thread
        if worker and worker is not threading.current_thread():
            worker.join(timeout=1.5)
        reader = self._reader_thread
        if reader and reader is not threading.current_thread():
            reader.join(timeout=0.5)

    def handle_message(self, message: Mapping[str, Any]) -> None:
        """Dispatch one decoded JSON-RPC object.

        This public-ish method is useful for deterministic transport tests; real
        transports call it from the reader thread.
        """

        if not isinstance(message, Mapping):
            return
        if "id" in message and ("result" in message or "error" in message):
            self._handle_response(message)
        elif "method" in message and "id" in message:
            self.manager._handle_server_request(self, message)
        elif "method" in message:
            self.manager._handle_notification(self, message)

    # ---- process and JSON-RPC transport ---------------------------------

    def _command(self) -> list[str]:
        configured = self.manager.config_value("managed.command", "codex")
        if isinstance(configured, str):
            command = shlex.split(configured, posix=os.name != "nt")
        elif isinstance(configured, (list, tuple)):
            command = [str(part) for part in configured]
        else:
            command = ["codex"]
        if not command:
            command = ["codex"]

        # A caller may provide the complete app-server command.  Otherwise add
        # the documented subcommand and opt into the request-user-input API.
        if "app-server" not in command:
            command.append("app-server")
        extra = self.manager.config_value("managed.app_server_args", [])
        if isinstance(extra, str):
            extra = shlex.split(extra, posix=os.name != "nt")
        if isinstance(extra, (list, tuple)):
            command.extend(str(part) for part in extra)
        # ``app-server`` is marked experimental in the CLI help, but recent
        # Codex releases do not accept a literal ``--experimental`` option.
        # Only add it for an explicitly configured legacy binary that supports
        # the flag; the default command must remain runnable today.
        if bool(self.manager.config_value("managed.experimental", False)) and \
                "--experimental" not in command:
            command.append("--experimental")

        if self.source.startswith("wsl:") and not any(
            part.lower().endswith("wsl.exe") or part.lower() == "wsl" for part in command[:1]
        ):
            distro = self.source.split(":", 1)[1].strip()
            if not distro:
                raise ValueError("WSL 来源缺少发行版名称")
            wrapper = self.manager.config_value(
                "managed.wsl_command", ["wsl.exe", "-d", distro, "--"]
            )
            if isinstance(wrapper, str):
                wrapper = shlex.split(wrapper, posix=os.name != "nt")
            command = [str(part) for part in (wrapper or [])] + command
        return command

    @staticmethod
    def _call_factory(factory: Callable[..., Any], command: list[str], source: str) -> Any:
        last: Exception | None = None
        # Accommodate both common test factories: f(command, source) and
        # f(command).  No factory is called more than once successfully.
        for args in ((command, source), (command,), ()):
            try:
                return factory(*args)
            except TypeError as exc:
                last = exc
        if last is not None:
            raise last
        raise RuntimeError("无法创建 app-server 传输")

    def _open_transport(self) -> None:
        with self._opening:
            if self._ready.is_set() or self._stopping:
                return
            self.error = ""
            command = self._command()
            if self._initial_transport is not None:
                transport = self._initial_transport
                self._initial_transport = None
            elif self.transport_factory is not None:
                transport = self._call_factory(self.transport_factory, command, self.source)
            elif self.process_factory is not None:
                transport = self._call_factory(self.process_factory, command, self.source)
            else:
                transport = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            self._transport = transport
            self._process = transport if hasattr(transport, "poll") else getattr(transport, "process", None)
            self._generation += 1
            generation = self._generation
            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                args=(transport, generation),
                name=f"deskpet-codex-reader-{self.source}",
                daemon=True,
            )
            self._reader_thread.start()

            initialize = {
                "clientInfo": {
                    "name": "deskpet",
                    "title": "DeskPet",
                    "version": "1.0",
                },
                "capabilities": {"experimentalApi": True},
            }
            try:
                result, error = self._request("initialize", initialize)
                if error:
                    raise RuntimeError(_error_text(error))
                # The protocol requires this notification after the initialize
                # response and before thread/start or thread/resume.
                # ``initialized`` is a notification with no params in the
                # generated app-server schema.
                self._write({"jsonrpc": "2.0", "method": "initialized"})
                self._ready.set()
                self.manager._backend_ready(self, generation)
                self.error = ""
            except Exception as exc:
                self.error = _error_text(exc)
                self._ready.clear()
                self._wake_waiters(exc)
                self._close_transport()
                self.manager._backend_disconnected(self, self.error)
                raise

    def _reader_loop(self, transport: Any, generation: int) -> None:
        reader = getattr(transport, "stdout", None)
        if reader is None:
            reader = transport
        try:
            while not self._stop.is_set() and generation == self._generation:
                if hasattr(reader, "readline"):
                    raw = reader.readline()
                elif hasattr(reader, "recv"):
                    raw = reader.recv()
                else:
                    break
                if raw is None:
                    time.sleep(0.01)
                    continue
                if raw == "" or raw == b"":
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                # Stdio is JSONL.  Splitting also makes simple StringIO test
                # transports that return several lines at once behave naturally.
                for line in str(raw).splitlines():
                    line = line.strip()
                    if not line or line.lower().startswith("content-length:"):
                        continue
                    try:
                        obj = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    self.handle_message(obj)
        except Exception as exc:
            if not self._stop.is_set():
                self.error = _error_text(exc)
        finally:
            if generation == self._generation and not self._stopping:
                self._ready.clear()
                self._wake_waiters(RuntimeError(self.error or "app-server disconnected"))
                self._close_transport()
                self.manager._backend_disconnected(
                    self, self.error or "app-server disconnected"
                )

    def _next_id(self) -> int:
        with self._rpc_lock:
            self._next_request_id += 1
            return self._next_request_id

    def _request(self, method: str, params: Mapping[str, Any]) -> tuple[Any, Any]:
        request_id = self._next_id()
        waiter = _Waiter()
        key = _id_key(request_id)
        with self._rpc_lock:
            self._waiters[key] = waiter
        try:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": _safe_copy(dict(params)),
                }
            )
        except Exception:
            with self._rpc_lock:
                self._waiters.pop(key, None)
            raise
        timeout = float(self.manager.config_value("managed.rpc_timeout", 15.0) or 15.0)
        timeout = max(0.2, min(timeout, 120.0))
        if not waiter.event.wait(timeout):
            with self._rpc_lock:
                self._waiters.pop(key, None)
            raise TimeoutError(f"app-server 请求超时: {method}")
        return waiter.result, waiter.error

    def _handle_response(self, message: Mapping[str, Any]) -> None:
        key = _id_key(message.get("id"))
        with self._rpc_lock:
            waiter = self._waiters.pop(key, None)
        if waiter is None:
            # Server responses to server requests are not expected.  Ignore an
            # unknown id rather than accidentally resolving a newer request.
            return
        if "error" in message:
            waiter.error = message.get("error")
        else:
            waiter.result = message.get("result")
        waiter.event.set()

    def _write(self, message: Mapping[str, Any]) -> None:
        transport = self._transport
        if transport is None:
            raise RuntimeError("app-server 传输未就绪")
        with self._write_lock:
            sender = getattr(transport, "send", None)
            if sender is not None:
                try:
                    sender(_safe_copy(dict(message)))
                except TypeError:
                    sender(_json_line(message))
                return
            writer = getattr(transport, "stdin", None) or transport
            data = _json_line(message)
            try:
                writer.write(data)
            except TypeError:
                writer.write(data.encode("utf-8"))
            flush = getattr(writer, "flush", None)
            if flush is not None:
                flush()

    def _wake_waiters(self, error: Exception) -> None:
        with self._rpc_lock:
            waiters = list(self._waiters.values())
            self._waiters.clear()
        for waiter in waiters:
            waiter.error = {"code": -32000, "message": str(error)}
            waiter.event.set()

    def _close_transport(self) -> None:
        transport, process = self._transport, self._process
        self._transport = None
        self._process = None
        for obj in (getattr(transport, "stdin", None), getattr(transport, "stdout", None)):
            close = getattr(obj, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
        close = getattr(transport, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass
        if process is not None:
            poll = getattr(process, "poll", None)
            try:
                alive = poll is None or poll() is None
            except Exception:
                alive = False
            if alive:
                terminate = getattr(process, "terminate", None)
                if terminate is not None:
                    try:
                        terminate()
                    except Exception:
                        pass

    # ---- backend worker --------------------------------------------------

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                operation = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if operation is None:
                break
            if self._stop.is_set():
                break
            try:
                if not self._ready.is_set():
                    self._open_transport()
                if not self._ready.is_set():
                    raise RuntimeError(self.error or "app-server 未就绪")
                self._execute(operation)
            except Exception as exc:
                key = operation[1] if len(operation) > 1 else ""
                self.manager._operation_error(str(key), _error_text(exc))

    def _execute(self, operation: tuple[Any, ...]) -> None:
        kind = operation[0]
        key = str(operation[1]) if len(operation) > 1 else ""
        if kind == "ensure":
            self._ensure_session(key)
        elif kind == "send":
            self._ensure_session(key)
            text = str(operation[2])
            session = self.manager._session(key)
            if session is None or not session.thread_id:
                raise RuntimeError("thread/start 未返回 thread id")
            params: dict[str, Any] = {
                "threadId": session.thread_id,
                "input": [{"type": "text", "text": text}],
            }
            result, error = self._request("turn/start", params)
            if error:
                raise RuntimeError(_error_text(error))
            self.manager._on_turn_response(key, result)
        elif kind == "interrupt":
            self._ensure_session(key)
            session = self.manager._session(key)
            if session is None or not session.thread_id or not session.turn_id:
                self.manager._operation_error(key, "当前没有可中断的回合")
                return
            result, error = self._request(
                "turn/interrupt",
                {"threadId": session.thread_id, "turnId": session.turn_id},
            )
            if error:
                raise RuntimeError(_error_text(error))
            self.manager._on_interrupt_response(key, result)
        elif kind == "reply":
            # operation[2] is the original JSON-RPC id, operation[3] is the
            # typed result.  The remaining fields are a generation/turn guard:
            # a queued reply must not authorize a request that was resolved or
            # replaced while the worker was busy.
            if not self.manager._reply_is_live(
                key,
                str(operation[4]),
                operation[2],
                int(operation[5]),
                str(operation[6] or ""),
                str(operation[7] or ""),
            ):
                self.manager._on_reply_cancelled(key, str(operation[4]))
                return
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": operation[2],
                    "result": _safe_copy(operation[3]),
                }
            )
            self.manager._on_reply_written(key, str(operation[4]))

    def _ensure_session(self, key: str) -> None:
        session = self.manager._session(key)
        if session is None:
            raise RuntimeError("会话不存在")
        if session.thread_started and session.thread_id:
            return
        if session.resume_requested and session.thread_id:
            method = "thread/resume"
            params: dict[str, Any] = {"threadId": session.thread_id}
        else:
            method = "thread/start"
            params = self.manager._thread_start_params(session)
        result, error = self._request(method, params)
        if error:
            raise RuntimeError(_error_text(error))
        self.manager._on_thread_response(key, result, resumed=(method == "thread/resume"))


class ManagedManager:
    """Non-blocking manager for app-server-backed Codex sessions.

    ``config`` can be the project's ``Config`` object or a plain mapping.  A
    backend is started lazily and shared by all sessions with the same source.
    ``process_factory``/``transport_factory`` are optional test seams; normal
    callers simply use the configured ``codex`` executable.
    """

    def __init__(
        self,
        config: Any = None,
        *,
        process_factory: Callable[..., Any] | None = None,
        transport_factory: Callable[..., Any] | None = None,
        backend_factory: Callable[..., ManagedBackend] | None = None,
        queue_limit: int | None = None,
    ) -> None:
        self.config = config if config is not None else {}
        self.history_limit = self._bounded_int(
            self.config_value("managed.history_limit", 300), 300, 1, 5000
        )
        self.history_item_chars = self._bounded_int(
            self.config_value("managed.history_item_chars", 20000), 20000, 256, 200000
        )
        self.audit_limit = self._bounded_int(
            self.config_value("managed.audit_limit", 300), 300, 20, 5000
        )
        self.max_sessions = self._bounded_int(
            self.config_value("managed.max_sessions", 32), 32, 1, 256
        )
        self.live_message_limit = self._bounded_int(
            self.config_value("managed.live_message_limit", 128), 128, 8, 1024
        )
        self.completed_item_limit = self._bounded_int(
            self.config_value("managed.completed_item_limit", 512), 512, 16, 4096
        )
        self.queue_limit = self._bounded_int(
            queue_limit if queue_limit is not None else self.config_value("managed.queue_limit", 256),
            256,
            32,
            5000,
        )
        self.process_factory = process_factory
        self.transport_factory = transport_factory
        self.backend_factory = backend_factory
        self._lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}
        self._backends: dict[str, ManagedBackend] = {}
        self._counter = 0
        self._mode = str(self.config_value("connection_mode", "hybrid") or "hybrid")
        self._stopped = False

    @staticmethod
    def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(value)))
        except (TypeError, ValueError):
            return default

    def config_value(self, path: str, default: Any = None) -> Any:
        return _config_get(self.config, path, default)

    # ---- public API ------------------------------------------------------

    def create(self, cwd: str, source: str = "windows", *, thread_id: str | None = None) -> str:
        """Create a session record and return its UI key immediately."""

        cwd = str(cwd or "").strip()
        source = str(source or "").strip()
        if not cwd:
            raise ValueError("工作目录不能为空")
        if not source or any(ch in source for ch in "\r\n\x00"):
            raise ValueError("来源无效")
        if source.startswith("wsl:") and not source.split(":", 1)[1].strip():
            raise ValueError("WSL 来源缺少发行版名称")
        with self._lock:
            if self._stopped:
                raise RuntimeError("会话管理器已停止")
            if self._mode == "readonly":
                raise RuntimeError("当前为只读监听模式，无法新建受控会话")
            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError("受控会话数量已达到上限")
            self._counter += 1
            key = f"managed|{source}|{self._counter}"
            session = _Session(
                key=key,
                cwd=cwd,
                source=source,
                history_limit=self.history_limit,
                history_item_chars=self.history_item_chars,
                live_message_limit=self.live_message_limit,
                completed_item_limit=self.completed_item_limit,
            )
            if thread_id:
                session.thread_id = str(thread_id)
                session.resume_requested = True
            self._sessions[key] = session
            backend = self._backend_for(source)
            queued = backend.enqueue(("ensure", key))
            if not queued:
                session.status = "error"
                session.error = "无法排队启动 app-server"
            return key

    def resume(self, key: str, thread_id: str | None = None) -> tuple[bool, str]:
        """Resume a persisted thread through the manager-owned backend."""

        with self._lock:
            session = self._sessions.get(str(key))
            if session is None:
                return False, "会话不存在"
            if thread_id is not None:
                value = str(thread_id).strip()
                if not value:
                    return False, "thread id 不能为空"
                session.thread_id = value
            if not session.thread_id:
                return False, "会话还没有 thread id"
            session.thread_started = False
            session.resume_requested = True
            session.error = ""
            session.status = "connecting"
            backend = self._backends.get(session.source)
            if backend is None:
                backend = self._backend_for(session.source)
            if not backend.enqueue(("ensure", session.key)):
                session.status = "error"
                session.error = "无法排队恢复会话"
                return False, session.error
            return True, "已排队恢复"

    def send(self, key: str, text: str) -> tuple[bool, str]:
        text = str(text or "").strip()
        if not text:
            return False, "消息不能为空"
        with self._lock:
            session = self._sessions.get(str(key))
            if session is None:
                return False, "会话不存在"
            if self._stopped:
                return False, "会话管理器已停止"
            self._history_append(session, "user", text)
            session.status = "working"
            session.active = True
            self._touch(session)
            backend = self._backends.get(session.source) or self._backend_for(session.source)
            if not backend.enqueue(("send", session.key, text)):
                self._operation_error(session.key, "无法排队发送消息")
                return False, session.error
            return True, "已排队"

    def interrupt(self, key: str) -> tuple[bool, str]:
        with self._lock:
            session = self._sessions.get(str(key))
            if session is None:
                return False, "会话不存在"
            backend = self._backends.get(session.source)
            if backend is None or not backend.enqueue(("interrupt", session.key)):
                return False, "无法排队中断请求"
            session.status = "working"
            self._touch(session)
            return True, "已请求中断"

    def approve(self, key: str, request_id: Any, decision: Any) -> tuple[bool, str]:
        """Submit a typed app-server approval response for one exact request."""

        with self._lock:
            session = self._sessions.get(str(key))
            if session is None:
                return False, "会话不存在"
            pending = self._find_pending(session, request_id)
            if pending is None:
                return False, "请求已失效或不存在"
            if pending.method not in APPROVAL_METHODS:
                return False, "该请求需要填写表单，不能用批准按钮回复"
            if pending.state != "pending":
                return False, "请求正在提交或已经处理"
            result = self._approval_result(pending, decision)
            if result is None:
                return False, "审批决定无效"
            return self._submit_pending(session, pending, result, decision)

    def answer(self, key: str, request_id: Any, answers: Any) -> tuple[bool, str]:
        """Submit the manual ``item/tool/requestUserInput`` answer payload."""

        with self._lock:
            session = self._sessions.get(str(key))
            if session is None:
                return False, "会话不存在"
            pending = self._find_pending(session, request_id)
            if pending is None:
                return False, "请求已失效或不存在"
            if pending.method not in {USER_INPUT_METHOD, MCP_ELICITATION_METHOD}:
                return False, "该请求是审批请求，请使用批准或拒绝"
            if pending.state != "pending":
                return False, "请求正在提交或已经处理"
            if pending.method == MCP_ELICITATION_METHOD:
                result = self._mcp_result(answers)
                if result is None:
                    return False, "表单回复格式无效；请填写对象或选择取消"
            else:
                normalized = self._normalize_answers(answers)
                if normalized is None:
                    return False, "回复格式应为 question id 到 answers 列表"
                result = {"answers": normalized}
            return self._submit_pending(
                session,
                pending,
                result,
                "manual-answer",
            )

    def cancel(self, key: str, request_id: Any) -> tuple[bool, str]:
        """Cancel a manual MCP elicitation form without providing content."""

        with self._lock:
            session = self._sessions.get(str(key))
            if session is None:
                return False, "会话不存在"
            pending = self._find_pending(session, request_id)
            if pending is None:
                return False, "请求已失效或不存在"
            if pending.method != MCP_ELICITATION_METHOD:
                return False, "只有 MCP 表单请求支持取消"
            if pending.state != "pending":
                return False, "请求正在提交或已经处理"
            return self._submit_pending(session, pending, {"action": "cancel"}, "cancel")

    def set_auto(self, key: str, enabled: bool) -> tuple[bool, str]:
        """Toggle automatic approval for exactly one session.

        User-input forms are never answered automatically.  Existing approval
        requests are submitted once when the switch is enabled.
        """

        with self._lock:
            session = self._sessions.get(str(key))
            if session is None:
                return False, "会话不存在"
            session.auto = bool(enabled)
            pending = [
                item
                for item in session.pending.values()
                if item.method in APPROVAL_METHODS and item.state == "pending"
            ]
            for item in pending:
                result = self._approval_result(item, "accept")
                if result is not None:
                    self._submit_pending(session, item, result, "auto-accept")
            self._touch(session)
            return True, "已开启本会话自动批准" if session.auto else "已关闭本会话自动批准"

    def set_mode(self, mode: str) -> tuple[bool, str]:
        mode = str(mode or "").strip()
        if mode not in {"hybrid", "readonly", "managed"}:
            return False, "接入方式无效"
        with self._lock:
            self._mode = mode
            setter = getattr(self.config, "set", None)
            if setter is not None:
                try:
                    setter("connection_mode", mode)
                except Exception:
                    pass
        return True, mode

    def snapshots(self) -> dict[str, Any]:
        """Return copied ``Snapshot`` objects for Monitor integration."""

        with self._lock:
            if self._stopped:
                return {}
            return {key: self._snapshot(session) for key, session in self._sessions.items()}

    def instances(self) -> list[Any]:
        """Return AgentInstance-compatible records for Monitor integration."""

        with self._lock:
            if self._stopped:
                return []
            result = []
            for session in self._sessions.values():
                backend = self._backends.get(session.source)
                result.append(self._instance(session, backend.pid if backend else 0))
            return result

    def details(self, key: str) -> dict[str, Any]:
        """Return the stable, JSON-friendly UI session contract."""

        with self._lock:
            session = self._sessions.get(str(key))
            if session is None:
                return {}
            pending = [
                {
                    "request_id": item.ui_id,
                    "summary": item.summary,
                    "method": item.method,
                    "state": item.state,
                    "params": _safe_copy(item.params),
                }
                for item in session.pending.values()
                if item.state in {"pending", "submitting", "failed"}
            ]
            return {
                "history": _safe_copy(list(session.history)),
                "pending": pending,
                "auto": bool(session.auto),
                "error": session.error,
                "thread_id": session.thread_id,
                "session_id": session.session_id,
                "turn_id": session.turn_id,
                "cwd": session.cwd,
                "source": session.source,
                "status": session.status,
                "phase": session.phase,
                "goal": session.goal,
                "summary": session.summary,
                "connection": "managed",
                "can_approve": any(
                    item.method in APPROVAL_METHODS
                    and item.state in {"pending", "submitting"}
                    for item in session.pending.values()
                ),
                "audit": _safe_copy(list(session.audit)),
            }

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            backends = list(self._backends.values())
        for backend in backends:
            backend.close()

    close = stop

    # ---- manager/backend helpers ----------------------------------------

    def _backend_for(self, source: str) -> ManagedBackend:
        backend = self._backends.get(source)
        if backend is not None:
            return backend
        if self.backend_factory is not None:
            try:
                backend = self.backend_factory(self, source)
            except TypeError:
                backend = self.backend_factory(source)  # type: ignore[misc]
        else:
            backend = ManagedBackend(
                self,
                source,
                process_factory=self.process_factory,
                transport_factory=self.transport_factory,
            )
        self._backends[source] = backend
        backend.start()
        return backend

    def _session(self, key: str) -> _Session | None:
        with self._lock:
            return self._sessions.get(str(key))

    def _operation_error(self, key: str, error: str) -> None:
        with self._lock:
            session = self._sessions.get(str(key))
            if session is None:
                return
            session.status = "error"
            session.error = _shorten(error, 500)
            session.active = False
            self._touch(session)

    def _backend_disconnected(self, backend: ManagedBackend, error: str) -> None:
        with self._lock:
            for session in self._sessions.values():
                if session.source != backend.source:
                    continue
                # Automatic approval is deliberately a per-live-connection
                # setting.  A reconnect must never silently inherit it.
                session.auto = False
                session.thread_started = False
                session.resume_requested = bool(session.thread_id)
                for item in list(session.pending.values()):
                    item.state = "failed"
                    self._audit(session, "disconnected", item)
                session.pending.clear()
                session.pending_wire.clear()
                session.status = "error"
                session.error = _shorten(error or "app-server 已断开", 500)
                session.active = False
                self._touch(session)

    def _backend_ready(self, backend: ManagedBackend, generation: int) -> None:
        """Attach a fresh connection generation to every source session."""

        with self._lock:
            for session in self._sessions.values():
                if session.source == backend.source:
                    session.connection_generation = int(generation)

    def _reply_is_live(
        self,
        key: str,
        ui_id: str,
        wire_id: Any,
        generation: int,
        thread_id: str,
        turn_id: str,
    ) -> bool:
        """Check the identity captured when a response was queued."""

        with self._lock:
            session = self._sessions.get(str(key))
            if session is None:
                return False
            pending = session.pending.get(ui_id)
            if pending is None or pending.state != "submitting":
                return False
            if _id_key(pending.wire_id) != _id_key(wire_id):
                return False
            backend = self._backends.get(session.source)
            if backend is None or not backend.ready or backend.generation != generation:
                return False
            # Some app-server versions include thread/turn ids only on the
            # request payload and never emit the corresponding started
            # notification.  An empty local id cannot prove that a queued
            # response is stale, so apply the guard once the manager knows it.
            if thread_id and session.thread_id and session.thread_id != thread_id:
                return False
            if turn_id and session.turn_id and session.turn_id != turn_id:
                return False
            return True

    def _on_reply_cancelled(self, key: str, ui_id: str) -> None:
        with self._lock:
            session = self._sessions.get(str(key))
            if session is None:
                return
            pending = session.pending.get(ui_id)
            if pending is not None:
                self._audit(session, "cancelled-before-write", pending)
                # A stale request is no longer actionable.  Remove it instead
                # of leaving a disabled approval button in the UI.
                session.pending.pop(ui_id, None)
                session.pending_wire.pop(_id_key(pending.wire_id), None)
                pending.state = "resolved"
                if not session.pending:
                    session.status = "working" if session.active else "idle"
                self._touch(session)

    def _thread_start_params(self, session: _Session) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": session.cwd,
            "approvalPolicy": self.config_value(
                "managed.approval_policy", "on-request"
            ),
            "sandbox": self.config_value(
                "managed.sandbox", "workspace-write"
            ),
            "serviceName": "deskpet",
            "threadSource": "deskpet",
        }
        for config_key, wire_key in (
            ("managed.model", "model"),
            ("managed.personality", "personality"),
            ("managed.ephemeral", "ephemeral"),
        ):
            value = self.config_value(config_key, _MISSING)
            if value is not _MISSING and value is not None:
                params[wire_key] = value
        return params

    def _on_thread_response(self, key: str, result: Any, *, resumed: bool) -> None:
        with self._lock:
            session = self._sessions.get(str(key))
            if session is None:
                return
            result = result if isinstance(result, Mapping) else {}
            thread = result.get("thread") if isinstance(result.get("thread"), Mapping) else result
            thread_id = thread.get("id") or result.get("threadId")
            if thread_id:
                session.thread_id = str(thread_id)
            session_id = thread.get("sessionId") or thread.get("session_id")
            if session_id:
                session.session_id = str(session_id)
            if thread.get("cwd") and not session.cwd:
                session.cwd = str(thread["cwd"])
            session.thread_started = bool(session.thread_id)
            session.resume_requested = False
            session.error = ""
            session.status = "idle" if session.thread_started else "error"
            session.active = False
            self._touch(session)
            if resumed:
                turns = thread.get("turns") or result.get("turns") or []
                self._ingest_turn_history(session, turns)

    def _on_turn_response(self, key: str, result: Any) -> None:
        with self._lock:
            session = self._sessions.get(str(key))
            if session is None:
                return
            result = result if isinstance(result, Mapping) else {}
            turn = result.get("turn") if isinstance(result.get("turn"), Mapping) else result
            turn_id = turn.get("id") or result.get("turnId")
            if turn_id:
                session.turn_id = str(turn_id)
            session.status = "working"
            session.active = True
            session.error = ""
            self._touch(session)

    def _on_interrupt_response(self, key: str, result: Any) -> None:
        with self._lock:
            session = self._sessions.get(str(key))
            if session is None:
                return
            session.active = False
            session.status = "done"
            session.summary = "已请求中断"
            self._touch(session)

    def _on_reply_written(self, key: str, ui_id: str) -> None:
        with self._lock:
            session = self._sessions.get(str(key))
            if session is None:
                return
            pending = session.pending.get(ui_id)
            if pending is None:
                return
            self._audit(session, "submitted", pending)
            # Keep submitting visible until item/completed or turn/completed.
            self._touch(session)

    def _handle_server_request(self, backend: ManagedBackend, message: Mapping[str, Any]) -> None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        params = message.get("params")
        if not isinstance(params, Mapping):
            params = {}
        if method not in SUPPORTED_SERVER_REQUESTS:
            backend._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "DeskPet 不支持此 app-server 请求"},
                }
            )
            return
        session = self._session_for_params(backend.source, params)
        if session is None:
            backend._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "找不到对应的受控会话"},
                }
            )
            return

        with self._lock:
            identity = _id_key(request_id)
            duplicate = session.pending_wire.get(identity)
            if duplicate is not None:
                # A repeated delivery must not add a second button or trigger a
                # second automatic response.
                return
            ui_id = self._ui_request_id(session, request_id, backend.generation)
            copied = dict(_safe_copy(dict(params)))
            pending = _Pending(
                session_key=session.key,
                wire_id=_safe_copy(request_id),
                ui_id=ui_id,
                method=method,
                params=copied,
                summary=self._request_summary(method, copied),
            )
            session.pending[ui_id] = pending
            session.pending_wire[identity] = pending
            self._audit(session, "received", pending)
            session.status = "input" if method == USER_INPUT_METHOD else "waiting"
            session.active = True
            self._touch(session)
            should_auto = session.auto and method in APPROVAL_METHODS
            if should_auto:
                result = self._approval_result(pending, "accept")
                if result is not None:
                    self._submit_pending(session, pending, result, "auto-accept")

    def _handle_notification(self, backend: ManagedBackend, message: Mapping[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params")
        if not isinstance(params, Mapping):
            params = {}
        thread_id = params.get("threadId")
        if not thread_id and isinstance(params.get("thread"), Mapping):
            thread_id = params["thread"].get("id")
        session = self._session_for_params(backend.source, params, thread_id=thread_id)
        if session is None:
            return
        with self._lock:
            if method == "thread/started":
                self._update_thread(session, params.get("thread") or {})
                session.status = "idle" if session.thread_id else "error"
                session.thread_started = bool(session.thread_id)
            elif method == "thread/status/changed":
                status = params.get("status") or {}
                typ = status.get("type") if isinstance(status, Mapping) else str(status)
                if not session.pending:
                    if typ == "systemError":
                        session.status = "error"
                    elif typ == "active":
                        session.status = "working"
                    elif typ == "idle":
                        session.status = "idle"
            elif method == "turn/started":
                turn = params.get("turn") or {}
                if isinstance(turn, Mapping) and turn.get("id"):
                    session.turn_id = str(turn["id"])
                session.active = True
                session.status = "input" if self._has_input(session) else (
                    "waiting" if self._has_approval(session) else "working"
                )
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                if isinstance(turn, Mapping):
                    if turn.get("id"):
                        session.turn_id = str(turn["id"])
                    self._ingest_turn_items(session, turn.get("items") or [])
                self._clear_pending_for_turn(session, params.get("turnId") or session.turn_id)
                session.active = False
                session.status = "error" if session.error else "done"
            elif method == "item/started":
                self._ingest_item(session, params.get("item") or {}, completed=False)
            elif method == "item/completed":
                item = params.get("item") or {}
                self._ingest_item(session, item, completed=True)
                item_id = item.get("id") if isinstance(item, Mapping) else None
                self._clear_pending_for_item(session, item_id)
            elif method == "item/agentMessage/delta":
                item_id = str(params.get("itemId") or "")
                delta = str(params.get("delta") or "")
                if item_id and delta:
                    self._append_live_message(session, item_id, delta)
                    session.summary = _shorten(delta)
                    session.phase = session.phase or "thinking"
            elif method == "item/plan/delta":
                session.phase = "plan"
                session.summary = _shorten(params.get("delta")) or session.summary
            elif method == "turn/plan/updated":
                session.phase = "plan"
                plan = params.get("plan") or params.get("delta")
                if isinstance(plan, Mapping):
                    plan = plan.get("text") or plan.get("summary") or plan
                session.summary = _shorten(plan) or session.summary
            elif method in {"item/commandExecution/outputDelta", "item/fileChange/outputDelta"}:
                delta = _shorten(params.get("delta"))
                if delta:
                    session.summary = delta
                session.phase = "executing" if "commandExecution" in method else "coding"
            elif method == "thread/goal/updated":
                goal = params.get("goal") or {}
                session.goal = _shorten(
                    goal.get("objective") if isinstance(goal, Mapping) else goal, 240
                )
            elif method == "thread/goal/cleared":
                session.goal = ""
            elif method == "serverRequest/resolved":
                request_id = params.get("requestId")
                pending = session.pending_wire.pop(_id_key(request_id), None)
                if pending is not None:
                    pending.state = "resolved"
                    session.pending.pop(pending.ui_id, None)
                    if not session.pending:
                        session.status = "working" if session.active else "idle"
            elif method == "thread/name/updated":
                session.title = _shorten(params.get("threadName"), 120)
            elif method == "error":
                err = params.get("error") or params
                session.error = _error_text(err)
                if not params.get("willRetry"):
                    session.status = "error"
            self._touch(session)

    # ---- request and event state ----------------------------------------

    def _session_for_params(
        self,
        source: str,
        params: Mapping[str, Any],
        *,
        thread_id: Any = None,
    ) -> _Session | None:
        with self._lock:
            target = str(thread_id or params.get("threadId") or "")
            if target:
                for session in self._sessions.values():
                    if session.source == source and session.thread_id == target:
                        return session
            candidates = [
                session
                for session in self._sessions.values()
                if session.source == source
            ]
            if len(candidates) == 1:
                return candidates[0]
            starting = [session for session in candidates if not session.thread_id]
            if len(starting) == 1:
                return starting[0]
            return None

    @staticmethod
    def _request_summary(method: str, params: Mapping[str, Any]) -> str:
        if method == "item/commandExecution/requestApproval":
            command = params.get("command")
            if command:
                return _shorten(f"执行命令：{command}")
            reason = params.get("reason")
            return _shorten(reason or "请求执行命令")
        if method == "item/fileChange/requestApproval":
            reason = params.get("reason") or params.get("grantRoot")
            return _shorten(reason or "请求修改文件")
        if method == "item/permissions/requestApproval":
            reason = params.get("reason")
            return _shorten(reason or "请求额外权限")
        if method == MCP_ELICITATION_METHOD:
            return _shorten(params.get("message") or "MCP 服务需要你的回复")
        questions = params.get("questions") or []
        if isinstance(questions, list):
            visible = []
            for question in questions[:3]:
                if isinstance(question, Mapping):
                    if question.get("isSecret"):
                        visible.append("需要你的敏感回复")
                    else:
                        visible.append(str(question.get("question") or question.get("header") or ""))
            text = "；".join(v for v in visible if v)
            if text:
                return _shorten(text)
        return "需要你的回复"

    @staticmethod
    def _ui_request_id(session: _Session, wire_id: Any, generation: int) -> str:
        # The UI receives an opaque token.  It contains no authorization data,
        # but includes a live connection generation and a monotonic per-session
        # sequence so a stale button can never target a later request that reuses
        # the same JSON-RPC id.
        del wire_id  # the original value remains on _Pending.wire_id
        session.request_sequence += 1
        return f"g{int(generation)}:r{session.request_sequence}"

    @staticmethod
    def _find_pending(session: _Session, request_id: Any) -> _Pending | None:
        # Public callers must use the opaque id returned by details().  Do not
        # accept a raw wire id or a textual fallback: doing so would make a
        # stale UI event able to approve a newer request.
        if not isinstance(request_id, str):
            return None
        return session.pending.get(request_id)

    def _approval_result(self, pending: _Pending, decision: Any) -> dict[str, Any] | None:
        method = pending.method
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            if isinstance(decision, str):
                aliases = {"approve": "accept", "allow": "accept", "deny": "decline", "reject": "decline"}
                value = aliases.get(decision, decision)
                if value not in APPROVAL_DECISIONS:
                    return None
                decision = value
            elif isinstance(decision, Mapping):
                keys = set(decision)
                if keys not in ({"acceptWithExecpolicyAmendment"}, {"applyNetworkPolicyAmendment"}):
                    return None
                decision = _safe_copy(dict(decision))
            else:
                return None
            return {"decision": decision}

        # Permissions requests use a profile response rather than a decision
        # enum.  Accept means grant exactly the requested profile; an explicit
        # dict can provide a narrower profile from the UI.
        if isinstance(decision, Mapping) and "permissions" in decision:
            profile = decision.get("permissions")
            scope = decision.get("scope", "turn")
        elif isinstance(decision, Mapping):
            profile = decision
            scope = "turn"
        elif isinstance(decision, str):
            aliases = {"approve": "accept", "allow": "accept", "deny": "decline", "reject": "decline"}
            value = aliases.get(decision, decision)
            if value not in APPROVAL_DECISIONS:
                return None
            profile = pending.params.get("permissions") if value.startswith("accept") else {}
            scope = "session" if value == "acceptForSession" else "turn"
        else:
            return None
        if scope not in {"turn", "session"}:
            return None
        if not isinstance(profile, Mapping):
            return None
        safe_profile: dict[str, Any] = {}
        for name in ("fileSystem", "network"):
            if name in profile and isinstance(profile[name], Mapping):
                safe_profile[name] = _safe_copy(dict(profile[name]))
        return {"permissions": safe_profile, "scope": scope}

    @staticmethod
    def _normalize_answers(value: Any) -> dict[str, dict[str, list[str]]] | None:
        if not isinstance(value, Mapping):
            return None
        if "answers" in value and isinstance(value.get("answers"), Mapping):
            value = value["answers"]
        out: dict[str, dict[str, list[str]]] = {}
        for question_id, answer in value.items():
            if isinstance(answer, Mapping):
                answer = answer.get("answers")
            if isinstance(answer, str):
                answer = [answer]
            if not isinstance(answer, (list, tuple)) or not all(
                isinstance(item, str) for item in answer
            ):
                return None
            out[str(question_id)] = {"answers": list(answer)}
        return out

    @staticmethod
    def _mcp_result(value: Any) -> dict[str, Any] | None:
        """Normalize a form answer to the MCP elicitation response shape."""

        if isinstance(value, Mapping) and value.get("action") in {"accept", "decline", "cancel"}:
            action = str(value["action"])
            result: dict[str, Any] = {"action": action}
            if action == "accept" and "content" in value:
                if not isinstance(value["content"], Mapping):
                    return None
                result["content"] = _safe_copy(dict(value["content"]))
            if "_meta" in value:
                result["_meta"] = _safe_copy(value["_meta"])
            return result
        if not isinstance(value, Mapping):
            return None
        # The dashboard supplies field -> value pairs for form mode.  Values
        # remain structured content; app-server owns schema validation.
        return {"action": "accept", "content": _safe_copy(dict(value))}

    def _submit_pending(
        self,
        session: _Session,
        pending: _Pending,
        result: dict[str, Any],
        decision: Any,
    ) -> tuple[bool, str]:
        pending.state = "submitting"
        self._audit(session, "submit", pending, decision=decision)
        backend = self._backends.get(session.source)
        generation = backend.generation if backend is not None else 0
        thread_id = str(session.thread_id or pending.params.get("threadId") or "")
        turn_id = str(session.turn_id or pending.params.get("turnId") or "")
        if backend is None or not backend.enqueue(
            (
                "reply",
                session.key,
                pending.wire_id,
                result,
                pending.ui_id,
                generation,
                thread_id,
                turn_id,
            )
        ):
            pending.state = "failed"
            session.error = "无法排队提交请求"
            self._audit(session, "failed", pending)
            return False, session.error
        self._touch(session)
        return True, "已提交"

    def _has_approval(self, session: _Session) -> bool:
        return any(item.method in APPROVAL_METHODS for item in session.pending.values())

    def _has_input(self, session: _Session) -> bool:
        return any(item.method == USER_INPUT_METHOD for item in session.pending.values())

    @staticmethod
    def _history_append(session: _Session, role: str, text: Any) -> dict[str, str]:
        value = str(text or "")
        if len(value) > session.history_item_chars:
            value = value[: session.history_item_chars - 1] + "…"
        entry = {"role": role, "text": value}
        session.history.append(entry)
        # Deques with maxlen silently evict old objects; discard their live item
        # references so a long streaming response cannot retain old history.
        live = set(map(id, session.history))
        for item_id, item in list(session.live_messages.items()):
            if id(item) not in live:
                session.live_messages.pop(item_id, None)
        return entry

    @staticmethod
    def _append_live_message(session: _Session, item_id: str, delta: str) -> None:
        entry = session.live_messages.get(item_id)
        if entry is None:
            entry = {"role": "assistant", "text": ""}
            session.history.append(entry)
            session.live_messages[item_id] = entry
        max_chars = session.history_item_chars
        if len(entry["text"]) < max_chars:
            entry["text"] = (entry["text"] + delta)[:max_chars]
        session.completed_items.discard(item_id)
        while len(session.live_messages) > session.live_message_limit:
            old_id = next(iter(session.live_messages))
            if old_id == item_id and len(session.live_messages) == 1:
                break
            session.live_messages.pop(old_id, None)

    def _append_agent_message(self, session: _Session, item: Mapping[str, Any], completed: bool) -> None:
        item_id = str(item.get("id") or "")
        text = _normalize_text_content(item.get("text") or item.get("content"))
        if not item_id:
            if text:
                self._history_append(session, "assistant", text)
        elif item_id in session.completed_items and completed:
            return
        else:
            entry = session.live_messages.get(item_id)
            if entry is None:
                entry = self._history_append(session, "assistant", text)
                session.live_messages[item_id] = entry
            elif text and completed:
                entry["text"] = text[: session.history_item_chars]
            elif text:
                entry["text"] = (entry["text"] + text)[: session.history_item_chars]
            if completed:
                self._mark_completed(session, item_id)
        if text:
            session.summary = _shorten(text)
        session.phase = session.phase or "thinking"

    def _ingest_item(self, session: _Session, item: Any, *, completed: bool) -> None:
        if not isinstance(item, Mapping):
            return
        item_type = str(item.get("type") or "")
        if item_type == "agentMessage" or (
            item_type == "message" and item.get("role", "assistant") == "assistant"
        ):
            self._append_agent_message(session, item, completed)
            return
        if item_type == "userMessage" or (
            item_type == "message" and item.get("role") == "user"
        ):
            text = _normalize_text_content(item.get("content") or item.get("text"))
            if text and not any(entry.get("role") == "user" and entry.get("text") == text for entry in session.history):
                self._history_append(session, "user", text)
            return
        item_id = str(item.get("id") or "")
        if completed and item_id in session.completed_items:
            return
        if item_type == "commandExecution":
            command = _shorten(item.get("command"), 140)
            if command:
                # Keep the live bubble semantic and bounded even when the
                # protocol sends a full shell command.  The complete command
                # remains available in the bounded history/audit records.
                session.summary = classify_tool("commandExecution", command, 140)
            output = item.get("aggregatedOutput")
            if completed and output:
                session.summary = _shorten(output)
            session.phase = self._command_phase(command)
        elif item_type == "fileChange":
            detail = item.get("path") or item.get("file") or item.get("reason")
            session.summary = classify_tool("edit", detail, 140)
            session.phase = "coding"
        elif item_type == "plan":
            session.summary = _shorten(item.get("text")) or session.summary
            session.phase = "plan"
        if completed and item_id:
            self._mark_completed(session, item_id)

    @staticmethod
    def _command_phase(command: str) -> str:
        lower = command.lower()
        if any(word in lower for word in ("test", "pytest", "unittest", "cargo check")):
            return "testing"
        if any(word in lower for word in ("cat ", "grep ", "rg ", "find ", "ls ", "dir ", "git diff")):
            return "reading"
        if any(word in lower for word in ("sed ", "python", "node ", "npm ", "cargo ", "go ")):
            return "executing"
        return "coding" if command else "executing"

    @staticmethod
    def _mark_completed(session: _Session, item_id: str) -> None:
        if not item_id:
            return
        session.completed_items.add(item_id)
        try:
            session.completed_order.remove(item_id)
        except ValueError:
            pass
        session.completed_order.append(item_id)
        while len(session.completed_items) > session.completed_item_limit:
            oldest = session.completed_order.popleft()
            session.completed_items.discard(oldest)

    def _ingest_turn_history(self, session: _Session, turns: Any) -> None:
        if not isinstance(turns, list):
            return
        for turn in turns:
            if isinstance(turn, Mapping):
                self._ingest_turn_items(session, turn.get("items") or [])

    def _ingest_turn_items(self, session: _Session, items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            self._ingest_item(session, item, completed=True)

    @staticmethod
    def _update_thread(session: _Session, thread: Any) -> None:
        if not isinstance(thread, Mapping):
            return
        if thread.get("id"):
            session.thread_id = str(thread["id"])
        if thread.get("sessionId"):
            session.session_id = str(thread["sessionId"])
        if thread.get("cwd"):
            session.cwd = str(thread["cwd"])
        if thread.get("name"):
            session.title = _shorten(thread["name"], 120)

    @staticmethod
    def _clear_pending_for_item(session: _Session, item_id: Any) -> None:
        if not item_id:
            return
        for ui_id, item in list(session.pending.items()):
            if str(item.params.get("itemId") or "") == str(item_id):
                item.state = "resolved"
                session.pending.pop(ui_id, None)
                session.pending_wire.pop(_id_key(item.wire_id), None)

    @staticmethod
    def _clear_pending_for_turn(session: _Session, turn_id: Any) -> None:
        if not turn_id:
            return
        for ui_id, item in list(session.pending.items()):
            if str(item.params.get("turnId") or "") == str(turn_id):
                item.state = "resolved"
                session.pending.pop(ui_id, None)
                session.pending_wire.pop(_id_key(item.wire_id), None)

    @staticmethod
    def _touch(session: _Session) -> None:
        session.freshness = time.time()

    @staticmethod
    def _audit(
        session: _Session,
        event: str,
        pending: _Pending,
        *,
        decision: Any = _MISSING,
    ) -> None:
        record: dict[str, Any] = {
            "ts": time.time(),
            "event": event,
            "request_id": pending.ui_id,
            "method": pending.method,
            "summary": pending.summary,
            "state": pending.state,
        }
        if decision is not _MISSING:
            if isinstance(decision, Mapping):
                record["decision"] = _safe_copy(dict(decision))
            else:
                record["decision"] = str(decision)
        session.audit.append(record)

    # ---- Snapshot/AgentInstance compatibility --------------------------

    def _instance(self, session: _Session, pid: int) -> Any:
        from .models import AgentInstance, AgentKind

        kwargs = {
            "kind": AgentKind.CODEX,
            "pid": int(pid or 0),
            "source": session.source,
            "started_at": session.created_at,
            "cwd": session.cwd,
            "key": session.key,
            "session_id": session.session_id,
        }
        try:
            return AgentInstance(**kwargs)
        except TypeError:
            kwargs.pop("session_id", None)
            return AgentInstance(**kwargs)

    def _approval_model(self, pending: _Pending) -> Any:
        from .models import ApprovalRequest

        kwargs = {
            "summary": pending.summary,
            "exact": True,
            "request_id": pending.ui_id,
            "protocol": "app-server",
            "thread_id": str(pending.params.get("threadId") or ""),
            "turn_id": str(pending.params.get("turnId") or ""),
            "item_id": str(pending.params.get("itemId") or ""),
            "state": pending.state,
        }
        try:
            return ApprovalRequest(**kwargs)
        except TypeError:
            return ApprovalRequest(
                summary=pending.summary,
                exact=True,
                request_id=str(pending.wire_id),
            )

    def _snapshot(self, session: _Session) -> Any:
        from .models import AgentKind, Snapshot, Status

        pending = next(
            (
                item
                for item in session.pending.values()
                if item.method in APPROVAL_METHODS
                and item.state in {"pending", "submitting"}
            ),
            None,
        )
        if pending is None:
            pending = next(
                (item for item in session.pending.values() if item.state in {"pending", "submitting"}),
                None,
            )
        status_name = session.status.upper()
        status = getattr(Status, status_name, Status.UNKNOWN)
        backend = self._backends.get(session.source)
        pid = backend.pid if backend else 0
        approval = self._approval_model(pending) if pending and pending.method in APPROVAL_METHODS else None
        kwargs = {
            "key": session.key,
            "kind": AgentKind.CODEX,
            "source": session.source,
            "pid": pid,
            "status": status,
            "title": session.title or session.goal,
            "last_line": session.summary,
            "mode": "managed",
            "approval": approval,
            "session_file": "",
            "ts": session.freshness,
            "exact_waiting": approval is not None,
            "phase": session.phase,
            "goal": session.goal,
            "summary": session.summary,
            "connection": "managed",
            "session_id": session.session_id,
            "turn_id": session.turn_id,
            "cwd": session.cwd,
            "freshness": session.freshness,
            "can_approve": approval is not None,
        }
        try:
            return Snapshot(**kwargs)
        except TypeError:
            old = {
                key: value
                for key, value in kwargs.items()
                if key in {"key", "kind", "source", "pid", "status", "title", "last_line", "mode", "approval", "session_file", "ts", "exact_waiting"}
            }
            snap = Snapshot(**old)
            for key, value in kwargs.items():
                if not hasattr(snap, key):
                    try:
                        setattr(snap, key, value)
                    except Exception:
                        pass
            return snap


__all__ = [
    "APPROVAL_METHODS",
    "USER_INPUT_METHOD",
    "MCP_ELICITATION_METHOD",
    "ManagedBackend",
    "ManagedManager",
]
