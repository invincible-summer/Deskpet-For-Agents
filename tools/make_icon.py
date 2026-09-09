# -*- coding: utf-8 -*-
"""命令行生成 assets/icon.ico：程序化小猫（V4.1.5，非桌宠形象）。

托盘启动时会自动生成/自愈该文件，此脚本仅供手动重建：

  python tools/make_icon.py
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pet.icon import ensure_icon_ico, icon_ico_path  # noqa: E402

if __name__ == "__main__":
    path = ensure_icon_ico()
    if path:
        size = Path(path).stat().st_size
        print(f"OK {path} ({size} bytes)")
    else:
        print(f"FAILED to write {icon_ico_path()}")
        sys.exit(1)
