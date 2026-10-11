"""DSH（DeepSeek Harness）无插件只读监听 adapter（无插件监听 plan §三/P3）。

上游事实（SourceLink 2026-10-11 §3，DSH master d743267）：
  * 数据根：``$DSH_HOME``（默认 ``~/.dsh``）下的 ``sessions/``；绝不读
    ``$DSH_HOME/.credentials.yaml``、launch token 或 Host 私有 IPC。
  * 物理布局：``sessions/--<projectKey(cwd)>--/<encodeSegment(id)>/
    session[.vN].jsonl[.zstd]``；``v0`` 是 ``session.jsonl``，当前
    ``SESSION_FORMAT_VERSION=4``；同一 session 目录同时出现两种编码的
    canonical 文件是上游也拒绝的 encoding mismatch。
  * 物理 append：每个批次一个独立、带 checksum 的 Zstandard frame
    （zstd.ts ``scanZstdFrames``）；只有完整帧可解码，尾部半帧必须跳过。
  * 事件行：``{type, seq, time, data, ignorable?}``；``turn/start``/
    ``turn/end``（reason.kind）、``step/start``/``step/end``、
    ``tool/call``/``tool/result``、``approval/asked{id,toolName}``↔
    ``approval/decided{id,outcome}`` 同 session 按 id 配对；未带
    ``ignorable=true`` 的未知类型上游自己也会拒绝重构。

安全规则（plan §二/§六）：
  * 只读已知单文件尾部；不写 cursor 到 Agent 目录；不解码半帧；
  * WAITING 仅来自完整可归属的 asked→（尚未）decided，且回放预算内的
    事件才算数——很久以前未配对的 asked 不得伪装仍在等待；
  * 打开的 turn 静默超过上限 → UNKNOWN（crash orphan 不是 Working）；
  * 未来格式版本 / 双编码并存 / 未知必需事件 → 该 session 降级 UNKNOWN；
  * ``zstandard`` 解码库只在真正需要解码 zstd 帧时才 import。
"""
import json
import os
import re
import time
from dataclasses import dataclass, field

from . import paths
from .base import BaseWatcher, FileState
from .desktop import (
    DesktopSessionSource,
    DesktopSourceSnapshot,
    SessionClaimKey,
    bounded_diagnostics,
    claims_cover,
)
from .models import (
    AgentInstance,
    AgentKind,
    AgentSurface,
    Confidence,
    EvidenceSource,
    Observation,
    Phase,
    Status,
)

# 上游 core/session/src/types.ts：SESSION_FORMAT_VERSION = 4
DSH_FORMAT_VERSION = 4

# ---------------------------------------------------------- 上游格式移植

_ZSTD_MAGIC = 0xFD2FB528
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]$")
_HEX4 = "~04X"

# 上限（plan §七：CPU/解压/内存上限）
MAX_TAIL_CHUNK = 4 * 1024 * 1024        # 单轮最多读 4MB 增量
MAX_FRAME_DECOMPRESSED = 8 * 1024 * 1024  # 单帧解压输出上限
MAX_PENDING_FRAME_BYTES = 8 * 1024 * 1024  # 半帧缓冲上限（超限视为损坏）
MAX_EVENT_LINE = 1024 * 1024            # 单行 JSON 上限
MAX_EVENTS_PER_POLL = 512               # 单轮最多消化事件数
MAX_SESSION_DIRS = 16                   # 每个 project 目录最多考察的 session 目录
MAX_PROJECT_DIRS = 32                   # desktop source 最多考察的 project 目录
APPROVAL_REPLAY_SEC = 600.0             # asked 事件回放预算
OPEN_TURN_QUIET_SEC = 120.0             # 打开的 turn 无新事件的降级时限
DONE_WINDOW_SEC = 8.0
ERROR_WINDOW_SEC = 30.0
IDLE_LEASE_SEC = 600.0
GOAL_MAX = 120
SUMMARY_MAX = 160


def _utf16_units(text: str):
    """按 JS 语义迭代 UTF-16 code units（代理对拆成两个 unit）。"""
    for ch in str(text):
        cp = ord(ch)
        if cp > 0xFFFF:
            cp -= 0x10000
            yield 0xD800 + (cp >> 10)
            yield 0xDC00 + (cp & 0x3FF)
        else:
            yield cp


def dsh_encode_segment(raw: str) -> str:
    """SessionId → 安全路径段（上游 format.ts ``encodeSegment`` 精确移植）。

    安全字符原样、其余按 code unit 转为 ``~XXXX``（大写十六进制）；
    ``.``/``..`` 显式转义，遍历（``../``、绝对路径、分隔符）被中和。
    """
    raw = str(raw or "")
    if raw == "":
        raise ValueError("cannot encode an empty path segment")
    if raw == ".":
        return "~002E"
    if raw == "..":
        return "~002E~002E"
    out = []
    for unit in _utf16_units(raw):
        ch = chr(unit)
        if ch != "~" and _SAFE_SEGMENT.match(ch):
            out.append(ch)
        else:
            out.append("~%04X" % unit)
    return "".join(out)


def dsh_decode_segment(segment: str) -> str:
    """``~XXXX`` 段 → 原始字符串（尽力而为，仅供诊断显示）。"""
    return re.sub(r"~([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)),
                  str(segment or ""))


