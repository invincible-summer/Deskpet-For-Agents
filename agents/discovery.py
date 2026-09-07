"""进程发现层：Windows 原生（psutil）+ WSL（wsl.exe ps）。只读、无 hooks。"""
import os
import re
import subprocess
import threading
import time

from .models import AgentKind, AgentInstance

# 自己（桌宠）进程不参与匹配
_SELF_PID = os.getpid()


def _norm_cmdline(proc) -> str:
    try:
        cl = proc.info.get("cmdline") or []
        return " ".join(cl).lower()
    except Exception:
        return ""


def _match_kind(name: str, cmd: str) -> AgentKind | None:
    base = name.lower()
    if base == "claude.exe":
        return AgentKind.CLAUDE
    if base.startswith("codex") and base.endswith(".exe"):
        return AgentKind.CODEX
    if base == "kimi.exe" or base == "kimi":
        return AgentKind.KIMI
    if "claude-code" in cmd or "@anthropic-ai/claude-code" in cmd or "@anthropic-ai\\claude-code" in cmd:
        return AgentKind.CLAUDE
    if "kimi-cli" in cmd or "kimi_cli" in cmd or "/kimi" in cmd or "\\kimi" in cmd:
        return AgentKind.KIMI
    if "pi-coding-agent" in cmd:
        return AgentKind.PI
    if base == "node.exe" or base == "python.exe" or base == "pythonw.exe":
        # node/python 的判定依赖命令行，已在上面处理
        return None
    return None


def scan_windows(exclude_pids: set[int] | None = None) -> list[AgentInstance]:
    """扫描 Windows 进程；可排除受控 manager 已拥有的 PID。"""
    import psutil

    exclude_pids = {int(pid) for pid in (exclude_pids or set()) if pid}
    out: list[AgentInstance] = []
    try:
        procs = list(psutil.process_iter(attrs=["pid", "name", "cmdline", "create_time"]))
    except Exception:
        return out

    matched: list[tuple[AgentInstance, object]] = []
    for p in procs:
        if p.info["pid"] == _SELF_PID or p.info["pid"] in exclude_pids:
            continue
        name = p.info.get("name") or ""
        cmd = _norm_cmdline(p)
        if not name and not cmd:
            continue
        kind = _match_kind(name, cmd)
        if not kind:
            continue
        inst = AgentInstance(
            kind=kind, pid=p.info["pid"], source="windows",
            started_at=p.info.get("create_time") or 0.0,
        )
        matched.append((inst, p))

    # 同 kind 去重：如果 A 是 B 的祖先（如 npm shim -> node），只保留子进程
    by_kind: dict[AgentKind, list[tuple[AgentInstance, object]]] = {}
    for inst, p in matched:
        by_kind.setdefault(inst.kind, []).append((inst, p))
    for kind, items in by_kind.items():
        pids = {p.pid for _, p in items}
        parents: dict[int, int] = {}
        for _, p in items:
            try:
                parents[p.pid] = p.ppid()
            except Exception:
                parents[p.pid] = 0
        for inst, p in items:
            # 沿父链走，若到达同 kind 的另一个匹配进程，则本进程是包装器，跳过
            cur, hops = parents.get(p.pid, 0), 0
            while cur and cur not in pids and hops < 8:
                try:
                    cur = psutil.Process(cur).ppid()
                except Exception:
                    break
                hops += 1
            if cur in pids and cur != p.pid:
                continue
            out.append(inst)
    return out


class WslScanner:
    """WSL 发行版与内部进程扫描。wsl.exe -l 输出为 UTF-16；ps 输出为 UTF-8。"""

    def __init__(self):
        self._distros: list[str] = []
        self._distros_ts = 0.0
        self._lock = threading.Lock()
        self.last_ok = True
        self.last_error = ""

    def _list_running_distros(self) -> list[str]:
        if time.time() - self._distros_ts < 15:
            return self._distros
        try:
            r = subprocess.run(
                ["wsl.exe", "-l", "-v"], capture_output=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            text = r.stdout.decode("utf-16", errors="replace")
            rc = getattr(r, "returncode", 0)
            if rc not in (0, None):
                raise RuntimeError(f"wsl.exe -l failed ({rc})")
        except Exception as exc:
            self.last_ok = False
            self.last_error = str(exc)
            return self._distros
        distros = []
        for line in text.splitlines():
            parts = line.split()
            # 形如：* Ubuntu   Running   2   或  Ubuntu  Running  2
            if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].lower() == "running":
                name = parts[1] if parts[0] == "*" else parts[0]
                if name and not name.startswith("NAME"):
                    distros.append(name)
        with self._lock:
            self._distros = distros
            self._distros_ts = time.time()
            self.last_ok = True
            self.last_error = ""
        return distros

    def scan(self, exclude_pids: set[int] | None = None) -> list[AgentInstance]:
        """扫描各发行版；命令失败时保留 last_ok=False 供 Monitor 保留旧缓存。"""
        exclude_pids = {int(pid) for pid in (exclude_pids or set()) if pid}
        out: list[AgentInstance] = []
        distros = self._list_running_distros()
        if not self.last_ok:
            return out
        scan_ok = True
        for distro in distros:
            try:
                r = subprocess.run(
                    ["wsl.exe", "-d", distro, "--", "sh", "-c",
                     "ps -eo pid=,etimes=,args= 2>/dev/null"],
                    capture_output=True, timeout=8,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                text = r.stdout.decode("utf-8", errors="replace")
            except Exception:
                scan_ok = False
                continue
            if getattr(r, "returncode", 0) not in (0, None):
                scan_ok = False
                continue
            now = time.time()
            for line in text.splitlines():
                m = re.match(r"\s*(\d+)\s+(\d+)\s+(.+)$", line)
                if not m:
                    continue
                pid_s, etimes_s, args = m.groups()
                low = args.lower()
                kind = None
                # 匹配 Linux 侧的 agent 进程
                if re.search(r"(^|/)claude( |$|\.)", low) or "claude-code" in low or "@anthropic-ai/claude-code" in low:
                    kind = AgentKind.CLAUDE
                elif re.search(r"(^|/)codex( |$|\.)", low) or low.startswith("codex ") or "codex-x86_64" in low or "codex-aarch64" in low:
                    kind = AgentKind.CODEX
                elif "kimi" in low and ("kimi-cli" in low or "/kimi" in low or low.startswith("kimi") or " kimi " in low):
                    kind = AgentKind.KIMI
                elif "pi-coding-agent" in low or re.search(r"(^|/)pi( |$)", low):
                    kind = AgentKind.PI
                if not kind:
                    continue
                # 排除 grep/sh -c 包装自身
                if "sh -c" in low or "grep" in low or "ps -eo" in low:
                    continue
                try:
                    pid = int(pid_s)
                except ValueError:
                    continue
                if pid in exclude_pids:
                    continue
                out.append(AgentInstance(
                    kind=kind, pid=pid, source=f"wsl:{distro}",
                    started_at=now - float(etimes_s),
                ))
        self.last_ok = scan_ok
        self.last_error = "" if scan_ok else "WSL 进程扫描部分失败"
        return out
