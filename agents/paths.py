"""各 Agent 会话数据根定位与安全路径规则。

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
import ntpath
import os
import posixpath
import time

from .models import AgentKind

# /proc/<pid>/environ 中允许进入 Python 的变量
# OPENCODE_DB / DSH_HOME 是上游官方的数据根覆盖（非机密路径；
# 认证类变量一律不进 allowlist。
ENV_ALLOWLIST = (
    "WT_SESSION",
    "WT_PROFILE_ID",
    "WSL_DISTRO_NAME",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "KIMI_CODE_HOME",
    "PI_CODING_AGENT_SESSION_DIR",
    "OPENCODE_DB",
    "DSH_HOME",
    "TMUX",
    "STY",
    "TERM_PROGRAM",
)

_LEGACY_KIMI = ".kimi"


def _user_home() -> str:
    return os.path.expanduser("~")


# ------------------------------------------------ v4.4 Desktop 数据根（plan2 §14）

def codex_home_root() -> str:
    """Codex Desktop 状态根：CODEX_HOME env 优先，否则 ~/.codex。

    只做本机定位，不做网络路径；返回未展开的规范 Windows 路径。
    """
    env = os.environ.get("CODEX_HOME") or ""
    root = env.strip() if env.strip() else os.path.join(_user_home(), ".codex")
    return os.path.normpath(root)


def codex_state_db_candidates(codex_home: str | None = None) -> list[str]:
    """Codex 状态 DB 候选（state_N.sqlite 文件名随上游版本演进）。

    返回按版本号降序的现存候选；capability probe 由调用方执行
    （plan2 §6.3：schema 永远 capability-detect，不硬编码版本）。
    """
    import re
    roots: list[str] = []
    if codex_home:
        roots.append(codex_home)
    else:
        # Newer Codex builds can place the SQLite state separately from the
        # rollout home.  The variable is a path override, not a credential;
        # resolve relative values against this process cwd (absolute values are
        # the normal installation form).
        sqlite_home = (os.environ.get("CODEX_SQLITE_HOME") or "").strip()
        if sqlite_home:
            roots.append(os.path.abspath(sqlite_home))
        roots.append(codex_home_root())
    deduped_roots: list[str] = []
    seen_roots: set[str] = set()
    for root in roots:
        norm_root = os.path.normcase(os.path.abspath(os.path.expanduser(root)))
        if norm_root not in seen_roots:
            seen_roots.add(norm_root)
            deduped_roots.append(root)
    out: list[tuple[int, tuple[int, ...], str]] = []
    for root_rank, root in enumerate(deduped_roots):
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            m = re.fullmatch(r"state_(\d+)\.sqlite", name)
            if m:
                path = os.path.join(root, name)
                version = tuple(int(x) for x in m.group(1).split("_"))
                out.append((root_rank, version, path))
    # An explicit CODEX_SQLITE_HOME is authoritative even when its schema
    # number is older than a stale state DB under CODEX_HOME.  Within one root,
    # retain the newest state_N.sqlite first.
    out.sort(key=lambda item: (
        item[0], tuple(-part for part in item[1]),
        os.path.normcase(item[2])))
    paths_out: list[str] = []
    seen_paths: set[str] = set()
    for _root_rank, _version, path in out:
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen_paths:
            continue
        seen_paths.add(norm)
        paths_out.append(path)
    return paths_out


def codex_rollout_roots(codex_home: str | None = None) -> list[str]:
    """rollout 文件的合法 containment 根（sessions / archived_sessions）。

    path 校验（plan2 §7.2）：DB rollout_path 规范化后必须位于其中之一。
    """
    home = codex_home or codex_home_root()
    return [
        os.path.normpath(os.path.join(home, "sessions")),
        os.path.normpath(os.path.join(home, "archived_sessions")),
    ]


def zcode_cli_root() -> str:
    """ZCode CLI 数据根（官方 Hooks 文档证实 ~/.zcode/cli 布局）。"""
    override = os.environ.get("ZCODE_CLI_DIR") or ""
    root = override.strip() if override.strip() else os.path.join(
        _user_home(), ".zcode", "cli")
    return os.path.normpath(root)


def zcode_db_path() -> str:
    return os.path.join(zcode_cli_root(), "db", "db.sqlite")


def zcode_log_root() -> str:
    return os.path.join(zcode_cli_root(), "log")


def _zcode_wsl_child(home: str, *parts: str) -> str:
    """Build a contained Linux path below one already-resolved WSL HOME."""
    root = posixpath.normpath(str(home or "").strip())
    if not root.startswith("/") or root == "/":
        raise ValueError("zcode WSL HOME must be an absolute non-root path")
    child = posixpath.normpath(posixpath.join(root, *parts))
    if child != root and not child.startswith(root.rstrip("/") + "/"):
        raise ValueError("zcode WSL path escaped HOME")
    return child


def zcode_wsl_cli_root(home: str) -> str:
    return _zcode_wsl_child(home, ".zcode", "cli")


def zcode_wsl_db_path(home: str) -> str:
    return _zcode_wsl_child(home, ".zcode", "cli", "db", "db.sqlite")


def zcode_wsl_server_root(home: str) -> str:
    return _zcode_wsl_child(home, ".zcode", "server")


def zcode_wsl_server_node(home: str) -> str:
    return _zcode_wsl_child(home, ".zcode", "server", "node")


# --------------------------------------- OpenCode / DSH 数据根（无插件监听 plan）
#
# 上游事实（对齐版本见 AGENTS.md；OpenCode dev 86976449 / DSH master 22ddee3）：
#   * OpenCode data root = XDG 风格：<XDG_DATA_HOME|~/.local/share>/opencode；
#     DB 名 opencode.db（latest/dev/beta/next/prod 渠道）或 opencode-<channel>.db
#     （cli/src/database-path.ts）。Windows 同样落在 %USERPROFILE% 下
#     （util/global-roots.ts 使用 os.homedir()）。
#   * OpenCode service 注册文件 = <XDG_STATE_HOME|~/.local/state>/opencode/
#     service.json{version,url,pid,...}（service-registration.ts）。
#   * DSH home = $DSH_HOME || ~/.dsh（util/home-paths）；sessions root =
#     dshHomePath('sessions')（bundle/base/cordis.patch.yml）。
# 只做正向有界探测：绝不扫描整个 HOME，不读 OPENCODE_PASSWORD 等任何
# 认证变量，不读 $DSH_HOME/.credentials.yaml。

_OPENCODE_DB_MAX_CANDIDATES = 4
_OPENCODE_SERVICE_MAX = 4


def opencode_data_root() -> str:
    """OpenCode Windows 数据根（XDG_DATA_HOME 覆盖优先）。"""
    env = os.environ.get("XDG_DATA_HOME") or ""
    base = env.strip() if env.strip() else os.path.join(
        _user_home(), ".local", "share")
    return os.path.normpath(os.path.join(base, "opencode"))


def opencode_state_root() -> str:
    """OpenCode Windows state 根（service 注册文件所在目录）。"""
    env = os.environ.get("XDG_STATE_HOME") or ""
    base = env.strip() if env.strip() else os.path.join(
        _user_home(), ".local", "state")
    return os.path.normpath(os.path.join(base, "opencode"))


def opencode_db_candidates(data_root: str | None = None) -> list[str]:
    """OpenCode DB 候选（有界、正向）：opencode.db 优先，之后渠道后缀。

    不扫描 HOME；只在确认的数据根内列出白名单文件名；返回存在的候选，
    opencode.db 在最前，其余按 mtime 新→旧（当前活跃渠道通常最新）。
    """
    import re
    root = data_root or opencode_data_root()
    out: list[tuple[float, str]] = []
    canonical = os.path.join(root, "opencode.db")
    try:
        names = os.listdir(root)
    except OSError:
        return []
    pat = re.compile(r"^opencode-[A-Za-z0-9._-]+\.db$")
    for name in names:
        if name == "opencode.db":
            continue
        if pat.match(name):
            path = os.path.join(root, name)
            try:
                out.append((os.stat(path).st_mtime, path))
            except OSError:
                continue
    out.sort(reverse=True)
    tail = [p for _m, p in out[: _OPENCODE_DB_MAX_CANDIDATES - 1]]
    try:
        os.stat(canonical)
        return [canonical, *tail]
    except OSError:
        return tail


def opencode_service_files(state_root: str | None = None) -> list[str]:
    """OpenCode service 注册文件候选（service.json + 渠道后缀，有界）。"""
    import re
    root = state_root or opencode_state_root()
    try:
        names = os.listdir(root)
    except OSError:
        return []
    pat = re.compile(r"^service(-[A-Za-z0-9._-]+)?\.json$")
    matched = [n for n in names if pat.match(n)]
    # service.json（默认渠道）优先，其余按名字稳定排序
    matched.sort(key=lambda n: (n != "service.json", n))
    return [os.path.join(root, n) for n in matched[:_OPENCODE_SERVICE_MAX]]


def dsh_home_root() -> str:
    """DSH Windows home：$DSH_HOME 覆盖，否则 ~/.dsh。"""
    env = os.environ.get("DSH_HOME") or ""
    root = env.strip() if env.strip() else os.path.join(_user_home(), ".dsh")
    return os.path.normpath(root)


def dsh_sessions_root() -> str:
    return os.path.join(dsh_home_root(), "sessions")


def opencode_wsl_data_root(home: str) -> str:
    return _zcode_wsl_child(home, ".local", "share", "opencode")


def opencode_wsl_state_root(home: str) -> str:
    return _zcode_wsl_child(home, ".local", "state", "opencode")


def dsh_wsl_sessions_root(home: str) -> str:
    return _zcode_wsl_child(home, ".dsh", "sessions")


def wsl_unc(distro: str, linux_path: str) -> str:
    """Linux 绝对路径 → Windows UNC 路径。

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


