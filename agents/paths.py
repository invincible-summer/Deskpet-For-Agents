"""各 Agent 会话数据根定位与安全路径规则（plan.md §8/§9/§11-§13/§35）。

已查证的落盘路径（2026-09）：
  Codex       : $CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl（默认 ~/.codex）
  Claude Code : $CLAUDE_CONFIG_DIR/projects/<改写路径>/<uuid>.jsonl（默认 ~/.claude）
                $CLAUDE_CONFIG_DIR/sessions/<pid>.json 为 PID → sessionId hint
  Kimi Code   : $KIMI_CODE_HOME/session_index.jsonl + sessions/<workDirKey>/
                <sessionId>/agents/main/wire.jsonl（默认 ~/.kimi-code；
                legacy ~/.kimi/sessions 仅作 fallback）
  pi          : ~/.pi/agent/sessions/<编码cwd>/<ts>_<uuid>.jsonl

WSL 用户 HOME 一律通过 PID → uid → getent passwd 解析，不再枚举 /home/*。
所有自动发现路径必须位于该 Agent 自己的数据 root，不递归 /mnt/c、/etc，
不跟 symlink 扫出去。
"""
import json
import os
import posixpath
import time

from .models import AgentKind

# /proc/<pid>/environ 中允许进入 Python 的变量（plan.md §7）
ENV_ALLOWLIST = (
    "WT_SESSION",
    "WT_PROFILE_ID",
    "WSL_DISTRO_NAME",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "KIMI_CODE_HOME",
    "PI_CODING_AGENT_SESSION_DIR",
    "TMUX",
    "STY",
    "TERM_PROGRAM",
)

_LEGACY_KIMI = ".kimi"


def _user_home() -> str:
    return os.path.expanduser("~")


def wsl_unc(distro: str, linux_path: str) -> str:
    """Linux 绝对路径 → Windows UNC 路径（plan.md §9）。

    安全规则：要求绝对路径、规范化、拒绝空路径与 `..` 越界；
    调用方必须保证数据根来自 HOME 或 allowlisted env，不跟随任意路径。
    """
    distro = str(distro or "").strip()
    path = str(linux_path or "").strip()
    if not distro:
        raise ValueError("wsl_unc: distro is required")
    if not path:
        raise ValueError("wsl_unc: empty path")
    if not path.startswith("/"):
        raise ValueError(f"wsl_unc: not an absolute linux path: {path!r}")
    parts = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError(f"wsl_unc: path traversal rejected: {path!r}")
        parts.append(seg)
    if not parts:
        return "\\\\wsl.localhost\\" + distro
    return "\\\\wsl.localhost\\" + distro + "\\" + "\\".join(parts)


def unc_to_linux(path: str) -> str:
    """Windows UNC（\\\\wsl.localhost\\Distro\\...）→ Linux 路径（尽力而为）。"""
    text = str(path or "")
    prefix = "\\\\wsl.localhost\\"
    if not text.lower().startswith(prefix.lower()):
        return text
    rest = text[len(prefix):]
    if "\\" not in rest:
        return "/"
    _distro, _, tail = rest.replace("/", "\\").partition("\\")
    return "/" + tail.replace("\\", "/")


def default_root(kind: AgentKind, home: str = "") -> str:
    """Agent 默认数据根（Linux 侧相对 HOME 的目录）。"""
    sub = {
        AgentKind.CLAUDE: ".claude",
        AgentKind.CODEX: ".codex",
        AgentKind.KIMI: ".kimi-code",
        AgentKind.PI: ".pi",
    }[kind]
    base = home or _user_home()
    if base.startswith("\\\\wsl.localhost\\") or base.startswith("\\\\wsl$\\"):
        return base.rstrip("\\") + "\\" + sub.replace("/", os.sep)
    linux = base.rstrip("/") + "/" + sub
    if os.name == "nt" and not base.startswith("\\\\"):
        return linux.replace("/", os.sep)
    return linux


