"""V3 进程发现层：Windows 原生（psutil）+ WSL ProcessProbe（plan.md §7-§9）。

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
import subprocess
import threading
import time
from dataclasses import dataclass

from .models import AgentKind, AgentInstance
from .paths import ENV_ALLOWLIST

_SELF_PID = os.getpid()

# ps 列顺序（与 _PS_FORMAT 一一对应）
_PS_FORMAT = "pid=,ppid=,sid=,pgid=,tpgid=,tty=,uid=,etimes=,comm=,args="


# ----------------------------------------------------- 进程树 canonicalization

@dataclass
class ProcessCandidate:
    kind: AgentKind
    pid: int
    ppid: int
    comm: str = ""
    args: str = ""


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
    """Windows 原生进程扫描：canonicalize 后只为 runtime 进程建实例。"""
    import psutil

    out: list[AgentInstance] = []
    try:
        procs = list(psutil.process_iter(
            attrs=["pid", "name", "cmdline", "create_time", "ppid"]))
    except Exception:
        return out

    parent_by_pid: dict[int, int] = {}
    info_by_pid: dict[int, dict] = {}
    for p in procs:
        try:
            parent_by_pid[p.info["pid"]] = int(p.info.get("ppid") or 0)
            info_by_pid[p.info["pid"]] = p.info
        except Exception:
            continue

    candidates: list[ProcessCandidate] = []
    for pid, info in info_by_pid.items():
        if pid == _SELF_PID:
            continue
        name = info.get("name") or ""
        cmd = " ".join(str(x) for x in (info.get("cmdline") or [])).lower()
        if not name and not cmd:
            continue
        kind = _match_kind(name, cmd)
        if kind:
            candidates.append(ProcessCandidate(
                kind=kind, pid=pid, ppid=parent_by_pid.get(pid, 0)))

    canonical, launchers = canonicalize_agent_processes(candidates, parent_by_pid)
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
        try:
            cwd = psutil.Process(inst.pid).cwd()
            inst.cwd = cwd or ""
            inst.home = os.path.expanduser("~")
        except Exception:
            pass
        out.append(inst)
    return out


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
        self.allow_root_metadata = bool(allow_root_metadata)
        self._distros: list[str] = []
        self._distros_ts = 0.0
        self._lock = threading.Lock()
        self.last_ok = True
        self.last_error = ""
        # 每 distro 的最后成功结果，供扫描失败时保留缓存
        self._cache: dict[str, list[AgentInstance]] = {}
        self._distro_ok: dict[str, bool] = {}
        # 应用生命周期内见过（running 过）的 distro：停止后持续发空 tombstone
        self._known_distros: set[str] = set()
        # 注意：running 清单不做正结果缓存（V3.1.2）——过期 "Running" 会经
        # wsl -d 探测把用户刚停止的 distro 重新启动。
        # /proc starttime 缺失时的稳定 fallback 代次缓存（防 PID reuse 退化）
        self._fallback: dict[tuple[str, int], _FallbackIdentity] = {}
        self._fallback_gen = 0
        # 性能计数
        self.spawn_count = 0    # wsl.exe 调用次数
        self.scan_count = 0
        self.scan_ms = 0.0
        self.metadata_pid_count = 0

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
        if self.allow_root_metadata:
            missing = [p for p in pids if p in meta and not meta[p].get("cwd")]
            missing += [p for p in pids if p not in meta]
            missing = sorted(set(missing))
            if missing:
                try:
                    text = _run_wsl(distro, build_metadata_script(missing),
                                    user="root")
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
             ) -> tuple[dict[str, list[AgentInstance]], dict[str, bool]]:
        """全量扫描；返回按真实 source（wsl:Ubuntu 等）分组的结果与健康位。

        三态语义（V3.1.1）：
          * healthy + instances —— distro 运行且 Agent 被发现；
          * healthy + []        —— 权威确认该 distro 当前无 Agent（或已停止），
            tombstone 每轮持续输出，Monitor 侧经 gone_grace 清除旧实例；
          * unhealthy           —— 枚举/ps 读取失败，保留上一轮缓存实例
            且绝不判死（无法读取 ≠ 已经不存在）。

        只有本轮 fresh --list --running 确认 Running 的 distro 才执行
        wsl -d（V3.1.2：过期的 Running 清单绝不授权进入 distro——那会
        把用户刚停止的发行版重新启动）。每 cycle 1×--list --running +
        每 Running distro 1×ps + 1×metadata 批查询（只查 canonical PID）。
        """
        t0 = time.perf_counter()
        exclude_pids = {int(pid) for pid in (exclude_pids or set()) if pid}
        inv = self._list_running_distros()
        instances_by_source: dict[str, list[AgentInstance]] = {}
        healthy: dict[str, bool] = {}
        if not inv.authoritative:
            # 枚举失败：不更新 known distros、不发 tombstone；
            # 已知 source 保留缓存实例并全部标记不健康。
            with self._lock:
                cached = {d: list(v) for d, v in self._cache.items()}
            for distro in self._known_distros:
                source = f"wsl:{distro}"
                instances_by_source[source] = cached.get(distro, [])
                healthy[source] = False
            self.last_ok = False
            self.scan_count += 1
            self.scan_ms = time.perf_counter() - t0
            return instances_by_source, healthy

        running = set(inv.running)
        self._known_distros.update(running)
        # 已停止的 distro：每轮持续输出空 source + healthy=True。只发一次
        # 会让 Monitor 的 gone timer 下一轮就失去 authoritative 依据。
        for distro in self._known_distros - running:
            source = f"wsl:{distro}"
            instances_by_source[source] = []
            healthy[source] = True
            with self._lock:
                self._cache.pop(distro, None)
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
                healthy[source] = False
                with self._lock:
                    self._distro_ok[distro] = False
                    cached = self._cache.get(distro)
                self.last_error = str(exc)
                instances_by_source.setdefault(source, [])
                if cached:
                    instances_by_source[source] = list(cached)
                continue
            matched: list[tuple] = []
            for row in rows:
                pid, ppid, sid, pgid, tpgid, tty, uid, etimes, comm, args = row
                if "sh -c" in args.lower() or "grep" in args.lower() or "ps -eo" in args.lower():
                    continue
                kind = self._match_agent(comm, args)
                if kind:
                    matched.append(row)
            # 进程树 canonicalization：npm/node wrapper 只保留最深 runtime
            parent_by_pid = {row[0]: row[1] for row in rows}
            candidates = [ProcessCandidate(
                kind=self._match_agent(r[8], r[9]) or AgentKind.CODEX,
                pid=r[0], ppid=r[1], comm=r[8], args=r[9]) for r in matched]
            canonical, launchers = canonicalize_agent_processes(
                candidates, parent_by_pid)
            canonical_rows = [r for r in matched if r[0] in canonical]

            meta = self._metadata(distro, [r[0] for r in canonical_rows])
            self.metadata_pid_count = len(canonical_rows)
            now = time.time()
            instances = []
            for pid, ppid, sid, pgid, tpgid, tty, uid, etimes, comm, args in canonical_rows:
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
                    cwd=info.get("cwd", ""),
                    home=info.get("home", ""),
                    launcher_pids=launchers.get(pid, ()),
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
            # fallback 代次缓存只保留本轮见到的 PID
            seen = {(distro, r[0]) for r in canonical_rows}
            for key in list(self._fallback):
                if key[0] == distro and key not in seen:
                    self._fallback.pop(key, None)
            with self._lock:
                self._cache[distro] = instances
                self._distro_ok[distro] = True
            instances_by_source[source] = instances
            healthy[source] = True
        self.last_ok = scan_ok
        if self.last_ok:
            self.last_error = ""
        self.scan_count += 1
        self.scan_ms = time.perf_counter() - t0
        return instances_by_source, healthy