def dsh_project_key(cwd: str) -> str:
    """cwd → project 目录名（上游 format.ts ``projectKey`` 精确移植）。

    分隔符（/ \\ :）折叠为单个 ``-``，首部 ``-`` 剥离，空值回退
    ``root``，slug 截 251 个 code unit，整体 ``--…--`` 包裹。
    """
    cwd = str(cwd or "")
    if cwd == "":
        raise ValueError("cannot encode an empty project path")
    readable: list[str] = []
    separator_run = False
    for unit in _utf16_units(cwd):
        ch = chr(unit)
        if ch in ("/", "\\", ":"):
            if not separator_run:
                readable.append("-")
            separator_run = True
        elif ch != "~" and _SAFE_SEGMENT.match(ch):
            readable.append(ch)
            separator_run = False
        else:
            readable.append("~%04X" % unit)
            separator_run = False
    slug = re.sub(r"^-+", "", "".join(readable)) or "root"
    slug = "".join(slug[i] for i in range(min(len(slug), 251)))
    return f"--{slug}--"


def dsh_project_dir(root: str, cwd: str | None) -> str:
    """上游 format.ts ``projectDir``：cwd 缺失 → ``_no-cwd``。"""
    if cwd is None:
        return os.path.join(root, "_no-cwd")
    return os.path.join(root, dsh_project_key(cwd))


def dsh_session_dir(root: str, cwd: str | None, session_id: str) -> str:
    return os.path.join(dsh_project_dir(root, cwd),
                        dsh_encode_segment(session_id))


_CANONICAL_LOG_NAME = re.compile(r"^session(?:\.v([1-9][0-9]*))?\.jsonl$")


def parse_generation_filename(name: str) -> tuple[int, str] | None:
    """canonical 世代文件名 → (version, compression)；非 canonical 为 None。

    与上游 session-format/src/filename.ts + jsonl format.ts 一致：
    v0 无后缀；临时名、大写 V、前导零、``.v0`` 都不是 canonical。
    """
    text = str(name or "")
    if text.endswith(".zstd"):
        base, compression = text[:-5], "zstd"
    else:
        base, compression = text, "none"
    m = _CANONICAL_LOG_NAME.match(base)
    if m is None:
        return None
    version = 0 if m.group(1) is None else int(m.group(1))
    return version, compression


def scan_zstd_frames(buffer: bytes,
                     max_frames: int = 4096) -> tuple[list[tuple[int, int]], int | None]:
    """结构化扫描拼接 Zstandard 帧（上游 zstd.ts ``scanZstdFrames`` 移植）。

    不解压任何 block；EOF 打断最后一帧时返回 tornStart。返回
    ([(start, end)], tornStart|None)。损坏结构（bad magic / reserved
    位 / reserved block type）抛 ValueError——与上游一致拒绝。
    """
    frames: list[tuple[int, int]] = []
    offset = 0
    length = len(buffer)
    while offset < length:
        start = offset
        if length - offset < 4:
            return frames, start
        if int.from_bytes(buffer[offset:offset + 4], "little") != _ZSTD_MAGIC:
            raise ValueError(
                f"corrupt Zstandard session log: invalid frame magic at byte {offset}")
        offset += 4
        if offset == length:
            return frames, start
        descriptor = buffer[offset]
        offset += 1
        if (descriptor & 0x18) != 0:
            raise ValueError(
                "corrupt Zstandard session log: reserved frame-header bit "
                f"at byte {offset - 1}")
        content_size_flag = descriptor >> 6
        single_segment = (descriptor & 0x20) != 0
        checksum = (descriptor & 0x04) != 0
        dictionary_flag = descriptor & 0x03
        dictionary_bytes = 4 if dictionary_flag == 3 else dictionary_flag
        content_size_bytes = (
            (1 if single_segment else 0) if content_size_flag == 0
            else 1 << content_size_flag)
        remaining = (0 if single_segment else 1) + dictionary_bytes + content_size_bytes
        if length - offset < remaining:
            return frames, start
        offset += remaining
        while True:
            if length - offset < 3:
                return frames, start
            block_header = int.from_bytes(buffer[offset:offset + 3], "little")
            offset += 3
            last_block = (block_header & 1) != 0
            block_type = (block_header >> 1) & 0x03
            block_size = block_header >> 3
            if block_type == 0x03:
                raise ValueError(
                    "corrupt Zstandard session log: reserved block type "
                    f"at byte {offset - 3}")
            payload = 1 if block_type == 0x01 else block_size
            if length - offset < payload:
                return frames, start
            offset += payload
            if last_block:
                break
        if checksum:
            if length - offset < 4:
                return frames, start
            offset += 4
        frames.append((start, offset))
        if len(frames) >= max_frames:
            return frames, None
    return frames, None


def decode_zstd_frame(frame: bytes) -> bytes:
    """解码单个完整帧并校验 checksum；解压输出有硬上限。

    ``zstandard`` 只在这里按需 import（plan §二.6：不把重依赖常驻 GUI
    进程启动路径；库缺失 → ValueError 由调用方降级）。用 stream_reader
    逐块解码：带声明内容大小的帧会绕过 decompress() 的 max_output_size，
    上限必须由读取方强制（超限即拒绝，防解压炸弹）。
    """
    try:
        import zstandard  # type: ignore
    except ImportError as exc:
        raise ValueError("zstandard library unavailable") from exc
    try:
        dctx = zstandard.ZstdDecompressor()
        out = bytearray()
        with dctx.stream_reader(frame) as reader:
            while True:
                chunk = reader.read(65536)
                if not chunk:
                    break
                out += chunk
                if len(out) > MAX_FRAME_DECOMPRESSED:
                    raise ValueError("decompressed output exceeds cap")
        return bytes(out)
    except ValueError:
        raise
    except Exception as exc:  # checksum/结构/超限都按损坏处理
        raise ValueError(f"zstd frame decode failed: {type(exc).__name__}")


# ---------------------------------------------------------- 事件契约

