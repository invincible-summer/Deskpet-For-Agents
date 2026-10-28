# -*- coding: utf-8 -*-
"""Shared original app artwork for the dashboard, window and system tray.

Bundled assets are immutable program resources. The tray keeps its existing
LocalAppData ICO cache, replacing an older icon only when its content differs.
The geometric ``draw_cat`` below belongs to the built-in skin and is independent
of the application identity; user-supplied skin art is never used as an icon.
"""
from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path

from .runtime_paths import get_runtime_paths

ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def icon_asset_path(name: str = "deskpet.png") -> Path:
    """Resolve the same immutable artwork in source and compiled installs."""
    return get_runtime_paths().program_root / "assets" / "brand" / name


def icon_image(size: int):
    """Load a small RGBA copy; callers own their Tk image references."""
    from PIL import Image

    size = max(8, min(256, int(size)))
    with Image.open(icon_asset_path()) as source:
        return source.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)


@lru_cache(maxsize=1)
def icon_ico_bytes() -> bytes:
    """Use the prebuilt multi-size ICO; the GUI never encodes icon frames."""
    return icon_asset_path("deskpet.ico").read_bytes()


_HEAD = (62, 76, 86, 255)        # 蓝灰猫头
_EAR_INNER = (216, 150, 162, 255)  # 粉色内耳
_EYE = (255, 255, 255, 255)
_PUPIL = (34, 40, 46, 255)
_NOSE = (217, 124, 133, 255)
_WHISKER = (196, 208, 214, 255)


def draw_cat(size: int):
    """按指定像素尺寸绘制小猫头像（4x 超采样抗锯齿）。"""
    from PIL import Image, ImageDraw

    ss = 4
    big = max(size, 8) * ss
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def pt(x: float, y: float):
        return (x * big, y * big)

    # 头：偏下的圆
    d.ellipse([pt(.10, .18), pt(.90, .98)], fill=_HEAD)
    # 耳朵：两只三角立在头顶两侧（左耳尖向左上、右耳镜像）
    left_ear = [pt(.17, .35), pt(.37, .20), pt(.18, .04)]
    right_ear = [pt(.83, .35), pt(.63, .20), pt(.82, .04)]
    d.polygon(left_ear, fill=_HEAD)
    d.polygon(right_ear, fill=_HEAD)
    # 内耳：向质心收缩的小三角
    def shrink(tri, k=0.55):
        cx = sum(p[0] for p in tri) / 3
        cy = sum(p[1] for p in tri) / 3
        return [(cx + (p[0] - cx) * k, cy + (p[1] - cy) * k) for p in tri]

    d.polygon(shrink(left_ear), fill=_EAR_INNER)
    d.polygon(shrink(right_ear), fill=_EAR_INNER)
    # 眼睛：白椭圆 + 深瞳孔
    for cx in (.365, .635):
        d.ellipse([pt(cx - .070, .505), pt(cx + .070, .640)], fill=_EYE)
        d.ellipse([pt(cx - .032, .545), pt(cx + .032, .615)], fill=_PUPIL)
    # 鼻子：小三角
    d.polygon([pt(.455, .685), pt(.545, .685), pt(.500, .745)], fill=_NOSE)
    # 胡须：只在足够大的尺寸画（小尺寸会糊成一团）
    if size >= 48:
        lw = max(2, big // 96)
        for side in (-1, 1):
            for i, (y0, y1) in enumerate(((.66, .63), (.72, .71), (.78, .79))):
                x0 = .500 + side * .155
                x1 = .500 + side * (.385 - i * .025)
                d.line([pt(x0, y0), pt(x1, y1)], fill=_WHISKER, width=lw)
    return img.resize((size, size), Image.LANCZOS)


def icon_ico_path(root_dir: str | None = None) -> str:
    if root_dir is not None:
        # Explicit root is retained for deterministic unit/build tooling.
        return os.path.join(root_dir, "assets", "icon.ico")
    return str(get_runtime_paths().runtime_icon)


def ensure_icon_ico(root_dir: str | None = None) -> str | None:
    """Ensure the tray cache contains the current bundled app artwork.

    Missing/stale files are replaced atomically. An inaccessible cache returns
    None so the caller can use its shared system icon fallback.
    """
    try:
        path = icon_ico_path(root_dir)
        data = icon_ico_bytes()
        digest = hashlib.sha256(data).digest()
        if os.path.isfile(path):
            with open(path, "rb") as f:
                if hashlib.sha256(f.read()).digest() == digest:
                    return path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return path
    except Exception:
        return None
