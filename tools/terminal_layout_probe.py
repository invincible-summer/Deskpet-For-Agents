"""Windows Terminal topology 实机 probe（v4plan §20）。

只打印 topology metadata，绝不打印 terminal text：
  HWND / window PID / create time / class
  Tab index / Tab RuntimeId / selected / Tab name
  TermControl RuntimeId / focused

用途：验证 Tab reorder 的 RuntimeId 稳定性、inactive→active 的
detach/reattach 行为、split pane 的 SetFocus 行为。结果与上游源码
不符时只调整 V4.1 的 feature-detection/fallback，绝不引入键盘模拟。

用法：
  python tools/terminal_layout_probe.py            # 一轮 topology 快照
  python tools/terminal_layout_probe.py --watch    # 每 2s 快照（观察
                                                   # reorder/tear-out 前后）
  python tools/terminal_layout_probe.py --focus    # 演示 SetFocus：
                                                   # 对每个可见 TermControl
                                                   # 依次 SetFocus 后报告
                                                   # GetFocusedElement 位置
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.models import TabInfo, TerminalLocation, WindowIdentity
from agents.terminal_uia import (
    UIA_CLASSNAME_PROPERTY_ID,
    UIA_SELECTION_ITEM_PATTERN_ID,
    UIA_TAB_ITEM_CONTROL_TYPE_ID,
    UIA_CONTROL_TYPE_PROPERTY_ID,
    WT_WINDOW_CLASS,
    TerminalBackend,
    UiaBackend,
)
from actions import winkeys


def fmt_rid(rid) -> str:
    if rid is None:
        return "?"
    return "(" + ",".join(str(x) for x in rid) + ")"


def snapshot(backend: UiaBackend) -> dict:
    """一轮 topology 快照（不含任何终端文本）。"""
    layout = backend.discover_layout()
    out = {"windows": []}
    for hwnd in sorted(layout.windows):
        ident: WindowIdentity = layout.windows[hwnd]
        win_entry = {
            "hwnd": hwnd,
            "window_pid": ident.pid,
            "process_created": round(ident.process_created, 3),
            "window_class": ident.window_class,
            "selected_tab": fmt_rid(
                layout.selected_tabs.get(hwnd, (None,))[1:2] or None)
            if hwnd in layout.selected_tabs else "",
            "tabs": [],
            "term_controls": [],
        }
        for tab_id, tab in sorted(layout.tabs.items(),
                                  key=lambda kv: kv[1].index_hint):
            if tab.hwnd != hwnd:
                continue
            win_entry["tabs"].append({
                "index": tab.index_hint,
                "tab_runtime_id": fmt_rid(tab_id[1]),
                "selected": tab.selected,
                "name": tab.title[:40],   # tab 名是 UI 元数据不是终端正文
            })
        for pane_id, pane in sorted(layout.panes.items()):
            if pane.hwnd != hwnd:
                continue
            win_entry["term_controls"].append({
                "term_control_runtime_id": fmt_rid(pane_id[1]),
                "tab_runtime_id": fmt_rid(pane.tab_id[1])
                if pane.tab_id else "",
            })
        out["windows"].append(win_entry)

    # focused location
    location: TerminalLocation | None = backend.focused_location()
    if location is not None:
        out["focused"] = {
            "hwnd": location.window.hwnd,
            "tab_runtime_id": fmt_rid(location.tab_id[1])
            if location.tab_id else "",
            "pane_runtime_id": fmt_rid(location.pane_id[1])
            if location.pane_id else "",
        }
    else:
        out["focused"] = None
    return out


def demo_focus(backend: UiaBackend):
    """对每个可见 TermControl 依次 SetFocus，验证 split pane 行为。"""
    layout = backend.discover_layout()
    print("== SetFocus demo（每个可见 TermControl 一次）==")
    for pane_id, pane in sorted(layout.panes.items()):
        ok = backend.focus_pane(pane_id)
        time.sleep(0.3)
        location = backend.focused_location()
        focused_pane = (location.pane_id == pane_id) if location else False
        print(f"  pane {fmt_rid(pane_id[1])} hwnd={pane.hwnd} "
              f"SetFocus={'OK' if ok else 'FAIL'} "
              f"focused_after={focused_pane}")


def demo_select(backend: UiaBackend):
    """对第一个窗口的每个 Tab 依次 Select，验证 SelectionItemPattern。"""
    layout = backend.discover_layout()
    if not layout.windows:
        print("没有发现 Windows Terminal 窗口")
        return
    hwnd = sorted(layout.windows)[0]
    tabs = sorted((t for t in layout.tabs.values() if t.hwnd == hwnd),
                  key=lambda t: t.index_hint)
    print(f"== Select demo（hwnd {hwnd} 的 {len(tabs)} 个 Tab）==")
    for tab in tabs:
        ok = backend.select_tab(tab.tab_id)
        time.sleep(0.4)
        selected = backend.selected_tab(hwnd)
        verified = (selected.tab_id == tab.tab_id) if selected else False
        print(f"  tab[{tab.index_hint}] {fmt_rid(tab.tab_id[1])} "
              f"Select={'OK' if ok else 'FAIL'} verified={verified}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true",
                        help="每 2s 输出一轮快照（Ctrl+C 停止）")
    parser.add_argument("--focus", action="store_true",
                        help="运行 SetFocus 演示")
    parser.add_argument("--select", action="store_true",
                        help="运行 Select Tab 演示")
    parser.add_argument("--json", action="store_true",
                        help="输出 JSON 而不是表格文本")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("仅支持 Windows")
        return 1

    backend = UiaBackend()
    if not backend.start():
        print("UIA 初始化失败:", backend.startup_error or "unknown")
        return 1
    try:
        if args.focus:
            demo_focus(backend)
            return 0
        if args.select:
            demo_select(backend)
            return 0
        while True:
            data = snapshot(backend)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                for win in data["windows"]:
                    print(f"Window HWND {win['hwnd']} · PID "
                          f"{win['window_pid']} · created "
                          f"{win['process_created']} · class "
                          f"{win['window_class']}")
                    for tab in win["tabs"]:
                        mark = "*" if tab["selected"] else " "
                        print(f"  {mark} Tab[{tab['index']}] "
                              f"rid={tab['tab_runtime_id']} "
                              f"name={tab['name']!r}")
                    for tc in win["term_controls"]:
                        print(f"    TermControl rid="
                              f"{tc['term_control_runtime_id']} "
                              f"tab={tc['tab_runtime_id']}")
                focused = data.get("focused")
                if focused:
                    print(f"Focused: hwnd={focused['hwnd']} "
                          f"tab={focused['tab_runtime_id']} "
                          f"pane={focused['pane_runtime_id']}")
                else:
                    print("Focused: （不在 Windows Terminal 内）")
                print()
            if not args.watch:
                break
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        backend.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
