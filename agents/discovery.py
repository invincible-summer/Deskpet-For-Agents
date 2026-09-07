"""V3 进程发现层：Windows 原生（psutil）+ WSL ProcessProbe（plan.md §7-§9）。

只读、无 hooks、无注入。WSL 探测分三层：
  1. wsl.exe -l -v  → Running 发行版（15s 缓存）
  2. 每发行版一条 ps → pid/ppid/sid/pgid/tpgid/tty/uid/etimes/comm/args
  3. 仅对匹配到的 Agent PID 做一次批量 metadata 查询：
     /proc/<pid>/cwd、/proc/<pid>/stat（starttime ticks = 进程 token）、
     /proc/<pid>/environ —— environ 在 WSL 内部就按 allowlist 过滤，
     Python 永远看不到 OPENAI_API_KEY 之类的其他变量。

用户 HOME 通过 uid → getent passwd 解析，不枚举 /home/*。
"""
import os
import re
import subprocess
import threading
import time

from .models import AgentKind, AgentInstance
from .paths import ENV_ALLOWLIST

_SELF_PID = os.getpid()

# ps 列顺序（与 _PS_FORMAT 一一对应）
_PS_FORMAT = "pid=,ppid=,sid=,pgid=,tpgid=,tty=,uid=,etimes=,comm=,args="


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


def scan_windows() -> list[AgentInstance]:
    """Windows 原生进程扫描：附带 cwd/ppid/create_time 身份。"""
    import psutil

    out: list[AgentInstance] = []
    try:
        procs = list(psutil.process_iter(
            attrs=["pid", "name", "cmdline", "create_time", "ppid"]))
    except Exception:
        return out

    matched: list[tuple[AgentInstance, object]] = []
    for p in procs:
        if p.info["pid"] == _SELF_PID:
            continue
        name = p.info.get("name") or ""
        cmd = _norm_cmdline(p)
        if not name and not cmd:
            continue
        kind = _match_kind(name, cmd)
        if not kind:
            continue
        created = float(p.info.get("create_time") or 0.0)
        inst = AgentInstance(
            kind=kind, pid=p.info["pid"], source="windows",
            started_at=created,
            process_token=f"{created:.3f}",
            ppid=int(p.info.get("ppid") or 0),
        )
        matched.append((inst, p))

    # 同 kind 去重：A 是 B 的祖先（npm shim -> node）时只保留子进程
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
        keep: list[AgentInstance] = []
        for inst, p in items:
            cur, hops = parents.get(p.pid, 0), 0
            while cur and cur not in pids and hops < 8:
                try:
                    cur = psutil.Process(cur).ppid()
                except Exception:
                    break
                hops += 1
            if cur in pids and cur != p.pid:
                continue
            keep.append(inst)
        # 补充 cwd（懒读取，仅匹配进程）
        for inst in keep:
            try:
                cwd = psutil.Process(inst.pid).cwd()
                inst.cwd = cwd or ""
                inst.home = os.path.expanduser("~")
            except Exception:
                pass
        out.extend(keep)
    return out


# ------------------------------------------------------------------ WSL

