"""一次性生成 assets/icon.ico（从 walk.gif 中间帧裁方缩到 64px）。"""
import os
from PIL import Image
root = r"D:\mycode\program\deskpet"
gif = os.path.join(root, "assets", "cache", "default@240", "walk.gif")
im = Image.open(gif)
im.seek(0)
rgba = im.convert("RGBA")
w, h = rgba.size
side = min(w, h)
rgba = rgba.crop(((w - side) // 2, (h - side) // 2,
                  (w + side) // 2, (h + side) // 2)).resize((64, 64), Image.LANCZOS)
out = os.path.join(root, "assets", "icon.ico")
rgba.save(out, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
print("saved", out, os.path.getsize(out), "bytes")
