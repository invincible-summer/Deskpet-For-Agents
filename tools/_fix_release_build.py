from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Nuitka names the standalone directory from the source module (main.dist), even
# when --output-filename changes the executable. Normalize it after compilation.
p = ROOT / "tools" / "build_release.py"
text = p.read_text(encoding="utf-8")
old = '''    run(cmd)
    if not (DIST / "DeskPet.exe").is_file():
        raise SystemExit(f"Nuitka output missing: {DIST / 'DeskPet.exe'}")

    for name in ("README.md", "THIRD_PARTY_NOTICES.md"):
'''
new = '''    run(cmd)
    candidates = sorted(p for p in WORK.glob("*.dist") if p.is_dir())
    if DIST not in candidates:
        if len(candidates) != 1:
            raise SystemExit(f"Unexpected Nuitka dist directories: {candidates}")
        candidates[0].replace(DIST)
    if not (DIST / "DeskPet.exe").is_file():
        raise SystemExit(f"Nuitka output missing: {DIST / 'DeskPet.exe'}")

    for name in ("README.md", "THIRD_PARTY_NOTICES.md"):
'''
if old not in text:
    raise RuntimeError("build dist block not found")
text = text.replace(old, new, 1)

# The first real Windows build showed that forcing LTO spends most of the build
# in a whole-program link (~19 minutes for this dependency set). The release
# contract does not depend on LTO and we have no measured runtime benefit yet,
# so make the build deterministic and maintainable with LTO disabled.
old = '        "--lto=yes",\n'
new = '        "--lto=no",\n'
if old not in text:
    raise RuntimeError("build LTO option not found")
text = text.replace(old, new, 1)
p.write_text(text, encoding="utf-8", newline="\n")

# Release-content checks must be scoped to paths *inside* the distribution.
# The staging parent is intentionally .release/, so examining absolute path
# parts would reject every valid artifact before it can be packaged.
p = ROOT / "tools" / "release_acceptance.py"
text = p.read_text(encoding="utf-8")
old = '''    for path in dist.rglob("*"):
        if path.name in banned_names:
            raise SystemExit(f"mutable user data leaked into release: {path}")
        lowered = {p.lower() for p in path.parts}
        if "tests" in lowered or ".release" in lowered:
            raise SystemExit(f"development-only path leaked into release: {path}")
'''
new = '''    for path in dist.rglob("*"):
        if path.name in banned_names:
            raise SystemExit(f"mutable user data leaked into release: {path}")
        relative = path.relative_to(dist)
        lowered = {part.lower() for part in relative.parts}
        if "tests" in lowered or ".release" in lowered:
            raise SystemExit(f"development-only path leaked into release: {relative}")
'''
if old not in text:
    raise RuntimeError("release relative-path validation block not found")
text = text.replace(old, new, 1)

# Validate the actual bundled FFmpeg binary, not merely package metadata.
needle = '''    if conv.returncode != 1 or "usage:" not in (conv.stdout + conv.stderr):
        raise SystemExit("internal converter dispatch smoke failed")

    print("release acceptance: OK")
'''
insert = '''    if conv.returncode != 1 or "usage:" not in (conv.stdout + conv.stderr):
        raise SystemExit("internal converter dispatch smoke failed")

    ffmpeg = sorted(dist.rglob("*ffmpeg*.exe"))
    if not ffmpeg:
        raise SystemExit("bundled FFmpeg executable missing")
    ff = ffmpeg[0]
    license_run = subprocess.run([str(ff), "-L"], capture_output=True,
                                 text=True, timeout=20, check=True)
    conf_run = subprocess.run([str(ff), "-buildconf"], capture_output=True,
                              text=True, timeout=20, check=True)
    ffmpeg_report = (f"binary: {ff.relative_to(dist)}\\n\\n"
                     + license_run.stdout + license_run.stderr
                     + "\\n\\n" + conf_run.stdout + conf_run.stderr)
    if "--enable-nonfree" in ffmpeg_report.lower():
        raise SystemExit("bundled FFmpeg uses --enable-nonfree and cannot be released")
    (dist.parent / "ffmpeg-build.txt").write_text(
        ffmpeg_report, encoding="utf-8", errors="replace")

    print("release acceptance: OK")
'''
if needle not in text:
    raise RuntimeError("release acceptance insertion point not found")
text = text.replace(needle, insert, 1)
p.write_text(text, encoding="utf-8", newline="\n")

# Keep the actual FFmpeg audit in release diagnostics.
p = ROOT / ".github" / "workflows" / "release.yml"
text = p.read_text(encoding="utf-8")
old = '''            .release/nuitka-report.xml
            benchmark-report.json
'''
new = '''            .release/nuitka-report.xml
            .release/ffmpeg-build.txt
            benchmark-report.json
'''
if old not in text:
    raise RuntimeError("release diagnostics block not found")
text = text.replace(old, new, 1)
p.write_text(text, encoding="utf-8", newline="\n")

print("release build hardening applied")