def _run_wsl(distro: str | None, script: str, timeout: float = 8.0,
             user: str | None = None) -> str:
    """执行 wsl.exe 命令并返回 UTF-8 stdout；失败抛异常。

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
    return r.stdout.decode("utf-8", errors="replace")


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
        "    h=$(getent passwd \"$u\" 2>/dev/null | cut -d: -f6)\n"
        "    printf 'H\\t%s\\t%s\\n' \"$u\" \"${h:-}\"\n"
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
                                       "home": "", "env": {}})
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
        elif tag == "E":
            name, _, val = value.partition("=")
            if name.strip() in ENV_ALLOWLIST:
                cur["env"][name.strip()] = val.strip()
    return out


def filter_env_line(line: str) -> str | None:
    """单行 env 过滤（测试用）：仅 allowlist 变量通过。"""
    name = line.split("=", 1)[0]
    return line if name in ENV_ALLOWLIST else None


class WslProcessProbe:
    """WSL 发行版与内部进程探测；单发行版失败不影响其他（plan §54）。"""

    def __init__(self):
        self._distros: list[str] = []
        self._distros_ts = 0.0
        self._lock = threading.Lock()
        self.last_ok = True
        self.last_error = ""
        # 每 distro 的最后成功结果，供扫描失败时保留缓存
        self._cache: dict[str, list[AgentInstance]] = {}
        self._distro_ok: dict[str, bool] = {}
        self.spawn_count = 0   # 性能计数：wsl.exe 调用次数

    # ---- 第一层：发行版 ----
    def _list_running_distros(self) -> list[str]:
        with self._lock:
            if time.time() - self._distros_ts < 15:
                return list(self._distros)
        try:
            r = subprocess.run(
                ["wsl.exe", "-l", "-v"], capture_output=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.spawn_count += 1
            text = r.stdout.decode("utf-16", errors="replace")
            rc = getattr(r, "returncode", 0)
            if rc not in (0, None):
                raise RuntimeError(f"wsl.exe -l failed ({rc})")
        except Exception as exc:
            self.last_ok = False
            self.last_error = str(exc)
            with self._lock:
                return list(self._distros)
        distros = []
        for line in text.splitlines():
            parts = line.split()
            # 形如：* Ubuntu   Running   2   或  Ubuntu  Running  2
            if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].lower() == "running":
                name = parts[1] if parts[0] == "*" else parts[0]
                if name and not name.startswith("NAME") and name != "NAME":
                    distros.append(name)
        with self._lock:
            self._distros = distros
            self._distros_ts = time.time()
        return distros

    # ---- 第二层：进程表 ----
    def _ps_scan(self, distro: str, exclude_pids: set[int]) -> list[tuple]:
        """一条 ps 拿全表；返回 (pid,ppid,sid,pgid,tpgid,tty,uid,etimes,comm,args)。"""
        text = _run_wsl(distro, f"ps -eo {_PS_FORMAT} 2>/dev/null")
        self.spawn_count += 1
        rows = []
        for line in text.splitlines():
            parts = line.split(None, 9)
            if len(parts) < 10:
                continue
            try:
                pid, ppid, sid, pgid, tpgid = (int(x) for x in parts[:5])
                uid = int(parts[6])
                etimes = int(parts[7])
            except ValueError:
                continue
            tty, comm, args = parts[5], parts[8], parts[9]
            if pid in exclude_pids:
                continue
            rows.append((pid, ppid, sid, pgid, tpgid, tty, uid, etimes, comm, args))
        return rows

    @staticmethod
    def _match_agent(comm: str, args: str) -> AgentKind | None:
        low = args.lower()
        if re.search(r"(^|/)claude( |$|\.)", low) or "claude-code" in low or "@anthropic-ai/claude-code" in low:
            return AgentKind.CLAUDE
        if re.search(r"(^|/)codex( |$|\.)", low) or low.startswith("codex ") or "codex-x86_64" in low or "codex-aarch64" in low:
            return AgentKind.CODEX
        if "kimi" in low and ("kimi-cli" in low or "/kimi" in low or low.startswith("kimi") or " kimi " in low):
            return AgentKind.KIMI
        if "pi-coding-agent" in low or re.search(r"(^|/)pi( |$)", low):
            return AgentKind.PI
        return None

    # ---- 第三层：仅匹配 PID 的 metadata ----
    def _metadata(self, distro: str, pids: list[int]) -> dict[int, dict]:
        if not pids:
            return {}
        try:
            text = _run_wsl(distro, build_metadata_script(pids))
            self.spawn_count += 1
            meta = parse_metadata(text)
        except Exception:
            return {}
        # 部分进程因权限读不到（如 agent 以 root 运行）：用 root 重试一次
        missing = [p for p in pids if p in meta and not meta[p].get("cwd")]
        missing += [p for p in pids if p not in meta]
        missing = sorted(set(missing))
        if missing:
            try:
                text = _run_wsl(distro, build_metadata_script(missing), user="root")
                self.spawn_count += 1
                for pid, extra in parse_metadata(text).items():
                    base = meta.setdefault(pid, {"cwd": "", "ticks": "", "uid": "",
                                                 "home": "", "env": {}})
                    for key in ("cwd", "ticks", "uid", "home"):
                        if extra.get(key) and not base.get(key):
                            base[key] = extra[key]
                    base["env"].update(extra.get("env", {}))
            except Exception:
                pass
        return meta

    def scan(self, exclude_pids: set[int] | None = None) -> list[AgentInstance]:
        """全量扫描；每 distro 每 cycle 约 1×ps + 1×metadata 批查询。"""
        exclude_pids = {int(pid) for pid in (exclude_pids or set()) if pid}
        distros = self._list_running_distros()
        if not self.last_ok and not distros:
            with self._lock:
                return [i for rows in self._cache.values() for i in rows]
        scan_ok = True
        for distro in distros:
            try:
                rows = self._ps_scan(distro, exclude_pids)
            except Exception as exc:
                self._distro_ok[distro] = False
                self.last_error = str(exc)
                scan_ok = False
                continue
            matched = []
            for row in rows:
                pid, ppid, sid, pgid, tpgid, tty, uid, etimes, comm, args = row
                if "sh -c" in args.lower() or "grep" in args.lower() or "ps -eo" in args.lower():
                    continue
                kind = self._match_agent(comm, args)
                if kind:
                    matched.append(row)
            meta = self._metadata(distro, [r[0] for r in matched])
            now = time.time()
            instances = []
            for pid, ppid, sid, pgid, tpgid, tty, uid, etimes, comm, args in matched:
                kind = self._match_agent(comm, args)
                if kind is None:
                    continue
                info = meta.get(pid, {})
                env = info.get("env", {})
                inst = AgentInstance(
                    kind=kind,
                    pid=pid, source=f"wsl:{distro}",
                    process_token=str(info.get("ticks") or ""),
                    started_at=now - float(etimes),
                    ppid=ppid, sid=sid, pgid=pgid, tpgid=tpgid,
                    tty=tty if tty != "?" else "",
                    uid=uid,
                    cwd=info.get("cwd", ""),
                    home=info.get("home", ""),
                )
                inst.wt_session = env.get("WT_SESSION", "")
                inst.wt_profile_id = env.get("WT_PROFILE_ID", "")
                inst.codex_home = env.get("CODEX_HOME", "")
                inst.claude_config_dir = env.get("CLAUDE_CONFIG_DIR", "")
                inst.kimi_code_home = env.get("KIMI_CODE_HOME", "")
                hints = [n for n in ("TMUX", "STY", "TERM_PROGRAM") if env.get(n)]
                inst.terminal_hint = "·".join(hints[:1])
                if info.get("home") and not inst.user:
                    inst.user = os.path.basename(info["home"].rstrip("/")) or ""
                instances.append(inst)
            self._cache[distro] = instances
            self._distro_ok[distro] = True
        self.last_ok = scan_ok and all(self._distro_ok.get(d, True) for d in distros)
        self.last_error = "" if self.last_ok else self.last_error
        with self._lock:
            return [i for d in distros for i in self._cache.get(d, [])]
