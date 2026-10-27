"""V3 进程发现层：Windows 原生（psutil）+ WSL ProcessProbe。

只读、无 hooks、无注入。WSL 探测分三层：
  1. wsl.exe --list --running --quiet → Running 发行版（每轮全新查询，
     绝不缓存正结果；失败时回退解析 -l -v 表格）
  2. 每发行版一条 ps → pid/ppid/sid/pgid/tpgid/tty/uid/etimes/comm/args
  3. 仅对 canonical Agent PID 做一次批量 metadata 查询：
     /proc/<pid>/cwd、/proc/<pid>/stat（starttime ticks = 进程 token）、
     /proc/<pid>/environ —— environ 在 WSL 内部就按 allowlist 过滤，
     Python 永远看不到 OPENAI_API_KEY 之类的其他变量。

用户 HOME 通过 uid → getent passwd 解析，不枚举 /home/*。

V3.1 加固：
  * 进程树 canonicalization：npm/python wrapper 与真正的 runtime（node 等）
    同为匹配候选时，只保留最深的后代进程；Windows 与 WSL 共用同一规则，
    不跨 kind 折叠、不按 kind 全局去重。
  * source 健康按真实来源隔离（wsl:Ubuntu / wsl:Debian 互不影响）。
  * /proc starttime 读不到时用稳定的 fallback 代次 token，绝不退化成裸 PID。
  * root metadata 重试默认关闭（privacy.wsl_root_metadata_fallback）。
"""
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass

from .models import (
    AgentKind,
    AgentInstance,
    DesktopHost,
    RemoteRuntimeContext,
    SourceProbeSnapshot,
    TerminalAttachment,
)
from .paths import ENV_ALLOWLIST

_SELF_PID = os.getpid()

# ps 列顺序（与 _PS_FORMAT 一一对应）
_PS_FORMAT = "pid=,ppid=,sid=,pgid=,tpgid=,tty=,uid=,etimes=,stat=,comm=,args="

# /proc stat 进程状态（proc_pid_stat(5)）：这些状态的进程对象已死/正在回收，
# 绝不作为 live Agent（v4plan §4.4）。S/T/D/R/I 等只说明当前没跑 CPU。
_DEAD_STATES = frozenset("ZXx")

# 官方“常驻服务形态”的进程自证据（2026-10 实机取证）：
#   Codex `codex app-server --remote-control --listen unix://…` 与
#   `codex app-server daemon pid-update-loop` 是官方 remote-control
#   常驻 runtime（.research/codex-upstream.md §8.3 记录了
#   $CODEX_HOME/app-server-control / app-server-daemon 官方路径），
#   刻意脱离 controlling TTY（tty=? / tpgid=-1）。这类进程的 DETACHED
#   是设计意图而非孤儿，必须保留为可观察 Agent；没有任何 daemon 自证据
#   的 DETACHED 仍按孤儿过滤。
_DAEMON_FORM_MARKS = (
    "app-server",
    "app-server-daemon",
    "pid-update-loop",
    "--remote-control",
    "--listen",
    "--managed-daemon",
)


class ProbeUnavailable(RuntimeError):
    """进程枚举整体不可用：本轮结果 authoritative=False，不得判死旧实例。"""


# ----------------------------------------------------- 进程树 canonicalization

@dataclass
class ProcessCandidate:
    kind: AgentKind
    pid: int
    ppid: int
    comm: str = ""
    args: str = ""


def classify_ps_terminal_attachment(tty: str, tpgid: int) -> TerminalAttachment:
    """已有 ps 字段 → 三态 terminal attachment（v4.2.3 §2.3）。

    tty/tpgid 是 Linux 内核对 controlling terminal / foreground process
    group 的事实，不需要任何新 syscall。注意：
      * 不用 pgid == tpgid 作条件——Agent 可以暂时处于 background
        process group 但仍拥有有效 controlling TTY；
      * 不单独用 ppid==1 判 orphan——init/supervisor 场景中 PPID
        不足以构成 terminal-detached 证据。
    """
    tty = str(tty or "").strip()
    no_tty = tty in {"", "?", "??", "-"}
    no_fg = int(tpgid or 0) <= 0
    if not no_tty and not no_fg:
        return TerminalAttachment.ATTACHED
    if no_tty and no_fg:
        return TerminalAttachment.DETACHED
    return TerminalAttachment.UNKNOWN


def is_daemon_form(args) -> bool:
    """命令行是否声明官方常驻服务形态（2026-10 实机取证）。

    这类进程刻意脱离 controlling TTY，因此它的 DETACHED 不是孤儿证据。
    只用明确的自证据标记，绝不按进程名猜——普通 CLI 被 detach 时
    仍然要按孤儿过滤。
    """
    low = str(args or "").lower()
    return any(mark in low for mark in _DAEMON_FORM_MARKS)


def is_codex_maintenance(args) -> bool:
    """The daemon PID publisher is housekeeping, never a user Agent session."""
    try:
        argv = shlex.split(args) if isinstance(args, str) else list(args or ())
    except ValueError:
        return False
    commands = [str(arg).casefold() for arg in argv]
    if commands[1:4] == ["app-server", "daemon", "pid-update-loop"]:
        return True
    return (bool(commands)
            and commands[0].replace("\\", "/").rsplit("/", 1)[-1]
            in {"node", "node.exe"}
            and len(commands) > 1 and "codex" in commands[1]
            and commands[2:5] == ["app-server", "daemon", "pid-update-loop"])


def canonical_terminal_attachment(
        canonical_pid: int,
        launcher_pids: tuple[int, ...],
        rows_by_pid: dict[int, tuple],
) -> TerminalAttachment:
    """按 canonical group（canonical + same-kind launcher）判定附件状态。

    Codex Node wrapper + Rust runtime 等同 kind 多层进程必须整组判断：
    任一成员 ATTACHED 即保留；全部 DETACHED 才过滤；矛盾/缺失 → UNKNOWN。
    rows_by_pid 值为 _ps_scan 的行元组（pid,ppid,sid,pgid,tpgid,tty,...）。

    v4.4.1 例外：同一 canonical group 内任一成员带官方 daemon 自证据时，
    "全部 DETACHED"不再判孤儿——把整组提升为 UNKNOWN 保留观察
    （`codex app-server --remote-control` 常驻形态脱离 TTY 是设计意图）。
    """
    states = []
    daemon_evidence = False
    for pid in (canonical_pid, *launcher_pids):
        row = rows_by_pid.get(pid)
        if row is None:
            continue
        # 行布局：pid,ppid,sid,pgid,tpgid,tty,uid,etimes,stat,comm,args
        states.append(classify_ps_terminal_attachment(row[5], row[4]))
        if is_daemon_form(row[10]):
            daemon_evidence = True
    if any(s is TerminalAttachment.ATTACHED for s in states):
        return TerminalAttachment.ATTACHED
    if states and all(s is TerminalAttachment.DETACHED for s in states):
        if daemon_evidence:
            return TerminalAttachment.UNKNOWN
        return TerminalAttachment.DETACHED
    return TerminalAttachment.UNKNOWN


def canonicalize_agent_processes(
        candidates: list[ProcessCandidate],
        parent_by_pid: dict[int, int],
        max_hops: int = 16,
) -> tuple[set[int], dict[int, tuple[int, ...]]]:
    """同 kind 内：候选 A 是候选 B 的祖先 → A 是 wrapper，保留 B（runtime）。

    返回 (canonical_pids, canonical_pid -> 匹配到的同 kind 祖先 launcher pids)。
    规则：
      * 只在同一 AgentKind 内折叠（Claude wrapper + Codex child 不合并）；
      * 没有祖先关系的两个同 kind 进程都保留（两个独立 Claude）；
      * 不识别 npm/python 等具体名字，只看真实进程树。
    """
    by_pid = {c.pid: c for c in candidates}
    wrappers: set[int] = set()
    matched_ancestors: dict[int, list[int]] = {}
    for cand in candidates:
        chain: list[int] = []
        cur = parent_by_pid.get(cand.pid, 0)
        hops = 0
        while cur and hops < max_hops:
            if cur in by_pid:
                chain.append(cur)
                other = by_pid[cur]
                if other.kind == cand.kind and cur != cand.pid:
                    wrappers.add(cur)
            cur = parent_by_pid.get(cur, 0)
            hops += 1
        if chain:
            matched_ancestors[cand.pid] = chain
    canonical = {c.pid for c in candidates if c.pid not in wrappers}
    launchers: dict[int, tuple[int, ...]] = {}
    for pid in canonical:
        canonical_kind = by_pid[pid].kind
        anc = matched_ancestors.get(pid, ())
        # launcher 诊断必须严格同 kind：跨 kind 的祖先（即使它是别处的
        # wrapper）绝不能显示成本实例的 launcher。
        same_kind = tuple(p for p in anc
                          if p in wrappers and by_pid[p].kind == canonical_kind)
        if same_kind:
            launchers[pid] = same_kind
    return canonical, launchers


