"""Build DeskPet Windows x64 portable distribution with Nuitka standalone."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pet.version import APP_NAME, APP_VERSION
from pet.icon import ensure_icon_ico

WORK = ROOT / ".release"
DIST = WORK / "DeskPet.dist"
ZIP_PATH = WORK / "DeskPet-windows-x64-portable.zip"
SUMS = WORK / "SHA256SUMS.txt"
REPORT = WORK / "nuitka-report.xml"


def run(cmd: list[str], **kwargs) -> None:
    print("+", subprocess.list2cmdline(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT, **kwargs)


def build() -> None:
    if sys.platform != "win32":
        raise SystemExit("Portable build must run on Windows")
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Portable build requires Python 3.12")
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    icon_root = WORK / "icon-root"
    icon = ensure_icon_ico(str(icon_root))
    if not icon:
        raise SystemExit("Failed to generate DeskPet build icon")

    cmd = [
        sys.executable, "-m", "nuitka",
        "--mode=standalone",
        "--assume-yes-for-downloads",
        "--windows-console-mode=disable",
        "--enable-plugin=tk-inter",
        "--deployment",
        "--lto=no",
        f"--output-dir={WORK}",
        "--output-filename=DeskPet.exe",
        f"--report={REPORT}",
        f"--windows-icon-from-ico={icon}",
        f"--product-name={APP_NAME}",
        f"--file-description={APP_NAME}",
        f"--product-version={APP_VERSION}",
        f"--file-version={APP_VERSION}",
        "--include-package=pet",
        "--include-package=agents",
        "--include-package=actions",
        "--include-module=tools.convert",
        "--include-package-data=imageio_ffmpeg",
        str(ROOT / "main.py"),
    ]
    run(cmd)
    candidates = sorted(p for p in WORK.glob("*.dist") if p.is_dir())
    if DIST not in candidates:
        if len(candidates) != 1:
            raise SystemExit(f"Unexpected Nuitka dist directories: {candidates}")
        candidates[0].replace(DIST)
    if not (DIST / "DeskPet.exe").is_file():
        raise SystemExit(f"Nuitka output missing: {DIST / 'DeskPet.exe'}")

    for name in ("README.md", "THIRD_PARTY_NOTICES.md"):
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, DIST / name)

    run([sys.executable, str(ROOT / "tools" / "release_acceptance.py"),
         "--dist", str(DIST)])

    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        for path in sorted(DIST.rglob("*")):
            if path.is_file():
                zf.write(path, Path("DeskPet") / path.relative_to(DIST))
    digest = hashlib.sha256(ZIP_PATH.read_bytes()).hexdigest()
    SUMS.write_text(f"{digest}  {ZIP_PATH.name}\n", encoding="ascii")
    print(f"OK {ZIP_PATH}")
    print(f"OK {SUMS}")


if __name__ == "__main__":
    build()
