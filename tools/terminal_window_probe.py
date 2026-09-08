"""Windows Terminal 窗口唤起实机 probe（v4.1.1 plan §25）。

只做 window 级操作：列出 / 验证 / 激活 WT 顶层窗口。
输出只含 hwnd / pid / process_created / class / title / valid /
foreground result——绝不输出 Terminal 可见文本，也不切换 Tab/Pane。

用法：
  python tools/terminal_window_probe.py --list
  python tools/terminal_window_probe.py --validate <hwnd>
  python tools/terminal_window_probe.py --activate <hwnd>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import winkeys
from agents.terminal_uia import WT_WINDOW_CLASS


def list_windows() -> int:
    rows = [r for r in winkeys.enum_windows()
            if str(r[3]) == WT_WINDOW_CLASS]
    if not rows:
        print("没有发现 Windows Terminal 顶层窗口")
        return 0
    print(f"{'hwnd':>10}  {'pid':>8}  {'created':>12}  {'class':<28} title")
    for hwnd, pid, title, cls in rows:
        ident = winkeys.window_identity(int(hwnd))
        created = f"{ident.process_created:.1f}" if ident else "?"
        valid = "valid" if (ident and winkeys.validate_window(ident)) \
            else "invalid"
        print(f"{int(hwnd):>10}  {int(pid):>8}  {created:>12}  "
              f"{cls:<28} [{valid}] {title[:40]!r}")
    return 0


def validate(hwnd: int) -> int:
    ident = winkeys.window_identity(hwnd)
    if ident is None:
        print(f"hwnd {hwnd}: 无法建立 WindowIdentity（窗口不存在或进程"
              f"不可读）")
        return 1
    ok = winkeys.validate_window(ident)
    print(f"hwnd {ident.hwnd} pid {ident.pid} "
          f"created {ident.process_created:.1f} class {ident.window_class}")
    print(f"valid: {ok}")
    return 0 if ok else 1


def activate(hwnd: int) -> int:
    ident = winkeys.window_identity(hwnd)
    if ident is None or not winkeys.validate_window(ident):
        print(f"hwnd {hwnd}: identity 校验失败，拒绝激活（fail-closed）")
        return 1
    winkeys.restore_window(hwnd)
    if winkeys.try_set_foreground(hwnd):
        print(f"hwnd {hwnd}: OK（已恢复并置于前台）")
        return 0
    winkeys.flash_window(hwnd)
    print(f"hwnd {hwnd}: FOREGROUND_DENIED（OS 拒绝抢前台，已闪烁"
          f"任务栏提醒）")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true",
                        help="列出全部 WT 顶层窗口")
    parser.add_argument("--validate", type=int, metavar="HWND",
                        help="验证 WindowIdentity（IsWindow/PID/create_time/class）")
    parser.add_argument("--activate", type=int, metavar="HWND",
                        help="restore + SetForegroundWindow（被拒时 Flash）")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("仅支持 Windows")
        return 1
    if args.validate is not None:
        return validate(args.validate)
    if args.activate is not None:
        return activate(args.activate)
    return list_windows()


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if _stream and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    sys.exit(main())
