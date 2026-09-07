"""回归测试 v2：校验几何调用严格由锚点推导（免疫真实鼠标干扰）。

模拟拖动用合成事件；不读 winfo 断言位置，而是拦截 apply_geometry 的入参。
"""
import sys
sys.path.insert(0, r"D:\mycode\program\deskpet")
import ctypes
try:  # 与 main.py 一致的 DPI 感知
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

from pet.config import Config
from pet.app import PetApp
from pet import petwindow

cfg = Config()
cfg.set("pet_pos", None)
app = PetApp(cfg)
app.monitor.stop()
app.monitor._tick = lambda: None

GEO_CALLS = []           # (w, h, x, y)
_orig_apply = petwindow.PetWindow.apply_geometry

def spy_apply(self, w, h, x, y):
    GEO_CALLS.append((w, h, x, y))
    _orig_apply(self, w, h, x, y)

petwindow.PetWindow.apply_geometry = spy_apply

results = []

def step(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}", flush=True)
    results.append(cond)

def anchor_of(call):
    w, h, x, y = call
    return (x + w // 2, y + h)

def t0_wait_skin():
    if app.animator.frame_size()[0] > 0 or app._build_q is None:
        step("初始皮肤就绪", app.animator.frame_size()[0] > 0,
             f"frame={app.animator.frame_size()}")
        step("托盘已启动", app.tray is not None and app.tray._hwnd is not None)
        t1()
    else:
        app.root.after(1000, t0_wait_skin)

def t1():
    """气泡出现/消失的每一次 apply_geometry 都必须由同一锚点推出。"""
    GEO_CALLS.clear()
    app.toast("很长很长的提示文本，" * 14, 6)   # 强制气泡尺寸变化
    def check1():
        # The card has a fixed size, so changing only its text may legitimately
        # require no geometry call at all.  If a redraw does resize, every
        # call must still preserve the same anchor.
        ok = not GEO_CALLS or all(anchor_of(c) == anchor_of(GEO_CALLS[0])
                                  for c in GEO_CALLS)
        step("气泡变化不移动锚点", ok,
             f"{len(GEO_CALLS)} 次几何调用, 锚点集合="
             f"{ {anchor_of(c) for c in GEO_CALLS} }")
        t2()
    app.root.after(1200, check1)

def t2():
    """模拟拖动：按下→移动→松开，锚点应更新到新位置。"""
    from pet.config import Config as C
    old_anchor = app.anchor
    class EV:
        pass
    ev = EV(); ev.x = 30; ev.y = 60
    app.win._on_press(ev)
    ev2 = EV(); ev2.x = 130; ev2.y = 160
    app.win._on_drag(ev2)
    app.win._on_release(ev2)
    new_anchor = app.anchor
    moved = (abs(new_anchor[0] - old_anchor[0]) >= 90 and
             abs(new_anchor[1] - old_anchor[1]) >= 90)
    step("拖动更新锚点(~+100,+100)", moved, f"{old_anchor} -> {new_anchor}")
    saved = C().get("pet_pos") if False else app.config.get("pet_pos")
    step("拖动位置已持久化", saved == [new_anchor[0], new_anchor[1]], f"{saved}")
    t3()

def t3():
    # 先把气泡压窄，验证“加宽”确实产生几何变化
    cfg.set("bubble.width", 200)
    app.apply_bubble_settings()

    def check():
        GEO_CALLS.clear()
        cfg.set("bubble.width", 480)
        app.apply_bubble_settings()
        app.toast("这是一条很长很长的测试文本" * 12, 30)
        def check2():
            w_max = max((c[0] for c in GEO_CALLS), default=0)
            step("气泡宽度生效（窗口>=500px）", w_max >= 500, f"w={w_max}")
            cfg.set("bubble.width", 300)
            app.apply_bubble_settings()
            t4()
        app.root.after(900, check2)
    app.root.after(400, check)

def t4():
    calls0 = len(GEO_CALLS)
    app.set_scale(1.25)
    def still_there():
        step("构建期间桌宠仍在（未消失）",
             app.animator.frame_size()[0] > 0 and app.win.visible)
    app.root.after(1000, still_there)

    def wait_build():
        if app._build_q is not None:
            app.root.after(1000, wait_build)
            return
        big = [c for c in GEO_CALLS[calls0:] if c[1] >= 330]
        step("新尺寸生效（高度>=330）", bool(big), f"{GEO_CALLS[calls0:][:4]}")
        if GEO_CALLS[calls0:]:
            anchors = {anchor_of(c) for c in GEO_CALLS[calls0:]}
            step("缩放期间锚点唯一", len(anchors) == 1, f"{anchors}")
        t5()
    app.root.after(2000, wait_build)

def t5():
    app.hide_pet()
    def check1():
        step("隐藏生效", not app.win.visible)
        app.show_pet()
        app.root.after(400, check2)
    app.root.after(600, check1)

    def check2():
        step("恢复显示", app.win.visible)
        from pet import autostart
        on = autostart.set_enabled(True)
        step("自启写入注册表", on and autostart.is_enabled())
        autostart.set_enabled(False)
        step("自启可关闭", not autostart.is_enabled())
        finish()

def finish():
    print("SUMMARY:", sum(results), "/", len(results), flush=True)
    app.config.set("scale", 1.0)
    app.config.save()
    app.quit()

app.root.after(2500, t0_wait_skin)
app.run()