# 旧四类文件 watcher 的数据根映射；OpenCode/DSH 有专用 adapter
# （无插件监听 plan §三），绝不能误入该映射。
_LEGACY_FILE_KINDS = (AgentKind.CLAUDE, AgentKind.CODEX,
                      AgentKind.KIMI, AgentKind.PI)
_LEGACY_DIR_BY_KIND = {
    AgentKind.CLAUDE: ".claude",
    AgentKind.CODEX: ".codex",
    AgentKind.KIMI: ".kimi-code",
    AgentKind.PI: ".pi",
}
_LEGACY_SCAN_DEPTH = {
    AgentKind.CLAUDE: 2,   # projects/<proj>/<uuid>.jsonl
    AgentKind.CODEX: 4,    # sessions/Y/M/D/rollout-*.jsonl
    AgentKind.KIMI: 5,     # sessions/<key>/<id>/agents/main/wire.jsonl
    AgentKind.PI: 2,       # sessions/<proj>/<file>.jsonl
}


def default_root(kind: AgentKind, home: str = "") -> str:
    """Agent 默认数据根（Linux 侧相对 HOME 的目录）。

    区分"Windows HOME"与"Linux HOME"只能看路径形态，绝不能看 os.name：
    在 Windows 上运行的 DeskPet 也会拿到 WSL 实例的 Linux HOME
    （/home/<user>），把它 replace("/", os.sep) 会得到 \\home\\x 这种
    既不是合法 Windows 路径、也不是合法 Linux 路径的结果，随后被
    wsl_unc() 拒绝 → 该 source 永远 0 候选（监听静默失效）。
    """
    if kind not in _LEGACY_FILE_KINDS:
        raise ValueError(f"kind {kind} has no legacy file-watcher root")
    sub = _LEGACY_DIR_BY_KIND[kind]
    base = home or _user_home()
    if base.startswith("\\\\wsl.localhost\\") or base.startswith("\\\\wsl$\\"):
        return base.rstrip("\\") + "\\" + sub.replace("/", os.sep)
    linux = base.rstrip("/") + "/" + sub
    if base.startswith("/") or base.startswith("\\"):
        # Linux/UNC 布局：保持 posix 分隔符，供 wsl_unc() 原样接受。
        return linux
    if os.name == "nt":
        return linux.replace("/", os.sep)
    return linux


