"""Desktop 逻辑会话 source 基础设施（plan2 §5/§14）。

只放三类东西，不做插件注册框架：
  * SessionClaimKey —— 跨 surface 去重键（terminal exact binding 优先）；
  * DesktopSourceSnapshot —— 一次 poll 的不可拆开结果（三态语义与
    SourceProbeSnapshot 一致：non-authoritative 时 Monitor 保留 last good）；
  * DesktopSessionSource 协议 + terminal claim 构造 helper。

Codex/ZCode 具体实现在各自模块（codex_desktop.py / zcode_desktop.py）；
本模块绝不出现 schema SQL 或对宿主进程的任何控制。
"""
import os
from dataclasses import dataclass, field

from .models import AgentInstance, AgentKind, AgentSurface, DesktopHost, Observation

# 诊断条目上限（plan2 §5：有界 diagnostics）
DIAGNOSTICS_MAX = 8


def bounded_diagnostics(items, limit: int = DIAGNOSTICS_MAX) -> tuple[str, ...]:
    """把任意诊断序列压成有界 tuple；超出部分计数说明。"""
    out: list[str] = []
    for item in list(items or ())[:limit]:
        text = str(item)
        if len(text) > 200:
            text = text[:197] + "..."
        out.append(text)
    return tuple(out)


def canonical_session_path(path: str) -> str:
    """路径规范化（normcase + realpath；只用于比较，不用于打开文件）。

    DB/用户数据里的路径是 untrusted local metadata；这里只做字符串级
    canonical 化供 claim 匹配，任何"打开"动作由各 source 的 containment
    校验负责（plan2 §7.2）。
    """
    try:
        return os.path.normcase(os.path.realpath(str(path)))
    except Exception:
        return os.path.normcase(str(path))


@dataclass(frozen=True)
class SessionClaimKey:
    """跨 surface 去重键（plan2 §3.4）。

    Codex CLI/desktop 看到同一 thread/rollout 时只显示一次，优先级：
    live terminal exact binding > desktop exact catalog binding >
    ambiguous candidate。title/cwd 绝不参与去重（只能做诊断/弱过滤）。
    """
    kind: str                  # AgentKind.value
    canonical_data_root: str = ""
    session_id: str = ""
    canonical_session_path: str = ""


def claims_cover(claims: frozenset, kind: str, session_id: str = "",
                 canonical_path: str = "") -> bool:
    """terminal claim 集是否已覆盖该逻辑会话（plan2 §3.4 优先级）。

    匹配只用 exact 身份证据：session_id 相等或 canonical 会话路径相等
    （路径比较前双方都做 normcase，文件系统大小写不致造成漏配）。
    两者都缺失时不匹配（宁可重复显示也不能错杀 terminal target）。
    """
    if not claims:
        return False
    want_sid = str(session_id or "")
    want_path = canonical_session_path(canonical_path) if canonical_path else ""
    if not want_sid and not want_path:
        return False
    for claim in claims:
        if claim.kind != kind:
            continue
        if want_sid and claim.session_id and claim.session_id == want_sid:
            return True
        have_path = canonical_session_path(claim.canonical_session_path) \
            if claim.canonical_session_path else ""
        if want_path and have_path and have_path == want_path:
            return True
    return False


def build_terminal_claims(watchers: dict,
                          instances: dict[str, AgentInstance]) -> frozenset:
    """从 terminal watcher 的 exact 绑定建立 claim set（plan2 §5.3）。

    只有 watcher 已绑定会话文件的 TERMINAL 实例才产生 claim；
    session_id 缺失时退回路径派生的 file_id（仍是 exact 文件身份）。
    """
    claims: set[SessionClaimKey] = set()
    for kind, watcher in watchers.items():
        bound = getattr(watcher, "_instance_files", None) or {}
        for key, path in bound.items():
            inst = instances.get(key)
            if inst is not None and getattr(
                    inst, "surface", AgentSurface.TERMINAL) is not AgentSurface.TERMINAL:
                continue
            files = getattr(watcher, "files", None) or {}
            st = files.get(path)
            session_id = str(getattr(st, "session_id", "") or "")
            file_id = str(getattr(st, "file_id", "") or "")
            if not session_id and not file_id and not path:
                continue
            kind_value = kind.value if isinstance(kind, AgentKind) else str(kind)
            claims.add(SessionClaimKey(
                kind=kind_value,
                canonical_data_root="",
                session_id=session_id or file_id,
                canonical_session_path=canonical_session_path(path),
            ))
    return frozenset(claims)


@dataclass(frozen=True)
class DesktopSourceSnapshot:
    """一个 desktop source 一轮 poll 的不可拆开结果（plan2 §5）。

    三态语义：
      authoritative=True  —— instances/observations 是本轮权威发现；
                             Monitor 据此合并并判定"source 内部退场"；
      authoritative=False —— source 暂时不可读（DB locked/corrupt 等）：
                             Monitor 保留 last good targets 并标记 stale，
                             绝不用本轮结果判死任何会话（plan2 §9/§13）。
    """
    instances: tuple[AgentInstance, ...] = ()
    observations: dict[str, Observation] = field(default_factory=dict)
    claims: frozenset = frozenset()
    host_keys: frozenset = frozenset()
    authoritative: bool = False
    diagnostics: tuple[str, ...] = ()


class DesktopSessionSource:
    """Desktop source 最小合同（plan2 §5）。

    实现要求：
      * poll(hosts, claimed_sessions, now) 纯只读、有界耗时（在 Monitor
        后台线程执行，绝不上 Tk）；
      * 只为 hosts 中匹配自己 kind 的宿主产出逻辑会话；helper 进程
        永远不形成 target；
      * 被 terminal claim 覆盖的会话绝不发 target（去重优先级）；
      * host 退出时 Monitor 调用 drop_host(host_key)，实现必须清空该
        host 的全部运行期状态（lease/tailer/cache），不留线程。
    """

    kind: AgentKind = AgentKind.CODEX

    def poll(self, hosts: tuple[DesktopHost, ...],
             claimed_sessions: frozenset,
             now: float) -> DesktopSourceSnapshot:
        raise NotImplementedError

    def drop_host(self, host_key: str) -> None:
        raise NotImplementedError
