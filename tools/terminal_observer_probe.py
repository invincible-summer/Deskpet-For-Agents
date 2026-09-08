"""Windows Terminal UIA 观察（observation-only）实机 probe（v4.1.1 §25）。

默认绝不打印终端原文：只输出 control 数量 / 事件计数 / 可见读取计数 /
识别器状态。只有显式 --verbose-text 才打印当前可见区域前 200 字符，
并明确标注这是诊断模式。

验收 matrix（实机手动执行）：
  1 WT / 1 control / 1 WSL Codex              自动 HIGH、状态实时
  1 WT / split 2 controls / Codex+Claude      不串 WAITING
  2 WT windows                                HWND 归属正确
  2 WSL same-kind Claude                      两个 Agent 独立
  Ubuntu + Debian                             distro 隔离
  Agent 启动早于 DeskPet                       late-start session bind
  DeskPet 启动早于 Agent                        新 process 自动发现
  control 新建                                <1-2s 发现（StructureChanged）
  control 删除                                subscription 正确释放
  approval 打开                                WAITING
  approval 关闭                                ≤1.5s 清 WAITING
  terminal UIA unavailable                     session 功能继续工作
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.terminal_uia import TerminalObserver, UiaBackend


def main(verbose_text: bool = False, duration: float = 60.0) -> int:
    backend = UiaBackend()
    if not backend.start():
        print("UIA backend 不可用（comtypes 缺失或 UIA 被系统禁用）")
        if backend.startup_error:
            print(f"startup_error: {backend.startup_error}")
        return 1
    observer = TerminalObserver(backend, cfg={"terminal_observer": True})
    observer.refresh_controls(force=True)
    wt_windows = {c.hwnd for c in observer.controls.values()}
    print(f"Windows Terminal windows: {len(wt_windows)}")
    print(f"TermControls: {len(observer.controls)}")
    for control_id, control in observer.controls.items():
        print(f"  hwnd={control.hwnd} pid={control.window_pid} "
              f"title={control.title!r}")
    readable = any(backend.read_visible(cid) for cid in observer.controls)
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
        saw_structure = saw_structure or stats["rediscoveries"] > before
        if stats["events"] != last_events or stats["visible_reads"] != last_reads:
            bstats = backend.stats()
            print(f"  events={stats['events']} dropped={stats['dropped']} "
                  f"triggers={stats['triggers']} "
                  f"visible_reads={stats['visible_reads']} "
                  f"fallback_reads={stats['text_fallback_reads']} "
                  f"waiting_controls={len(observer.observations)} "
                  f"uia_timeouts={bstats['uia_timeouts']} "
                  f"queue_dropped={bstats['uia_queue_dropped']}")
            last_events = stats["events"]
            last_reads = stats["visible_reads"]
        for control_id, obs in observer.observations.items():
            print(f"  [STATE] control={control_id} status={obs.status.value} "
                  f"phase={obs.phase.value if obs.phase else ''} "
                  f"kind={obs.agent_kind.value if obs.agent_kind else '?'} "
                  f"summary={obs.summary!r}")
        if verbose_text:
            for control_id in observer.controls:
                text = backend.read_visible(control_id)
                if text:
                    print(f"  [VISUAL {control_id}] {text[:200]!r}")
    bstats = backend.stats()
    print("Notification/TextChanged: 活动事件已计入 events 计数")
    print(f"StructureChanged: {'触发过重发现' if saw_structure else '未观察到（可手动开关 pane 验证）'}")
    print(f"backend: calls={bstats['uia_calls']} errors={bstats['uia_errors']} "
          f"control_subs={bstats['uia_control_subs']} "
          f"window_subs={bstats['uia_window_subs']}")
    observer.stop()
    print("结束。")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if _stream and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose-text", action="store_true",
                        help="诊断模式：打印终端可见区域原文（前 200 字符）")
    parser.add_argument("--duration", type=float, default=60.0,
                        help="监听秒数（默认 60）")
    args = parser.parse_args()
    sys.exit(main(args.verbose_text, args.duration))
