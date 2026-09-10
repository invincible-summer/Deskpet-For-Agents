"""同机基线 TTFV 探针：PetApp() 构造 → 首个 Pet 窗口 <Map> 的耗时。

对任意版本（e0ed22f 基线 / 当前修复版）使用同一外部测量方法，保证
TTFV 对比公平。在 worktree 根目录运行。
"""
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def one_round(tmp_cfg: str) -> float:
    import pet.config as cfg_mod
    cfg_mod.CONFIG_PATH = tmp_cfg
    from pet.app import PetApp
    t0 = time.perf_counter()
    app = PetApp(cfg_mod.Config())
    view = app.pet_manager.views["pet-1"]
    mapped = []
    view.window.root.bind(
        "<Map>", lambda _e: mapped.append(time.perf_counter()))
    view.window.root.event_generate("<Map>")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not mapped:
        try:
            app.root.update()
        except Exception:
            break
        time.sleep(0.002)
    ttfv = (mapped[0] - t0) if mapped else float("nan")
    try:
        app.quit()
    except Exception:
        pass
    time.sleep(0.4)
    return ttfv


def main(rounds=5):
    import pet.config as cfg_mod
    tmp = Path(tempfile.mkdtemp(prefix="deskpet-ttfv-"))
    src = Path(cfg_mod.CONFIG_PATH)
    dst = tmp / "config.json"
    if src.is_file():
        shutil.copy2(src, dst)
    else:
        dst.write_text("{}", encoding="utf-8")
    saved = cfg_mod.CONFIG_PATH
    vals = []
    try:
        for _ in range(rounds):
            vals.append(one_round(str(dst)))
    finally:
        cfg_mod.CONFIG_PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)
    valid = [v for v in vals if v == v]
    print(json.dumps({
        "rounds": len(valid),
        "median_ms": round(statistics.median(valid) * 1000, 1),
        "p95_ms": round(sorted(valid)[min(len(valid) - 1,
                                          int(len(valid) * 0.95))] * 1000, 1),
        "min_ms": round(min(valid) * 1000, 1),
        "max_ms": round(max(valid) * 1000, 1),
    }))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
