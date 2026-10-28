# -*- coding: utf-8 -*-
"""Refresh the LocalAppData tray cache from the bundled original app icon.

The tray refreshes the same cache automatically; this is a manual repair tool:

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