def _norm_cmdline(proc) -> str:
    try:
        cl = proc.info.get("cmdline") or []
        return " ".join(cl).lower()
    except Exception:
        return ""


def _is_dsh_cli_cmd(cmd: str) -> bool:
    """识别 DSH 官方 CLI 的真实启动入口（大小写/路径分隔符不敏感）。

    ``@deepseek-ai/dsh`` 当前把 ``dsh`` 指向 ``lib/bin.js``；npm/pnpm
    会把它包装成 node 命令，进程表里不会出现裸 ``dsh``。只匹配官方
    package/bin 入口、``.bin`` shim、桌面共享 CLI 和 Python module 入口，
    避免把 prompt 或 ``~/.dsh`` 数据路径里的关键字误判为 agent。
    """
    low = str(cmd or "").lower().replace("\\", "/")
    if re.search(r"(?:^|/)\.bin/dsh(?:\.cmd|\.exe)?(?:\s|$)", low):
        return True
    if re.search(
            r"(?:^|/)node_modules/@deepseek-ai/dsh/(?:lib/)?bin\.js(?:\s|$)",
            low):
        return True
    if re.search(r"(?:^|/)\.dsh/bin/dsh(?:\.js)?(?:\s|$)", low):
        return True
    return bool(re.search(
        r"(?:^|\s)-m\s+(?:deepseek[_-])?dsh(?:\s|$)", low))


def _match_kind(name: str, cmd: str) -> AgentKind | None:
    base = name.lower()
    if base == "claude.exe":
        return AgentKind.CLAUDE
    if base == "kimi.exe" or base == "kimi":
        return AgentKind.KIMI
    if base == "opencode.exe" or base == "dsh.exe":
        # CLI 二进制本体（桌面宿主已在角色分类中按 Electron 佐证分派；
        # DSH 桌面 exe 名为 deepseek harness.exe，与 CLI 无同名冲突）
        return (AgentKind.OPENCODE if base == "opencode.exe"
                else AgentKind.DSH)
    if "claude-code" in cmd or "@anthropic-ai/claude-code" in cmd or "@anthropic-ai\\claude-code" in cmd:
        return AgentKind.CLAUDE
    if "kimi-cli" in cmd or "kimi_cli" in cmd or "/kimi" in cmd or "\\kimi" in cmd:
        return AgentKind.KIMI
    if "pi-coding-agent" in cmd:
        return AgentKind.PI
    if _OPENCODE_PKG_MARK in cmd:
        # npm 全局 shim（node.exe ...node_modules\opencode-ai\...）
        return AgentKind.OPENCODE
    if _is_dsh_cli_cmd(cmd):
        return AgentKind.DSH
    if base == "node.exe" or base == "python.exe" or base == "pythonw.exe":
        # node/python 的判定依赖命令行，已在上面处理
        return None
    return None


# ------------------------------------------------- Windows 运行时角色（plan2 §4）
#
# v4.4 把"二元 kind 分类"升级为内部 runtime-role 分类，但仍只有一次
# psutil census。角色只影响本次 census 内的归组，不是新对外合同：
#   * TERMINAL_AGENT            —— 终端宿主下的 CLI Agent（4.3.1 语义）
#   * CODEX_DESKTOP_HOST        —— Codex 桌面应用宿主（GUI 主进程）
#   * CODEX_APP_SERVER_HELPER   —— Codex 宿主树内的 app-server/辅助进程
#   * ZCODE_DESKTOP_HOST        —— ZCode 桌面应用宿主
#   * ZCODE_AGENT_HELPER        —— ZCode 宿主树内的 app-server/plugin 辅助
#   * OTHER                     —— 不构成任何 target（含证据冲突项）

ROLE_TERMINAL_AGENT = "terminal_agent"
ROLE_CODEX_DESKTOP_HOST = "codex_desktop_host"
ROLE_CODEX_APP_SERVER_HELPER = "codex_app_server_helper"
ROLE_ZCODE_DESKTOP_HOST = "zcode_desktop_host"
ROLE_ZCODE_AGENT_HELPER = "zcode_agent_helper"
# 无插件监听 plan §三：OpenCode / DSH 桌面宿主与 helper
ROLE_OPENCODE_DESKTOP_HOST = "opencode_desktop_host"
ROLE_OPENCODE_HELPER = "opencode_helper"
ROLE_DSH_DESKTOP_HOST = "dsh_desktop_host"
ROLE_DSH_HELPER = "dsh_helper"
ROLE_OTHER = "other"

# Electron 多进程标记（renderer/gpu/utility/crashpad 均以 --type= 开头）
_ELECTRON_HELPER_FLAG = "--type="

# 终端宿主进程名（CLI Agent 的 ancestry 证据；不含 code.exe——IDE 集成
# 的 codex 语言服务器与集成终端无法从 ancestry 区分，宁可漏报）
_TERMINAL_HOST_BASENAMES = frozenset({
    "windowsterminal.exe", "openterminal.exe", "openconsole.exe",
    "conhost.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
    "bash.exe", "wsl.exe", "mintty.exe", "wezterm-gui.exe", "alacritty.exe",
})

# GUI 宿主 exe（大小写不敏感；Windows 文件系统不区分大小写）
_ZCODE_GUI_BASENAMES = frozenset({"zcode.exe"})
_CHATGPT_GUI_BASENAMES = frozenset({"chatgpt.exe"})
# ChatGPT Desktop 成为 Codex 宿主的正向证据：MSIX 包族目录含 openai.codex
_CHATGPT_CODEX_PKG_MARK = "openai.codex"
# Codex 桌面/CLI 家族进程名（含 codex-app-server.exe 等 helper）
_CODEX_PROC_MARK = ("codex", ".exe")
# Codex app-server/daemon 子命令（cmdline 明确开关名）
_CODEX_HELPER_ARGS = frozenset({"app-server", "app-server-daemon", "daemon"})
# ZCode helper：zcode.cjs 入口 + CLI/host-local 辅助进程名
_ZCODE_HELPER_CMD_MARKS = ("zcode.cjs",)
_ZCODE_HELPER_BASENAMES = frozenset({"zcode-cli.exe", "zcode-host-local.exe"})
# OpenCode Desktop（electron-builder productName：OpenCode / OpenCode Dev /
# OpenCode Beta；NSIS exe 与 productName 同名）与捆绑 CLI（resources/
# opencode-cli.exe，Electron RunAsNode 子进程）。CLI 二进制同样叫
# opencode.exe —— 桌面宿主必须有 Electron 佐证（同 exe --type= 后代或
# opencode-cli.exe 后代），否则按 CLI/OTHER 处理，绝不冒充。
_OPENCODE_GUI_BASENAMES = frozenset({
    "opencode.exe", "opencode dev.exe", "opencode beta.exe"})
_OPENCODE_UNAMBIGUOUS_GUI = frozenset({"opencode dev.exe", "opencode beta.exe"})
_OPENCODE_HELPER_BASENAMES = frozenset({"opencode-cli.exe"})
# DSH 官方 Desktop：productName "DeepSeek Harness"；Electron RunAsNode
# Host 与主进程同 exe（cmdline 不同），helper 按同 exe 后代收敛。
_DSH_GUI_BASENAMES = frozenset({"deepseek harness.exe"})
# npm 包名佐证（CLI shim 可能叫 node.exe；只在命令行包含包路径时采信）
_OPENCODE_PKG_MARK = "opencode-ai"