def instance_roots(inst, kind: AgentKind | None = None) -> list[str]:
    """某个 AgentInstance 的会话数据根（Windows 路径列表，供扫描）。

    仅服务旧四类文件 watcher；OpenCode/DSH 由各自 adapter 定位数据根。

    WSL 实例：优先 env 覆盖根（allowlisted），其次该 uid 的真实 HOME；
    数据根必须由 wsl_unc 转换，绝不遍历 \\home\\*。
    Windows 实例：当前用户 HOME（无法读取其他进程 env，无覆盖根）。

    PI（v4.2.3 §8.2）：canonical session root 是
    `<home>/.pi/agent/sessions`（或 PI_CODING_AGENT_SESSION_DIR 覆盖），
    不从 `~/.pi` 整根递归——比放大 depth 更省目录枚举、缩小隐私扫描面。
    """
    kind = kind or inst.kind
    if kind not in _LEGACY_FILE_KINDS:
        return []
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
                default = home.rstrip("/") + "/" + _LEGACY_DIR_BY_KIND[kind]
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
    if kind not in _LEGACY_FILE_KINDS:
        raise ValueError(f"kind {kind} has no legacy file-watcher layout")
    return os.path.join(_LEGACY_DIR_BY_KIND[kind],
                        {
                            AgentKind.CLAUDE: "projects",
                            AgentKind.CODEX: "sessions",
                            AgentKind.KIMI: "sessions",
                            AgentKind.PI: os.path.join("agent", "sessions"),
                        }[kind])


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
    # ``sessionDir`` is sometimes serialized with Windows separators even when
    # the test/reader runs on POSIX. Normalize those separators before joining;
    # otherwise ``..\\..\\other`` is treated as a literal filename and slips
    # through commonpath containment.
    rel = session_dir.replace("\\", os.sep)
    if ntpath.splitdrive(session_dir)[0] or os.path.isabs(rel):
        return None
    candidate = os.path.normpath(os.path.join(root, rel))
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


