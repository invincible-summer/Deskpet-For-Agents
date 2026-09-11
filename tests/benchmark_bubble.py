"""Self-contained retained bubble-render benchmark.

Runs without screenshots, human input, or pre-existing test artifacts.
"""
from __future__ import annotations

import json
import sys
import time
import tkinter as tk
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pet.bubble import BubbleModel, BubbleRenderer
from tests.test_ui import MemoryConfig


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".test-artifacts"


def main() -> None:
    from main import _dpi_aware

    _dpi_aware()
    root = tk.Tk()
    root.withdraw()
    canvas = tk.Canvas(root, width=960, height=650)
    canvas.pack()
    renderer = BubbleRenderer(canvas, MemoryConfig())
    renderer.model = BubbleModel(
        visible=True,
        status="Codex · 工作中",
        text="正在验证气泡 retained render 是否稳定且不积累 canvas items。",
        footer="打开终端",
        agent_key="benchmark-agent",
    )

    renderer.layout()
    renderer.draw(10, 10, 160, 160)
    root.update_idletasks()
    item_count_before = len(canvas.find_all())
    rss_before = psutil.Process().memory_info().rss

    rounds = 500
    started = time.perf_counter()
    for _ in range(rounds):
        renderer.layout()
        renderer.draw(10, 10, 160, 160)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    root.update_idletasks()

    item_count_after = len(canvas.find_all())
    rss_after = psutil.Process().memory_info().rss
    result = {
        "rounds": rounds,
        "render_ms": round(elapsed_ms, 2),
        "canvas_items_before": item_count_before,
        "canvas_items_after": item_count_after,
        "rss_delta_kb": round((rss_after - rss_before) / 1024, 2),
    }
    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / "bubble-benchmark.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8")
    root.destroy()

    stable = item_count_after == item_count_before
    print(json.dumps(result, ensure_ascii=False))
    print("  [PASS] 500 次同状态 render 不积累 canvas items"
          if stable else
          "  [FAIL] 500 次同状态 render 积累了 canvas items")
    if not stable:
        raise SystemExit(1)
    print("BENCHMARK OK")


if __name__ == "__main__":
    main()