_MAX_ANCESTRY_HOPS = 16
_DIAG_MAX = 8


def _args_of(info: dict) -> tuple[str, ...]:
    cl = info.get("cmdline") or []
    return tuple(str(x) for x in cl)


def _basename_of(info: dict) -> str:
    return str(info.get("name") or "").lower()


def _exe_of(info: dict) -> str:
    return str(info.get("exe") or "").lower()


def _is_electron_helper(args: tuple[str, ...]) -> bool:
    return any(a.startswith(_ELECTRON_HELPER_FLAG) for a in args)


def _is_zcode_helper_cmd(args: tuple[str, ...]) -> bool:
    joined = " ".join(args).lower()
    return any(mark in joined for mark in _ZCODE_HELPER_CMD_MARKS)


def _is_codex_helper_cmd(args: tuple[str, ...]) -> bool:
    # 与 _CODEX_HELPER_ARGS 精确匹配的开关名（不匹配 prompt/path 参数）
    return any(a in _CODEX_HELPER_ARGS for a in args)


def _ancestor_chain(pid: int, parent_by_pid: dict[int, int],
                    max_hops: int = _MAX_ANCESTRY_HOPS) -> list[int]:
    chain: list[int] = []
    cur = parent_by_pid.get(pid, 0)
    hops = 0
    while cur and hops < max_hops:
        chain.append(cur)
        cur = parent_by_pid.get(cur, 0)
        hops += 1
    return chain


def _classify_windows_inventory(info_by_pid: dict[int, dict],
                                parent_by_pid: dict[int, int],
                                ) -> tuple[dict[int, str], list[str]]:
    """一次内存分类：pid → role + 有界诊断（plan2 §4.1 证据顺序）。

    证据优先级：明确 cmdline/helper 子命令 > GUI exe + GUI ancestry >
    终端 ancestry CLI > 同 kind 子进程收敛到 canonical host；
    冲突时不作为 terminal agent 发出（宁可 Desktop source UNKNOWN）。
    """
    roles: dict[int, str] = {}
    diagnostics: list[str] = []

    def note(msg: str):
        if len(diagnostics) < _DIAG_MAX:
            diagnostics.append(msg)

    def info(pid: int) -> dict:
        return info_by_pid.get(pid) or {}

    def basename(pid: int) -> str:
        return _basename_of(info(pid))

    # ---- 1) 定位 GUI 宿主主进程（无同 exe 祖先、无 --type=、无 helper 命令） ----
    zcode_mains: list[int] = []
    chatgpt_mains: list[int] = []
    codex_gu: list[int] = []
    opencode_candidates: list[int] = []   # opencode.exe（CLI/桌面二义）
    opencode_mains: list[int] = []
    dsh_mains: list[int] = []
    for pid, inf in info_by_pid.items():
        args = _args_of(inf)
        base = _basename_of(inf)
        if base in _ZCODE_GUI_BASENAMES:
            if _is_electron_helper(args) or _is_zcode_helper_cmd(args):
                continue
            if any(basename(a) in _ZCODE_GUI_BASENAMES
                   for a in _ancestor_chain(pid, parent_by_pid)):
                continue
            zcode_mains.append(pid)
        elif base in _CHATGPT_GUI_BASENAMES:
            if _is_electron_helper(args):
                continue
            if any(basename(a) in _CHATGPT_GUI_BASENAMES
                   for a in _ancestor_chain(pid, parent_by_pid)):
                continue
            chatgpt_mains.append(pid)
        elif base in _DSH_GUI_BASENAMES:
            if _is_electron_helper(args):
                continue
            if any(basename(a) in _DSH_GUI_BASENAMES
                   for a in _ancestor_chain(pid, parent_by_pid)):
                continue
            dsh_mains.append(pid)
        elif base in _OPENCODE_GUI_BASENAMES:
            if _is_electron_helper(args):
                continue
            if any(basename(a) in _OPENCODE_GUI_BASENAMES
                   for a in _ancestor_chain(pid, parent_by_pid)):
                continue
            if base in _OPENCODE_UNAMBIGUOUS_GUI:
                opencode_mains.append(pid)
            else:
                opencode_candidates.append(pid)
        elif base.startswith(_CODEX_PROC_MARK[0]) and base.endswith(_CODEX_PROC_MARK[1]):
            codex_gu.append(pid)

    # OpenCode.exe 二义消解（无插件监听 plan §三：通用进程不能冒充桌面宿主）：
    # 桌面宿主必须有 Electron 佐证——同 exe --type= 后代或 opencode-cli.exe
    # 后代；否则交给终端 CLI 判定（terminal ancestry / conhost），都不满足
    # 保持 OTHER（fail closed）。
    for pid in opencode_candidates:
        if _has_electron_helper_descendant(pid, parent_by_pid, info_by_pid,
                                           _OPENCODE_GUI_BASENAMES) \
                or _has_basename_descendant(pid, parent_by_pid, info_by_pid,
                                            _OPENCODE_HELPER_BASENAMES):
            opencode_mains.append(pid)

    # ChatGPT Desktop 的 Codex 正向证据（plan2 §4.3）：包族目录或树内
    # codex*.exe 后代；否则绝不把 ChatGPT.exe 当 Codex。
    def _has_codex_descendant(root: int) -> bool:
        seen: set[int] = set()
        stack = [root]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            b = basename(cur)
            if (cur != root and b.startswith("codex")
                    and b.endswith(".exe")):
                return True
            stack.extend(p for p, pp in parent_by_pid.items() if pp == cur)
            if len(seen) > 512:
                return False
        return False

    codex_host_roots: list[int] = []
    for pid in chatgpt_mains:
        if (_CHATGPT_CODEX_PKG_MARK in _exe_of(info(pid))
                or _has_codex_descendant(pid)):
            codex_host_roots.append(pid)

    # 独立 Codex GUI app（未来形态）：无 codex.exe 祖先、无终端 ancestry、
    # 且存在同 exe Electron(--type=) 后代或 app-server/daemon 后代；
    # 已位于任何桌面宿主树内的 codex*.exe 不重复成 root（helper 收敛）。
    _desktop_mains = set(codex_host_roots) | set(chatgpt_mains) | set(zcode_mains)
    for pid in codex_gu:
        if pid in codex_host_roots:
            continue
        args = _args_of(info(pid))
        if _is_electron_helper(args) or _is_codex_helper_cmd(args):
            continue
        if any(basename(a) == "codex.exe"
               for a in _ancestor_chain(pid, parent_by_pid)):
            continue
        if any(a in _desktop_mains
               for a in _ancestor_chain(pid, parent_by_pid)):
            continue
        if _has_codex_descendant(pid) and not _tree_has_terminal_ancestor(
                pid, parent_by_pid, info_by_pid):
            codex_host_roots.append(pid)

    # ---- 2) 宿主树归组：祖先链到达任一宿主 root 的进程 ----
    host_root_by_pid: dict[int, tuple[str, int]] = {}
    for root in codex_host_roots:
        host_root_by_pid[root] = (ROLE_CODEX_DESKTOP_HOST, root)
    for root in zcode_mains:
        host_root_by_pid[root] = (ROLE_ZCODE_DESKTOP_HOST, root)
    for root in opencode_mains:
        host_root_by_pid[root] = (ROLE_OPENCODE_DESKTOP_HOST, root)
    for root in dsh_mains:
        host_root_by_pid[root] = (ROLE_DSH_DESKTOP_HOST, root)

    def _host_of(pid: int) -> tuple[str, int] | None:
        if pid in host_root_by_pid:
            return host_root_by_pid[pid]
        for anc in _ancestor_chain(pid, parent_by_pid):
            if anc in host_root_by_pid:
                return host_root_by_pid[anc]
        return None

    # 宿主 root 自身获得 HOST 角色（plan2 §4.1.4：helper 收敛到 root）
    for root, (role, _r) in host_root_by_pid.items():
        roles[root] = role

    for pid in info_by_pid:
        if pid in host_root_by_pid:
            continue
        host = _host_of(pid)
        if host is None:
            continue
        role, _root = host
        base = basename(pid)
        if role == ROLE_CODEX_DESKTOP_HOST:
            # codex*.exe / app-server 子命令 / ChatGPT 同 app Electron
            # 进程 → Codex helper；树内其他进程（bash 等）保持 OTHER，
            # 不形成 target
            is_codex_proc = (
                (base.startswith("codex") and base.endswith(".exe"))
                or base in _CHATGPT_GUI_BASENAMES
                or _is_codex_helper_cmd(_args_of(info(pid))))
            roles[pid] = (ROLE_CODEX_APP_SERVER_HELPER if is_codex_proc
                          else ROLE_OTHER)
        elif role == ROLE_ZCODE_DESKTOP_HOST:
            is_zcode_proc = (base in _ZCODE_GUI_BASENAMES
                             or base in _ZCODE_HELPER_BASENAMES
                             or _is_zcode_helper_cmd(_args_of(info(pid))))
            roles[pid] = (ROLE_ZCODE_AGENT_HELPER if is_zcode_proc
                          else ROLE_OTHER)
        elif role == ROLE_OPENCODE_DESKTOP_HOST:
            # OpenCode 家族：GUI exe 本体（Electron helper/RunAsNode）与
            # 捆绑 opencode-cli.exe；树内其他进程保持 OTHER
            is_opencode_proc = (base in _OPENCODE_GUI_BASENAMES
                                or base in _OPENCODE_HELPER_BASENAMES)
            roles[pid] = (ROLE_OPENCODE_HELPER if is_opencode_proc
                          else ROLE_OTHER)
        else:
            # DSH：Electron 主进程与 RunAsNode Host 同 exe；node/pnpm
            # 等 profile runner 子进程不单独成 target
            is_dsh_proc = base in _DSH_GUI_BASENAMES
            roles[pid] = (ROLE_DSH_HELPER if is_dsh_proc
                          else ROLE_OTHER)

    # ---- 3) 终端 CLI 判定（未被宿主树认领的进程） ----
    def _terminal_ancestry(pid: int) -> bool:
        for anc in _ancestor_chain(pid, parent_by_pid):
            if basename(anc) in _TERMINAL_HOST_BASENAMES:
                # 终端宿主自身位于桌面宿主树内（ZCode 集成终端）时，
                # 它承载的仍是真实 CLI——继续成立
                return True
        return False

    for pid, inf in info_by_pid.items():
        if roles.get(pid) not in (None, ROLE_OTHER):
            continue
        base = _basename_of(inf)
        args = _args_of(inf)
        if base.startswith("codex") and base.endswith(".exe"):
            if is_codex_maintenance(args):
                roles[pid] = ROLE_OTHER
                continue
            # CLI 判定严格于 4.3.1 的宽 name 匹配：必须终端 ancestry 或
            # 控制台宿主证据；explorer/未知 ancestry 的 codex.exe 不再
            # 冒充 CLI（避免桌面宿主 runtime 误报）。
            # v4.4.1 例外：官方 daemon/app-server 常驻形态自带确凿自证据
            # （刻意脱离控制台），不再要求 terminal ancestry——否则
            # `codex app-server --remote-control` 整组 Agent 会被漏报。
            if is_daemon_form(args):
                roles[pid] = ROLE_TERMINAL_AGENT
            elif _terminal_ancestry(pid) or _has_conhost_child(
                    pid, parent_by_pid, info_by_pid):
                roles[pid] = ROLE_TERMINAL_AGENT
            else:
                roles[pid] = ROLE_OTHER
                note(f"codex.exe pid={pid} unattributed: no terminal ancestry")
            continue
        if base == "opencode.exe":
            # 与 Codex 相同的严格 CLI 判定；桌面宿主候选已在前面按
            # Electron 佐证归类，这里的 opencode.exe 只可能是 CLI 形态
            if is_daemon_form(args):
                roles[pid] = ROLE_TERMINAL_AGENT
            elif _terminal_ancestry(pid) or _has_conhost_child(
                    pid, parent_by_pid, info_by_pid):
                roles[pid] = ROLE_TERMINAL_AGENT
            else:
                roles[pid] = ROLE_OTHER
                note(f"opencode.exe pid={pid} unattributed: no terminal ancestry")
            continue
        # claude/kimi/pi/dsh 保持 name/cmdline 匹配（无桌面宿主冲突；
        # ZCode/终端集成 shell 内的真实 CLI 不因宿主树而被吞）
        cmd = " ".join(_args_of(inf)).lower()
        kind = _match_kind(base, cmd)
        if kind is not None:
            roles[pid] = ROLE_TERMINAL_AGENT
    return roles, diagnostics