# 上游 KNOWN_SESSION_EVENT_TYPES（core/session/src/known-event-types.ts）
KNOWN_EVENT_TYPES = frozenset({
    "agent-preset/selected", "agent/inbox/spliced", "approval/asked",
    "approval/decided", "approval/policy", "assistant/attempt",
    "assistant/message", "command/done", "command/run", "compaction/end",
    "compaction/prune", "compaction/start", "compaction/summary",
    "deliverables/presented", "developer/message",
    "feedback/message-delete", "feedback/message-put", "feedback/record",
    "goal/change", "hook/invoked", "hook/result", "image/offload",
    "llm/retry", "llm/retry-started", "model/selection",
    "permission/preset", "plan/mode", "request/context", "request/header",
    "sandbox/mode", "schedule/change",
    "session-log-deepseek/delivery-accepted", "session/end-seed",
    "session/title", "session/title-llm-request", "step/end", "step/start",
    "subagent/catalog", "subagent/descriptor",
    "subagent/model-selection-policy", "system/message", "team/member",
    "team/message/delivered", "team/message/queued", "team/task",
    "todo/write", "tool-workflow/agent-end", "tool-workflow/agent-start",
    "tool-workflow/run-end", "tool-workflow/run-start", "tool/call",
    "tool/ptc-dispatch", "tool/ptc-dispatch-start", "tool/result",
    "turn/end", "turn/start", "user/message",
    "web/deepseek-search-llm-request", "working-directory/change",
    "workspace/changes",
})

_HEADER_REQUIRED_KEYS = ("type", "version", "id", "createdAt", "isSeeded",
                         "delegationDepth")
_HEADER_OPTIONAL_KEYS = ("cwd", "parentSession", "origin", "agentPreset")
_HEADER_KEYS = frozenset(_HEADER_REQUIRED_KEYS + _HEADER_OPTIONAL_KEYS)

_TURN_END_NEUTRAL = frozenset(
    {"aborted", "blocked", "max-tokens", "interrupted", "forked"})


@dataclass
class DshHeaderInfo:
    version: int = -1
    session_id: str = ""
    created_at: float = 0.0
    cwd: str = ""
    parent_session: str = ""
    origin: str = ""
    delegation_depth: int = 0
    valid: bool = False


def parse_header_line(obj) -> DshHeaderInfo:
    """首行 JSON → 头部身份（上游 isHeaderLine 结构校验移植）。

    未知键 / 缺失必需键 / 类型不符 → valid=False（fail closed）。
    """
    info = DshHeaderInfo()
    if not isinstance(obj, dict):
        return info
    if obj.get("type") != "session":
        return info
    if any(k not in obj for k in _HEADER_REQUIRED_KEYS):
        return info
    if any(k not in _HEADER_KEYS for k in obj):
        return info
    if not isinstance(obj.get("version"), int):
        return info
    if not isinstance(obj.get("id"), str) or not obj["id"]:
        return info
    created = obj.get("createdAt")
    if not isinstance(created, int) or created < 0:
        return info
    if not isinstance(obj.get("isSeeded"), bool):
        return info
    depth = obj.get("delegationDepth")
    if not isinstance(depth, int) or depth < 0:
        return info
    cwd = obj.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd):
        return info
    parent = obj.get("parentSession")
    if parent is not None and not isinstance(parent, str):
        return info
    origin = obj.get("origin")
    if origin is not None and origin != "subagent":
        return info
    info.version = obj["version"]
    info.session_id = obj["id"]
    info.created_at = created / 1000.0
    info.cwd = cwd or ""
    info.parent_session = parent or ""
    info.origin = origin or ""
    info.delegation_depth = depth
    info.valid = True
    return info


