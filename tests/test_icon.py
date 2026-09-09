# -*- coding: utf-8 -*-
"""程序化小猫托盘图标（V4.1.5）。

版权立场：图标全部由代码绘制（pet/icon.py），不读取任何皮肤/桌宠
素材；仓库保持零二进制，assets/icon.ico 只是本机生成产物。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pet.icon import CAT_SIZES, cat_ico_bytes, draw_cat, ensure_icon_ico


class CatIconTests(unittest.TestCase):
    def test_draw_cat_structure_has_ears_not_just_a_circle(self):
        """结构签名：两耳之间（头顶上方）透明、耳尖处有内容——
        这是"带耳朵的猫"，不是随便一个圆。"""
        for s in (16, 64, 256):
            img = draw_cat(s)
            self.assertEqual(img.size, (s, s))
            self.assertEqual(img.mode, "RGBA")
            alpha = img.getchannel("A")
            self.assertIsNotNone(alpha.getbbox())      # 有实心内容
            mid_top = alpha.getpixel((s // 2, max(0, int(s * 0.06))))
            self.assertLess(mid_top, 16)            # 两耳之间接近全透明
            left_ear = alpha.getpixel((int(s * 0.19), int(s * 0.10)))
            right_ear = alpha.getpixel((int(s * 0.81), int(s * 0.10)))
            self.assertGreater(left_ear, 200)       # 耳尖实心
            self.assertGreater(right_ear, 200)

    def test_draw_cat_deterministic(self):
        self.assertEqual(bytes(draw_cat(32).tobytes()),
                         bytes(draw_cat(32).tobytes()))

    def test_ico_contains_all_native_sizes(self):
        data = cat_ico_bytes()
        self.assertEqual(data[:4], b"\x00\x00\x01\x00")   # ICONDIR type=icon
        count = int.from_bytes(data[4:6], "little")
        self.assertEqual(count, len(CAT_SIZES))
        widths = set()
        for i in range(count):
            off = 6 + i * 16
            widths.add(data[off] or 256)   # 0 表示 256
        self.assertEqual(widths, set(CAT_SIZES))
        self.assertGreater(len(data), 4000)

    def test_ensure_icon_writes_replaces_stale_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "assets", "icon.ico")
            out = ensure_icon_ico(td)
            self.assertEqual(out, path)
            with open(path, "rb") as f:
                first = f.read()
            self.assertEqual(first, cat_ico_bytes())
            # 内容一致：不重写（mtime 不变）
            mtime = os.stat(path).st_mtime_ns
            ensure_icon_ico(td)
            self.assertEqual(os.stat(path).st_mtime_ns, mtime)
            # 旧图标（如皮肤裁帧版）被自动替换回小猫
            with open(path, "wb") as f:
                f.write(b"stale-pet-face-icon")
            ensure_icon_ico(td)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), first)

    def test_tray_load_icon_uses_cat_not_default(self):
        """tray._load_icon 经 ensure_icon_ico 拿到小猫 HICON（PIL 是 core
        依赖，正常环境必然生成成功），而非 IDI_APPLICATION 回退。"""
        import pet.tray as tray
        icon = tray.TrayIcon("DeskPet")
        hicon = 0
        try:
            hicon = icon._load_icon()
            self.assertTrue(hicon)
            self.assertNotEqual(hicon, tray.user32.LoadIconW(None, 32512))
        finally:
            if hicon:
                tray.user32.DestroyIcon(hicon)


if __name__ == "__main__":
    unittest.main()