def _tree_has_terminal_ancestor(pid: int, parent_by_pid: dict[int, int],
                                info_by_pid: dict[int, dict]) -> bool:
    for anc in _ancestor_chain(pid, parent_by_pid):
        if _basename_of(info_by_pid.get(anc) or {}) in _TERMINAL_HOST_BASENAMES:
            return True
    return False


def _descendants_of(root: int, parent_by_pid: dict[int, int],
                    limit: int = 512) -> set[int]:
    """有界遍历 root 的后代 pid 集合（含 root；环/超限保护）。"""
    seen = {root}
    stack = [root]
    while stack:
        cur = stack.pop()
        for child, ppid in parent_by_pid.items():
            if ppid == cur and child not in seen:
                seen.add(child)
                stack.append(child)
                if len(seen) > limit:
                    return seen
    return seen


def _has_electron_helper_descendant(root: int, parent_by_pid: dict[int, int],
                                    info_by_pid: dict[int, dict],
                                    exe_names: frozenset) -> bool:
    """root 是否有同 exe 名的 Electron helper（--type=）后代。"""
    for pid in _descendants_of(root, parent_by_pid):
        if pid == root:
            continue
        inf = info_by_pid.get(pid) or {}
        if (_basename_of(inf) in exe_names
                and _is_electron_helper(_args_of(inf))):
            return True
    return False


def _has_basename_descendant(root: int, parent_by_pid: dict[int, int],
                             info_by_pid: dict[int, dict],
                             exe_names: frozenset) -> bool:
    for pid in _descendants_of(root, parent_by_pid):
        if pid != root and _basename_of(info_by_pid.get(pid) or {}) in exe_names:
            return True
    return False


def _has_conhost_child(pid: int, parent_by_pid: dict[int, int],
                       info_by_pid: dict[int, dict]) -> bool:
    """控制台子系统进程会拉起 conhost 子进程（explorer 双击运行 CLI）。"""
    for child, ppid in parent_by_pid.items():
        if ppid == pid and _basename_of(info_by_pid.get(child) or {}) == "conhost.exe":
            return True
    return False


@dataclass(frozen=True)
class WindowsRuntimeInventory:
    """一次 Windows census 的两类权威结果（plan2 §4.2）。

    terminal_instances 与 desktop_hosts 来自同一 psutil 枚举；
    Monitor 绝不为 Desktop 再启动第二个扫描线程。
    """
    terminal_instances: tuple[AgentInstance, ...] = ()
    desktop_hosts: tuple[DesktopHost, ...] = ()
    authoritative: bool = True
    diagnostics: tuple[str, ...] = ()


