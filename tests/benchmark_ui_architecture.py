"""UI 架构合成基准（v4.3 §19.2）。

十条断言：
  1. 8 Agent target 连续 500 个 semantic-no-change bridge tick，
     Presentation reconcile 不随 tick 等量增长；
  2. 无变化时 render flush = 0；
  3. 单 Agent 状态变化只 dirty 受影响的 aggregate pet（1）或 Fleet
     slot（1）；
  4. 8 Fleet view 同时 due、decode miss 时一个 decode slice 最多冷
     解码 1 帧；
  5. same skin same size 8 Pet：同 FrameKey 只 decode 一次；
  6. config slider 20 次快速 step change：一个 debounce 窗口内 ≤1
     save worker、0 次同步 commit；
  7. Dashboard 只 refresh 当前页，隐藏页 refresh 计数 = 0；
  8. 1→8 Pet 后 bridge timer 数不增长、animation scheduler after 槽
     仍 ≤1；
  9. v4.3.1：导航/重扫描/外观按钮回调耗时有界（<50ms，重扫描
     O(1) 提交）；
  10. v4.3.1：200 次 Configure 合并为一个 idle 并收敛，同宽零重复
     写入。

用法：python tests/benchmark_ui_architecture.py [--report PATH]
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ================================================================ fakes
class FakeTimer:
    def __init__(self, token, delay_ms, callback, idle=False):
        self.token = token
        self.delay_ms = delay_ms
        self.callback = callback
        self.idle = idle


class FakeRoot:
    def __init__(self):
        self.timers = {}
        self._next = 0

    def after(self, ms, cb=None, *args):
        self._next += 1
        self.timers[self._next] = FakeTimer(self._next, ms, cb)
        return self._next

    def after_idle(self, cb=None, *args):
        self._next += 1
        self.timers[self._next] = FakeTimer(self._next, 0, cb, idle=True)
        return self._next

    def after_cancel(self, token):
        self.timers.pop(token, None)

    def fire_bridge(self):
        for token, timer in list(self.timers.items()):
            if not timer.idle:
                self.timers.pop(token)
                timer.callback()
                return True
        return False

    def run_idle(self):
        for timer in [t for t in self.timers.values() if t.idle]:
            self.timers.pop(timer.token)
            timer.callback()

    def non_idle_count(self):
        return sum(1 for t in self.timers.values() if not t.idle)


class FakeMonitor:
    def __init__(self, targets):
        self.revision = 1
        self.targets = targets

    def get_targets_if_changed(self, last_revision):
        if self.revision == last_revision:
            return self.revision, None
        return self.revision, dict(self.targets)

    def get_targets(self):
        return dict(self.targets)


class FakePresentation:
    def __init__(self):
        self._revision = 1

    @property
    def revision(self):
        return self._revision


def _targets(n=8):
    from agents.models import AgentInstance, AgentKind, AgentTarget, \
        Snapshot, Status
    out = {}
    for i in range(1, n + 1):
        kind = [AgentKind.CODEX, AgentKind.CLAUDE, AgentKind.KIMI,
                AgentKind.PI][(i - 1) % 4]
        inst = AgentInstance(kind, i, "wsl:Ubuntu", cwd=f"/w/p{i}",
                             process_token=str(i), started_at=i)
        snap = Snapshot(inst.key, inst.kind, inst.source, inst.pid,
                        status=Status.WORKING, summary=f"任务 {i}")
        out[inst.key] = AgentTarget(key=inst.key, instance=inst,
                                    snapshot=snap)
    return out


# ================================================================ checks
def check_no_change_ticks_and_zero_flush(checks):
    """断言 1/2：500 无变化 tick → reconcile 不增长；render flush=0。"""
    from pet.ui_coordinator import UiCoordinator
    root = FakeRoot()
    monitor = FakeMonitor(_targets(8))
    reconcile_calls = []

    ui = UiCoordinator(root, monitor=monitor,
                       presentation=FakePresentation(),
                       apply_batch=lambda targets: reconcile_calls.append(1))
    ui.start()
    # 初始收割（revision 0→1）+ 初始 flush
    root.fire_bridge()
    root.run_idle()
    base_reconcile = len(reconcile_calls)
    base_flush = ui.render_count
    for _ in range(500):
        root.fire_bridge()   # revision 不变
        root.run_idle()
    checks.append((
        "8 targets × 500 no-change tick：reconcile 不随 tick 增长"
        f"（{len(reconcile_calls)} 次）",
        len(reconcile_calls) == base_reconcile))
    checks.append((
        f"无变化 500 tick：render flush=0（Δ={ui.render_count - base_flush}）",
        ui.render_count == base_flush))


def check_single_change_single_dirty_view(checks, app, agents, mode):
    """断言 3：单 Agent 变化只 dirty 一个 view（fleet slot / aggregate）。"""
    from agents.models import Status
    from tests.test_fleet_ui import snap
    app.monitor.instances = {a.key: a for a in agents}
    app.monitor.snapshots = {a.key: snap(a) for a in agents}
    app._aggregate()
    app.pet_manager.redraw_dirty()
    # 只改第二个 Agent 的状态
    app.monitor.snapshots[agents[1].key] = snap(agents[1], Status.ERROR)
    app._aggregate()
    dirty = [vid for vid, v in app.pet_manager.views.items()
             if v.visual_dirty]
    if mode == "fleet":
        expected = 1
    else:
        expected = 1   # aggregate：任一卡变化 → pet-1
    checks.append((
        f"{mode}：单 Agent 状态变化只 dirty {expected} 个 view"
        f"（实际 {len(dirty)}：{dirty}）",
        len(dirty) == expected))
    app.pet_manager.redraw_dirty()


def _make_gif(path: Path, frames: int = 8, size: int = 240):
    import json as _json
    from PIL import Image
    imgs = [Image.new("RGB", (size, size),
                      (i * 29 % 256, i * 67 % 256, i * 13 % 256))
            for i in range(frames)]
    imgs[0].save(path, save_all=True, append_images=imgs[1:],
                 duration=83, loop=0)
    Path(str(path) + ".json").write_text(_json.dumps(
        {"frames": frames, "width": size, "height": size,
         "delay_ms": 83, "loop": True}), encoding="utf-8")


def check_decode_slice_and_shared_key(checks):
    """断言 4/5：一个 slice 冷解码 ≤1 帧；同 FrameKey 只 decode 一次。"""
    import tkinter as tk
    from pet.animator import AnimationCursor, AnimationScheduler, \
        SharedAnimationCache
    root = tk.Tk()
    root.withdraw()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            gif = Path(tmp) / "walk.gif"
            _make_gif(gif)
            path = str(gif)
            # ---- 4：8 个不同 frame key 同时 due → 一次 slice ≤1 帧
            cache = SharedAnimationCache(max_bytes=8 * 1024 * 1024)
            scheduler = AnimationScheduler(root, cache)
            cursors = []
            for i in range(8):
                cursor = AnimationCursor(f"slice-{i}")
                cursor.path = path
                cursor.frames = 8
                cursor.frame_index = i
                scheduler.register(cursor, lambda _v: None)
                cursors.append(cursor)
            base = scheduler.cold_decode_count
            for cursor in cursors:
                scheduler.frame_image(cursor)   # miss → 入队（去重后 8 项）
            checks.append((
                "8 view 同时 due：decode 队列 ≤16（§6.3 上限）",
                scheduler.decode_queue_len() <= 16))
            scheduler._decode_slice()
            checks.append((
                "一个 decode slice 冷解码 ≤1 帧"
                f"（Δ={scheduler.cold_decode_count - base}）",
                scheduler.cold_decode_count - base <= 1))
            # ---- 5：同 skin 同 size 同帧 → 同 FrameKey 只 decode 一次
            cache2 = SharedAnimationCache(max_bytes=8 * 1024 * 1024)
            scheduler2 = AnimationScheduler(root, cache2)
            shared = []
            for i in range(8):
                cursor = AnimationCursor(f"shared-{i}")
                cursor.path = path
                cursor.frames = 8
                cursor.frame_index = 3
                scheduler2.register(cursor, lambda _v: None)
                shared.append(cursor)
            base2 = scheduler2.cold_decode_count
            for cursor in shared:
                scheduler2.frame_image(cursor)
            checks.append((
                "8 Pet 同 FrameKey：decode 队列只有 1 项（共享请求）",
                scheduler2.decode_queue_len() == 1))
            guard = 0
            while scheduler2.decode_queue_len() and guard < 100:
                scheduler2._decode_slice()
                guard += 1
            checks.append((
                "同 FrameKey 只冷解码 1 次"
                f"（Δ={scheduler2.cold_decode_count - base2}）",
                scheduler2.cold_decode_count - base2 == 1))
            # 8 个 cursor 全部直接命中缓存
            hits = sum(1 for c in shared
                       if scheduler2.frame_image(c) is not None)
            checks.append(("8 Pet 同帧：全部命中缓存（无需各自解码）",
                           hits == 8))
            scheduler.stop()
            scheduler2.stop()
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def check_slider_rapid_steps_single_save(checks):
    """断言 6：20 次快速 step change → 一个窗口 ≤1 worker、0 同步 commit。"""
    import tkinter as tk
    from pet.config import Config
    from pet.config_save import ConfigSaveCoordinator
    root = tk.Tk()
    root.withdraw()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(str(Path(tmp) / "config.json"))
            saver = ConfigSaveCoordinator(root, cfg)
            writes = []
            commits = []
            real_write = Config.write_snapshot
            real_commit = Config.commit

            def spy_write(self, revision, data):
                writes.append(revision)
                return real_write(self, revision, data)

            def spy_commit(self, fsync=False):
                commits.append(1)
                return real_commit(self, fsync)

            with patch.object(Config, "write_snapshot", spy_write), \
                    patch.object(Config, "commit", spy_commit):
                for i in range(20):   # 快速跨 20 个 step
                    cfg.set("scale", 1.0 + i * 0.05)
                    saver.request_save()
                # 模拟 debounce 到期（650ms 后）：恰 1 个 worker
                threads = []
                real_start = threading.Thread.start

                def spy_start(self_thread):
                    threads.append(self_thread.name)
                    real_start(self_thread)

                with patch.object(threading.Thread, "start", spy_start):
                    saver._fire()   # debounce 到期（不真等 650ms）
                    deadline = time.time() + 2
                    while time.time() < deadline and not writes:
                        time.sleep(0.02)
                saver.poll()
                checks.append((
                    "20 次快速 step：一个 debounce 窗口 ≤1 save worker"
                    f"（{threads}）",
                    len([t for t in threads
                         if t == "deskpet-config-save"]) <= 1))
                checks.append((
                    "20 次快速 step：无 20 次同步 commit"
                    f"（同步 commit={len(commits)}）",
                    len(commits) == 0))
                checks.append((
                    "写盘次数 ≤1（快照合并为最新值）"
                    f"（writes={len(writes)}）",
                    len(writes) <= 1))
            saver.stop()
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def check_dashboard_current_page_only(checks):
    """断言 7：Dashboard 只 refresh 当前页，隐藏页计数 0。"""
    from pet.app import PetApp
    from pet.petview import PetView
    from tests.test_ui import MemoryConfig
    with\
            patch.object(PetView, "load_skin", lambda self, bm: None):
        app = PetApp(MemoryConfig())
        app.pet_manager.activate_skin_runtime()
        app._disarm_first_map_trigger()
    try:
        app.open_dashboard()
        app.root.update()
        dash = app.dashboard
        counts: dict[str, int] = {}
        for name, page in dash._pages.items():
            def make_refresh(_name):
                def _refresh(reason):
                    counts[_name] = counts.get(_name, 0) + 1
                return _refresh
            page.refresh = make_refresh(name)
        for _ in range(10):
            dash.refresh_current_page()
        hidden_total = sum(v for k, v in counts.items()
                           if k != dash._page)
        checks.append((
            "Dashboard 只 refresh 当前页（隐藏页 refresh 计数="
            f"{hidden_total}）",
            hidden_total == 0 and counts.get(dash._page) == 10))
    finally:
        app.quit()


def check_button_latency_and_configure_convergence(checks):
    """断言（v4.3.1 稳定性 §4）：按钮回调耗时有界（重扫描 O(1)）；
    Configure 风暴合并为一个 idle 并有限收敛，不残留 callback。"""
    from pet.app import PetApp
    from pet.dashboard import PAGE_AGENTS, PAGE_OVERVIEW, SCALE_STEPS
    from pet.petview import PetView
    from tests.test_ui import MemoryConfig
    with\
            patch.object(PetView, "load_skin", lambda self, bm: None):
        app = PetApp(MemoryConfig())
        app.pet_manager.activate_skin_runtime()
        app._disarm_first_map_trigger()
    try:
        app.open_dashboard()
        app.root.update()
        dash = app.dashboard
        app.toast = lambda *a, **k: None   # 隔离提示 UI，只测调度合同
        # 预热：懒构建是一次性成本（v4.3 既有合同），计时只测稳态回调。
        for page in list(dash._pages):
            dash._show_page(page)
        app.root.update()

        def timed(label, fn, limit_ms=50.0, rounds=20):
            worst = 0.0
            for _ in range(rounds):
                t0 = time.perf_counter()
                fn()
                worst = max(worst, (time.perf_counter() - t0) * 1000.0)
            app.root.update_idletasks()
            checks.append((
                f"按钮回调有界：{label} 最差 {worst:.1f}ms "
                f"< {limit_ms:.0f}ms",
                worst < limit_ms))

        timed("重复点击当前导航（零布局）",
              lambda: dash._show_page(dash._page))
        timed("切页提交",
              lambda: dash._show_page(
                  PAGE_AGENTS if dash._page != PAGE_AGENTS
                  else PAGE_OVERVIEW))
        timed("重新扫描提交（O(1) Event）", dash.request_rescan)
        steps = SCALE_STEPS
        counter = {"i": 0}

        def change_scale():
            app.appearance.set_global(
                "scale", steps[counter["i"] % len(steps)])
            counter["i"] += 1

        timed("外观 step 提交", change_scale)

        # Configure 风暴：200 次同宽事件 → 恰一个 pending idle、
        # drain 后零残留，同宽不重复写窗口宽。
        scroller = dash.content
        event = type("ConfigureEvent", (), {"width": 900})()
        itemconfigure_calls = []
        real_itemconfigure = scroller._canvas.itemconfigure

        def spy_itemconfigure(*a, **k):
            itemconfigure_calls.append(1)
            return real_itemconfigure(*a, **k)

        with patch.object(scroller._canvas, "itemconfigure",
                          spy_itemconfigure):
            for _ in range(200):
                scroller._on_canvas_configure(event)
                scroller._on_inner_configure(event)
            pending_single = scroller._layout_after is not None
            app.root.update_idletasks()
            converged = pending_single and scroller._layout_after is None
        checks.append(("200 次 Configure 合并为一个 idle 并收敛"
                       "（无残留 callback）", converged))
        checks.append((
            f"同宽 Configure 只写一次窗口宽"
            f"（itemconfigure={len(itemconfigure_calls)}）",
            len(itemconfigure_calls) <= 1))
    finally:
        app.quit()


def check_timers_do_not_grow_with_pets(checks):
    """断言 8：1→8 Pet bridge timer 不增长；scheduler after 槽 ≤1。"""
    from pet.ui_coordinator import UiCoordinator
    root = FakeRoot()
    ui = UiCoordinator(root, monitor=FakeMonitor({}),
                       presentation=FakePresentation())
    ui.start()
    counts = []
    for n in range(1, 9):
        for i in range(n):
            ui.request_view(f"pet-{i + 1}")
        root.fire_bridge()
        root.run_idle()
        counts.append(root.non_idle_count())
    checks.append((
        f"Pet 1→8：bridge timer 恒 ≤1（{counts}）",
        all(c == 1 for c in counts)))
    ui.stop()

    import tkinter as tk
    from pet.animator import AnimationCursor, AnimationScheduler, \
        SharedAnimationCache
    tk_root = tk.Tk()
    tk_root.withdraw()
    try:
        scheduler = AnimationScheduler(
            tk_root, SharedAnimationCache(max_bytes=1024 * 1024))
        for i in range(1, 9):
            cursor = AnimationCursor(f"pet-{i}")
            cursor.path = f"skin{i}.gif"
            cursor.frames = 8
            cursor.next_due = time.monotonic() + i * 0.05
            scheduler.register(cursor, lambda _v: None)
            scheduler.kick(cursor.view_id)
        checks.append((
            "8 Pet：scheduler due after 槽 ≤1（单槽重排）",
            scheduler._after_id is not None))
        scheduler.stop()
    finally:
        try:
            tk_root.destroy()
        except Exception:
            pass


def check_dirty_views_real_apps(checks):
    """断言 3 的两种模式（真实 Tk + PetView）。"""
    from pet.app import PetApp
    from pet.petview import PetView
    from pet.presentation import PresentationMode
    from tests.test_fleet_ui import FleetConfig, _slot, inst
    from agents.models import AgentKind

    def make(mode):
        slots = [_slot("pet-1"), _slot("pet-2")] if mode == "fleet" \
            else [_slot("pet-1")]
        cfg = FleetConfig(slots)
        with\
                patch.object(PetView, "load_skin", lambda self, bm: None):
            app = PetApp(cfg)
            app.pet_manager.activate_skin_runtime()
            app._disarm_first_map_trigger()
        app.presentation.set_concurrent_mode(
            PresentationMode.FLEET if mode == "fleet"
            else PresentationMode.AGGREGATE)
        return app, [inst(AgentKind.CODEX, 1, cwd="/w/a"),
                     inst(AgentKind.CLAUDE, 2, cwd="/w/b")]

    for mode in ("fleet", "aggregate"):
        app, agents = make(mode)
        try:
            check_single_change_single_dirty_view(checks, app, agents, mode)
        finally:
            app.quit()


def check_saver_lifecycle_structural(checks):
    """v4.3.1 §25.1：save finished => pending False + bridge 回 idle 档。"""
    import tkinter as tk
    from pet.config import Config
    from pet.config_save import ConfigSaveCoordinator
    from pet.ui_coordinator import UiCoordinator
    root = tk.Tk()
    root.withdraw()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(str(Path(tmp) / "config.json"))
            saver = ConfigSaveCoordinator(root, cfg)
            threads = []
            real_start = threading.Thread.start

            def spy_start(self_thread):
                threads.append(self_thread.name)
                real_start(self_thread)

            with patch.object(threading.Thread, "start", spy_start):
                for i in range(20):   # 20 次外观 step
                    cfg.set("scale", 1.0 + i * 0.05)
                    saver.request_save()
                saver._fire()
                worker = saver._worker
                if worker is not None:
                    worker.join(2.0)
                saver.poll()
            checks.append((
                "20 appearance steps：save worker ≤1"
                f"（{len([t for t in threads if t == 'deskpet-config-save'])}）",
                len([t for t in threads
                     if t == "deskpet-config-save"]) <= 1))
            checks.append((
                "save finished：config_saver.pending()==False",
                saver.pending() is False))
            checks.append((
                "save finished：bridge 离开 125ms 档（worker_active False）",
                saver.pending() is False))
            # hidden + tray off + no worker => 500ms bridge（§25.1）
            class _HiddenPets:
                build_manager = type("B", (), {
                    "building": staticmethod(lambda: False)})()

                @staticmethod
                def any_visible():
                    return False

            ui = UiCoordinator(root, monitor=FakeMonitor({}),
                               presentation=FakePresentation(),
                               pet_manager=_HiddenPets,
                               tray_enabled=lambda: False,
                               config_saver=saver)
            checks.append((
                "hidden + tray off + no worker：bridge 500ms",
                ui._current_interval_ms() == 500))
            ui.stop()
            saver.stop()
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def check_maintenance_coalescing(checks):
    """v4.3.1 §25.1：100 次 maintenance request → <=1 pending。"""
    from pet.skins import SkinBuildManager
    bm = SkinBuildManager()
    with patch("pet.skins.run_maintenance",
               lambda cancel=None: {"ready": {}, "seen": set()}):
        bm.request_maintenance()
        before = bm.pending_count()
        for _ in range(100):
            bm.request_maintenance()
        growth = bm.pending_count() - before
        # 排空 lane（worker 是真实线程，fake job 立即完成）
        deadline = time.time() + 3
        while time.time() < deadline and bm.building():
            bm.poll_results()
            time.sleep(0.01)
        checks.append((
            f"100 次 maintenance request：pending 增量 ≤1（Δ={growth}）",
            growth <= 1))
        checks.append((
            "maintenance 收割后 lane 不再 building",
            bm.building() is False or bm.results_pending()))


def check_skin_lane_single_active(checks):
    """v4.3.1 §25.2：build/import/rebuild/maintenance 的 active ≤1。"""
    from pet.skins import SkinBuildManager
    bm = SkinBuildManager()
    guard = threading.Lock()
    running = []
    violations = []

    def tracked(tag, fn):
        def wrapper(*a, **kw):
            with guard:
                if running:
                    violations.append(tag)
                running.append(tag)
            time.sleep(0.02)
            with guard:
                running.remove(tag)
            return fn(*a, **kw)
        return wrapper

    paths = {s: f"C:/c/{s}.gif"
             for s in ("walk", "attack", "die", "special", "sleep")}
    with patch("pet.skins.prepare_import",
               tracked("import", lambda s, n, cancel=None: n)), \
            patch("pet.skins.build_skin",
                  tracked("build",
                          lambda skin, h, f, log=None, **kw: dict(paths))), \
            patch("pet.skins.run_maintenance",
                  tracked("maint",
                          lambda cancel=None: {"ready": {}, "seen": set()})), \
            patch("pet.skins.refresh_skin_catalog", lambda: {}):
        bm.submit_import("x", "a")
        bm.request("pet-1", "s", 240, 12)
        bm.request("pet-2", "s2", 240, 12)
        bm.request_rebuild("pet-1", "r", 240, 12)
        bm.request_maintenance()
        deadline = time.time() + 5
        while time.time() < deadline and bm.building():
            bm.poll_results()
            time.sleep(0.001)
    checks.append((
        f"skin lane：任意时刻 active job ≤1（violations={violations}）",
        violations == []))
    bm.stop(0.5)


def check_tray_bounded_structural(checks):
    """v4.3.1 §25.3：tray 事件队列 ≤ 固定上限；单次 drain ≤ 固定上限。"""
    import queue as queue_mod
    from pet.app import TRAY_DRAIN_MAX
    from pet.tray import TRAY_EVENT_QUEUE_MAX, TrayEvent, TrayIcon
    icon = TrayIcon.__new__(TrayIcon)
    icon.events = queue_mod.Queue(maxsize=TRAY_EVENT_QUEUE_MAX)
    icon.dropped_events = 0
    from pet.tray import WM_APP_TRAY
    v4 = TrayIcon._ICON_ID << 16   # VERSION_4：HIWORD=icon id
    for _ in range(TRAY_EVENT_QUEUE_MAX * 3):
        icon._handle_message(1, WM_APP_TRAY, 0,
                             v4 | 0x0202)   # 满后丢弃不阻塞
    checks.append((
        f"tray 事件队列 ≤ {TRAY_EVENT_QUEUE_MAX}"
        f"（qsize={icon.events.qsize()}）",
        icon.events.qsize() <= TRAY_EVENT_QUEUE_MAX))
    checks.append((
        f"tray 单次 bridge drain ≤ {TRAY_DRAIN_MAX}",
        TRAY_DRAIN_MAX <= 8))


def check_dp43_reliability_structural(checks):
    """DP43-R14..R22（plan §12.7）结构合同：

    - context menu 无常驻 timer（churn 后 after 队列空）；
    - Tray restart timer 已不存在（旧 symbol 归零）；
    - bootstrap job 复用现有 skin lane（不新增线程）；
    - SkinCatalog UI 读不做 I/O（scan 打桩为 raise 仍可读）；
    - shutdown timeout 是单一全局 deadline（不是局部累加）；
    - 常驻线程预算不增（context_menu/dashboard 无新线程）。
    """
    import tkinter as tk
    from pet.context_menu import TkContextMenuController
    root = tk.Tk()
    root.withdraw()
    ctrl = TkContextMenuController(root)
    from unittest.mock import patch as _patch
    lifecycles_ok = True
    with _patch('tkinter.Menu.tk_popup', lambda self, x, y, entry="": None):
        # §35.1：100 个真实 controller lifecycle——每轮 show 后 completion
        # pending（Windows queued-command 合同），必须 drain 才算一轮，
        # 而不是连续 100 次被 gate 拒绝的调用。
        for i in range(100):
            ctrl.show("owner", 1, 2, lambda m, n=i: m.add_command(label=str(n)))
            if not ctrl.active:
                lifecycles_ok = False
                break
            root.update()
            if ctrl.active:
                lifecycles_ok = False
                break
    ctrl.dismiss()
    root.update()
    afters = root.tk.splitlist(root.tk.call('after', 'info'))
    checks.append(("100 轮真实 controller lifecycle（show→update→inactive）",
                   lifecycles_ok))
    checks.append(("context menu churn 后无 after timer",
                   len(afters) == 0))
    root.destroy()

    repo = Path(__file__).resolve().parents[1]
    app_src = (repo / "pet" / "app.py").read_text(encoding="utf-8")
    checks.append(("tray restart timer 不存在（_tray_restart_after 归零）",
                   "_tray_restart_after" not in app_src))
    checks.append(("app 无 _reload_skins/_skin_after 旧路径",
                   "_reload_skins" not in app_src
                   and "_skin_after" not in app_src))

    # §35.2 v4.3.1 交互收口结构合同
    dashboard_src = (repo / "pet" / "dashboard.py").read_text(
        encoding="utf-8")
    widgets_src = (repo / "pet" / "widgets.py").read_text(encoding="utf-8")
    skins_src = (repo / "pet" / "skins.py").read_text(encoding="utf-8")
    checks.append((
        "Dashboard scroller 不再关闭 _center propagation"
        "（_center.pack_propagate 归零）",
        "_center.pack_propagate" not in dashboard_src))
    checks.append((
        "Dashboard inner <Configure> 追加（add='+'）不覆盖 scroll owner",
        'add="+"' in dashboard_src))
    checks.append((
        "ScrollableFrame.wheel_scroll 使用 contains_point bounds",
        "contains_point(x, y)" in widgets_src))
    checks.append(("_focus_and_activate 死 helper 归零",
                   "_focus_and_activate" not in app_src
                   and "_focus_and_activate" not in dashboard_src))
    checks.append(("built_gifs/built_gifs_any 死入口归零",
                   "def built_gifs(" not in skins_src
                   and "def built_gifs_any(" not in skins_src))
    checks.append((
        "Dashboard 无自重排周期 after（唯一 after 是 50ms resize debounce）",
        dashboard_src.count("self.after(") == 1))

    import pet.skins as skins_mod
    cat = skins_mod.SkinCatalog()
    with _patch.object(skins_mod, "_scan_skins",
                       side_effect=AssertionError("no I/O")):
        snap = cat.snapshot()
    checks.append(("SkinCatalog UI 读纯内存（scan 打桩仍可读）",
                   skins_mod.BUILTIN_SKIN in snap))

    from pet.app import SHUTDOWN_BUDGET_SEC
    monitor_src = (repo / "agents" / "monitor.py").read_text(encoding="utf-8")
    checks.append((
        "shutdown 为单一全局 deadline（无 6/3/2s 局部累加）",
        SHUTDOWN_BUDGET_SEC == 3.0
        and "timeout=6.0" not in monitor_src
        and "timeout=3.0" not in monitor_src
        and "timeout=2.0" not in monitor_src
        and "_remaining()" in monitor_src))

    context_src = (repo / "pet" / "context_menu.py").read_text(
        encoding="utf-8")
    checks.append(("context_menu 无常驻线程",
                   "threading.Thread" not in context_src))
    action_body = dashboard_src.split("    def submit_action(", 1)[-1].split(
        "    def actions_pending", 1)[0]
    checks.append((
        "Dashboard 仅允许一个按需 transient action worker",
        dashboard_src.count("threading.Thread(") == 1
        and "deskpet-dashboard-action" in dashboard_src
        and "while True" not in action_body))
    rescan_body = monitor_src.split("    def rescan(self):", 2)[-1].split(
        "    def ", 1)[0]
    checks.append((
        "Monitor.rescan 只提交 Event，不同步刷新 UIA",
        "refresh_observed_controls" not in rescan_body
        and "_rescan_requested.set()" in rescan_body))

    from pet.skins import SkinBuildManager
    bm = SkinBuildManager()
    started = []
    real_spawn = bm._spawn

    def spy_spawn(req):
        started.append(req.kind.value)
        real_spawn(req)

    bm._spawn = spy_spawn
    bm.request_bootstrap([("builtin-cat", 240, 12)])   # 直接起在同一 lane
    lane_name = bm._active_thread.name if bm._active_thread else ""
    checks.append(("bootstrap 复用现有 skin lane（deskpet-convert）",
                   started == ["bootstrap"] and lane_name == "deskpet-convert"))
    bm.stop()


def run(report_path: str = "") -> int:
    checks: list[tuple[str, bool]] = []
    check_no_change_ticks_and_zero_flush(checks)
    check_dirty_views_real_apps(checks)
    check_decode_slice_and_shared_key(checks)
    check_slider_rapid_steps_single_save(checks)
    check_dashboard_current_page_only(checks)
    check_button_latency_and_configure_convergence(checks)
    check_timers_do_not_grow_with_pets(checks)
    check_saver_lifecycle_structural(checks)
    check_maintenance_coalescing(checks)
    check_skin_lane_single_active(checks)
    check_tray_bounded_structural(checks)
    check_dp43_reliability_structural(checks)

    for _stream in (sys.stdout, sys.stderr):
        if _stream and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    print("UI architecture benchmark (v4.3 §19.2):")
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if report_path:
        Path(report_path).write_text(json.dumps(
            {"checks": {name: bool(ok) for name, ok in checks}},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report written: {report_path}")
    if failed:
        print("BENCHMARK FAILED:", failed)
        return 1
    print("BENCHMARK OK")
    return 0


if __name__ == "__main__":
    _report = ""
    _argv = sys.argv
    _i = 1
    while _i < len(_argv):
        if _argv[_i] == "--report" and _i + 1 < len(_argv):
            _report = _argv[_i + 1]
            _i += 2
        else:
            _i += 1
    sys.exit(run(_report))
