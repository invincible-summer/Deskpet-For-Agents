# -*- coding: utf-8 -*-
"""Original bundled app icon, tray cache and native HICON ownership."""
from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pet.icon import ICON_SIZES, icon_ico_bytes, icon_image, ensure_icon_ico


class AppIconTests(unittest.TestCase):
    def test_bundled_artwork_scales_with_transparency(self):
        for s in (16, 64, 256):
            img = icon_image(s)
            self.assertEqual(img.size, (s, s))
            self.assertEqual(img.mode, "RGBA")
            alpha = img.getchannel("A")
            self.assertIsNotNone(alpha.getbbox())
            self.assertLess(alpha.getpixel((0, 0)), 16)
            self.assertGreater(alpha.getpixel((s // 2, s // 2)), 200)

    def test_ico_contains_all_native_sizes(self):
        data = icon_ico_bytes()
        self.assertEqual(data[:4], b"\x00\x00\x01\x00")   # ICONDIR type=icon
        count = int.from_bytes(data[4:6], "little")
        self.assertEqual(count, len(ICON_SIZES))
        widths = set()
        for i in range(count):
            off = 6 + i * 16
            widths.add(data[off] or 256)   # 0 表示 256
        self.assertEqual(widths, set(ICON_SIZES))
        self.assertGreater(len(data), 4000)

    def test_ensure_icon_writes_replaces_stale_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "assets", "icon.ico")
            out = ensure_icon_ico(td)
            self.assertEqual(out, path)
            with open(path, "rb") as f:
                first = f.read()
            self.assertEqual(first, icon_ico_bytes())
            # 内容一致：不重写（mtime 不变）
            mtime = os.stat(path).st_mtime_ns
            ensure_icon_ico(td)
            self.assertEqual(os.stat(path).st_mtime_ns, mtime)
            # 旧图标被自动替换成当前应用图标
            with open(path, "wb") as f:
                f.write(b"stale-pet-face-icon")
            ensure_icon_ico(td)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), first)

    def test_tray_load_icon_uses_bundled_artwork_not_default(self):
        """The tray loads the shared app artwork rather than IDI_APPLICATION.
        DP43-R15：返回 (hicon, owns)——文件 icon 归本进程所有，测试销毁
        后必须复位 ownership，避免 TrayIcon teardown 重复 Destroy。"""
        import pet.tray as tray
        icon = tray.TrayIcon("DeskPet")
        hicon = 0
        try:
            hicon, owns = icon._load_icon()
            self.assertTrue(hicon)
            self.assertTrue(owns)
            default = tray.user32.LoadIconW(
                None, ctypes.cast(32512, ctypes.c_wchar_p))
            self.assertNotEqual(hicon, default)
        finally:
            if hicon:
                icon._hicon = hicon
                icon._owns_hicon = True
                icon._destroy_owned_hicon()


if __name__ == "__main__":
    unittest.main()
