"""Static and executable acceptance for a built DeskPet standalone dist."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pet.version import APP_VERSION


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", required=True)
    ns = ap.parse_args(argv)
    dist = Path(ns.dist).resolve()
    exe = dist / "DeskPet.exe"
    if not exe.is_file():
        raise SystemExit("DeskPet.exe missing")

    banned_names = {"config.json", "config.json.bak", ".venv"}
    for path in dist.rglob("*"):
        if path.name in banned_names:
            raise SystemExit(f"mutable user data leaked into release: {path}")
        relative = path.relative_to(dist)
        lowered = {part.lower() for part in relative.parts}
        if "tests" in lowered or ".release" in lowered:
            raise SystemExit(f"development-only path leaked into release: {relative}")

    out = subprocess.run([str(exe), "--version"], capture_output=True,
                         text=True, timeout=20, check=True)
    if out.stdout.strip() != APP_VERSION:
        raise SystemExit(
            f"version mismatch: exe={out.stdout.strip()!r} source={APP_VERSION!r}")

    # Missing converter args must exit quickly through the internal worker path;
    # if dispatch happens after the GUI mutex/Tk boundary this command would hang.
    conv = subprocess.run([str(exe), "--deskpet-internal-converter"],
                          capture_output=True, text=True, timeout=20)
    if conv.returncode != 1 or "usage:" not in (conv.stdout + conv.stderr):
        raise SystemExit("internal converter dispatch smoke failed")

    ffmpeg = sorted(dist.rglob("*ffmpeg*.exe"))
    if not ffmpeg:
        raise SystemExit("bundled FFmpeg executable missing")
    ff = ffmpeg[0]
    license_run = subprocess.run([str(ff), "-L"], capture_output=True,
                                 text=True, timeout=20, check=True)
    conf_run = subprocess.run([str(ff), "-buildconf"], capture_output=True,
                              text=True, timeout=20, check=True)
    ffmpeg_report = (f"binary: {ff.relative_to(dist)}\n\n"
                     + license_run.stdout + license_run.stderr
                     + "\n\n" + conf_run.stdout + conf_run.stderr)
    if "--enable-nonfree" in ffmpeg_report.lower():
        raise SystemExit("bundled FFmpeg uses --enable-nonfree and cannot be released")
    (dist.parent / "ffmpeg-build.txt").write_text(
        ffmpeg_report, encoding="utf-8", errors="replace")

    print("release acceptance: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
