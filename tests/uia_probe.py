"""Windows Terminal UIA 人工验收探针（plan.md §63 + V3.1 审核 §23）。

默认绝不打印终端原文：只输出 element identity / 事件类型 / 识别状态 /
后端诊断计数。只有显式传入 --verbose-text 才打印当前可见区域前 200
字符，并明确标注这是诊断模式。

验收 matrix（实机手动执行）：
  1 WT / 1 pane / 1 WSL Codex              自动 HIGH、状态实时
  1 WT / split 2 panes / Codex+Claude      不串 WAITING
  2 tabs / 同 cwd basename                 不错绑
  2 WT windows                             HWND 归属正确
  2 WSL same-kind Claude                   两个 Agent 独立
  Ubuntu + Debian                          distro 隔离
  Agent 启动早于 DeskPet                   late-start session bind
  DeskPet 启动早于 Agent                   新 process 自动发现
  pane 新建                                <1-2s 发现（StructureChanged）
  pane 删除                                subscription 正确释放
  approval 打开                            WAITING
  approval 关闭                            ≤1.5s 清 WAITING
  Agent DONE 后 shell prompt 更新           仍能看到 DONE
  Session parser 暂不可读                  terminal activity fallback WORKING
  terminal UIA unavailable                 session 功能继续工作
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.terminal_uia import UiaBackend, TerminalObserver


def main(verbose_text: bool = False, duration: float = 60.0) -> int:
    backend = UiaBackend()
    if not backend.start():
        print("UIA backend 不可用（comtypes 缺失或 UIA 被系统禁用）")
        if backend.startup_error:
            print(f"startup_error: {backend.startup_error}")
        return 1
    observer = TerminalObserver(backend, cfg={"terminal_observer": True})
    observer.refresh_panes(force=True)
    wt_windows = {pane.hwnd for pane in observer.panes.values()}
    print(f"Windows Terminal windows: {len(wt_windows)}")
    print(f"TermControl panes: {len(observer.panes)}")
    for pane_id, pane in observer.panes.items():
        print(f"  hwnd={pane.hwnd} pid={pane.window_pid} "
              f"title={pane.title!r} runtime={pane.pane_id[1]}")
    readable = any(backend.read_visible(pid) for pid in observer.panes)
    print(f"VisibleText: {'readable' if readable else 'EMPTY'}")
    if verbose_text:
        print("!! 诊断模式：以下将打印终端可见区域原文（前 200 字符）")
    print(f"监听 {duration:.0f}s 的 UIA 事件"
          f"（notification / text-changed / structure）…")
    deadline = time.time() + duration
    last_events = -1
    last_reads = -1
    saw_structure = False
    while time.time() < deadline:
        time.sleep(1.0)
        before = observer.stats["rediscoveries"]
        observer.poll(time.time())
        stats = observer.stats
        saw_structure = saw_structure or observer.stats["rediscoveries"] > before
        if stats["events"] != last_events or stats["visible_reads"] != last_reads:
            bstats = backend.stats()
            print(f"  events={stats['events']} dropped={stats['dropped']} "
                  f"triggers={stats['triggers']} visible_reads={stats['visible_reads']} "
                  f"fallback_reads={stats['text_fallback_reads']} "
                  f"waiting_panes={len(observer.observations)} "
                  f"uia_timeouts={bstats['uia_timeouts']} "
                  f"queue_dropped={bstats['uia_queue_dropped']}")
            last_events = stats["events"]
            last_reads = stats["visible_reads"]
        for pane_id, obs in observer.observations.items():
            print(f"  [STATE] pane={pane_id} status={obs.status.value} "
                  f"phase={obs.phase.value if obs.phase else ''} "
                  f"kind={obs.agent_kind.value if obs.agent_kind else '?'} "
                  f"summary={obs.summary!r}")
        if verbose_text:
            for pane_id in observer.panes:
                text = backend.read_visible(pane_id)
                if text:
                    print(f"  [VISUAL {pane_id}] {text[:200]!r}")
    bstats = backend.stats()
    print("Notification/TextChanged: 活动事件已计入 events 计数")
    print(f"StructureChanged: {'触发过重发现' if saw_structure else '未观察到（可手动开关 pane 验证）'}")
    print(f"backend: calls={bstats['uia_calls']} errors={bstats['uia_errors']} "
          f"pane_subs={bstats['uia_pane_subs']} window_subs={bstats['uia_window_subs']}")
    observer.stop()
    print("结束。")
    return 0


if __name__ == "__main__":
    # --verbose-text 显式开启终端原文诊断（旧 --verbose 仍兼容）
    verbose = ("--verbose-text" in sys.argv) or ("--verbose" in sys.argv)
    sys.exit(main(verbose))