def _kimi_state_workdir(session_dir: str) -> str:
    """Read only the bounded cwd hint from a Kimi session state file.

    ``session_index.jsonl`` is an accelerator, not a durable fact source: recent
    Kimi Code builds can rewrite it or omit a resumed session.  The session-owned
    state.json is therefore used as a bounded fallback.  Any malformed or
    unreadable state is deliberately ignored.
    """
    try:
        with open(os.path.join(session_dir, "state.json"), "r",
                  encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return ""
    if not isinstance(obj, dict):
        return ""
    return str(obj.get("workDir") or obj.get("cwd")
               or obj.get("workingDirectory") or "").rstrip("/\\")


def _kimi_fallback_session_dirs(root: str, cwd: str,
                                limit: int = 64) -> list[tuple[float, str]]:
    """Find recent session-owned wires when the index is stale or incomplete.

    The scan is strictly bounded to ``<root>/sessions/<workDirKey>/<id>``;
    it never walks the whole home directory and never follows symlinks.  State
    cwd is required, so an ambiguous directory is skipped rather than guessed.
    """
    sessions = os.path.join(root, "sessions")
    entries: list[tuple[float, str]] = []
    try:
        workdirs = list(os.scandir(sessions))
    except OSError:
        return entries
    workdirs = [e for e in workdirs
                if e.is_dir(follow_symlinks=False)]
    # Prefer directories whose contents changed recently, but keep the bound
    # deterministic when mtime is unavailable.
    workdirs.sort(key=lambda e: (_mtime_of(e.path) or 0.0,
                                e.name.casefold()), reverse=True)
    for work in workdirs[:16]:
        try:
            sessions_for_work = list(os.scandir(work.path))
        except OSError:
            continue
        sessions_for_work = [e for e in sessions_for_work
                             if e.is_dir(follow_symlinks=False)]
        sessions_for_work.sort(key=lambda e: (_mtime_of(e.path) or 0.0,
                                              e.name.casefold()),
                               reverse=True)
        for session in sessions_for_work[:limit]:
            if _kimi_state_workdir(session.path).rstrip("/\\") != cwd:
                continue
            wire = os.path.join(session.path, "agents", "main", "wire.jsonl")
            mtime = _mtime_of(wire)
            if mtime is not None:
                entries.append((mtime, wire))
    entries.sort(reverse=True)
    return entries[:limit]


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
    seen: set[str] = set()
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
            key = os.path.normcase(wire)
            if mtime is not None and key not in seen:
                seen.add(key)
                out.append((mtime, wire))
        # Index rows can be absent after ``--continue`` or after an index
        # rewrite.  Recover only session-owned facts whose state cwd matches;
        # no fallback is allowed to infer ownership from directory names.
        for mtime, wire in _kimi_fallback_session_dirs(root, cwd):
            key = os.path.normcase(wire)
            if key not in seen:
                seen.add(key)
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

    仅服务旧四类文件 watcher；OpenCode/DSH 的会话发现由各自 adapter
    负责（无插件监听 plan §三），进入这里直接拒绝。

    window_sec=None 时不过滤 mtime（late-start fallback 用，仍只取最新
    MAX_SCAN_CANDIDATES 个）；symlink 不跟随（plan §35）。
    """
    if kind not in _LEGACY_FILE_KINDS:
        raise ValueError(f"kind {kind} has no legacy file-watcher layout")
    try:
        window = None if window_sec is None else max(0.0, float(window_sec))
    except (TypeError, ValueError):
        window = 180.0
    now = time.time()
    found: list[tuple[float, str]] = []

    def walk_jsonl(root: str, depth: int):
        suffixes = (".jsonl", ".jsonl.zst") if kind is AgentKind.CODEX else (".jsonl",)
        try:
            with os.scandir(root) as it:
                for e in it:
                    try:
                        if e.is_file(follow_symlinks=False) and e.name.lower().endswith(suffixes):
                            # Codex may transparently compress archived rollouts
                            # to ``.jsonl.zst``.  A plain sibling is authoritative
                            # during materialization; exposing both would make
                            # the watcher parse one logical session twice.
                            if e.name.lower().endswith(".jsonl.zst"):
                                plain = os.path.join(
                                    os.path.dirname(e.path), e.name[:-4])
                                try:
                                    if os.path.isfile(plain):
                                        continue
                                except OSError:
                                    continue
                            st = e.stat(follow_symlinks=False)
                            if window is None or now - st.st_mtime <= window:
                                found.append((st.st_mtime, e.path))
                        elif e.is_dir(follow_symlinks=False) and depth > 0:
                            walk_jsonl(e.path, depth - 1)
                    except OSError:
                        continue
        except OSError:
            return

    depth = _LEGACY_SCAN_DEPTH[kind]
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