def instance_roots(inst, kind: AgentKind | None = None) -> list[str]:
    """某个 AgentInstance 的会话数据根（Windows 路径列表，供扫描）。

    WSL 实例：优先 env 覆盖根（allowlisted），其次该 uid 的真实 HOME；
    数据根必须由 wsl_unc 转换，绝不遍历 \\home\\*。
    Windows 实例：当前用户 HOME（无法读取其他进程 env，无覆盖根）。

    PI（v4.2.3 §8.2）：canonical session root 是
    `<home>/.pi/agent/sessions`（或 PI_CODING_AGENT_SESSION_DIR 覆盖），
    不从 `~/.pi` 整根递归——比放大 depth 更省目录枚举、缩小隐私扫描面。
    """
    kind = kind or inst.kind
    source = getattr(inst, "source", "")
    if source.startswith("wsl:"):
        distro = source.split(":", 1)[1]
        roots = []
        home = getattr(inst, "home", "")
        candidates = []
        if kind is AgentKind.PI:
            # env override allowlist（PI_CODING_AGENT_SESSION_DIR）
            env_root = str(getattr(inst, "pi_session_dir", "") or "")
            if env_root:
                candidates.append(env_root)
            if home:
                candidates.append(home.rstrip("/") + "/.pi/agent/sessions")
        else:
            env_root = inst.data_root(kind)
            if env_root:
                candidates.append(env_root)
            if home:
                default = home.rstrip("/") + "/" + {
                    AgentKind.CLAUDE: ".claude",
                    AgentKind.CODEX: ".codex",
                    AgentKind.KIMI: ".kimi-code",
                    AgentKind.PI: ".pi",
                }[kind]
                if default not in candidates:
                    candidates.append(default)
        for cand in candidates:
            try:
                roots.append(wsl_unc(distro, cand))
            except ValueError:
                continue
        return roots
    # Windows 原生
    return [default_root(kind)]


def session_subdir(kind: AgentKind) -> str:
    return {
        AgentKind.CLAUDE: os.path.join(".claude", "projects"),
        AgentKind.CODEX: os.path.join(".codex", "sessions"),
        AgentKind.KIMI: os.path.join(".kimi-code", "sessions"),
        AgentKind.PI: os.path.join(".pi", "agent", "sessions"),
    }[kind]


def windows_roots(kind: AgentKind) -> list[str]:
    """兼容旧测试调用：当前 Windows 用户的会话根。"""
    return [os.path.join(_user_home(), session_subdir(kind))]


def wsl_roots(kind: AgentKind, distro: str, users: list[str] | None = None) -> list[str]:
    """兼容旧测试调用：指定用户列表的会话根。"""
    sub = session_subdir(kind).replace(os.sep, "/")
    if users is None:
        users = list_wsl_users("\\\\wsl.localhost\\" + distro + "\\home\\")
    return [wsl_unc(distro, f"/home/{u}/{sub}") for u in users]


def list_wsl_users(home_base: str) -> list[str]:
    """兼容旧调用；V3 主流程不再使用（由 uid → getent 解析 HOME）。"""
    try:
        return [e.name for e in os.scandir(home_base)
                if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


# ---------------------------------------------------------------- Kimi 索引

KIMI_INDEX_NAME = "session_index.jsonl"
KIMI_INDEX_TAIL_BYTES = 256 * 1024   # 索引只读尾部，避免全量回放


def kimi_index_roots(inst) -> list[str]:
    """Kimi session_index.jsonl 的候选位置（新根 + legacy 根）。"""
    source = getattr(inst, "source", "")
    kind = AgentKind.KIMI
    out = []
    if source.startswith("wsl:"):
        distro = source.split(":", 1)[1]
        cands = []
        if inst.kimi_code_home:
            cands.append(inst.kimi_code_home)
        home = getattr(inst, "home", "")
        if home:
            cands.append(home.rstrip("/") + "/.kimi-code")
            cands.append(home.rstrip("/") + "/.kimi")
        for cand in cands:
            try:
                out.append(wsl_unc(distro, cand))
            except ValueError:
                continue
        return out
    home = _user_home()
    for sub in (".kimi-code", ".kimi"):
        out.append(os.path.join(home, sub))
    return out


def parse_kimi_index(text: str) -> list[dict]:
    """解析 session_index.jsonl 尾部行 → [{sessionId, sessionDir, workDir}]。"""
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict) and obj.get("sessionId") and obj.get("sessionDir"):
            entries.append(obj)
    return entries


