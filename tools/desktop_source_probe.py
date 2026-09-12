"""DeskPet 4.4.0 Desktop source 实机只读探针（plan2 §14）。

用途：在一台真实机器上确认 Codex Desktop / ZCode Desktop 的本地数据面
schema 与进程形态，为 capability 探测提供证据；输出全部脱敏：

  * 进程角色：basename/PID/create_time/祖先链；cmdline 只保留开关名
    （--type=、子命令 token），绝不输出完整参数值；
  * Codex state_N.sqlite：表/列 capability、originator/source/
    thread_source 枚举计数、rollout containment 结果；thread id 只输出
    sha256 前 12 位；
  * ZCode db.sqlite：表/列 capability、task_type/query_source/status
    枚举计数、permission 关联能力结论；session id 哈希化；
  * WAL/main 指纹；App Server 控制面 socket 是否存在（绝不连接）；
  * 桌面宿主顶层窗口 class 名（结构信息，无标题文本）。

绝不输出：prompt、session/terminal 文本、审批命令、tool arguments、
文件内容、token/secret。绝不写入任何 Agent 数据；绝不连接控制面。

用法：
  python tools/desktop_source_probe.py [--json OUT.json]
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import paths  # noqa: E402
from agents.discovery import scan_windows_inventory  # noqa: E402
from agents.sqlite_ro import ReadOnlySqlite, stat_fingerprint  # noqa: E402


def _hash(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _switch_tokens(argv: list[str]) -> list[str]:
    """cmdline → 仅开关名/子命令 token（不含任何参数值）。"""
    out = []
    for arg in argv[1:]:
        token = str(arg)
        if token.startswith("-"):
            out.append(token.split("=", 1)[0])
        elif len(out) == 0 and not token.startswith("-") and len(token) < 40:
            out.append("<cmd>")
    return out[:8]


def probe_processes() -> dict:
    inv = scan_windows_inventory()
    return {
        "authoritative": inv.authoritative,
        "terminal_instances": [
            {"kind": i.kind.value, "pid": i.pid, "token": i.process_token}
            for i in inv.terminal_instances],
        "desktop_hosts": [
            {"kind": h.kind.value, "pid": h.pid,
             "token": h.process_token, "host_key": h.host_key,
             "helper_count": len(h.helper_pids),
             "helpers": list(h.helper_pids)[:16],
             "cmdline_switches": _switch_tokens(list(h.cmdline))}
            for h in inv.desktop_hosts],
        "diagnostics": list(inv.diagnostics),
    }


def _table_columns(con: ReadOnlySqlite, tables: list[str]) -> dict:
    caps = con.read_schema(tables)
    if caps is None:
        return {"error": con.last_error}
    return {
        "user_version": caps.user_version,
        "tables": sorted(caps.tables),
        "columns": {t: list(caps.columns_of(t)) for t in caps.tables},
    }


def probe_codex() -> dict:
    home = paths.codex_home_root()
    cands = paths.codex_state_db_candidates(home)
    out = {"codex_home_exists": os.path.isdir(home),
           "state_db_candidates": [os.path.basename(c) for c in cands]}
    if not cands:
        out["note"] = "NO_STATE_DB"
        return out
    db = cands[0]
    fp = stat_fingerprint(db)
    out["fingerprint"] = fp.__dict__
    con = ReadOnlySqlite(db)
    try:
        if not con.open():
            out["open_error"] = con.last_error
            return out
        out["schema"] = _table_columns(con, ["threads"])
        # 枚举计数（只取枚举值与计数，不取任何文本内容）
        for enum_sql, key in (
                ("SELECT source, COUNT(*) FROM threads GROUP BY 1", "source_counts"),
                ("SELECT thread_source, COUNT(*) FROM threads GROUP BY 1", "thread_source_counts"),
                ("SELECT originator, COUNT(*) FROM threads GROUP BY 1", "originator_counts"),
                ("SELECT archived, COUNT(*) FROM threads GROUP BY 1", "archived_counts")):
            try:
                rows = con.query(enum_sql, max_rows=32)
            except Exception:
                rows = None
            if rows:
                out[key] = {str(r[0]): r[1] for r in rows}
        # 最近 thread：哈希 id + containment + 更新时间
        try:
            rows = con.query(
                "SELECT id, rollout_path, updated_at, archived, "
                "has_user_event FROM threads "
                "ORDER BY updated_at DESC LIMIT 16", max_rows=16)
        except Exception:
            rows = None
        items = []
        roots = [os.path.normcase(os.path.realpath(r))
                 for r in paths.codex_rollout_roots(home)]
        for row in rows or []:
            rp = str(row[1] or "")
            norm = os.path.normcase(os.path.realpath(rp))
            contained = any(norm == r or norm.startswith(r + os.sep)
                            for r in roots)
            items.append({
                "thread_id_hash": _hash(str(row[0])),
                "rollout_exists": os.path.isfile(rp),
                "rollout_contained": contained,
                "updated_at": int(row[2] or 0),
                "archived": int(row[3] or 0),
                "has_user_event": int(row[4] or 0),
            })
        out["recent_threads"] = items
        # App Server 控制面 socket：只报存在性，绝不连接
        for marker in ("app-server-control", "app-server-daemon"):
            p = Path(home) / marker
            out[f"appserver_{marker.replace('-', '_')}_exists"] = p.exists()
        return out
    finally:
        con.close()


def probe_zcode() -> dict:
    db_path = paths.zcode_db_path()
    out = {"db_path_exists": os.path.isfile(db_path)}
    if not os.path.isfile(db_path):
        out["note"] = "NO_ZCODE_DB"
        return out
    fp = stat_fingerprint(db_path)
    out["fingerprint"] = fp.__dict__
    con = ReadOnlySqlite(db_path)
    try:
        if not con.open():
            out["open_error"] = con.last_error
            return out
        out["schema"] = _table_columns(con, [
            "session", "message", "part", "model_usage", "tool_usage",
            "turn_usage", "session_input", "permission", "session_entry",
            "session_target"])
        for enum_sql, key in (
                ("SELECT task_type, COUNT(*) FROM session GROUP BY 1",
                 "task_type_counts"),
                ("SELECT query_source, COUNT(*) FROM model_usage GROUP BY 1",
                 "model_usage_query_source_counts"),
                ("SELECT status, COUNT(*) FROM turn_usage GROUP BY 1",
                 "turn_usage_status_counts"),
                ("SELECT approval_status, status, COUNT(*) FROM tool_usage "
                 "GROUP BY 1, 2", "tool_usage_approval_counts")):
            try:
                rows = con.query(enum_sql, max_rows=48)
            except Exception:
                rows = None
            if rows:
                out[key] = {"|".join(str(x) for x in r[:-1]): r[-1]
                            for r in rows}
        # root session 概况：哈希 id + parent 关系（无 title/path 内容）
        try:
            rows = con.query(
                "SELECT id, parent_id, time_updated, task_type FROM session "
                "ORDER BY time_updated DESC LIMIT 16", max_rows=16)
        except Exception:
            rows = None
        items = []
        for row in rows or []:
            items.append({
                "session_id_hash": _hash(str(row[0])),
                "has_parent": row[1] is not None,
                "time_updated": int(row[2] or 0),
                "task_type": str(row[3] or ""),
            })
        out["recent_sessions"] = items
        # permission 精确映射能力结论
        try:
            n = con.query("SELECT COUNT(*) FROM permission", max_rows=1)
            out["permission_rows"] = n[0][0] if n else None
        except Exception:
            out["permission_rows"] = None
        return out
    finally:
        con.close()


def probe_host_windows() -> dict:
    """宿主顶层窗口 class 名（结构信息；不含标题文本）。"""
    if os.name != "nt":
        return {"skipped": "non-windows"}
    user32 = ctypes.windll.user32
    classes: dict[str, int] = {}

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if user32.IsWindowVisible(hwnd):
            classes[buf.value] = classes.get(buf.value, 0) + 1
        return True

    user32.EnumWindows(_cb, 0)
    interesting = {k: v for k, v in classes.items()
                   if any(mark in k.lower()
                          for mark in ("chatgpt", "codex", "zcode", "chrome",
                                       "electron"))}
    return {"window_classes": interesting}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", dest="json_out", default="",
                        help="把脱敏 JSON 结果写入该文件")
    args = parser.parse_args()

    report = {
        "probe": "deskpet-desktop-source",
        "os": sys.platform,
        "processes": probe_processes(),
        "codex": probe_codex(),
        "zcode": probe_zcode(),
        "host_windows": probe_host_windows(),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
