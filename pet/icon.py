# -*- coding: utf-8 -*-
"""程序化托盘图标：原创几何小猫（V4.1.5）。

版权立场（与 .gitignore"素材版权保护"一致）：

  * 托盘图标**绝不使用桌宠形象/皮肤素材**——皮肤是用户自备的版权
    素材，只放在本机 ``assets/pets/``（不入库）；
  * 图标完全由代码绘制（椭圆 + 三角），零素材依赖，仓库保持零二进制；
  * ``assets/icon.ico`` 仍是本机生成产物（gitignored）：启动时缺文件
    自动生成，内容哈希与当前绘制不一致自动重写——旧版"从皮肤 GIF
    裁帧"生成的桌宠图标会在下一次启动被无感替换成小猫；
  * PIL 属于 core 依赖（requirements-core: Pillow>=10）；任何失败都
    只返回 None，托盘回退系统默认图标，绝不影响启动。
"""
from __future__ import annotations

import hashlib
import io
import os

# 绘制内容变更时 +1：内容哈希天然不同，旧 icon.ico 会被自动重写
ICON_REVISION = 1
CAT_SIZES = (16, 24, 32, 48, 64, 128, 256)

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


def cat_ico_bytes() -> bytes:
    """多尺寸 ICO（16–256 每档原生绘制，保证 16px 任务栏清晰）。"""
    from PIL import Image

    frames = [draw_cat(s) for s in CAT_SIZES]
    buf = io.BytesIO()
    frames[-1].save(buf, format="ICO", append_images=frames[:-1],
                    sizes=[(s, s) for s in CAT_SIZES])
    return buf.getvalue()


def icon_ico_path(root_dir: str | None = None) -> str:
    base = root_dir or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "icon.ico")


def ensure_icon_ico(root_dir: str | None = None) -> str | None:
    """确保 assets/icon.ico 存在且等于当前小猫绘制。

    * 缺文件 → 生成；内容哈希不一致（如旧版皮肤裁帧图标）→ 原子重写；
    * 任何异常（无 PIL / 目录不可写）→ 返回 None，调用方回退默认图标。
    """
    try:
        path = icon_ico_path(root_dir)
        data = cat_ico_bytes()
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
