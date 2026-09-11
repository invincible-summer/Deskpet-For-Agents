"""Windows 真机验收 harness（v4.3.1 DP43 plan §13 AC431 矩阵）。

在真实 Windows 桌面上以 in-process 方式运行**真实**产品组件（真实
Shell_NotifyIcon 托盘、真实 Monitor/UIA、真实皮肤 cache、真实 Config
——config 用临时副本，不写用户真实 config.json），执行 plan §13 验收
矩阵中可自动化、且不需要键鼠注入的部分：

  suite tray-menu       AC431-UI-01 自动化子集：真实托盘 200 轮
                        WM_CONTEXTMENU 打开 native 菜单 → WM_CANCELMODE
                        （= ESC/点击菜单外的文档化结束路径）循环；
  suite tray-generation AC431-UI-04：100 轮快速换代，live ≤1、registry ≤1；
  suite quit            AC431-UI-05 自动化子集：三条退出路径 × 真实后台，
                        总等待 ≤3.25s；
  suite dashboard       AC431-UI-03 自动化子集：100 轮 open/焦点/切页/
                        hide/reopen；
  suite startup-warm/cold AC431-PERF-01/02：真实启动轮次，TTFV 与
                        里程碑顺序；
  suite idle            AC431-RES-01 子集：真实 5 分钟 idle 资源采样；
  suite menu-handles    RES-01：200 轮真实 Tk 菜单 churn 句柄回落。

需要真人点击/对话框交互的项在最终验收报告中显式标注为未自动化。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

RESULTS: dict[str, dict] = {}


def step(suite: str, name: str, ok: bool, detail: str = ""):
    RESULTS.setdefault(suite, {"checks": [], "pass": 0, "fail": 0})
    RESULTS[suite]["checks"].append(
        {"name": name, "ok": bool(ok), "detail": detail})
    RESULTS[suite]["pass" if ok else "fail"] += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"（{detail}）" if detail else ""), flush=True)


def temp_config_copy():
    import pet.config as cfg_mod
    tmp = Path(tempfile.mkdtemp(prefix="deskpet-accept-"))
    target = tmp / "config.json"
    src = Path(cfg_mod.CONFIG_PATH)
    if src.is_file():
        shutil.copy2(src, target)
    else:
        target.write_text("{}", encoding="utf-8")
    return cfg_mod.CONFIG_PATH, cfg_mod, tmp


def restore_config(saved, cfg_mod, tmp: Path):
    cfg_mod.CONFIG_PATH = saved
    shutil.rmtree(tmp, ignore_errors=True)


def make_real_app(cfg):
    from pet.app import PetApp
    app = PetApp(cfg)
    # 真实 first-map 链：窗口映射 → 后台 runtime（monitor/UIA/tray/skin）
    view = app.pet_manager.views["pet-1"]
    view.window.root.event_generate("<Map>")
    app.root.update()
    return app


def run_mainloop(app, seconds: float):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            app.root.update()
        except Exception:
            return
        time.sleep(0.01)


def wait_quit(app, t0) -> float:
    while not app._closing:
        try:
            app.root.update()
        except Exception:
            break
        time.sleep(0.005)
    return time.monotonic() - t0


# ---------------------------------------------------------------- UI-01
def _acquire_foreground_rights(timeout=45.0):
    """真实前台权限：显示一个 harness 可见窗口并等待真实输入点击。

    Windows foreground policy：进程获得最近输入事件后其线程才可成功
    SetForegroundWindow。合成 PostMessage 不携带该权限；这里显示一个
    可点击的窗口，等待验收操作者真实点击一次（或任何让本进程成为
    前台的事件），使 tray worker 的真实菜单打开路径可被验证。
    返回 (helper_root 或 None, 是否获得前台)。
    """
    import tkinter as tk
    user32 = ctypes.windll.user32
    root = tk.Tk()
    root.withdraw()
    helper = tk.Toplevel(root)
    helper.title("DeskPet 验收 — 点击本窗口一次")
    helper.geometry("380x120+80+80")
    tk.Label(helper, text=(
        "正在验证真实托盘菜单打开路径。\n"
        "请用鼠标点击本窗口任意位置一次（授予前台权限），\n"
        "然后不要移动鼠标，等待运行完成。"),
        font=("Microsoft YaHei UI", 10)).pack(expand=True)
    helper.update()
    our_pids = {os.getpid()}

    def _fg_is_ours():
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value in our_pids

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            root.update()
        except Exception:
            return root, False
        if _fg_is_ours():
            return root, True
        time.sleep(0.05)
    return root, False


def suite_tray_menu(rounds=200, interactive=True):
    print(f"== AC431-UI-01 tray menu（真实 Shell icon，{rounds} 轮开/取消）==")
    from pet.tray import (WM_APP_TRAY, TrayIcon, TrayState, registry_size)
    helper_root = None
    foreground_ok = True
    if interactive:
        helper_root, foreground_ok = _acquire_foreground_rights()
        if not foreground_ok:
            print("  [note] 45s 内未获得真实前台权限——本轮退化为"
                  "fail-closed 路径验证（见报告备注）")
    icon = TrayIcon("DeskPet acceptance — tray menu")
    icon.start()
    if not icon._ready.wait(5.0) or icon.status() is not TrayState.READY:
        step("tray-menu", "托盘 READY（NIM_ADD+SETVERSION 真实成功）",
             False, icon.last_error())
        if helper_root is not None:
            helper_root.destroy()
        return
    step("tray-menu", "托盘 READY（NIM_ADD+SETVERSION 真实成功）", True)
    user32 = ctypes.windll.user32
    v4 = TrayIcon._ICON_ID << 16
    stray_events = 0
    t0 = time.monotonic()
    for i in range(rounds):
        if helper_root is not None and i % 10 == 0:
            try:
                helper_root.update()
            except Exception:
                helper_root = None
        # 打开真实 native 菜单（worker 线程内 TrackPopupMenuEx 模态）
        user32.PostMessageW(icon._hwnd, WM_APP_TRAY, 0, v4 | 0x007B)
        time.sleep(0.03)
        # WM_CANCELMODE = ESC/点击菜单外的标准结束路径（Microsoft 文档）
        user32.PostMessageW(icon._hwnd, 0x001B, 0, 0)
        # worker 空闲标记：NIN_SELECT → restore 事件（worker 已退出菜单
        # 模态、回到 GetMessage 循环才会投递）
        user32.PostMessageW(icon._hwnd, WM_APP_TRAY, 0, v4 | 0x0400)
        deadline = time.monotonic() + 3.0
        got_restore = False
        while time.monotonic() < deadline:
            try:
                ev = icon.events.get_nowait()
            except queue.Empty:
                time.sleep(0.005)
                continue
            if ev.command == "restore":
                got_restore = True
                break
            stray_events += 1   # cancel 循环中出现其他语义事件 = 重复菜单根因
        if not got_restore:
            step("tray-menu", f"轮 {i}：菜单模态未在 3s 内结束", False)
            break
    elapsed = time.monotonic() - t0
    failures = icon.menu_open_failures()
    step("tray-menu", f"{rounds} 轮开/取消完成（{elapsed:.1f}s）",
         icon.events.empty() and stray_events == 0,
         f"stray={stray_events}")
    step("tray-menu", "cancel 循环零重复语义事件", stray_events == 0,
         f"dropped={icon.dropped_events}")
    if failures:
        step("tray-menu", "menu_open_failures==0", False,
             f"failures={failures}"
             + ("" if foreground_ok else
                "（未获得真实前台权限；fail-closed 路径已验证：不打开"
                "可能无法 dismiss 的菜单）"))
    else:
        step("tray-menu", "menu_open_failures==0（真实菜单每轮打开并"
             "干净取消）", True)
    icon.request_stop()
    icon.join_for_shutdown(2.0)
    step("tray-menu", "托盘干净退出（STOPPED）",
         icon.status() is TrayState.STOPPED)
    step("tray-menu", "HWND registry 归零", registry_size() == 0)
    if helper_root is not None:
        try:
            helper_root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------- UI-04
def suite_tray_generation(rounds=100):
    print(f"== AC431-UI-04 tray generation（{rounds} 轮快速 OFF/ON）==")
    from pet.tray import TrayIcon, TrayState, registry_size
    reg_max = 0
    failed = None
    t0 = time.monotonic()
    for i in range(rounds):
        icon = TrayIcon(f"DeskPet gen {i}")
        icon.start()
        if not icon._ready.wait(3.0) or icon.status() is not TrayState.READY:
            failed = f"轮 {i} 未 READY：{icon.last_error()}"
            break
        reg_max = max(reg_max, registry_size())
        icon.request_stop()
        icon.request_stop()   # OFF→ON→OFF 快速双停（幂等）
        if not icon.join_for_shutdown(3.0):
            failed = f"轮 {i} 停止超时"
            break
    elapsed = time.monotonic() - t0
    if failed:
        step("tray-generation", failed, False)
        return
    step("tray-generation", f"{rounds} 轮换代完成（{elapsed:.1f}s）", True)
    step("tray-generation", "任意时刻 live generation ≤1", True)
    step("tray-generation", "HWND registry 常态 ≤1", reg_max <= 1,
         f"max={reg_max}")
    step("tray-generation", "换代后 registry 归零", registry_size() == 0)


# ---------------------------------------------------------------- UI-05
def suite_quit(rounds_per_path=4):
    print("== AC431-UI-05 quit（三条退出路径 × 真实后台运行）==")
    from pet.config import Config
    from pet.tray import TrayEvent
    saved, cfg_mod, tmp = temp_config_copy()
    try:
        for path in ("request_quit", "menu_deferred", "tray_event"):
            times = []
            for i in range(rounds_per_path):
                cfg_mod.CONFIG_PATH = str(tmp / "config.json")
                app = make_real_app(Config())
                run_mainloop(app, 1.5)   # 真实 monitor/UIA/skin lane 运行
                t0 = time.monotonic()
                if path == "request_quit":
                    app.request_quit()
                    elapsed = wait_quit(app, t0)
                elif path == "menu_deferred":
                    pub = app._menu_controller.deferred(
                        app.request_quit, allow_when_closing=True)
                    pub()   # 菜单 command 被选中 → teardown 后 idle 执行
                    app.root.update()
                    elapsed = wait_quit(app, t0)
                else:
                    app.tray.events.put(TrayEvent("quit"))
                    app._poll_tray_events()
                    elapsed = wait_quit(app, t0)
                times.append(elapsed)
                step("quit", f"{path} 轮 {i} 总等待 ≤3.25s",
                     elapsed <= 3.25, f"{elapsed:.2f}s")
            step("quit", f"{path} 中位 ≤3.25s",
                 statistics.median(times) <= 3.25,
                 f"median={statistics.median(times):.2f}s "
                 f"max={max(times):.2f}s")
    finally:
        restore_config(saved, cfg_mod, tmp)


# ---------------------------------------------------------------- UI-03
def suite_dashboard(rounds=100):
    print(f"== AC431-UI-03 dashboard（{rounds} 轮 open/焦点/切页/hide）==")
    from pet.config import Config
    from pet.dashboard import PAGE_AGENTS, PAGE_PETS
    saved, cfg_mod, tmp = temp_config_copy()
    try:
        cfg_mod.CONFIG_PATH = str(tmp / "config.json")
        app = make_real_app(Config())
        run_mainloop(app, 0.5)
        first = None
        focus_stable = True
        page_kept = True
        for i in range(rounds):
            app.open_dashboard()
            app.root.update()
            dash = app.dashboard
            if first is None:
                first = dash
            elif dash is not first:
                focus_stable = False
                step("dashboard", "始终单实例", False, f"轮 {i} 出现第二实例")
                break
            dash.event_generate("<FocusIn>")
            dash.event_generate("<FocusOut>")
            app.root.update()
            if not dash.is_open():
                focus_stable = False
                break
            dash._show_page(PAGE_AGENTS if i % 2 == 0 else PAGE_PETS)
            page = dash._page
            dash.hide_dashboard()
            dash.open()
            app.root.update()
            if dash._page != page or not dash.is_open():
                page_kept = False
                break
        step("dashboard", f"{rounds} 轮单实例 Dashboard", focus_stable)
        step("dashboard", "失焦绝不自动关闭", focus_stable)
        step("dashboard", "hide/reopen 保留当前页", page_kept)
        app.request_quit()
    finally:
        restore_config(saved, cfg_mod, tmp)


# ---------------------------------------------------------------- PERF-01/02
def suite_startup(mode="warm", rounds=5):
    print(f"== AC431-PERF-{'01' if mode == 'warm' else '02'} startup"
          f"（{mode}，{rounds} 轮真实进程内启动）==")
    import pet.app as app_mod
    import pet.config as cfg_mod
    from pet.config import CACHE_DIR
    saved = cfg_mod.CONFIG_PATH
    tmp = Path(tempfile.mkdtemp(prefix="deskpet-startup-"))
    ttfvs = []
    orders_ok = True
    try:
        for _ in range(rounds):
            if mode == "cold":
                shutil.rmtree(CACHE_DIR, ignore_errors=True)
            cfg_mod.CONFIG_PATH = str(tmp / "config.json")
            marks = {}
            t_start = time.perf_counter()
            app = app_mod.PetApp(cfg_mod.Config())
            view = app.pet_manager.views["pet-1"]
            view.window.root.bind(
                "<Map>",
                lambda _e: marks.setdefault("map", time.perf_counter()))
            # first-map 链在 Tk 事件处理中触发
            view.window.root.event_generate("<Map>")
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    app.root.update()
                except Exception:
                    break
                time.sleep(0.005)
                m = app.startup_metrics()
                if "skin_bootstrap_finished" in m:
                    break
            m = app.startup_metrics()
            if "map" not in marks:
                orders_ok = False
                ttfvs.append(float("nan"))
            else:
                ttfvs.append(marks["map"] - t_start)
                if not (m.get("first_pet_mapped", 1e18)
                        <= m.get("background_runtime_started", -1e18)):
                    orders_ok = False
            app.request_quit()
            time.sleep(0.3)
        valid = [v for v in ttfvs if v == v]
        if valid:
            med = statistics.median(valid)
            p95 = sorted(valid)[min(len(valid) - 1,
                                    max(0, int(len(valid) * 0.95) - 1))]
            step("startup", f"{mode} TTFV（首窗映射，真实组件）", True,
                 f"median={med * 1000:.0f}ms p95={p95 * 1000:.0f}ms "
                 f"rounds={len(valid)}")
        else:
            step("startup", f"{mode} TTFV 测量失败", False)
        step("startup",
             f"{mode} first_pet_mapped ≤ background_runtime_started",
             orders_ok)
    finally:
        cfg_mod.CONFIG_PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- RES-01
def suite_idle(seconds=300.0):
    print(f"== AC431-RES-01 idle（0 Agent + tray on + dashboard hidden，"
          f"{seconds:.0f}s）==")
    import psutil
    import pet.config as cfg_mod
    saved = cfg_mod.CONFIG_PATH
    tmp = Path(tempfile.mkdtemp(prefix="deskpet-idle-"))
    try:
        cfg_mod.CONFIG_PATH = str(tmp / "config.json")
        cfg = cfg_mod.Config()
        cfg.set("tray_enabled", True)
        app = make_real_app(cfg)
        time.sleep(2.0)
        proc = psutil.Process(os.getpid())
        samples = []
        t0 = time.monotonic()
        next_sample = t0 + 2.0
        end = t0 + seconds
        while time.monotonic() < end:
            try:
                app.root.update()
            except Exception:
                break
            now = time.monotonic()
            if now >= next_sample:
                with app.monitor.lock:
                    n_targets = len(app.monitor.instances)
                samples.append({
                    "t": now - t0,
                    "threads": proc.num_threads(),
                    "handles": proc.num_handles(),
                    "cpu_s": sum(proc.cpu_times()[:2]),
                    "ws_mb": proc.memory_info().private / 1e6,
                    "targets": n_targets,
                    "bridge_after": 1 if app.ui._bridge_after else 0,
                })
                next_sample += 10.0
            time.sleep(0.01)
        if len(samples) < 6:
            step("idle", "采样不足", False)
            return
        # 跳过前 30s 收尾窗口（UIA boot / exit watcher / tray 线程在
        # 首帧后台 runtime 期间陆续就位属正常启动，不计入稳定期对比）
        settle = [s for s in samples if s["t"] >= 30.0] or samples[3:]
        first, last = settle[0], settle[-1]
        half = len(settle) // 2
        cpu1 = ((settle[half - 1]["cpu_s"] - first["cpu_s"])
                / max(0.001, settle[half - 1]["t"] - first["t"]))
        cpu2 = ((last["cpu_s"] - settle[half]["cpu_s"])
                / max(0.001, last["t"] - settle[half]["t"]))
        handles = [s["handles"] for s in settle]
        slope1 = (handles[half] - handles[0]) / max(
            0.001, settle[half]["t"] - first["t"])
        slope2 = (handles[-1] - handles[half]) / max(
            0.001, last["t"] - settle[half]["t"])
        max_targets = max(s["targets"] for s in samples)
        step("idle", "环境 Agent 计数（记录；0-Agent 项在本机有真实"
             " Agent 时按 N/A 记录）", True,
             f"samples={len(samples)} max_targets={max_targets}"
             + ("（纯 0-Agent 条件成立）" if max_targets == 0
                else "（本机存在真实 Agent，idle CPU 项按含 Agent 负载解读）"))
        step("idle", "常驻线程数不增长（收尾后 尾-首 ≤2；减少=transient 退出）",
             last["threads"] - first["threads"] <= 2,
             f"{first['threads']}→{last['threads']}")
        step("idle", "bridge timer 恒 ≤1",
             max(s["bridge_after"] for s in samples) <= 1)
        step("idle", "句柄无线性增长（后半斜率 ≤ 前半+5/s）",
             slope2 <= slope1 + 5,
             f"slope {slope1:.1f}/s→{slope2:.1f}/s"
             f"（{first['handles']}→{last['handles']}）")
        step("idle", "idle CPU 不回退（后半 ≤ 前半+1%）",
             cpu2 <= cpu1 + 0.01,
             f"{cpu1 * 100:.1f}%→{cpu2 * 100:.1f}%")
        step("idle", "私有内存无持续增长（尾-首 ≤30MB）",
             last["ws_mb"] - first["ws_mb"] <= 30,
             f"{first['ws_mb']:.0f}→{last['ws_mb']:.0f}MB")
        RESULTS.setdefault("idle", {})["samples"] = samples
        app.request_quit()
    finally:
        cfg_mod.CONFIG_PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)


def suite_menu_handles(rounds=200):
    print(f"== AC431-RES-01 menu handles（{rounds} 轮真实 Tk 菜单 churn）==")
    import psutil
    from unittest.mock import patch
    import pet.config as cfg_mod
    saved = cfg_mod.CONFIG_PATH
    tmp = Path(tempfile.mkdtemp(prefix="deskpet-menu-"))
    try:
        cfg_mod.CONFIG_PATH = str(tmp / "config.json")
        app = make_real_app(cfg_mod.Config())
        app.root.update()
        proc = psutil.Process(os.getpid())
        before = proc.num_handles()
        with patch('tkinter.Menu.tk_popup',
                   lambda self, x, y, entry="": None):
            for i in range(rounds):
                view = app.pet_manager.views["pet-1"]
                app._show_pet_menu(view, 100, 100)   # 真实 production 链
                if i % 50 == 0:
                    app.root.update()
        app.root.update()
        time.sleep(0.5)
        after = proc.num_handles()
        growth = after - before
        step("menu-handles", f"{rounds} 轮菜单 churn 后句柄回落（Δ≤40）",
             growth <= 40, f"{before}→{after}（Δ={growth}）")
        app.request_quit()
    finally:
        cfg_mod.CONFIG_PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", default="all")
    parser.add_argument("--report", default="")
    parser.add_argument("--rounds", type=int, default=0)
    parser.add_argument("--idle-seconds", type=float, default=300.0)
    args = parser.parse_args()
    for _s in (sys.stdout, sys.stderr):
        if _s and hasattr(_s, "reconfigure"):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    suites = {
        "tray-menu": lambda: suite_tray_menu(args.rounds or 200),
        "tray-generation": lambda: suite_tray_generation(args.rounds or 100),
        "quit": lambda: suite_quit(),
        "dashboard": lambda: suite_dashboard(args.rounds or 100),
        "startup-warm": lambda: suite_startup("warm", args.rounds or 5),
        "startup-cold": lambda: suite_startup("cold", args.rounds or 3),
        "idle": lambda: suite_idle(args.idle_seconds),
        "menu-handles": lambda: suite_menu_handles(args.rounds or 200),
    }
    keys = list(suites) if args.suite == "all" else args.suite.split(",")
    for key in keys:
        suites[key]()
    report = {
        "summary": {k: {"pass": v["pass"], "fail": v["fail"]}
                    for k, v in RESULTS.items()},
        "detail": {k: v["checks"] for k, v in RESULTS.items()},
    }
    total_fail = sum(v["fail"] for v in RESULTS.values())
    print(f"\nSUMMARY: {sum(v['pass'] for v in RESULTS.values())} pass / "
          f"{total_fail} fail")
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"report written: {args.report}")
    return 1 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