def scan_windows_inventory() -> WindowsRuntimeInventory:
    """Windows 原生进程扫描：同一 census 产出终端 Agent 与桌面宿主。

    全局 process_iter 失败抛 ProbeUnavailable（authoritative=False），
    绝不返回空结果冒充"权威确认无 Agent"（v4plan §4.1）。
    """
    import psutil

    try:
        procs = list(psutil.process_iter(
            attrs=["pid", "name", "cmdline", "create_time", "ppid", "exe"]))
    except Exception as exc:
        raise ProbeUnavailable(f"process_iter failed: {exc!r}") from exc

    parent_by_pid: dict[int, int] = {}
    info_by_pid: dict[int, dict] = {}
    for p in procs:
        try:
            pid = p.info["pid"]
            if pid == _SELF_PID:
                continue
            parent_by_pid[pid] = int(p.info.get("ppid") or 0)
            info_by_pid[pid] = p.info
        except Exception:
            continue

    roles, diagnostics = _classify_windows_inventory(info_by_pid, parent_by_pid)

    # 终端候选照旧走 canonicalization（npm wrapper 折叠）
    candidates: list[ProcessCandidate] = []
    for pid, role in roles.items():
        if role != ROLE_TERMINAL_AGENT:
            continue
        info = info_by_pid.get(pid, {})
        base = _basename_of(info)
        if base.startswith("codex") and base.endswith(".exe"):
            kind = AgentKind.CODEX   # 角色分类已确认 CLI，不经过宽 name 匹配
        else:
            name = info.get("name") or ""
            cmd = " ".join(str(x) for x in (info.get("cmdline") or [])).lower()
            kind = _match_kind(name, cmd)
        if kind is None:
            continue
        candidates.append(ProcessCandidate(
            kind=kind, pid=pid, ppid=parent_by_pid.get(pid, 0)))

    canonical, launchers = canonicalize_agent_processes(candidates, parent_by_pid)
    out: list[AgentInstance] = []
    for cand in candidates:
        if cand.pid not in canonical:
            continue
        info = info_by_pid.get(cand.pid, {})
        created = float(info.get("create_time") or 0.0)
        inst = AgentInstance(
            kind=cand.kind, pid=cand.pid, source="windows",
            started_at=created,
            process_token=f"{created:.3f}",
            process_token_source="create_time",
            ppid=parent_by_pid.get(cand.pid, 0),
            launcher_pids=launchers.get(cand.pid, ()),
        )
        external_parent_pid = parent_by_pid.get(cand.pid, 0) or 0
        for launcher_pid in launchers.get(cand.pid, ()):
            external_parent_pid = parent_by_pid.get(launcher_pid, 0) or 0
        inst.external_parent_pid = external_parent_pid
        inst.external_parent_alive = (
            True if external_parent_pid in info_by_pid
            else False if external_parent_pid > 0
            else None)
        try:
            cwd = psutil.Process(inst.pid).cwd()
            inst.cwd = cwd or ""
            inst.home = os.path.expanduser("~")
        except Exception:
            pass
        out.append(inst)

    # 桌面宿主：每个 host root 一个 DesktopHost，helper 收敛、不单独成 target
    _HOST_ROLE_KIND = {
        ROLE_CODEX_DESKTOP_HOST: AgentKind.CODEX,
        ROLE_ZCODE_DESKTOP_HOST: AgentKind.ZCODE,
        ROLE_OPENCODE_DESKTOP_HOST: AgentKind.OPENCODE,
        ROLE_DSH_DESKTOP_HOST: AgentKind.DSH,
    }
    _HELPER_ROLES = (ROLE_CODEX_APP_SERVER_HELPER, ROLE_ZCODE_AGENT_HELPER,
                     ROLE_OPENCODE_HELPER, ROLE_DSH_HELPER)
    hosts: list[DesktopHost] = []
    for pid, role in sorted(roles.items()):
        if role not in _HOST_ROLE_KIND:
            continue
        info = info_by_pid.get(pid, {})
        kind = _HOST_ROLE_KIND[role]
        created = float(info.get("create_time") or 0.0)
        helpers = tuple(sorted(
            other for other, orole in roles.items()
            if orole in _HELPER_ROLES
            and _host_root_for(other, roles, parent_by_pid) == pid))
        hosts.append(DesktopHost(
            kind=kind, source="windows", pid=pid,
            process_token=f"{created:.3f}",
            exe=str(info.get("exe") or ""),
            cmdline=_args_of(info),
            started_at=created,
            helper_pids=helpers,
        ))
    return WindowsRuntimeInventory(
        terminal_instances=tuple(out),
        desktop_hosts=tuple(hosts),
        authoritative=True,
        diagnostics=tuple(diagnostics),
    )


def _host_root_for(pid: int, roles: dict[int, str],
                   parent_by_pid: dict[int, int]) -> int | None:
    """pid 所属的桌面宿主 root pid（自身或祖先链上的 host 角色）。"""
    host_roles = (ROLE_CODEX_DESKTOP_HOST, ROLE_ZCODE_DESKTOP_HOST,
                  ROLE_OPENCODE_DESKTOP_HOST, ROLE_DSH_DESKTOP_HOST)
    if roles.get(pid) in host_roles:
        return pid
    for anc in _ancestor_chain(pid, parent_by_pid):
        if roles.get(anc) in host_roles:
            return anc
    return None


def scan_windows() -> list[AgentInstance]:
    """兼容包装（plan2 §4.2）：只返回终端 Agent 实例，Monitor/测试不改。"""
    return list(scan_windows_inventory().terminal_instances)


# ------------------------------------------------------------------ WSL

def _decode_wsl_output(data: bytes) -> str:
    """wsl.exe 输出解码：UTF-16 BOM → 高 NUL 比例（UTF-16LE）→ UTF-8 → 替换。"""
    if not data:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if data:
        sample = data[:256]
        if sample.count(0) > len(sample) // 4:
            return data.decode("utf-16-le", errors="replace")
    try:
        text = data.decode("utf-8")
        if "\ufffd" not in text[:64]:
            return text
    except UnicodeDecodeError:
        pass
    return data.decode("utf-8", errors="replace")


def _run_wsl(distro: str | None, script: str, timeout: float = 8.0,
             user: str | None = None) -> str:
    """执行 wsl.exe 命令并返回解码后的 stdout；失败抛异常。

    必须用 --exec：`wsl --` 会把参数重新拼接并经默认 shell 再解释一次，
    脚本里的 $var/$(...) 会被外层 shell 先行展开清空；--exec 直接把
    argv 交给 sh，脚本只被解释一次。
    """
    argv = ["wsl.exe"]
    if distro:
        argv += ["-d", distro]
    if user:
        argv += ["-u", user]
    argv += ["--exec", "sh", "-c", script]
    r = subprocess.run(
        argv, capture_output=True, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    rc = getattr(r, "returncode", 0)
    if rc not in (0, None):
        raise RuntimeError(f"wsl.exe failed ({rc})")
    return _decode_wsl_output(r.stdout)


def build_metadata_script(pids: list[int], env_names=ENV_ALLOWLIST) -> str:
    """构造批量 metadata 查询脚本；environ 在 WSL 内部过滤 allowlist。

    输出记录格式（tab 分隔，cwd 可含空格）：
      P\\t<pid>  C\\t<cwd>  T\\t<start ticks>  H\\t<home>  E\\tVAR=value
    """
    pid_list = " ".join(str(int(p)) for p in pids)
    pattern = "^(" + "|".join(re.escape(n) for n in env_names) + ")="
    return (
        "for p in " + pid_list + "; do\n"
        "  [ -d /proc/$p ] || continue\n"
        "  printf 'P\\t%s\\n' \"$p\"\n"
        "  c=$(readlink /proc/$p/cwd 2>/dev/null) || c=''\n"
        "  printf 'C\\t%s\\n' \"$c\"\n"
        "  line=$(cat /proc/$p/stat 2>/dev/null) || { printf 'T\\t\\n'; }\n"
        "  if [ -n \"$line\" ]; then\n"
        "    rest=${line##*)}\n"
        "    set -- $rest\n"
        "    printf 'T\\t%s\\n' \"${20}\"\n"
        "  fi\n"
        "  u=$(ps -o uid= -p $p 2>/dev/null | tr -d ' ')\n"
        "  if [ -n \"$u\" ]; then\n"
        "    ent=$(getent passwd \"$u\" 2>/dev/null || true)\n"
        "    h=$(printf '%s\\n' \"$ent\" | cut -d: -f6)\n"
        "    n=$(printf '%s\\n' \"$ent\" | cut -d: -f1)\n"
        "    printf 'H\\t%s\\t%s\\n' \"$u\" \"${h:-}\"\n"
        "    printf 'U\\t%s\\n' \"${n:-}\"\n"
        "  fi\n"
        "  tr '\\0' '\\n' < /proc/$p/environ 2>/dev/null |"
        " grep -E '" + pattern + "' | sed 's/^/E\\t/'\n"
        "done\n"
    )


def parse_metadata(text: str) -> dict[int, dict]:
    """解析 build_metadata_script 输出 → {pid: {cwd, ticks, uid, home, env}}。

    Python 侧对 E 行再做一次 allowlist 过滤（纵深防御）。
    """
    out: dict[int, dict] = {}
    cur: dict | None = None
    for raw in text.splitlines():
        if "\t" not in raw:
            continue
        tag, _, value = raw.partition("\t")
        if tag == "P":
            try:
                pid = int(value.strip())
            except ValueError:
                cur = None
                continue
            cur = out.setdefault(pid, {"cwd": "", "ticks": "", "uid": "",
                                       "user": "", "home": "", "env": {}})
        elif cur is None:
            continue
        elif tag == "C":
            cur["cwd"] = value.strip()
        elif tag == "T":
            cur["ticks"] = value.strip()
        elif tag == "H":
            uid_s, _, home = value.partition("\t")
            cur["uid"] = uid_s.strip()
            cur["home"] = home.strip()
        elif tag == "U":
            cur["user"] = value.strip()
        elif tag == "E":
            name, _, val = value.partition("=")
            if name.strip() in ENV_ALLOWLIST:
                cur["env"][name.strip()] = val.strip()
    return out


def filter_env_line(line: str) -> str | None:
    """单行 env 过滤（测试用）：仅 allowlist 变量通过。"""
    name = line.split("=", 1)[0]
    return line if name in ENV_ALLOWLIST else None


def parse_running_quiet(text: str) -> list[str]:
    """解析 `wsl --list --running --quiet`：每行一个发行版名。"""
    names = []
    for line in text.splitlines():
        name = line.strip().strip("\ufeff").rstrip("\x00")
        if name and not name.lower().startswith("name"):
            names.append(name)
    return names


def parse_list_verbose(text: str) -> list[str]:
    """兼容 fallback：解析 `wsl -l -v` 表格的 Running 行。"""
    distros = []
    for line in text.splitlines():
        parts = line.split()
        # 形如：* Ubuntu   Running   2   或  Ubuntu  Running  2
        if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].lower() == "running":
            name = parts[1] if parts[0] == "*" else parts[0]
            if name and not name.startswith("NAME") and name != "NAME":
                distros.append(name)
    return distros


