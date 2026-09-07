"""Windows Terminal UIA 人工/CI 冒烟探针（plan.md §63）。

场景建议：
  1. 单 Windows Terminal + 1 个 WSL Codex
  2. 多 tab：Codex + Claude
  3. 一个 tab split 两 pane
  4. inactive tab（后台）
  5. 关闭 pane 后重开
  6. Codex 审批 overlay 出现/消失
  7. Plan mode 状态

probe 只打印：element identity / event type / recognized state。
默认不打印终端 raw text（--verbose 才打印可见区域前 200 字符，用于人工调试）。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.terminal_uia import (
    UiaBackend, TerminalObserver, DEFAULT_RECOGNIZERS,
)


def main(verbose: bool = False, duration: float = 60.0) -> int:
    backend = UiaBackend()
    if not backend.start():
        print("UIA backend 不可用（comtypes 缺失或 UIA 被系统禁用）")
        return 1
    observer = TerminalObserver(backend, cfg={"terminal_observer": True})
    observer.refresh_panes(force=True)
    print(f"发现 {len(observer.panes)} 个 TermControl pane：")
    for pane_id, pane in observer.panes.items():
        print(f"  hwnd={pane.hwnd} pid={pane.window_pid} title={pane.title!r} runtime={pane.pane_id[1]}")
    print(f"监听 {duration:.0f}s 的 UIA 事件（notification / text-changed）…")
    deadline = time.time() + duration
    last_events = 0
    last_reads = 0
    while time.time() < deadline:
        time.sleep(1.0)
        observer.poll(time.time())
        stats = observer.stats
        if stats["events"] != last_events or stats["visible_reads"] != last_reads:
            print(f"  events={stats['events']} dropped={stats['dropped']} "
                  f"triggers={stats['triggers']} visible_reads={stats['visible_reads']} "
                  f"waiting_panes={len(observer.observations)}")
            last_events = stats["events"]
            last_reads = stats["visible_reads"]
        for pane_id, obs in observer.observations.items():
            print(f"  [STATE] pane={pane_id} status={obs.status.value} "
                  f"phase={obs.phase.value if obs.phase else ''} "
                  f"summary={obs.summary!r}")
        if verbose:
            for pane_id in observer.panes:
                text = backend.read_visible(pane_id)
                if text:
                    print(f"  [VISUAL {pane_id}] {text[:200]!r}")
    observer.stop()
    print("结束。")
    return 0


if __name__ == "__main__":
    verbose = "--verbose" in sys.argv
    sys.exit(main(verbose))