def _linux_contains(root_linux: str, candidate: str) -> bool:
    root_linux = posixpath.normpath(root_linux).rstrip("/") or "/"
    if candidate == root_linux:
        return True
    return candidate.startswith(root_linux + "/")


def resolve_kimi_session_dir(root: str, session_dir: str,
                             distro: str = "") -> str | None:
    """session_index 的 sessionDir → 受控会话目录（v4.2.3 §7.3）。

    所有 sessionDir 在拼接 wire 前必须证明仍位于当前 authorized Kimi
    root；任何 `..` 越界、跨盘、无效 absolute/relative、commonpath 异常
    均返回 None。不跟 symlink（保持 follow_symlinks=False 规则）。

      * Windows root + relative sessionDir：normpath(join) 后 commonpath；
      * WSL root（UNC）+ relative sessionDir：转 Linux 路径规范化 containment；
      * WSL root + absolute Linux sessionDir：posixpath.normpath 后要求
        位于该 root 的 Linux 等价路径内，再 wsl_unc；
      * Windows-absolute/UNC-absolute sessionDir：拒绝。
    """
    session_dir = str(session_dir or "").strip().replace("\x00", "")
    root = str(root or "").strip().rstrip("\\/")
    if not session_dir or not root:
        return None
    is_wsl = bool(distro)
    if session_dir.startswith("\\\\"):
        return None
    if session_dir.startswith("/"):
        if not is_wsl:
            return None
        root_linux = unc_to_linux(root)
        if not root_linux.startswith("/"):
            return None
        try:
            norm = posixpath.normpath(session_dir)
        except ValueError:
            return None
        if not norm.startswith("/") or not _linux_contains(root_linux, norm):
            return None
        try:
            return wsl_unc(distro, norm)
        except ValueError:
            return None
    if os.path.isabs(session_dir):
        return None
    if is_wsl:
        root_linux = unc_to_linux(root)
        if not root_linux.startswith("/"):
            return None
        try:
            combined = posixpath.normpath(
                posixpath.join(root_linux, session_dir.replace("\\", "/")))
        except ValueError:
            return None
        if not combined.startswith("/") or not _linux_contains(root_linux, combined):
            return None
        try:
            return wsl_unc(distro, combined)
        except ValueError:
            return None
    candidate = os.path.normpath(os.path.join(root, session_dir))
    try:
        if os.path.commonpath([candidate, os.path.normpath(root)]) != \
                os.path.normpath(root):
            return None
    except ValueError:
        return None
    return candidate


def read_kimi_index_tail(root: str) -> list[dict]:
    path = os.path.join(root, KIMI_INDEX_NAME)
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    try:
        with open(path, "rb") as f:
            if size > KIMI_INDEX_TAIL_BYTES:
                f.seek(size - KIMI_INDEX_TAIL_BYTES)
                f.readline()   # 跳过半行
            data = f.read(KIMI_INDEX_TAIL_BYTES)
    except OSError:
        return []
    return parse_kimi_index(data.decode("utf-8", errors="replace"))


