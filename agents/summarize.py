"""本地摘要工具：把 Agent 对话流里的关键信息压缩成气泡友好的短句。

只做规则处理（截断、去 markdown、命令合并），绝不调用 LLM，
也不把 Agent 原始输出整段搬进气泡。
"""
import re

_MD_NOISE = re.compile(r"^[\s>#*\-`·•]+")
_MD_INLINE = re.compile(r"[`*_/]+")


def shorten(text: str, limit: int = 90) -> str:
    """取首个有效行 → 去掉 markdown 噪声 → 优先在句末截断，否则硬截断。"""
    if not text:
        return ""
    s = ""
    for raw_line in str(text).splitlines():
        line = _MD_NOISE.sub("", _MD_INLINE.sub("", raw_line))
        line = " ".join(line.split())
        if line:
            s = line
            break
    if not s:
        return ""
    if len(s) <= limit:
        return s
    head = s[:limit]
    for sep in ("。", "！", "？", ". ", "! ", "? ", "；", "; ", "，", ", "):
        i = head.rfind(sep)
        if i >= limit * 0.5:
            return head[: i + (0 if len(sep) == 1 else 1)].rstrip(" ,;，；")
    return head.rstrip() + "…"


def fmt_command(cmd, limit: int = 70) -> str:
    """命令列表/字符串 → 单行短命令（超长时中段省略）。"""
    if isinstance(cmd, (list, tuple)):
        cmd = " ".join(str(c) for c in cmd)
    s = " ".join(str(cmd).split())
    if len(s) > limit:
        keep = max(4, limit - 2)          # 给“…”留位
        return s[: keep // 2] + "…" + s[-(keep - keep // 2):]
    return s


def tool_line(name: str, detail: str, limit: int = 90) -> str:
    detail = shorten(detail, max(20, limit - len(name) - 2)) if detail else ""
    return f"{name}: {detail}" if detail else str(name)


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