@dataclass(frozen=True)
class DistroInventory:
    """一轮 WSL 发行版清单的权威性结论（V3.1.1）。

    authoritative=True 表示枚举成功（--list --running --quiet 或 -l -v
    任一路径成功）。此时 running==() 必须解释为"已成功确认当前没有
    Running 发行版"，绝不是"不知道所以保留旧 Agent"。
    authoritative=False 表示枚举失败：只能沿用旧名单 best-effort，
    不得据此判定任何 distro 停止（无法读取 ≠ 已经不存在）。
    """
    running: tuple[str, ...]
    authoritative: bool
    error: str = ""


@dataclass
class _FallbackIdentity:
    kind: AgentKind
    token: str
    last_etimes: int


class WslProcessProbe:
    """WSL 发行版与内部进程探测；source 健康按 distro 隔离（plan §54）。

    scan() 返回 (按真实 source 分组的实例, 各 source 是否健康)；
    一个 distro 扫描失败不会污染其他 distro 的实例与判定。
    """

    def __init__(self, allow_root_metadata: bool = False):
        # v4.3.1 DP43-R03：root 权限用 Event 承载——Dashboard 运行期切换
        # 必须立即生效（特别是 True→False 撤权），不得只读一次配置。
        self._root_metadata_allowed = threading.Event()
        if allow_root_metadata:
            self._root_metadata_allowed.set()
        self._distros: list[str] = []
        self._distros_ts = 0.0
        self._lock = threading.Lock()
        self.last_ok = True
        self.last_error = ""
        # 每 distro 的最后成功结果，供扫描失败时保留缓存
        self._cache: dict[str, list[AgentInstance]] = {}
        self._runtime_cache: dict[str, tuple[RemoteRuntimeContext, ...]] = {}
        self._distro_ok: dict[str, bool] = {}
        # 应用生命周期内见过（running 过）的 distro：停止后持续发空 tombstone
        self._known_distros: set[str] = set()
        # 注意：running 清单不做正结果缓存（V3.1.2）——过期 "Running" 会经
        # wsl -d 探测把用户刚停止的 distro 重新启动。
        # /proc starttime 缺失时的稳定 fallback 代次缓存（防 PID reuse 退化）
        self._fallback: dict[tuple[str, int], _FallbackIdentity] = {}
        self._fallback_gen = 0
        # 每次 scan() 的单调代次号（写入 SourceProbeSnapshot.generation）
        self._scan_gen = 0
        # 性能计数
        self.spawn_count = 0    # wsl.exe 调用次数
        self.scan_count = 0
        self.scan_ms = 0.0
        self.metadata_pid_count = 0
        # 因明确 DETACHED 被过滤的 canonical group 累计计数（非敏感统计）
        self.detached_filtered_count = 0

    # ---- 第一层：发行版 ----
    def _list_running_distros(self) -> DistroInventory:
        """枚举 Running 发行版；成功空输出 = 权威确认无 Running（V3.1.1）。

        每次调用都发起全新查询，绝不缓存正结果（V3.1.2 被动性闭环）：
        缓存里的 "Running" 可能刚被用户停止，而 wsl -d <distro> --exec
        本身会启动目标发行版——用过期清单授权 ps/metadata 探测会把用户
        刚停止的 distro 重新拉起，且 probe 间隔（3s）小于 WSL 空闲关机
        延迟，会形成自维持的 "探测保活" 循环。--list --running 是宿主侧
        查询，不会启动任何发行版，每轮重查的代价因此是安全的。
        """
        error = ""
        distros: list[str] | None = None
        try:
            r = subprocess.run(
                ["wsl.exe", "--list", "--running", "--quiet"],
                capture_output=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.spawn_count += 1
            rc = getattr(r, "returncode", 0)
            if rc not in (0, None):
                raise RuntimeError(f"wsl.exe --list --running failed ({rc})")
            text = _decode_wsl_output(r.stdout)
            distros = parse_running_quiet(text)
            if not distros and text.strip():
                # 某些版本 --quiet 仍打印表头：交给 fallback 判定
                raise RuntimeError("unparseable --running --quiet output")
        except Exception as exc:
            error = str(exc)
            try:
                distros = self._list_running_distros_verbose()
            except Exception as exc2:
                error = f"{error}; {exc2}"
                distros = None
        if distros is None:
            # 两条路径都失败：无法读取 ≠ 已经不存在。不更新已知名单，
            # 沿用旧 running 名单让 scan() 走"保留缓存 + 不判死"分支。
            self.last_error = error
            with self._lock:
                return DistroInventory(tuple(self._distros), False, error)
        self.last_error = ""
        with self._lock:
            self._distros = list(distros)
            self._distros_ts = time.time()
        return DistroInventory(tuple(distros), True, "")

    def _list_running_distros_verbose(self) -> list[str]:
        """兼容 fallback：`wsl -l -v` 表格解析；失败抛异常。

        成功但没有 Running 行 = 所有已安装 distro 都处于 Stopped，
        结果为空列表（合法且权威）。
        """
        r = subprocess.run(
            ["wsl.exe", "-l", "-v"], capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.spawn_count += 1
        rc = getattr(r, "returncode", 0)
        if rc not in (0, None):
            raise RuntimeError(f"wsl -l failed ({rc})")
        return parse_list_verbose(_decode_wsl_output(r.stdout))

    # ---- 第二层：进程表 ----
    def _ps_scan(self, distro: str, exclude_pids: set[int]) -> list[tuple]:
        """一条 ps 拿全表；返回 (pid,ppid,sid,pgid,tpgid,tty,uid,etimes,stat,comm,args)。

        Z/X/x 状态（zombie/dead）的进程对象已不在运行，直接不返回——
        它们不是 live Agent（v4plan §4.4）。
        """
        text = _run_wsl(distro, f"ps -eo {_PS_FORMAT} 2>/dev/null")
        self.spawn_count += 1
        rows = []
        for line in text.splitlines():
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            try:
                pid, ppid, sid, pgid, tpgid = (int(x) for x in parts[:5])
                uid = int(parts[6])
                etimes = int(parts[7])
            except ValueError:
                continue
            tty, stat, comm, args = parts[5], parts[8], parts[9], parts[10]
            if pid in exclude_pids:
                continue
            if stat[:1] in _DEAD_STATES:
                continue
            rows.append((pid, ppid, sid, pgid, tpgid, tty, uid, etimes,
                         stat, comm, args))
        return rows

    @staticmethod
    def _match_zcode_remote_runtime(comm: str, args: str) -> str | None:
        """Recognize only ZCode's current WSL remote-service entry points.

        The match is deliberately narrower than terminal Agent matching. These
        processes are evidence that a Desktop data plane is live; they must never be
        emitted as AgentSurface.TERMINAL targets.
        """
        try:
            argv = shlex.split(str(args or ""))
        except ValueError:
            return None
        if not argv:
            return None
        executable = argv[0].rsplit("/", 1)[-1].casefold()
        if executable in {"node", "nodejs"}:
            entry = ""
            for arg in argv[1:]:
                if arg in {"-e", "--eval", "-p", "--print", "-r", "--require",
                           "--loader", "--import"}:
                    return None
                if arg.startswith("-"):
                    continue
                entry = arg
                break
        else:
            entry = argv[0]
        low = entry.casefold()
        # 3.x/3.16 bundles keep the GLM runtime below this containment root,
        # while the entry filename changed between cjs and the SEA fallback.
        # Match only known entry names; arbitrary files under ``.zcode`` are
        # not evidence of a live Agent.
        if re.search(
                r"/\.zcode/server/agents/glm/"
                r"(?:zcode\.cjs|zcode-agent(?:\.(?:cjs|js|mjs))?)"
                r"$", low):
            return "agent"
        if low.endswith("/.zcode/server/zcode-server.cjs"):
            return "server"
        return None

    @staticmethod
    def _match_agent(comm: str, args: str) -> AgentKind | None:
        low = args.lower()
        if re.search(r"(^|/)claude( |$|\.)", low) or "claude-code" in low or "@anthropic-ai/claude-code" in low:
            return AgentKind.CLAUDE
        if re.search(r"(^|/)codex( |$|\.)", low) or low.startswith("codex ") or "codex-x86_64" in low or "codex-aarch64" in low:
            if is_codex_maintenance(args):
                return None
            return AgentKind.CODEX
        if "kimi" in low and ("kimi-cli" in low or "/kimi" in low or low.startswith("kimi") or " kimi " in low):
            return AgentKind.KIMI
        if "pi-coding-agent" in low or re.search(r"(^|/)pi( |$)", low):
            return AgentKind.PI
        # 无插件监听 plan：OpenCode / DSH CLI（严格 argv0 形态，避免把
        # 恰好包含这些词的其他命令当 Agent）
        if (re.search(r"(^|/)opencode( |$|\.)", low)
                or low.startswith("opencode ")
                or "/.opencode/bin/opencode" in low):
            return AgentKind.OPENCODE
        if (re.search(r"(^|/)dsh( |$|\.)", low) or low.startswith("dsh ")
                or "/.dsh/bin/dsh" in low
                or _is_dsh_cli_cmd(low)):
            # 标准 npm/pnpm 安装的 node .../@deepseek-ai/dsh/lib/bin.js
            # 也属于官方 CLI；宽泛的 `/.dsh/` 子串仍不能使用，否则
            # cat/tail ~/.dsh/sessions/... 会被误判成 agent。
            return AgentKind.DSH
        return None

    # ---- root metadata 权限（v4.3.1 DP43-R03：运行期可撤权） ----
    def set_allow_root_metadata(self, allowed: bool) -> None:
        """O(1)、线程安全：True→False 立即阻止后续 root retry；
        False→True 立即允许后续 root retry。in-flight 的那一次
        _run_wsl(user="root") 不强制 kill（下不为例）。"""
        if allowed:
            self._root_metadata_allowed.set()
        else:
            self._root_metadata_allowed.clear()

    def root_metadata_allowed(self) -> bool:
        return self._root_metadata_allowed.is_set()

    # ---- 第三层：仅 canonical PID 的 metadata ----
    def _metadata(self, distro: str, pids: list[int]) -> dict[int, dict]:
        if not pids:
            return {}
        try:
            text = _run_wsl(distro, build_metadata_script(pids))
            self.spawn_count += 1
            meta = parse_metadata(text)
        except Exception:
            return {}
        # 部分进程因权限读不到（如 agent 以 root 运行）。默认不提权：
        # Agent 仍然创建、cwd/home/env 可为空；只有用户显式打开高级选项
        # 才允许一次 root retry（仅读 cwd/token/uid/HOME/allowlist env）。
        # 权限在真正执行 root retry 前再次检查（DP43-R03）：普通 metadata
        # 查询与 root retry 之间用户可能已关闭开关。
        if self._root_metadata_allowed.is_set():
            missing = [p for p in pids if p in meta and not meta[p].get("cwd")]
            missing += [p for p in pids if p not in meta]
            missing = sorted(set(missing))
            if missing and self._root_metadata_allowed.is_set():
                try:
                    text = _run_wsl(distro, build_metadata_script(missing),
                                    user="root")
                    self.spawn_count += 1
                    for pid, extra in parse_metadata(text).items():
                        base = meta.setdefault(pid, {"cwd": "", "ticks": "", "uid": "",
                                                     "user": "", "home": "", "env": {}})
                        for key in ("cwd", "ticks", "uid", "user", "home"):
                            if extra.get(key) and not base.get(key):
                                base[key] = extra[key]
                        base["env"].update(extra.get("env", {}))
                except Exception:
                    pass
        return meta

    def _resolve_token(self, distro: str, pid: int, kind: AgentKind,
                       ticks: str, etimes: int) -> tuple[str, str]:
        """进程 incarnation：/proc starttime 优先；缺失时用稳定 fallback 代次。

        fallback token 在同 PID + 同 kind + etimes 单调递增时保持不变，
        etimes 明显回退（PID 复用）时换新代次——绝不退化成裸 PID identity。
        """
        if ticks:
            self._fallback.pop((distro, pid), None)
            return ticks, "proc"
        key = (distro, pid)
        prev = self._fallback.get(key)
        if prev is not None and prev.kind == kind and etimes >= prev.last_etimes:
            prev.last_etimes = etimes
            return prev.token, "fallback"
        self._fallback_gen += 1
        token = f"fb{self._fallback_gen}"
        self._fallback[key] = _FallbackIdentity(kind=kind, token=token,
                                                last_etimes=etimes)
        return token, "fallback"

    def scan(self, exclude_pids: set[int] | None = None
             ) -> dict[str, SourceProbeSnapshot]:
        """全量扫描；返回按真实 source（wsl:Ubuntu 等）键控的三态快照。

        三态语义（v4plan §3.2/§4.4）：
          * authoritative=True + instances —— distro 运行且 Agent 被发现；
          * authoritative=True + ()     —— 权威确认该 distro 当前无 Agent
            （或已停止），本结果有资格让 Monitor 立即清除旧实例；
          * authoritative=False —— 枚举/ps 读取失败，携带上一轮缓存实例
            且绝不判死（无法读取 ≠ 已经不存在）。

        只有本轮 fresh --list --running 确认 Running 的 distro 才执行
        wsl -d（V3.1.2：过期的 Running 清单绝不授权进入 distro——那会
        把用户刚停止的发行版重新启动）。每 cycle 1×--list --running +
        每 Running distro 1×ps + 1×metadata 批查询（只查 canonical PID）。
        """
        t0 = time.perf_counter()
        exclude_pids = {int(pid) for pid in (exclude_pids or set()) if pid}
        self._scan_gen += 1
        gen = self._scan_gen
        inv = self._list_running_distros()
        out: dict[str, SourceProbeSnapshot] = {}
        if not inv.authoritative:
            # 枚举失败：不更新 known distros、不发 tombstone；
            # 已知 source 保留缓存实例并全部 authoritative=False。
            with self._lock:
                cached = {d: list(v) for d, v in self._cache.items()}
                cached_rt = dict(self._runtime_cache)
            for distro in self._known_distros:
                out[f"wsl:{distro}"] = SourceProbeSnapshot(
                    source=f"wsl:{distro}", generation=gen,
                    observed_at=time.time(), authoritative=False,
                    instances=tuple(cached.get(distro, [])),
                    error=inv.error,
                    remote_runtimes=tuple(cached_rt.get(distro, ())))
            self.last_ok = False
            self.scan_count += 1
            self.scan_ms = time.perf_counter() - t0
            return out

        running = set(inv.running)
        self._known_distros.update(running)
        # 已停止的 distro：每轮持续输出 authoritative 空 tombstone。
        # 只发一次会让 Monitor 下一轮失去 authoritative 依据。
        for distro in self._known_distros - running:
            source = f"wsl:{distro}"
            out[source] = SourceProbeSnapshot(
                source=source, generation=gen, observed_at=time.time(),
                authoritative=True, instances=())
            with self._lock:
                self._cache.pop(distro, None)
                self._runtime_cache.pop(distro, None)
                self._distro_ok.pop(distro, None)
                # 整个 distro 已停止：旧 Linux PID incarnation 的 fallback
                # 代次没有保留意义（重启后 PID 从小整数重来也不继承旧 token）
                for key in list(self._fallback):
                    if key[0] == distro:
                        self._fallback.pop(key, None)

        scan_ok = True
        for distro in inv.running:
            source = f"wsl:{distro}"
            try:
                rows = self._ps_scan(distro, exclude_pids)
            except Exception as exc:
                scan_ok = False
                with self._lock:
                    self._distro_ok[distro] = False
                    cached = self._cache.get(distro)
                    cached_rt = self._runtime_cache.get(distro, ())
                self.last_error = str(exc)
                out[source] = SourceProbeSnapshot(
                    source=source, generation=gen, observed_at=time.time(),
                    authoritative=False,
                    instances=tuple(cached or ()), error=str(exc),
                    remote_runtimes=tuple(cached_rt))
                continue
            matched: list[tuple] = []
            for row in rows:
                (pid, ppid, sid, pgid, tpgid, tty, uid, etimes,
                 stat, comm, args) = row
                if "sh -c" in args.lower() or "grep" in args.lower() or "ps -eo" in args.lower():
                    continue
                kind = self._match_agent(comm, args)
                if kind:
                    matched.append(row)
            # ZCode Remote Development runtime is a Desktop data-plane signal, not
            # a terminal Agent. Collapse server + agent child for the same Linux uid
            # to one representative; prefer the actual agent child when both exist.
            remote_by_uid: dict[int, tuple[tuple, str]] = {}
            for row in rows:
                role = self._match_zcode_remote_runtime(row[9], row[10])
                if not role:
                    continue
                uid = int(row[6])
                prev = remote_by_uid.get(uid)
                if (prev is None
                        or (role == "agent" and prev[1] != "agent")
                        or (role == prev[1] and row[0] < prev[0][0])):
                    remote_by_uid[uid] = (row, role)

            # 进程树 canonicalization：npm/node wrapper 只保留最深 runtime
            parent_by_pid = {row[0]: row[1] for row in rows}
            candidates = [ProcessCandidate(
                kind=self._match_agent(r[9], r[10]) or AgentKind.CODEX,
                pid=r[0], ppid=r[1], comm=r[9], args=r[10]) for r in matched]
            canonical, launchers = canonicalize_agent_processes(
                candidates, parent_by_pid)
            # terminal attachment（v4.2.3 §2.3）：canonicalization 之后、
            # 构造 AgentInstance 之前，按 canonical group 判定；
            # DETACHED 整组不进入 authoritative snapshot，UNKNOWN 正常
            # 产生（transient ps 输出不得误删）。
            rows_by_pid = {row[0]: row for row in rows}
            canonical_rows: list[tuple] = []
            attachments: dict[int, TerminalAttachment] = {}
            for r in matched:
                if r[0] not in canonical:
                    continue
                attachment = canonical_terminal_attachment(
                    r[0], launchers.get(r[0], ()), rows_by_pid)
                if attachment is TerminalAttachment.DETACHED:
                    self.detached_filtered_count += 1
                    continue
                attachments[r[0]] = attachment
                canonical_rows.append(r)

            metadata_pids = sorted({r[0] for r in canonical_rows}
                                   | {item[0][0] for item in remote_by_uid.values()})
            meta = self._metadata(distro, metadata_pids)
            self.metadata_pid_count = len(metadata_pids)
            now = time.time()
            remote_runtimes: list[RemoteRuntimeContext] = []
            for uid, (row, role) in sorted(remote_by_uid.items()):
                info = meta.get(row[0], {})
                if str(info.get("uid") or "") != str(uid):
                    continue
                remote_runtimes.append(RemoteRuntimeContext(
                    kind=AgentKind.ZCODE,
                    transport="wsl-exec",
                    source=source,
                    distro=distro,
                    pid=int(row[0]),
                    uid=int(uid),
                    user=str(info.get("user") or ""),
                    home=str(info.get("home") or ""),
                    runtime_role=role,
                    observed_at=now,
                    generation=gen,
                    process_token=str(info.get("ticks") or ""),
                ))
            instances = []
            for (pid, ppid, sid, pgid, tpgid, tty, uid, etimes,
                 stat, comm, args) in canonical_rows:
                kind = self._match_agent(comm, args)
                if kind is None:
                    continue
                info = meta.get(pid, {})
                env = info.get("env", {})
                token, token_source = self._resolve_token(
                    distro, pid, kind, str(info.get("ticks") or ""), etimes)
                inst = AgentInstance(
                    kind=kind,
                    pid=pid, source=source,
                    process_token=token,
                    process_token_source=token_source,
                    started_at=now - float(etimes),
                    ppid=ppid, sid=sid, pgid=pgid, tpgid=tpgid,
                    tty=tty if tty != "?" else "",
                    uid=uid,
                    user=info.get("user", ""),
                    cwd=info.get("cwd", ""),
                    home=info.get("home", ""),
                    launcher_pids=launchers.get(pid, ()),
                    terminal_attachment=attachments.get(
                        pid, TerminalAttachment.UNKNOWN),
                )
                inst.wt_session = env.get("WT_SESSION", "")
                inst.wt_profile_id = env.get("WT_PROFILE_ID", "")
                inst.codex_home = env.get("CODEX_HOME", "")
                inst.claude_config_dir = env.get("CLAUDE_CONFIG_DIR", "")
                inst.kimi_code_home = env.get("KIMI_CODE_HOME", "")
                inst.pi_session_dir = env.get("PI_CODING_AGENT_SESSION_DIR", "")
                inst.dsh_home = env.get("DSH_HOME", "")
                inst.opencode_db = env.get("OPENCODE_DB", "")
                hints = [n for n in ("TMUX", "STY", "TERM_PROGRAM") if env.get(n)]
                inst.terminal_hint = "·".join(hints[:1])
                instances.append(inst)
            # fallback 代次缓存只保留本轮见到的 PID
            seen = {(distro, r[0]) for r in canonical_rows}
            for key in list(self._fallback):
                if key[0] == distro and key not in seen:
                    self._fallback.pop(key, None)
            with self._lock:
                self._cache[distro] = instances
                self._runtime_cache[distro] = tuple(remote_runtimes)
                self._distro_ok[distro] = True
            out[source] = SourceProbeSnapshot(
                source=source, generation=gen, observed_at=now,
                authoritative=True, instances=tuple(instances),
                remote_runtimes=tuple(remote_runtimes))
        self.last_ok = scan_ok
        if self.last_ok:
            self.last_error = ""
        self.scan_count += 1
        self.scan_ms = time.perf_counter() - t0
        return out
