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
    INPUT = "input"        # 等待用户输入（不是审批）
    DONE = "done"          # 刚完成任务（触发 special ×3）
    ERROR = "error"        # Agent 或会话报告了错误
    UNKNOWN = "unknown"    # 进程在但读不到状态（如 Codex 桌面版不落盘）


@dataclass
class ApprovalRequest:
    summary: str            # 待批复命令/内容摘要
    exact: bool = False     # True=磁盘精确检测，False=启发式推测
    # request_id 保留协议原值的字符串/整数语义；显示层可以按字符串比较，
    # 但写回 JSON-RPC 时不能把它重新编号或丢失原始类型。
    request_id: str | int = ""
    protocol: str = ""      # 产生请求的协议/适配器，例如 app-server
    thread_id: str = ""
    turn_id: str = ""
    item_id: str = ""
    state: str = "pending"  # pending / submitting / resolved / failed


@dataclass
class AgentInstance:
    kind: AgentKind
    pid: int
    source: str             # "windows" 或 "wsl:Ubuntu"
    started_at: float = 0.0
    cwd: str = ""
    key: str = field(default="")
    # 受控会话可以提供比 PID 更稳定的协议会话 ID；只读发现默认为空。
    session_id: str = ""

    def __post_init__(self):
        if not self.key:
            identity = self.session_id or self.pid
            self.key = f"{self.source}|{self.kind.value}|{identity}"


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
    # 以下字段为 UI 和受控会话共用的结构化状态。旧 watcher 只填写前面的
    # 字段也仍然是合法快照，因而全部提供保守默认值。
    phase: str = ""               # plan / thinking / reading / coding / testing ...
    goal: str = ""                # 任务目标，不能用 cwd 冒充
    summary: str = ""             # 最近一条已压缩的本地摘要
    connection: str = "readonly"  # readonly / managed
    session_id: str = ""
    turn_id: str = ""
    cwd: str = ""
    freshness: float = 0.0         # 最近事件/文件更新时间（epoch 秒）
    can_approve: bool = False      # 只有拥有精确请求通道的会话才为 True

    def bubble_text(self) -> str:
        """返回兼容旧 UI 的短文本，不把工作目录当作任务标题。"""
        candidates = (self.goal, self.summary, self.last_line, self.title)
        parts = []
        for value in candidates:
            value = str(value or "").strip()
            if not value or value.lower().startswith("cwd "):
                continue
            if value not in parts:
                parts.append(value)
        return " ｜ ".join(parts)
