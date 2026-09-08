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


@dataclass(frozen=True)
class SourceProbeSnapshot:
    """一个 process source 一轮探测的不可拆开结果（v4plan §3.2）。

    三态语义（不存在第四种组合的解释空间）：
      authoritative=True + instances —— 权威发现；
      authoritative=True + ()      —— 权威确认该 source 当前无匹配 Agent
                                      （本结果有资格宣布旧实例"不存在"）；
      authoritative=False          —— 无法读取；不得根据本轮结果判死，
                                      旧实例保留并标记 stale。
    """
    source: str
    generation: int
    observed_at: float
    authoritative: bool
    instances: tuple[AgentInstance, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class WindowIdentity:
    """Windows Terminal 顶层窗口的完整 incarnation 身份（v4plan §3.3）。

    HWND 会被系统复用；pid+create_time 组成 process incarnation，
    window_class 进一步收紧。四者同时一致才认为"还是发现时的那个窗口"。
    """
    hwnd: int
    pid: int
    process_created: float
    window_class: str


class WindowBindingConfidence(str, Enum):
    """窗口解析的可靠程度（v4.1.1 §4.2）。

    与 observation attribution 的置信度彻底分离：
      * CONFIRMED —— Windows native PID 祖先链唯一定位；
      * HIGH —— WSL/标题/cwd/distro 证据互相唯一，先定位 control
        再映射到其所属 window；
      * FALLBACK —— 桌面只有一个 WT 顶层窗口：允许唤起窗口，
        但不赋予 terminal evidence attribution；
      * AMBIGUOUS —— 多候选窗口，无法安全唯一定位（window=None）；
      * NONE —— 没有任何可用窗口。
    """
    CONFIRMED = "confirmed"
    HIGH = "high"
    FALLBACK = "fallback"
    AMBIGUOUS = "ambiguous"
    NONE = "none"


@dataclass
class TerminalWindowBinding:
    """Agent ↔ Windows Terminal 顶层窗口的关联（window-only，v4.1.1 §4.2）。

    只回答"这个 Agent 大概在哪个 WT 顶层窗口"；不含 Tab/Pane，
    不参与 terminal text attribution（那是 TerminalObservationBinding
    的职责）。运行期身份，绝不持久化。
    """
    provider: str = "windows-terminal"

    window: WindowIdentity | None = None
    title: str = ""

    confidence: WindowBindingConfidence = WindowBindingConfidence.NONE

    last_seen: float = 0.0
    validated_at: float = 0.0

    # 安全诊断（dashboard 高级诊断展示；禁止放 terminal text）
    reason: str = ""
    score: int = 0
    runner_up_score: int = 0

    @property
    def hwnd(self) -> int:
        return self.window.hwnd if self.window else 0


class ObservationBindingConfidence(str, Enum):
    """观察归属结果的允许置信度（v4.1.1 §4.4）。

    只有 CONFIRMED/HIGH 两个等级：AMBIGUOUS/NONE 不产生
    Agent-specific terminal evidence，根本不生成 binding。
    """
    CONFIRMED = "confirmed"
    HIGH = "high"


@dataclass(frozen=True)
class TerminalObservationBinding:
    """Agent ↔ 被观察 TermControl 的一次运行期归属（observation-only）。

    control_id 是 UIA observer 内部的短生命周期句柄，仅内存、不持久化、
    不参与 window activation、不在 Dashboard 普通诊断展示。
    """
    agent_key: str
    control_id: tuple
    confidence: ObservationBindingConfidence
    reason: str = ""


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
    """UI 唯一操作对象（plan.md §29）：实例 + 快照 + 终端窗口绑定。

    terminal_window 只承载 window 级诊断（能否唤起该 Agent 所在的
    Windows Terminal 顶层窗口）；terminal text attribution 由
    Monitor 内部的 observation binding 表负责，不进 UI 模型。
    """
    key: str
    instance: AgentInstance
    snapshot: Snapshot
    terminal_window: TerminalWindowBinding | None = None


class ActivationCode(str, Enum):
    """终端窗口激活的结果码（v4.1.1 §6.2）。fail-closed，不猜。

    window-level activation 不依赖 UIA，因此没有 UIA_UNAVAILABLE；
    也不再区分 Tab/Pane 失效（产品不承诺切换到具体 Tab/Pane）。
    """
    OK = "ok"
    AGENT_GONE = "agent_gone"       # Agent 进程已退出（exact key 不再 live）
    NO_BINDING = "no_binding"       # 没有可安全唤起的终端窗口绑定
    STALE_WINDOW = "stale_window"   # WindowIdentity 校验失败（HWND/PID 复用等）
    FOREGROUND_DENIED = "foreground_denied"  # OS 拒绝抢前台（已 Flash 提醒）


@dataclass(frozen=True)
class ActivationResult:
    """激活事务结果；detail 只含安全诊断，不含 terminal 原文。"""
    code: ActivationCode
    repaired: bool = False      # 是否经过一次 refresh/re-resolve 后成功
    detail: str = ""
