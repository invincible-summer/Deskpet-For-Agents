"""本地摘要工具：把 Agent 对话流里的关键信息压缩成气泡友好的短句。

只做规则处理（截断、去 markdown、命令合并），绝不调用 LLM，
也不把 Agent 原始输出整段搬进气泡。
"""
import json
import re

_MD_NOISE = re.compile(r"^[\s>#*\-`·•]+")
_MD_INLINE = re.compile(r"`+|\*{1,3}")
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


def activity_excerpt(value, limit: int = 160) -> str:
    """Keep the beginning of an observed action, without altering shell syntax."""
    limit = max(1, int(limit))
    text = " ".join(_ANSI.sub("", str(value or "")[:16384]).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


_TEXT_KEYS = ("objective", "goal", "task", "summary", "description", "text",
              "message", "content", "prompt", "title", "value")
_DETAIL_KEYS = ("pattern", "query", "q", "glob", "url", "file_path", "path",
                "filePath", "filepath", "target_file")
_DISPLAY_INPUT_LIMIT = 16384


def _main_text(value, depth=0) -> str:
    """Select human text from known envelopes, with a bounded traversal."""
    if depth > 5:
        return ""
    if isinstance(value, str):
        text = value[:_DISPLAY_INPUT_LIMIT].strip()
        if text.startswith(("{", "[")):
            try:
                return _main_text(json.loads(text), depth + 1)
            except (ValueError, RecursionError):
                # An incomplete/unknown envelope is not a useful task title.
                if text.startswith(("{", '[{', '["')):
                    return ""
        return text
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            text = _main_text(value.get(key), depth + 1)
            if text:
                return text
        return ""
    if isinstance(value, (list, tuple)):
        return " · ".join(text for item in value[:4]
                          if (text := _main_text(item, depth + 1)))
    return ""


def summarize_text(value, limit: int = 120) -> str:
    """Keep the task/action main clause, without rendering protocol fields."""
    text = _main_text(value)
    # A heading such as '目标' carries no useful information without its body.
    lines = text.splitlines()
    while len(lines) > 1 and lines[0].strip().lstrip("# ").rstrip(":：") in {
            "目标", "任务", "说明", "计划", "Goal", "Task", "Summary"}:
        lines.pop(0)
    return shorten("\n".join(lines), limit)


def tool_detail(value, limit: int = 120, _depth: int = 0) -> str:
    """Keep a tool's actual command/path/query, never substitute its goal."""
    if _depth > 5:
        return ""
    if isinstance(value, str):
        text = value[:_DISPLAY_INPUT_LIMIT].strip()
        if text.startswith(("{", "[")):
            try:
                return tool_detail(json.loads(text), limit, _depth + 1)
            except (ValueError, RecursionError):
                if text.startswith(("{", '[{', '["')):
                    return ""
        text = wrapped_command(text)
        if text.startswith("*** Begin Patch"):
            paths = re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", text, re.M)
            return activity_excerpt("、".join(paths[:3]), limit)
        return activity_excerpt(text, limit)
    if isinstance(value, dict):
        for key in ("cmd", "command"):
            command = value.get(key)
            if isinstance(command, str) and command:
                return activity_excerpt(wrapped_command(command), limit)
            if isinstance(command, (list, tuple)) and command:
                return activity_excerpt(" ".join(item[:_DISPLAY_INPUT_LIMIT]
                                        for item in command[:64]
                                        if isinstance(item, str)), limit)
        parts = []
        for key in _DETAIL_KEYS:
            item = value.get(key)
            if isinstance(item, (str, list, tuple)) and item:
                # Known inputs can intentionally resemble partial JSON (grep
                # patterns, for example); do not reinterpret them as envelopes.
                detail = (activity_excerpt(item, limit) if isinstance(item, str)
                          else activity_excerpt(" ".join(part[:_DISPLAY_INPUT_LIMIT]
                                                for part in item[:8]
                                                if isinstance(part, str)), limit))
                if detail and detail not in parts:
                    parts.append(detail)
            if len(parts) >= 2:
                break
        body = " · ".join(parts)
        if body:
            return activity_excerpt(body, limit)
        for key in ("arguments", "args", "action", "input", "patch", "patch_text",
                    "search_query", "queries"):
            if key in value:
                nested = tool_detail(value[key], limit, _depth + 1)
                if nested:
                    return nested
        # Descriptions, objectives, arbitrary output and metadata do not prove
        # which command or object is currently being operated on.
        return ""
    if isinstance(value, (list, tuple)):
        parts = [tool_detail(item, limit, _depth + 1) for item in value[:8]]
        return activity_excerpt(" ".join(part for part in parts if part), limit)
    return ""


def wrapped_command(value):
    """Extract a literal shell command from a Codex JavaScript tool wrapper.

    Never execute code or infer lifecycle state from source text. Dynamic
    expressions stay unavailable instead of exposing their transport wrapper.
    """
    if not isinstance(value, str):
        return value
    match = re.search(r'\btools\.exec_command\s*\(\s*\{', value)
    if match:
        # Tokenize literals atomically so a description mentioning 'cmd' cannot
        # masquerade as the outer object's command. No code is evaluated.
        tokens = re.findall(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[{}()\[\],:]|[A-Za-z_$][\w$]*|[^\s]''',
                            value[match.end():_DISPLAY_INPUT_LIMIT])
        depth = 1
        property_start = True
        for index, token in enumerate(tokens):
            if depth == 1 and property_start and token in {"cmd", '"cmd"', "'cmd'"}:
                if index + 3 < len(tokens) and tokens[index + 1] == ":":
                    literal = tokens[index + 2]
                    if tokens[index + 3] in {",", "}"} and literal[:1] in {'"', "'"}:
                        try:
                            if literal.startswith('"'):
                                return json.loads(literal)
                            # literal_eval accepts only literal data here, never
                            # variables/calls. The token is bounded to one string.
                            from ast import literal_eval
                            return literal_eval(literal)
                        except (ValueError, SyntaxError):
                            pass
                return ""
            if token in {"{", "(", "["}:
                depth += 1
            elif token in {"}", ")", "]"}:
                depth -= 1
                if depth == 0:
                    break
            property_start = token == "," and depth == 1
        return ""
    if (re.match(r'\s*(?:(?:text|image|audio|notify)\s*\(|(?:const|let|var|await)\s|//\s*@exec:)', value)
            and re.search(r'\btools\.', value)):
        return ""
    return value


def classify_tool(name: str, detail: str = "", limit: int = 90) -> str:
    """Compatibility entry point; Phase owns the category, summary the object."""
    return tool_detail(detail, limit)


def summarize_tool(name: str, detail: str = "", limit: int = 90) -> str:
    """Actual command/path/query only; missing input stays empty."""
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
