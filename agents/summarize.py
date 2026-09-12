"""本地摘要工具：把 Agent 对话流里的关键信息压缩成气泡友好的短句。

只做规则处理（截断、去 markdown、命令合并），绝不调用 LLM，
也不把 Agent 原始输出整段搬进气泡。
"""
import json
import re

_MD_NOISE = re.compile(r"^[\s>#*\-`·•]+")
_MD_INLINE = re.compile(r"[`*_/]+")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_LOG_PREFIX = re.compile(
    r"^(?:\[[^\]]{1,32}\]\s*|(?:trace|debug|info|warn|warning|error)\s*:\s*)",
    re.IGNORECASE,
)


def shorten(text: str, limit: int = 90) -> str:
    """把文本压成一条确定长度的本地摘要。

    只取首个有效行，去掉常见 Markdown/终端噪声，并优先在句末截断。
    该函数不解析协议，也不调用模型；所有调用方都可以在 UI 边界再次使用。
    """
    if not text:
        return ""
    limit = max(1, int(limit))
    s = ""
    for raw_line in str(text).splitlines():
        line = _ANSI.sub("", raw_line).strip()
        if not line or line.startswith("```"):
            continue
        line = _LOG_PREFIX.sub("", line)
        line = _MD_NOISE.sub("", _MD_INLINE.sub("", line))
        line = " ".join(line.split())
        if line:
            s = line
            break
    if not s:
        return ""
    if len(s) <= limit:
        return s
    head = s[:max(0, limit - 1)]
    for sep in ("。", "！", "？", ". ", "! ", "? ", "；", "; ", "，", ", "):
        i = head.rfind(sep)
        if i >= limit * 0.5:
            return head[: i + len(sep)].rstrip(" ,;，；")
    return head.rstrip() + "…"


def fmt_command(cmd, limit: int = 70) -> str:
    """命令列表/字符串 → 单行短命令（超长时中段省略）。"""
    limit = max(1, int(limit))
    if isinstance(cmd, (list, tuple)):
        cmd = " ".join(str(c) for c in cmd)
    s = " ".join(str(cmd).split())
    if len(s) > limit:
        if limit <= 3:
            return "…"[:limit]
        keep = limit - 1                  # 给一个“…”留位
        return s[: keep // 2] + "…" + s[-(keep - keep // 2):]
    return s


def wrapped_command(value):
    """Extract a literal shell command from a Codex JavaScript tool wrapper.

    Never execute code or infer lifecycle state from source text. Dynamic
    expressions stay as bounded source text instead of being guessed.
    """
    if not isinstance(value, str):
        return value
    match = re.search(r'\btools\.exec_command\s*\(\s*\{\s*["\']?cmd["\']?\s*:\s*("(?:\\.|[^"\\])*")\s*[,}]', value)
    if match:
        try:
            return json.loads(match.group(1))
        except ValueError:
            pass
    return value


_TOOL_RULES = (
    ("test", ("test", "pytest", "unittest", "vitest", "jest", "check"), "测试"),
    ("search", ("grep", "rg", "ripgrep", "search", "find", "glob", "query"), "搜索"),
    ("read", ("read", "cat", "head", "tail", "list", "ls", "stat", "inspect"), "读取"),
    ("write", ("write", "create", "save", "mkdir", "touch"), "写入"),
    ("edit", ("edit", "patch", "replace", "apply_patch", "modify", "delete"), "修改"),
    ("web", ("web", "browser", "fetch", "http", "url", "browse"), "查询"),
    ("execute", ("bash", "shell", "command", "exec", "run", "terminal"), "执行"),
    ("delegate", ("task", "agent", "delegate", "spawn"), "委派"),
)


def _tool_label(name: str, detail: str = "") -> str:
    """返回工具调用的稳定中文类别；未知工具只显示短名称。"""
    raw = " ".join(str(name or "").replace("_", " ").replace("-", " ").split())
    low = raw.lower()
    detail_low = str(detail or "").lower()
    # 低成本识别常见命令行工具，即使事件只给了命令正文。
    if any(word in detail_low for word in ("pytest", "npm test", "cargo test", "go test", "unittest", "vitest", "jest")):
        return "测试"
    for _kind, words, label in _TOOL_RULES:
        if any(word in low for word in words):
            return label
    if raw:
        return raw[:24]
    return "处理中"


def classify_tool(name: str, detail: str = "", limit: int = 90) -> str:
    """工具名/参数 → 适合气泡的一行本地活动摘要。

    只保留类别和有限参数，避免把完整 shell 命令或工具输入搬到界面。
    """
    limit = max(8, int(limit))
    label = _tool_label(name, detail)
    detail_s = fmt_command(detail, max(8, limit - len(label) - 2)) if detail else ""
    if detail_s:
        return shorten(f"{label}：{detail_s}", limit)
    return shorten(label, limit)


def summarize_tool(name: str, detail: str = "", limit: int = 90) -> str:
    """兼容旧 watcher 的工具摘要命名。"""
    return classify_tool(name, detail, limit)


def summarize_activity(assistant_text: str = "", tool_name: str = "",
                       tool_detail: str = "", limit: int = 120) -> str:
    """优先返回最近的助手文本，否则返回分类后的工具摘要。"""
    text = shorten(assistant_text, limit)
    return text or classify_tool(tool_name, tool_detail, limit)


def tool_line(name: str, detail: str, limit: int = 90) -> str:
    return classify_tool(name, detail, limit)


# ---- 模式名映射（本地查表） ----
CODEX_MODE = {
    "never": "免审批",
    "on-request": "按需审批",
    "on-failure": "失败时审批",
    "untrusted": "不信任",
}
CODEX_SANDBOX = {
    "read-only": "只读沙箱",
    "workspace-write": "工作区写入",
    "danger-full-access": "完全访问",
}
CLAUDE_MODE = {
    "default": "默认审批",
    "plan": "计划模式",
    "acceptEdits": "自动接受编辑",
    "bypassPermissions": "免审批",
}


def question_summary(payload, limit=120):
    """Compact structured questions without dumping option dictionaries."""
    if not isinstance(payload, dict):
        return "等待你的回复"
    questions = payload.get("questions")
    question = next((q for q in questions if isinstance(q, dict)), {}) if isinstance(questions, list) else payload
    text = question.get("question") or question.get("title") or question.get("prompt") or payload.get("message") or ""
    options = question.get("options")
    label = "等待选择回复" if options else "等待你的回复"
    return shorten(f"{label}：{text}" if text else label, limit)