def kimi_wire_candidates(inst) -> list[tuple[float, str]]:
    """按 cwd 从 session_index 解析 wire.jsonl 候选（无 mtime 时用 0）。

    sessionDir 必须经 resolve_kimi_session_dir containment 校验
    （v4.2.3 §7.3），越界/跨盘/绝对 Windows 路径一律不产生候选。
    """
    cwd = (getattr(inst, "cwd", "") or "").rstrip("/")
    out: list[tuple[float, str]] = []
    if not cwd:
        return out
    source = getattr(inst, "source", "")
    for root in kimi_index_roots(inst):
        distro = source.split(":", 1)[1] if source.startswith("wsl:") else ""
        for entry in read_kimi_index_tail(root):
            work = str(entry.get("workDir") or "").rstrip("/")
            if work != cwd:
                continue
            session_dir = resolve_kimi_session_dir(
                root, str(entry.get("sessionDir") or ""), distro)
            if not session_dir:
                continue
            wire = os.path.join(session_dir, "agents", "main", "wire.jsonl")
            mtime = _mtime_of(wire)
            if mtime is not None:
                out.append((mtime, wire))
    out.sort(reverse=True)
    return out


def _mtime_of(path: str) -> float | None:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


# ------------------------------------------------------- Claude PID registry

CLAUDE_PID_REGISTRY = "sessions"   # <root>/sessions/<pid>.json


def claude_pid_registry_files(inst) -> list[str]:
    """~/.claude/sessions/<pid>.json（强 binding hint，非真值）。"""
    source = getattr(inst, "source", "")
    pid = int(getattr(inst, "pid", 0) or 0)
    if pid <= 0:
        return []
    name = f"{pid}.json"
    out = []
    if source.startswith("wsl:"):
        distro = source.split(":", 1)[1]
        cands = []
        if inst.claude_config_dir:
            cands.append(inst.claude_config_dir)
        home = getattr(inst, "home", "")
        if home:
            cands.append(home.rstrip("/") + "/.claude")
        for cand in cands:
            try:
                out.append(wsl_unc(distro, f"{cand.rstrip('/')}/{CLAUDE_PID_REGISTRY}/{name}"))
            except ValueError:
                continue
        return out
    home = _user_home()
    roots = []
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        roots.append(env_dir)
    roots.append(os.path.join(home, ".claude"))
    return [os.path.join(r, CLAUDE_PID_REGISTRY, name) for r in roots]


def read_claude_pid_registry(path: str) -> dict:
    """读取 PID registry 单条记录；文件缺失/损坏返回空。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


# ------------------------------------------------------------ 目录扫描

def session_files(kind: AgentKind, roots: list[str],
                  window_sec: float | None) -> list[tuple[float, str]]:
    """在根目录下找会话文件，按 mtime 新→旧返回 [(mtime, path)]。

    window_sec=None 时不过滤 mtime（late-start fallback 用，仍只取最新
    MAX_SCAN_CANDIDATES 个）；symlink 不跟随（plan §35）。
    """
    try:
        window = None if window_sec is None else max(0.0, float(window_sec))
    except (TypeError, ValueError):
        window = 180.0
    now = time.time()
    found: list[tuple[float, str]] = []

    def walk_jsonl(root: str, depth: int):
        try:
            with os.scandir(root) as it:
                for e in it:
                    try:
                        if e.is_file(follow_symlinks=False) and e.name.lower().endswith(".jsonl"):
                            st = e.stat(follow_symlinks=False)
                            if window is None or now - st.st_mtime <= window:
                                found.append((st.st_mtime, e.path))
                        elif e.is_dir(follow_symlinks=False) and depth > 0:
                            walk_jsonl(e.path, depth - 1)
                    except OSError:
                        continue
        except OSError:
            return

    depth = {
        AgentKind.CLAUDE: 2,   # projects/<proj>/<uuid>.jsonl
        AgentKind.CODEX: 4,    # sessions/Y/M/D/rollout-*.jsonl
        AgentKind.KIMI: 5,     # sessions/<key>/<id>/agents/main/wire.jsonl
        AgentKind.PI: 2,       # sessions/<proj>/<file>.jsonl
    }[kind]
    for r in roots:
        walk_jsonl(r, depth)

    found.sort(reverse=True)
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
    if window is None:
        out = out[:MAX_SCAN_CANDIDATES]
    return out


MAX_SCAN_CANDIDATES = 12