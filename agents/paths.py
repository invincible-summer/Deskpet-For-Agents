"""各 Agent 会话文件根目录定位（Windows 原生 + WSL 经 \\\\wsl.localhost 读取）。

已查证的落盘路径（2026-09）：
  Claude Code : ~/.claude/projects/<改写路径>/<uuid>.jsonl
  Codex       : ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
  Kimi CLI    : ~/.kimi/sessions/<md5(cwd)>/<uuid>/wire.jsonl
  pi          : ~/.pi/agent/sessions/<编码cwd>/<ts>_<uuid>.jsonl
"""
import os

from .models import AgentKind

WSL_HOME_GLOB = ("\\\\wsl.localhost\\{distro}\\home\\",)


def _user_home() -> str:
    return os.path.expanduser("~")


def windows_roots(kind: AgentKind) -> list[str]:
    home = _user_home()
    sub = {
        AgentKind.CLAUDE: os.path.join(".claude", "projects"),
        AgentKind.CODEX: os.path.join(".codex", "sessions"),
        AgentKind.KIMI: os.path.join(".kimi", "sessions"),
        AgentKind.PI: os.path.join(".pi", "agent", "sessions"),
    }[kind]
    return [os.path.join(home, sub)]


def wsl_roots(kind: AgentKind, distro: str, users: list[str] | None = None) -> list[str]:
    sub = {
        AgentKind.CLAUDE: ".claude/projects",
        AgentKind.CODEX: ".codex/sessions",
        AgentKind.KIMI: ".kimi/sessions",
        AgentKind.PI: ".pi/agent/sessions",
    }[kind]
    base = "\\\\wsl.localhost\\" + distro + "\\home\\"
    if users is None:
        users = list_wsl_users(base)
    return [os.path.join(base, u, sub.replace("/", os.sep)) for u in users]


def list_wsl_users(home_base: str) -> list[str]:
    """枚举 \\\\wsl.localhost\\<distro>\\home\\ 下的用户目录。"""
    try:
        return [e.name for e in os.scandir(home_base) if e.is_dir()]
    except OSError:
        return []


def session_files(kind: AgentKind, roots: list[str], window_sec: float) -> list[tuple[float, str]]:
    """在根目录下找出活跃的会话文件，按 mtime 新→旧返回 [(mtime, path)]。"""
    import time

    now = time.time()
    found: list[tuple[float, str]] = []

    def walk_jsonl(root: str, depth: int):
        # 递归深度限制，避免扫到巨大目录
        try:
            with os.scandir(root) as it:
                for e in it:
                    try:
                        if e.is_file() and e.name.endswith(".jsonl"):
                            st = e.stat()
                            if now - st.st_mtime <= window_sec:
                                found.append((st.st_mtime, e.path))
                        elif e.is_dir() and depth > 0:
                            walk_jsonl(e.path, depth - 1)
                    except OSError:
                        continue
        except OSError:
            return

    depth = {
        AgentKind.CLAUDE: 2,   # projects/<proj>/<uuid>.jsonl
        AgentKind.CODEX: 4,    # sessions/Y/M/D/rollout-*.jsonl
        AgentKind.KIMI: 3,     # sessions/<md5>/<uuid>/wire.jsonl
        AgentKind.PI: 2,       # sessions/<proj>/<file>.jsonl
    }[kind]
    for r in roots:
        walk_jsonl(r, depth)

    found.sort(reverse=True)
    # 去重 & 过滤明显的附属文件
    out, seen = [], set()
    for mtime, path in found:
        name = os.path.basename(path)
        if name.startswith(".") or "subagents" in path or ".orphaned" in name or ".superseded" in name:
            continue
        real = path.lower()
        if real in seen:
            continue
        seen.add(real)
        out.append((mtime, path))
    return out