@dataclass
class DshEventTracker:
    """一份 session 日志的事件状态机（watcher 与 desktop source 共用）。

    只消费显式事实：turn 边界、step/tool 活动、approval 配对、
    error/done 结束原因。任何静默都不产生 WORKING/WAITING。
    """
    # 回放预算之外的旧 asked 不算 pending（plan §六 DSH 行）
    approval_replay_sec: float = APPROVAL_REPLAY_SEC
    # 事件事实
    open_turn: int = 0                 # 当前打开的 turn 号；0 = 无
    open_turn_last_ts: float = 0.0
    turn_known_over: bool = False
    pending_approvals: dict[str, tuple[str, float]] = field(default_factory=dict)
    last_tool_name: str = ""
    last_step_ts: float = 0.0
    last_assistant_ts: float = 0.0
    done_ts: float = 0.0
    error_ts: float = 0.0
    last_event_ts: float = 0.0
    last_event_wall: float = 0.0      # 读取到达时间（事件 time 缺失时兜底）
    goal: str = ""
    title: str = ""
    # 解析健康
    records_seen: int = 0
    unknown_required_type: str = ""
    parse_errors: int = 0

    def feed_event(self, obj, wall_ts: float) -> None:
        if not isinstance(obj, dict):
            self.parse_errors += 1
            return
        self.records_seen += 1
        etype = str(obj.get("type") or "")
        data = obj.get("data")
        data = data if isinstance(data, dict) else {}
        try:
            event_ts = float(obj.get("time") or 0) / 1000.0
        except (TypeError, ValueError):
            event_ts = 0.0
        ts = event_ts or wall_ts
        self.last_event_ts = max(self.last_event_ts, ts)
        self.last_event_wall = wall_ts
        if etype not in KNOWN_EVENT_TYPES and obj.get("ignorable") is not True:
            # 上游语义：未知的必需事件必须拒绝重构，不能静默跳过
            if not self.unknown_required_type:
                self.unknown_required_type = etype or "?"
            return
        if etype == "turn/start":
            try:
                self.open_turn = int(data.get("turn") or 0)
            except (TypeError, ValueError):
                self.open_turn = 0
            self.open_turn_last_ts = ts
        elif etype == "turn/end":
            try:
                ended = int(data.get("turn") or 0)
            except (TypeError, ValueError):
                ended = self.open_turn
            if not self.open_turn or ended == self.open_turn:
                self.open_turn = 0
            reason = data.get("reason")
            kind = str(reason.get("kind") or "") if isinstance(reason, dict) else ""
            self.turn_known_over = True
            if kind == "completed":
                self.done_ts = max(self.done_ts, ts)
            elif kind == "error":
                self.error_ts = max(self.error_ts, ts)
            # aborted/blocked/max-tokens/interrupted/forked：中性——
            # interrupted 是 crash 追补的 closer，绝不能算 Working/Done
        elif etype == "step/start":
            self.last_step_ts = max(self.last_step_ts, ts)
            if self.open_turn:
                self.open_turn_last_ts = max(self.open_turn_last_ts, ts)
        elif etype == "tool/call":
            name = str(data.get("name") or "")
            if name:
                self.last_tool_name = name[:SUMMARY_MAX]
            if self.open_turn:
                self.open_turn_last_ts = max(self.open_turn_last_ts, ts)
        elif etype == "assistant/message":
            self.last_assistant_ts = max(self.last_assistant_ts, ts)
            if self.open_turn:
                self.open_turn_last_ts = max(self.open_turn_last_ts, ts)
        elif etype == "approval/asked":
            rid = str(data.get("id") or "")
            if rid and ts >= self.last_event_ts - self.approval_replay_sec:
                self.pending_approvals[rid] = (
                    str(data.get("toolName") or "")[:SUMMARY_MAX], ts)
        elif etype == "approval/decided":
            self.pending_approvals.pop(str(data.get("id") or ""), None)
        elif etype == "session/title":
            title = data.get("title")
            if isinstance(title, str) and title.strip():
                self.title = title.strip()[:GOAL_MAX]
        elif etype == "user/message":
            if not self.goal:
                content = data.get("content")
                text = _first_text(content)
                if text:
                    self.goal = text[:GOAL_MAX]

    def prune_stale_approvals(self, now: float) -> None:
        """回放预算之外的未配对 asked 丢弃（历史残留不是 WAITING）。"""
        for rid in list(self.pending_approvals):
            _tool, ts = self.pending_approvals[rid]
            if now - ts > self.approval_replay_sec:
                self.pending_approvals.pop(rid, None)

    def observation(self, now: float) -> Observation | None:
        """事件事实 → Observation；证据不足返回 None（UNKNOWN）。"""
        if self.unknown_required_type:
            return None
        obs = Observation(source=EvidenceSource.SESSION,
                          timestamp=self.last_event_ts or self.last_event_wall,
                          session_bound=True)
        obs.title = self.title
        obs.goal = self.goal
        self.prune_stale_approvals(now)
        if self.pending_approvals:
            obs.status = Status.WAITING
            obs.phase = Phase.APPROVAL
            obs.confidence = Confidence.EXACT
            obs.turn_active = True
            obs.expires_at = 0.0
            tools = sorted({t for t, _ts in self.pending_approvals.values() if t})
            obs.waiting_detail = "、".join(tools)[:SUMMARY_MAX] or "等待批复"
            obs.summary = obs.waiting_detail
            return obs
        if self.error_ts and 0 <= now - self.error_ts < ERROR_WINDOW_SEC:
            obs.status = Status.ERROR
            obs.confidence = Confidence.EXACT
            return obs
        if self.open_turn:
            quiet = now - self.open_turn_last_ts
            if 0 <= quiet <= OPEN_TURN_QUIET_SEC:
                obs.status = Status.WORKING
                obs.confidence = Confidence.HIGH
                obs.turn_active = True
                if self.last_tool_name and now - self.last_step_ts <= OPEN_TURN_QUIET_SEC:
                    obs.phase = Phase.EXECUTING
                    obs.summary = self.last_tool_name
                elif self.last_assistant_ts:
                    obs.phase = Phase.THINKING
                return obs
            # 打开的 turn 长时间无事件：crash orphan，绝不能永久 Working
            return None
        if self.done_ts and 0 <= now - self.done_ts < DONE_WINDOW_SEC:
            obs.status = Status.DONE
            obs.confidence = Confidence.EXACT
            return obs
        if self.turn_known_over and self.done_ts:
            # 显式 completed turn 的余晖：DONE 窗口后回到有事实支撑的 IDLE
            if 0 <= now - self.done_ts < IDLE_LEASE_SEC:
                obs.status = Status.IDLE
                obs.confidence = Confidence.MEDIUM
                return obs
        return None


