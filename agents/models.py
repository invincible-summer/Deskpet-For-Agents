"""监听层的数据模型。"""
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
            "kimi": "Kimi CLI",
            "pi": "pi",
        }[self.value]


class Status(str, Enum):
    IDLE = "idle"          # 会话存活但空闲（等你输入）
    WORKING = "working"    # 正在工作
    WAITING = "waiting"    # 等待批复（可能为推测）
    DONE = "done"          # 刚完成任务（触发 special ×3）
    UNKNOWN = "unknown"    # 进程在但读不到状态（如 Codex 桌面版不落盘）


@dataclass
class ApprovalRequest:
    summary: str            # 待批复命令/内容摘要
    exact: bool = False     # True=磁盘精确检测，False=启发式推测
    request_id: str = ""


@dataclass
class AgentInstance:
    kind: AgentKind
    pid: int
    source: str             # "windows" 或 "wsl:Ubuntu"
    started_at: float = 0.0
    cwd: str = ""
    key: str = field(default="")

    def __post_init__(self):
        if not self.key:
            self.key = f"{self.source}|{self.kind.value}|{self.pid}"


@dataclass
class Snapshot:
    key: str
    kind: AgentKind
    source: str
    pid: int
    status: Status = Status.UNKNOWN
    title: str = ""              # 任务标题（ai-title / 会话主题等）
    last_line: str = ""          # 气泡显示的最新活动（本地摘要）
    mode: str = ""               # 当前模式（审批模式/沙箱等，本地查表）
    approval: ApprovalRequest | None = None
    session_file: str = ""
    ts: float = field(default_factory=time.time)
    exact_waiting: bool = False  # 审批检测是否精确

    def bubble_text(self) -> str:
        parts = []
        if self.title:
            parts.append(self.title)
        if self.last_line:
            parts.append(self.last_line)
        return " ｜ ".join(p for p in parts if p)
