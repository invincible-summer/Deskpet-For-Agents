"""Windows Terminal 窗口唤起实机 probe（v4.1.3 plan §38）。

只做 window 级操作：列出 / 验证 / 激活 WT 顶层窗口 / 解析 Agent 候选。
输出只含 hwnd / pid / process_created / class / title / valid /
foreground result / binding confidence+reason——绝不输出 Terminal 可见
文本，也不切换 Tab/Pane。

用法：
  python tools/terminal_window_probe.py --list
  python tools/terminal_window_probe.py --validate <hwnd>
  python tools/terminal_window_probe.py --activate <hwnd>
  python tools/terminal_window_probe.py --resolve

--resolve 实机定位（§38 判定表）：
  resolver 有 hwnd + probe --activate 成功 + DeskPet 不成功 → activation 链问题
  resolver 无 hwnd                                             → candidate selection 问题
  validate false                                               → WindowIdentity 问题
  validate true + FOREGROUND_DENIED                            → Windows foreground policy（Flash 即正确 fallback）
"""
from __future__ import annotations

import argparse
import sys
import time
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


def resolve() -> int:
    """实机解析 Agent → Terminal 窗口候选（§38，只输出非敏感诊断）。"""
    from agents.discovery import scan_windows
    from agents.terminal_resolver import TerminalWindowResolver
    from agents.terminal_uia import TerminalObserver, UiaBackend

    instances = []
    try:
        instances.extend(scan_windows())
    except Exception as exc:
        print(f"Windows 扫描失败：{exc!r}")
    try:
        from agents.discovery import WslProcessProbe
        for source, snap in WslProcessProbe().scan().items():
            instances.extend(snap.instances)
    except Exception as exc:
        print(f"WSL 扫描失败：{exc!r}")

    controls = {}
    screens = {}
    backend = UiaBackend()
    if backend.start():
        observer = TerminalObserver(backend, cfg={"terminal_observer": True})
        try:
            observer.start()   # _started=True：poll 与屏幕摘要通道才生效
            # 屏幕摘要冷启动：最多等 ~3s 让每 control 读到一次可见文本
            # （与产品内共享同一预算；输出绝不包含文本内容）
            deadline = time.time() + 3.0
            while time.time() < deadline:
                observer.poll(time.time())
                if observer.controls and len(observer.screen_texts()) >= \
                        len(observer.controls):
                    break
                time.sleep(0.4)
            controls = dict(observer.controls)
            screens = observer.screen_texts()
        finally:
            observer.stop()
    else:
        print("UIA 不可用（只影响 WSL 标题评分；native 祖先链与唯一窗口"
              "兜底不受影响）")

    print(f"agents={len(instances)} wt_controls={len(controls)} "
          f"screens={len(screens)}/{len(controls)}")
    if not instances:
        print("没有发现 Agent 进程")
        return 0

    resolver = TerminalWindowResolver()
    bindings = resolver.resolve(list(instances), controls, time.time(),
                                screens=screens)
    for inst in instances:
        b = bindings.get(inst.key)
        print("-" * 60)
        print(f"agent       {inst.kind.value} · {inst.source}"
              + (f" · project {inst.project}" if inst.project else ""))
        if b is None:
            print("binding     无")
            continue
        line = (f"binding     {b.confidence.value} · {b.reason or '?'}"
                + (f" · score {b.score}/{b.runner_up_score}"
                   if b.score else ""))
        print(line)
        if b.window is None:
            print("window      无候选（不可唤起）")
            continue
        w = b.window
        print(f"window      hwnd {w.hwnd} · pid {w.pid} · created "
              f"{w.process_created:.1f} · class {w.window_class}")
        print(f"valid       {winkeys.validate_window(w)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true",
                        help="列出全部 WT 顶层窗口")
    parser.add_argument("--validate", type=int, metavar="HWND",
                        help="验证 WindowIdentity（IsWindow/PID/create_time/class）")
    parser.add_argument("--activate", type=int, metavar="HWND",
                        help="restore + SetForegroundWindow（被拒时 Flash）")
    parser.add_argument("--resolve", action="store_true",
                        help="实机解析 Agent → 窗口候选（confidence/reason/"
                             "hwnd/identity/valid，不含终端文本）")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("仅支持 Windows")
        return 1
    if args.validate is not None:
        return validate(args.validate)
    if args.activate is not None:
        return activate(args.activate)
    if args.resolve:
        return resolve()
    return list_windows()


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if _stream and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    sys.exit(main())
