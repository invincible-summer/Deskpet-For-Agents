"""V3 被动观察层的数据模型。

V3 的核心原则（plan.md §4/§5/§70）：
  * Status / Phase / Mode 三者彻底分离：Plan 是 Mode 不是状态。
  * 每个状态都必须能回答"我是根据什么认为它处于这个状态"——
    Observation 携带证据来源、置信度与过期时间。
  * 静默永远不能推断 Waiting For Approve。
"""
import time
from dataclasses import dataclass, field
from enum import Enum


class AgentKind(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"
    KIMI = "kimi"
    PI = "pi"

    @property
    def label(self) -> str:
        return {
            "claude": "Claude Code",
            "codex": "Codex",
            "kimi": "Kimi",
            "pi": "pi",
        }[self.value]


class Status(str, Enum):
    IDLE = "idle"          # 进程存活 + 明确知道没有 active turn
    WORKING = "working"    # 正在工作（active turn 或近期活动证据）
    WAITING = "waiting"    # 等待审批（仅精确来源：wire ApprovalRequest / 终端可见审批 UI）
    INPUT = "input"        # 等待用户输入（不是审批）
    DONE = "done"          # 刚完成 turn（短展示窗口）
    ERROR = "error"        # Agent 或会话报告了错误
    UNKNOWN = "unknown"    # 进程在但证据不足；绝不伪装成 IDLE/WORKING


class Phase(str, Enum):
    NONE = ""
    THINKING = "thinking"
    PLANNING = "planning"
    READING = "reading"
    CODING = "coding"
    EXECUTING = "executing"
    TESTING = "testing"
    ANSWERING = "answering"
    APPROVAL = "approval"      # 等待审批
    USER_INPUT = "user_input"  # 等待用户输入


class Mode(str, Enum):
    """归一化的 Agent 模式（Plan/Default 等是模式，不是状态）。

    UNKNOWN：出现了非空但无法归一的原始值（如上游新增 "delegate"）。
    未知值不能静默吞掉——保留原始字符串供诊断展示。
    """
    NONE = ""
    UNKNOWN = "unknown"
    DEFAULT = "default"
    PLAN = "plan"
    ACCEPT_EDITS = "acceptEdits"
    AUTO = "auto"
    DONT_ASK = "dontAsk"
    BYPASS = "bypassPermissions"

    @property
    def label(self) -> str:
        return {
            "": "",
            "unknown": "Unknown",
            "default": "Default",
            "plan": "Plan",
            "acceptEdits": "Accept Edits",
            "auto": "Auto",
            "dontAsk": "Don't Ask",
            "bypassPermissions": "Bypass",
        }[self.value]


_MODE_ALIASES = {
    "": Mode.NONE,
    "default": Mode.DEFAULT,
    "code": Mode.DEFAULT,
    "execute": Mode.DEFAULT,
    "pair_programming": Mode.DEFAULT,
    "custom": Mode.DEFAULT,
    "plan": Mode.PLAN,
    "planning": Mode.PLAN,
    "acceptedits": Mode.ACCEPT_EDITS,
    "accept_edits": Mode.ACCEPT_EDITS,
    "auto": Mode.AUTO,
    "dontask": Mode.DONT_ASK,
    "dont_ask": Mode.DONT_ASK,
    "bypasspermissions": Mode.BYPASS,
    "bypass_permissions": Mode.BYPASS,
    "yolo": Mode.BYPASS,
}


def parse_mode(value) -> tuple[Mode, str]:
    """原始值 → (归一 Mode, 原始字符串)。未知非空值 → (UNKNOWN, 原始值)。"""
    raw = str(value or "").strip()
    mode = _MODE_ALIASES.get(raw.lower(), None)
    if mode is not None:
        return mode, raw
    if not raw:
        return Mode.NONE, ""
    return Mode.UNKNOWN, raw


def normalize_mode(value) -> Mode:
    return parse_mode(value)[0]


class EvidenceSource(str, Enum):
    PROCESS = "process"
    SESSION = "session"
    TERMINAL = "terminal"
    FUSED = "fused"


class Confidence(str, Enum):
    EXACT = "exact"
    HIGH = "high"
    MEDIUM = "medium"
    UNKNOWN = "unknown"


@dataclass
class Observation:
    """一条带证据语义的状态观察（plan.md §5）。

    expires_at == 0 表示该证据不过期（如 wire 中未解除的 ApprovalRequest、
    已知 active turn 的 WORKING）；> 0 表示过期后不再支撑其 status
    （如终端可见审批 UI 的 1.5s TTL、活动证据的宽限期）。
    """
    source: EvidenceSource
    timestamp: float
    status: Status | None = None
    phase: Phase | None = None
    mode: Mode = Mode.NONE
    goal: str = ""
    summary: str = ""
    confidence: Confidence = Confidence.UNKNOWN
    expires_at: float = 0.0
    # WORKING 的细分：turn_active=True 表示"已知 active turn"（不过期），
    # False 表示只是近期活动证据（依赖 expires_at 宽限）。
    turn_active: bool = False
    session_bound: bool = True   # 该观察是否来自已绑定的会话文件

    # 终端审批识别器命中的 Agent 种类（错误归属防护：绑定为 Codex 的
    # pane 上命中 Claude 审批文案时不能归属给 Codex）。
    # 泛化的终端活动观察（pane 有文本变化）必须保持 None。
    agent_kind: AgentKind | None = None
    # Mode 为 UNKNOWN 时保留的原始值（仅诊断展示，不进普通气泡）
    mode_raw: str = ""

    # 会话侧身份信息（由 watcher 填写；终端观察不需要）
    session_file: str = ""
    session_id: str = ""
    cwd: str = ""
    title: str = ""

    def live(self, now: float) -> bool:
        """证据是否仍然有效；过期证据不再支撑任何状态。"""
        if self.status is None:
            return False
        return self.expires_at <= 0 or now <= self.expires_at


@dataclass
class AgentInstance:
    """Agent 进程身份（plan.md §3.1）。

    key 包含稳定进程 incarnation（process_token），PID 被系统复用后
    不会继承上一个 Agent 的 session/terminal 绑定。
    """
    kind: AgentKind
    pid: int
    source: str                 # "windows" 或 "wsl:Ubuntu"
    process_token: str = ""     # Windows: create_time；WSL: /proc stat starttime ticks
    started_at: float = 0.0
    key: str = field(default="")
    cwd: str = ""
    # process_token 的来源：proc（/proc starttime ticks）/ create_time
    # （Windows psutil）/ fallback（metadata 失败时的稳定代次 token）
    process_token_source: str = ""
    # 匹配到的 launcher/祖先进程（npm shim 等），仅运行期诊断
    launcher_pids: tuple[int, ...] = ()

    ppid: int = 0
    uid: int | None = None
    user: str = ""

    # Unix process/session identity
    sid: int = 0
    pgid: int = 0
    tpgid: int = 0
    tty: str = ""

    # Windows Terminal inherited hints（来自 /proc/<pid>/environ allowlist）
    wt_session: str = ""
    wt_profile_id: str = ""

    # 用户 HOME（WSL：getent passwd <uid>；Windows：expanduser）
    home: str = ""

    # 只允许 allowlisted 环境变量派生的数据根
    codex_home: str = ""
    claude_config_dir: str = ""
    kimi_code_home: str = ""

    # 附加 hint（TMUX/STY/TERM_PROGRAM 归并显示，不持久化）
    terminal_hint: str = ""

    session_id: str = ""        # 会话解析成功后由 watcher 回填

    def __post_init__(self):
        if not self.key:
            identity = self.process_token or self.pid
            self.key = f"{self.source}|{self.kind.value}|{self.identity_token()}"

    def identity_token(self) -> str:
        if self.process_token:
            return f"{self.pid}|{self.process_token}"
        return str(self.pid)

    # ---- 展示辅助 ----
    @property
    def project(self) -> str:
        cwd = (self.cwd or "").rstrip("/\\")
        if not cwd:
            return ""
        name = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return name

    @property
    def distro(self) -> str:
        if self.source.startswith("wsl:"):
            return self.source.split(":", 1)[1]
        return ""

    @property
    def environment_label(self) -> str:
        if self.distro:
            text = f"WSL {self.distro}"
            if self.terminal_hint:
                text += f" · {self.terminal_hint}"
            return text
        return "Windows"

    def data_root(self, kind: AgentKind) -> str:
        """该 Agent 自己的会话数据根（Linux 路径，仅 WSL 使用）。"""
        if kind == AgentKind.CODEX:
            return self.codex_home
        if kind == AgentKind.CLAUDE:
            return self.claude_config_dir
        if kind == AgentKind.KIMI:
            return self.kimi_code_home
        return ""


class BindingConfidence(str, Enum):
    CONFIRMED = "confirmed"
    HIGH = "high"
    AMBIGUOUS = "ambiguous"
    NONE = "none"


@dataclass
class TerminalBinding:
    """Agent ↔ 终端窗口/Pane 的关联（plan.md §24）。

    WT_SESSION 没有官方 pane 查询接口，因此 confidence 是必须的：
    只有 CONFIRMED/HIGH 的绑定才允许把终端审批观察归属到该 Agent。
    """
    provider: str = "windows-terminal"
    hwnd: int = 0
    window_pid: int = 0
    window_created: float = 0.0
    window_class: str = ""
    title: str = ""
    # runtime-only UIA pane identity（TermControl RuntimeId）
    pane_id: tuple | None = None
    confidence: BindingConfidence = BindingConfidence.NONE
    observable: bool = False     # 该 pane 的可见文本是否可经 UIA 读取
    last_seen: float = 0.0
    # 绑定依据诊断（dashboard 高级诊断展示；score=最佳评分，runner_up=次佳）
    score: int = 0
    runner_up_score: int = 0
    agent_margin: int = 0       # 该 Agent 的 top1-top2 分差
    pane_margin: int = 0        # 该 pane 的 top1-top2 分差
    reason: str = ""            # 如 "kind+cwd+distro"

    def raise_target(self) -> int:
        return self.hwnd


@dataclass
class Snapshot:
    """StateReducer 融合后的最终快照，UI 只消费它。"""
    key: str
    kind: AgentKind
    source: str
    pid: int
    status: Status = Status.UNKNOWN
    phase: Phase = Phase.NONE
    mode: Mode = Mode.NONE
    title: str = ""               # 会话标题（ai-title 等）
    goal: str = ""                # ≤ goal_max_chars
    summary: str = ""             # ≤ summary_max_chars
    waiting_detail: str = ""      # WAITING 时的具体内容（如"Bash 命令需要确认"）
    session_file: str = ""
    session_id: str = ""
    session_bound: bool = False
    cwd: str = ""
    ts: float = field(default_factory=time.time)
    freshness: float = 0.0
    evidence: EvidenceSource = EvidenceSource.PROCESS
    confidence: Confidence = Confidence.UNKNOWN
    # 展示用的审批策略字符串（如"按需审批 · 工作区写入"），不是 Mode
    policy: str = ""
    # Mode 为 UNKNOWN 时的原始值（仅详情页诊断展示）
    mode_raw: str = ""
    # 会话解析器兼容性健康（OK / PARTIAL / UNKNOWN）与非敏感说明
    parser_health: str = ""
    parser_detail: str = ""
    # WSL/终端扫描退化时提示"状态可能延迟"（按实例真实 source 判定）
    stale: bool = False

    def bubble_text(self) -> str:
        candidates = (self.goal, self.summary, self.title)
        parts = []
        for value in candidates:
            value = str(value or "").strip()
            if not value or value.lower().startswith("cwd "):
                continue
            if value not in parts:
                parts.append(value)
        return " ｜ ".join(parts)


@dataclass
class AgentTarget:
    """UI 唯一操作对象（plan.md §29）：实例 + 快照 + 终端绑定。"""
    key: str
    instance: AgentInstance
    snapshot: Snapshot
    terminal: TerminalBinding | None = None