def _first_text(content) -> str:
    """user/message content → 首段文本（只取概要，隐私截断）。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
            elif isinstance(item, str) and item.strip():
                return item.strip()
    return ""


# ---------------------------------------------------------- 增量字节消费

@dataclass
class DshLogConsumer:
    """一个 DSH session 日志文件的增量解码器（纯字节 → 事件）。

    - 明文 ``.jsonl``：按行消费，残行留在缓冲；
    - ``.jsonl.zstd``：按完整 frame 消费，半帧留在缓冲直到写完整；
      解码只针对新增完整帧，绝不 replay 半帧；
    - 首行 header 决定 session 身份与格式版本；version != 4 时只保留
      身份，不再解释事件（历史世代由上游自己迁移）。
    """
    path: str = ""
    compression: str = "none"
    tracker: DshEventTracker = field(default_factory=DshEventTracker)
    header: DshHeaderInfo = field(default_factory=DshHeaderInfo)
    header_seen: bool = False
    refused: str = ""          # 非 zstd 损坏类拒绝原因（unknown/版本/双编码）
    # file_pos 只由读取方（DshFile._tail_pos / DshDesktopSource）推进；
    # _buffer 是尚未解码成事件的原始字节（zstd 半帧 / 明文残行）。
    file_pos: int = 0
    _buffer: bytes = b""
    _events_this_poll: int = 0

    def consume(self, chunk: bytes, wall_ts: float) -> None:
        if self.refused:
            return
        self._events_this_poll = 0
        self._buffer += chunk
        if self.compression == "zstd":
            self._consume_zstd(wall_ts)
        else:
            self._consume_plain(wall_ts)
        if len(self._buffer) > MAX_PENDING_FRAME_BYTES:
            # 缓冲无界增长只可能是写入端异常：拒绝该日志（fail closed）
            self.refused = "PENDING_BUFFER_OVERFLOW"
            self._buffer = b""

    def _consume_zstd(self, wall_ts: float) -> None:
        try:
            frames, _torn = scan_zstd_frames(
                self._buffer, max_frames=MAX_EVENTS_PER_POLL)
        except ValueError as exc:
            self.refused = f"CORRUPT:{exc}"
            self._buffer = b""
            return
        consumed = 0
        for start, end in frames:
            frame = self._buffer[start:end]
            try:
                plaintext = decode_zstd_frame(frame)
            except ValueError as exc:
                self.refused = f"DECODE:{exc}"
                self._buffer = b""
                return
            self._feed_plain(plaintext, wall_ts)
            consumed = end
        if consumed:
            self._buffer = self._buffer[consumed:]

    def _consume_plain(self, wall_ts: float) -> None:
        lines = self._buffer.split(b"\n")
        self._buffer = lines.pop()
        for raw in lines:
            self._feed_line(raw, wall_ts)

    def _feed_plain(self, plaintext: bytes, wall_ts: float) -> None:
        for raw in plaintext.split(b"\n"):
            self._feed_line(raw, wall_ts)

    def _feed_line(self, raw: bytes, wall_ts: float) -> None:
        if len(raw) > MAX_EVENT_LINE:
            self.tracker.parse_errors += 1
            return
        text = raw.decode("utf-8", errors="replace").strip("\r")
        if not text:
            return
        try:
            obj = json.loads(text)
        except (TypeError, ValueError):
            if not self.header_seen:
                self.refused = "HEADER_NOT_JSON"
                return
            self.tracker.parse_errors += 1
            return
        if not self.header_seen:
            info = parse_header_line(obj)
            if not info.valid:
                self.refused = "HEADER_INVALID"
                return
            self.header = info
            self.header_seen = True
            if info.version != DSH_FORMAT_VERSION:
                # 未来/历史格式：只取身份，不解释事件体
                self.refused = f"FORMAT_VERSION_{info.version}"
                return
            return
        if self._events_this_poll >= MAX_EVENTS_PER_POLL:
            return
        self._events_this_poll += 1
        self.tracker.feed_event(obj, wall_ts)


# ---------------------------------------------------------- 文件发现

def dsh_session_candidates(root: str, cwd: str,
                           window_sec: float = 180.0) -> list[tuple[float, str]]:
    """一个 cwd 的活跃 session 日志候选（mtime 新→旧，有界）。"""
    return dsh_project_candidates(dsh_project_dir(root, cwd), window_sec)


def dsh_project_candidates(project_dir: str,
                           window_sec: float = 180.0) -> list[tuple[float, str]]:
    """一个 project 目录内的活跃 session 日志候选（mtime 新→旧，有界）。

    只列该 project 目录；每个 session 目录只取 canonical 世代文件；
    双编码并存/目录不可读 → 跳过（fail closed，与上游 encodingMismatch
    / unsupported-version 行为一致）。
    """
    now = time.time()
    found: list[tuple[float, str]] = []
    try:
        entries = list(os.scandir(project_dir))
    except OSError:
        return []
    dirs = []
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False):
                dirs.append((e.stat(follow_symlinks=False).st_mtime, e.path))
        except OSError:
            continue
    dirs.sort(reverse=True)
    for _m, dir_path in dirs[:MAX_SESSION_DIRS]:
        log = _select_generation(dir_path)
        if log is None:
            continue
        try:
            mtime = os.stat(log).st_mtime
        except OSError:
            continue
        if now - mtime <= window_sec:
            found.append((mtime, log))
    found.sort(reverse=True)
    return found[:8]


def _select_generation(session_dir: str) -> str | None:
    """session 目录内选择当前世代文件（最高版本；双编码并存 → None）。"""
    try:
        names = list(os.scandir(session_dir))
    except OSError:
        return None
    canonical: list[tuple[int, str, str, float]] = []
    encodings: set[str] = set()
    for e in names:
        try:
            if not e.is_file(follow_symlinks=False):
                continue
            parsed = parse_generation_filename(e.name)
            if parsed is None:
                continue
            version, compression = parsed
            encodings.add(compression)
            canonical.append((version, compression, e.path,
                              e.stat(follow_symlinks=False).st_mtime))
        except OSError:
            continue
    if not canonical:
        return None
    if len(encodings) > 1:
        # 上游 encodingMismatch：两种物理编码并存即拒绝该 session
        return None
    canonical.sort(key=lambda item: (item[0], item[3]), reverse=True)
    version, _compression, path, _mtime = canonical[0]
    if version > DSH_FORMAT_VERSION:
        # 未来格式：绝不猜测解释（上游 SessionFormatUnsupportedError）
        return None
    return path


# ---------------------------------------------------------- CLI watcher

class DshFile(FileState):
    kind = AgentKind.DSH
    RECORD_TYPES = KNOWN_EVENT_TYPES

    def __init__(self, path: str):
        super().__init__(path)
        parsed = parse_generation_filename(os.path.basename(path))
        self.compression = parsed[1] if parsed else "none"
        self.consumer = DshLogConsumer(compression=self.compression)
        self._tail_pos = 0
        self._last_sig: tuple | None = None

    def _record_type(self, obj: dict) -> str:
        return str(obj.get("type") or "?")

    def poll_lines(self):
        """DSH 专用：按字节增量消费（zstd 帧/明文行），不走文本 tailer。"""
        arrival = time.time()
        try:
            st = os.stat(self.path)
        except OSError:
            return
        size = st.st_size
        sig = (size, getattr(st, "st_mtime_ns", 0),
               getattr(st, "st_dev", 0), getattr(st, "st_ino", 0))
        replaced = (self._last_sig is not None
                    and (sig[2:] != self._last_sig[2:]
                         or size < self._tail_pos))
        if replaced:
            self._tail_pos = 0
            self.consumer = DshLogConsumer(compression=self.compression)
        self._last_sig = sig
        if size == self._tail_pos:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self._tail_pos)
                data = f.read(min(size - self._tail_pos, MAX_TAIL_CHUNK))
                self._tail_pos = f.tell()
        except OSError:
            return
        before = self.consumer.tracker.last_event_ts
        self.consumer.consume(data, arrival)
        self.last_arrival_ts = arrival
        self.last_event_ts = max(self.last_event_ts,
                                 self.consumer.tracker.last_event_ts, before)
        self.parse_errors = self.consumer.tracker.parse_errors
        self.session_id = self.consumer.header.session_id
        self.cwd = self.consumer.header.cwd
        if self.consumer.tracker.title:
            self.title = self.consumer.tracker.title
        if not self.goal and self.consumer.tracker.goal:
            self.goal = self.consumer.tracker.goal

    def feed(self, line: str):  # pragma: no cover - DSH 走 poll_lines 字节路径
        pass

    def observation(self, now: float, cfg: dict) -> Observation | None:
        if self.consumer.refused:
            return None
        return self.consumer.tracker.observation(now)


class DshWatcher(BaseWatcher):
    """DSH CLI 终端实例 watcher：cwd → project 目录 → 最新 session 日志。

    归属规则（plan §二.1）：同 cwd 只有一个 live DSH 进程时才把最新活跃
    日志绑给它；同 cwd 多实例无法从外部区分 session → 全部不绑
    （fail closed，绝不合并/猜测）。
    """
    kind = AgentKind.DSH

    def _roots_for_source(self, source: str, instances: list) -> list[str]:
        if source == "windows":
            return [paths.dsh_sessions_root()]
        if source.startswith("wsl:"):
            distro = source.split(":", 1)[1]
            roots: list[str] = []
            seen = set()
            for inst in instances:
                if getattr(inst, "source", "") != source:
                    continue
                home = getattr(inst, "dsh_home", "") or getattr(inst, "home", "")
                if not home:
                    continue
                try:
                    linux_root = (getattr(inst, "dsh_home", "").rstrip("/")
                                  if getattr(inst, "dsh_home", "")
                                  else paths.dsh_wsl_sessions_root(home))
                except ValueError:
                    continue
                try:
                    root = paths.wsl_unc(distro, linux_root)
                except ValueError:
                    continue
                if root.lower() not in seen:
                    seen.add(root.lower())
                    roots.append(root)
            return roots
        return []

    def extra_candidates(self, source: str, instances: list) -> list[tuple[float, str]]:
        out: list[tuple[float, str]] = []
        seen: set[str] = set()
        groups = _group_instances_by_cwd(instances)
        for (_source, cwd), group in sorted(groups.items()):
            if not cwd or len(group) != 1:
                continue   # 同 cwd 多实例：归属不明，不产生候选
            inst = group[0]
            roots = self._roots_for_source(source, [inst])
            window = self.cfg.get("active_file_window_sec", 180)
            for root in roots:
                try:
                    for mtime, path in dsh_session_candidates(
                            root, cwd, float(window)):
                        if path.lower() not in seen:
                            seen.add(path.lower())
                            out.append((mtime, path))
                except Exception:
                    continue
        return out

    def _session_candidates(self, source: str, instances: list,
                            window_sec: float | None):
        # DSH 不做目录全量扫描：候选只来自实例 cwd 的正向定位
        try:
            return self.extra_candidates(source, instances)
        except Exception:
            return []

    def make_state(self, path: str) -> DshFile:
        return DshFile(path)

    def terminal_claims(self) -> frozenset:
        """已绑定 CLI 实例的会话 claim（desktop source 去重用）。"""
        claims = set()
        for key, path in self._instance_files.items():
            st = self.files.get(path)
            sid = str(getattr(st, "session_id", "") or "") if st else ""
            if sid:
                claims.add(SessionClaimKey(kind="dsh", session_id=sid,
                                           canonical_session_path=path))
        return frozenset(claims)

    def _after_files_pruned(self):
        pass

    def refresh_files(self, sources, instances=None, force=False):
        """复用 BaseWatcher 节奏，但候选始终按 cwd 正向生成（每轮重建）。"""
        now = time.time()
        instances = list(instances or [])
        current_keys = {getattr(i, "key", "") for i in instances}
        interval = self._scan_interval(
            any(k not in self._instance_files for k in current_keys))
        for source in sorted(set(sources)):
            due = (force
                   or now - self._source_last_scan.get(source, 0.0) >= interval)
            if not due:
                continue
            self._source_files[source] = list(
                self._session_candidates(source, instances, None))
            self._source_last_scan[source] = now
        bound_paths = {p for key, p in self._instance_files.items()
                       if key in current_keys and p in self.files}
        wanted: dict[str, str] = {}
        for source in sources:
            for _mtime, path in self._source_files.get(source, [])[:8]:
                wanted[path] = source
        for path in bound_paths:
            wanted[path] = self._file_source.get(path, "")
        for path in list(self.files):
            if path not in wanted:
                self.files.pop(path, None)
                self._file_source.pop(path, None)
        for path, source in wanted.items():
            if path in self.files:
                continue
            st = self.make_state(path)
            st.source = source
            try:
                size = os.path.getsize(path)
                st._tail_pos = 0 if size < 512 * 1024 else max(
                    0, size - 256 * 1024)
                st.last_mtime = os.stat(path).st_mtime
            except OSError:
                st._tail_pos = 0
            self.files[path] = st
            self._file_source[path] = source

    def _assign_files(self, instances: list):
        """同 cwd 单实例才绑定；多实例互斥不绑（不调用通用退化）。"""
        live = {getattr(i, "key", ""): i for i in instances}
        groups = _group_instances_by_cwd(instances)
        mapping: dict[str, str] = dict(self._instance_files)
        used: set[str] = set()
        # 保留仍然有效的旧绑定
        for key, path in list(mapping.items()):
            inst = live.get(key)
            st = self.files.get(path)
            if inst is not None and st is not None \
                    and st.source == getattr(inst, "source", "") \
                    and len(groups.get(
                        (_source_of(inst), _norm_cwd_of(inst)), ())) == 1:
                used.add(path)
            else:
                mapping.pop(key, None)
        for (source, cwd), group in sorted(groups.items()):
            if len(group) != 1:
                # 同 cwd 多实例：全部不绑（外部无法区分 session 归属）
                for inst in group:
                    mapping.pop(getattr(inst, "key", ""), None)
                continue
            inst = group[0]
            key = getattr(inst, "key", "")
            if mapping.get(key):
                continue
            best: tuple[float, str] | None = None
            for _m, path in self._source_files.get(source, []):
                st = self.files.get(path)
                if st is None or st.source != source or path in used:
                    continue
                if _norm_cwd_of(inst) and st.cwd \
                        and _norm_cwd_of(inst) == _norm_cwd(st.cwd):
                    if best is None or _m > best[0]:
                        best = (_m, path)
            if best is not None:
                mapping[key] = best[1]
                used.add(best[1])
        self._instance_files = mapping


def _group_instances_by_cwd(instances: list) -> dict[tuple[str, str], list]:
    groups: dict[tuple[str, str], list] = {}
    for inst in instances:
        groups.setdefault(
            (_source_of(inst), _norm_cwd_of(inst)), []).append(inst)
    return groups


def _source_of(inst) -> str:
    return str(getattr(inst, "source", "") or "")


def _norm_cwd_of(inst) -> str:
    return _norm_cwd(getattr(inst, "cwd", ""))


def _norm_cwd(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = os.path.normcase(os.path.normpath(text.rstrip("/\\")))
    return normalized.replace("/", os.sep)


# ---------------------------------------------------------- Desktop source

@dataclass
class _DshDesktopSession:
    path: str
    session_id: str = ""
    root_session_id: str = ""     # 子会话折叠后的 root id（自身为 root 时等于 session_id）
    bound_host_key: str = ""
    admitted: bool = False
    last_activity_ts: float = 0.0


class DshDesktopSource(DesktopSessionSource):
    """DSH 官方 Desktop 宿主的被动 source（共享 $DSH_HOME/sessions）。

    与 CLI watcher 的去重：CLI claims（session id / 路径）覆盖的会话绝不
    再发 desktop target。子会话（origin=subagent / parentSession）不单独
    成 pet，其审批/活动折叠进 root 会话。
    """
    kind = AgentKind.DSH

    def __init__(self, cfg: dict | None = None, sessions_root: str | None = None):
        self._cfg = dict(cfg or {})
        self._root = sessions_root or paths.dsh_sessions_root()
        self._logs: dict[str, DshLogConsumer] = {}
        self._sessions: dict[str, _DshDesktopSession] = {}
        self._child_consumers: dict[str, DshLogConsumer] = {}
        self._last_scan = 0.0
        self.refresh_count = 0

    # ------------------------------------------------------------ 公共
    def close(self) -> None:
        self._logs.clear()
        self._sessions.clear()
        self._child_consumers.clear()

    def drop_host(self, host_key: str) -> None:
        for sid in list(self._sessions):
            sess = self._sessions[sid]
            if not host_key or sess.bound_host_key == host_key:
                self._sessions.pop(sid, None)
                self._logs.pop(sess.path, None)

    def stats(self) -> dict:
        return {"dsh_desktop_sessions": len(self._sessions),
                "dsh_desktop_refreshes": self.refresh_count}

    def poll(self, hosts, claimed_sessions, now) -> DesktopSourceSnapshot:
        dsh_hosts = [h for h in hosts if h.kind is AgentKind.DSH]
        diag: list[str] = []
        if not dsh_hosts:
            self.close()
            return DesktopSourceSnapshot(
                authoritative=True, diagnostics=bounded_diagnostics(diag))
        host = _select_primary_host(dsh_hosts)
        window = float(self._cfg.get("active_file_window_sec", 180))
        if now - self._last_scan >= 3.0:
            self._last_scan = now
            self._scan_sessions(now, window, diag)
        instances: list[AgentInstance] = []
        observations: dict[str, Observation] = {}
        claims: set[SessionClaimKey] = set()
        for key, sess in sorted(self._sessions.items()):
            consumer = self._logs.get(sess.path)
            if consumer is None:
                continue
            self._tail_consumer(consumer, now)
            tracker = _merged_tracker(consumer, self._child_consumers, sess)
            if not sess.admitted:
                if not (tracker.pending_approvals
                        or (tracker.open_turn and now - tracker.open_turn_last_ts
                            <= OPEN_TURN_QUIET_SEC)
                        or (tracker.last_event_ts
                            and now - tracker.last_event_ts <= window)):
                    continue
                sess.admitted = True
                sess.bound_host_key = host.host_key
            if claims_cover(claimed_sessions, "dsh",
                            session_id=sess.session_id,
                            canonical_path=sess.path):
                continue
            inst = AgentInstance(
                kind=AgentKind.DSH, pid=0, source="windows",
                surface=AgentSurface.DESKTOP,
                host_pid=host.pid, host_process_token=host.process_token,
                host_key=host.host_key,
                logical_session_id=sess.session_id)
            obs = tracker.observation(now)
            if obs is None:
                obs = Observation(source=EvidenceSource.SESSION,
                                  timestamp=now, status=Status.UNKNOWN,
                                  session_bound=True,
                                  confidence=Confidence.UNKNOWN)
            obs.session_id = sess.session_id
            obs.session_file = sess.path
            obs.cwd = consumer.header.cwd
            if not obs.title and tracker.title:
                obs.title = tracker.title
            if not obs.goal and tracker.goal:
                obs.goal = tracker.goal
            observations[inst.key] = obs
            claims.add(SessionClaimKey(kind="dsh",
                                       session_id=sess.session_id,
                                       canonical_session_path=sess.path))
            instances.append(inst)
        return DesktopSourceSnapshot(
            instances=tuple(instances),
            observations=observations,
            claims=frozenset(claims),
            host_keys=frozenset(h.host_key for h in dsh_hosts),
            authoritative=True,
            diagnostics=bounded_diagnostics(diag))

    # ------------------------------------------------------------ 内部
    def _scan_sessions(self, now: float, window: float, diag: list[str]):
        self.refresh_count += 1
        try:
            project_dirs = list(os.scandir(self._root))
        except OSError:
            diag.append("SESSIONS_ROOT_UNREADABLE")
            return
        cands: list[tuple[float, str]] = []   # mtime, log path
        for entry in project_dirs[:MAX_PROJECT_DIRS * 4]:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                pst = entry.stat(follow_symlinks=False)
                if now - pst.st_mtime > max(window * 2, IDLE_LEASE_SEC):
                    continue
            except OSError:
                continue
            for mtime, log in dsh_project_candidates(entry.path, window):
                cands.append((mtime, log))
        cands.sort(reverse=True)
        live_paths = {os.path.normcase(log)
                      for _m, log in cands[:24]}
        for _m, log in cands[:24]:
            consumer = self._logs.get(log)
            if consumer is None:
                consumer = DshLogConsumer(
                    path=log,
                    compression=parse_generation_filename(
                        os.path.basename(log))[1])
                self._logs[log] = consumer
            self._tail_consumer(consumer, now)
            header = consumer.header
            if not consumer.header_seen or not header.session_id:
                continue
            is_child = bool(header.parent_session or header.origin == "subagent")
            if is_child:
                self._child_consumers.setdefault(log, consumer)
                continue
            sid = header.session_id
            sess = self._sessions.get(sid)
            if sess is None or sess.path != log:
                sess = _DshDesktopSession(
                    path=log, session_id=sid, root_session_id=sid)
                self._sessions[sid] = sess
            sess.last_activity_ts = max(sess.last_activity_ts,
                                        consumer.tracker.last_event_ts)
        # 子会话折叠：parentSession → root 会话（root 未跟踪则丢弃）
        for log, child in list(self._child_consumers.items()):
            parent = child.header.parent_session
            if parent and parent in self._sessions:
                continue
            self._child_consumers.pop(log, None)
        for sid in list(self._sessions):
            sess = self._sessions[sid]
            if os.path.normcase(sess.path) not in live_paths:
                self._sessions.pop(sid, None)
                self._logs.pop(sess.path, None)
        for log in list(self._logs):
            if (os.path.normcase(log) not in live_paths
                    and log not in self._child_consumers):
                self._logs.pop(log, None)

    @staticmethod
    def _tail_consumer(consumer: DshLogConsumer, now: float) -> None:
        """有界读取该日志的新增字节（明文残行留在 consumer 缓冲）。"""
        if consumer.refused or not consumer.path:
            return
        try:
            st = os.stat(consumer.path)
        except OSError:
            return
        if st.st_size <= consumer.file_pos:
            return
        try:
            with open(consumer.path, "rb") as f:
                f.seek(consumer.file_pos)
                data = f.read(min(st.st_size - consumer.file_pos,
                                  MAX_TAIL_CHUNK))
                consumer.file_pos = f.tell()
        except OSError:
            return
        consumer.consume(data, now)


def _select_primary_host(hosts):
    return min(hosts, key=lambda h: (
        float(getattr(h, "started_at", 0.0) or 0.0),
        int(getattr(h, "pid", 0) or 0), str(h.host_key)))


def _merged_tracker(consumer: DshLogConsumer,
                    children: dict[str, DshLogConsumer],
                    sess: _DshDesktopSession) -> DshEventTracker:
    """root + 折叠子会话的合并视图（复制后合并，不污染原状态）。

    子会话按 header.parentSession 精确匹配到 root（上游 seed lineage
    契约），不做路径启发式。
    """
    import copy as _copy
    merged = _copy.copy(consumer.tracker)
    if merged.pending_approvals:
        merged.pending_approvals = dict(merged.pending_approvals)
    for child in children.values():
        if child.header.parent_session != sess.session_id:
            continue
        tr = child.tracker
        for rid, item in tr.pending_approvals.items():
            merged.pending_approvals.setdefault(rid, item)
        merged.open_turn = merged.open_turn or tr.open_turn
        merged.open_turn_last_ts = max(merged.open_turn_last_ts,
                                       tr.open_turn_last_ts)
        merged.done_ts = max(merged.done_ts, tr.done_ts)
        merged.error_ts = max(merged.error_ts, tr.error_ts)
        merged.turn_known_over = merged.turn_known_over or tr.turn_known_over
        if not merged.title and tr.title:
            merged.title = tr.title
    return merged
