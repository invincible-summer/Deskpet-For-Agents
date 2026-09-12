from __future__ import annotations

from pathlib import Path
import textwrap

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    p = ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one match, got {count}: {old[:80]!r}")
    write(path, text.replace(old, new, 1))


# ---------------------------------------------------------------------------
# 1. Central runtime paths: program files are immutable; user data is LocalAppData.
write("pet/runtime_paths.py", r'''"""DeskPet runtime path boundary.

Program files and mutable user data are intentionally separated:

* program_root: source checkout / compiled distribution directory;
* data_root: ``%LOCALAPPDATA%\\DeskPet``;
* config, imported skins, generated cache and runtime icon live under data_root.

This module is a leaf: stdlib/Win32 only, no Tk/Pillow/config/skins imports.
"""
from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    program_root: Path
    data_root: Path
    config_file: Path
    config_backup: Path
    assets_dir: Path
    pets_dir: Path
    cache_dir: Path
    runtime_icon: Path


@dataclass(frozen=True)
class OpenFolderResult:
    ok: bool
    path: str
    error: str = ""


def is_compiled() -> bool:
    """Return whether this process is a frozen/compiled distribution."""
    return bool(getattr(sys, "frozen", False) or globals().get("__compiled__"))


def _program_root() -> Path:
    if is_compiled():
        containing = globals().get("__compiled__")
        containing_dir = getattr(containing, "containing_dir", None)
        if containing_dir:
            return Path(str(containing_dir)).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _known_local_app_data() -> Path:
    """Resolve FOLDERID_LocalAppData using SHGetKnownFolderPath.

    LOCALAPPDATA is a defensive fallback only. We deliberately never fall back
    to the program directory: a portable binary directory is not user data.
    """
    if os.name == "nt":
        try:
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            # FOLDERID_LocalAppData = F1B32785-6FBA-4FCF-9D55-7B8E7F157091
            folder_id = GUID(
                0xF1B32785, 0x6FBA, 0x4FCF,
                (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91),
            )
            shell32 = ctypes.windll.shell32
            ole32 = ctypes.windll.ole32
            path_ptr = ctypes.c_wchar_p()
            shell32.SHGetKnownFolderPath.argtypes = [
                ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                ctypes.POINTER(ctypes.c_wchar_p),
            ]
            shell32.SHGetKnownFolderPath.restype = ctypes.c_long
            hr = shell32.SHGetKnownFolderPath(
                ctypes.byref(folder_id), 0, None, ctypes.byref(path_ptr))
            if hr == 0 and path_ptr.value:
                value = Path(path_ptr.value)
                ole32.CoTaskMemFree(path_ptr)
                return value
            if path_ptr:
                ole32.CoTaskMemFree(path_ptr)
        except Exception:
            pass
    env = os.environ.get("LOCALAPPDATA", "").strip()
    if env:
        return Path(env)
    raise RuntimeError("无法解析 Windows LocalAppData；未使用程序目录作为兜底")


def _resolve() -> RuntimePaths:
    program_root = _program_root()
    data_root = _known_local_app_data() / "DeskPet"
    assets = data_root / "assets"
    return RuntimePaths(
        program_root=program_root,
        data_root=data_root,
        config_file=data_root / "config.json",
        config_backup=data_root / "config.json.bak",
        assets_dir=assets,
        pets_dir=assets / "pets",
        cache_dir=assets / "cache",
        runtime_icon=data_root / "icon.ico",
    )


_PATHS: RuntimePaths | None = None


def get_runtime_paths() -> RuntimePaths:
    global _PATHS
    if _PATHS is None:
        _PATHS = _resolve()
    return _PATHS


def ensure_data_root() -> Path:
    path = get_runtime_paths().data_root
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_asset_dirs() -> RuntimePaths:
    paths = get_runtime_paths()
    paths.pets_dir.mkdir(parents=True, exist_ok=True)
    paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return paths


def open_data_root() -> OpenFolderResult:
    """Create and open the DeskPet data directory with Windows ShellExecuteW."""
    try:
        path = ensure_data_root()
        if os.name != "nt":
            return OpenFolderResult(False, str(path), "仅 Windows 支持打开数据目录")
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        code = shell32.ShellExecuteW(None, "open", str(path), None, None, 1)
        value = int(code or 0)
        if value <= 32:
            return OpenFolderResult(False, str(path), f"ShellExecuteW 返回 {value}")
        return OpenFolderResult(True, str(path))
    except Exception as exc:
        path = ""
        try:
            path = str(get_runtime_paths().data_root)
        except Exception:
            pass
        return OpenFolderResult(False, path, str(exc))
''')

# ---------------------------------------------------------------------------
# 2. Config path boundary + first-write directory creation.
replace_once(
    "pet/config.py",
    "  * 继续使用项目内路径（config.json / assets/pets / assets/cache），\n    不迁 %LOCALAPPDATA%；\n",
    "  * 用户可变数据统一位于 %LOCALAPPDATA%\\DeskPet；程序目录不写配置/素材/cache；\n",
)
replace_once(
    "pet/config.py",
    "from dataclasses import dataclass\n\nROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\nCONFIG_PATH = os.path.join(ROOT, \"config.json\")\nBACKUP_PATH = os.path.join(ROOT, \"config.json.bak\")\nASSETS_DIR = os.path.join(ROOT, \"assets\")\nPETS_DIR = os.path.join(ASSETS_DIR, \"pets\")\nCACHE_DIR = os.path.join(ASSETS_DIR, \"cache\")\n",
    "from dataclasses import dataclass\n\nfrom .runtime_paths import get_runtime_paths\n\n_RUNTIME_PATHS = get_runtime_paths()\n# Compatibility exports: existing tests/tools may import these names, but their\n# single source of truth is RuntimePaths rather than the repository root.\nROOT = str(_RUNTIME_PATHS.program_root)\nCONFIG_PATH = str(_RUNTIME_PATHS.config_file)\nBACKUP_PATH = str(_RUNTIME_PATHS.config_backup)\nASSETS_DIR = str(_RUNTIME_PATHS.assets_dir)\nPETS_DIR = str(_RUNTIME_PATHS.pets_dir)\nCACHE_DIR = str(_RUNTIME_PATHS.cache_dir)\n",
)
replace_once(
    "pet/config.py",
    "        temporary = None\n        try:\n            fd, temporary = tempfile.mkstemp(\n",
    "        temporary = None\n        try:\n            # Fresh portable install has no DeskPet directory yet. Create it\n            # inside the worker immediately before the first atomic save.\n            os.makedirs(directory, exist_ok=True)\n            fd, temporary = tempfile.mkstemp(\n",
)

# ---------------------------------------------------------------------------
# 3. Skins use RuntimePaths directly; frozen converter re-enters DeskPet.exe.
replace_once(
    "pet/skins.py",
    "注意：不要在本模块顶层 import tools.convert（会连带加载 PIL）。\n转换全部走独立子进程 python -m tools.convert，主进程保持低内存。\n",
    "注意：不要在本模块顶层 import tools.convert（会连带加载 PIL）。\n转换全部走独立 converter 子进程；source 使用 python -m tools.convert，\ncompiled 使用 DeskPet.exe 的 internal converter 入口，主进程保持低内存。\n",
)
replace_once(
    "pet/skins.py",
    "# 必须定义在 `from .config import ...` 之前：pet.config 反向引用本常量\n# 构造 DEFAULTS，导入顺序保证两个方向都无循环失败。\nBUILTIN_SKIN = \"builtin-cat\"\n",
    "# pet.config 反向引用本常量构造 DEFAULTS；skins 自身只依赖\n# runtime_paths，不再从 config 取得文件系统路径，因此没有路径循环。\nBUILTIN_SKIN = \"builtin-cat\"\n",
)
replace_once(
    "pet/skins.py",
    "from .config import CACHE_DIR, PETS_DIR  # noqa: E402\n",
    "from .runtime_paths import get_runtime_paths, is_compiled  # noqa: E402\n\n_RUNTIME_PATHS = get_runtime_paths()\nPETS_DIR = str(_RUNTIME_PATHS.pets_dir)\nCACHE_DIR = str(_RUNTIME_PATHS.cache_dir)\n",
)
old_converter = '''        exe = sys.executable
        # pythonw 没有 stdout，子进程里 print 会崩，优先用 python.exe
        if os.path.splitext(exe)[0].endswith("pythonw"):
            sibling = os.path.join(os.path.dirname(exe), "python.exe")
            if os.path.isfile(sibling):
                exe = sibling
        cmd = [exe, "-m", "tools.convert", "--gated",
               src, out_dir, str(height), str(fps)]
        cwd = os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))
'''
new_converter = '''        exe = sys.executable
        if is_compiled():
            # Nuitka standalone: sys.executable is DeskPet.exe. Re-enter the
            # same executable through the early internal-converter dispatch.
            cmd = [exe, "--deskpet-internal-converter", "--gated",
                   src, out_dir, str(height), str(fps)]
            cwd = str(_RUNTIME_PATHS.program_root)
        else:
            # pythonw has no stdout; converter diagnostics need python.exe.
            if os.path.splitext(exe)[0].endswith("pythonw"):
                sibling = os.path.join(os.path.dirname(exe), "python.exe")
                if os.path.isfile(sibling):
                    exe = sibling
            cmd = [exe, "-m", "tools.convert", "--gated",
                   src, out_dir, str(height), str(fps)]
            cwd = str(_RUNTIME_PATHS.program_root)
'''
replace_once("pet/skins.py", old_converter, new_converter)
replace_once(
    "pet/skins.py",
    '        """执行 python -m tools.convert --gated；成功返回 True。"""\n',
    '        """执行受控 converter 子进程；成功返回 True。"""\n',
)

# ---------------------------------------------------------------------------
# 4. Converter explicit argv API, preserving the one-byte gate.
old_convert_tail = '''def _selftest() -> None:
    """CLI 入口：python -m tools.convert [--gated] <skin_dir> <out_dir> [height] [fps]
    在独立子进程中执行转换，numpy/scipy 内存随进程退出释放。

    --gated（v4.3.1 DP43-R05）：启动后先阻塞读 stdin 的 1 字节 gate
    再做任何真实工作。父进程先 AssignProcessToJobObject（Windows Job
    Object，KILL_ON_JOB_CLOSE）再放行 gate——保证 converter 在加入 job
    前绝不可能 spawn ffmpeg（无 assign race，DeskPet 退出可杀整棵树）。
    """
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--gated" in sys.argv[1:]:
        try:
            sys.stdin.buffer.read(1)   # 等 gate 字节
        except Exception:
            pass
    if len(args) < 2:
        print("usage: python -m tools.convert [--gated] <skin_dir> <out_dir> [height] [fps]")
        raise SystemExit(1)
    src_dir, out_dir = args[0], args[1]
    height = int(args[2]) if len(args) > 2 else 240
    fps = int(args[3]) if len(args) > 3 else 12
    convert_skin(src_dir, out_dir, height=height, fps=fps,
                 log=print if sys.stdout else None)


if __name__ == "__main__":
    _selftest()
'''
new_convert_tail = '''def main(argv=None) -> int:
    """Converter CLI used by both source and compiled internal entry points.

    ``--gated`` blocks on one stdin byte before any conversion work, so the
    parent can first place this process in a KILL_ON_JOB_CLOSE Job Object.
    """
    import sys
    raw = list(sys.argv[1:] if argv is None else argv)
    args = [a for a in raw if not a.startswith("--")]
    if "--gated" in raw:
        try:
            sys.stdin.buffer.read(1)
        except Exception:
            pass
    if len(args) < 2:
        print("usage: python -m tools.convert [--gated] <skin_dir> <out_dir> [height] [fps]")
        return 1
    src_dir, out_dir = args[0], args[1]
    height = int(args[2]) if len(args) > 2 else 240
    fps = int(args[3]) if len(args) > 3 else 12
    convert_skin(src_dir, out_dir, height=height, fps=fps,
                 log=print if sys.stdout else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
replace_once("tools/convert.py", old_convert_tail, new_convert_tail)
replace_once(
    "tools/convert.py",
    "# 重转换在独立子进程（python -m tools.convert）中进行，转换内存随子进程退出释放。\n",
    "# 重转换在独立 converter 子进程中进行，转换内存随子进程退出释放。\n",
)

# ---------------------------------------------------------------------------
# 5. main.py: early version/internal converter dispatch before mutex/Tk.
write("main.py", r'''"""DeskPet entry: early worker dispatch, DPI, single instance, GUI."""
import ctypes
import sys
import time


def _dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _single_instance() -> bool:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "DeskPet_SingleInstance_Mutex")
    return bool(handle) and kernel32.GetLastError() != 183


def _early_dispatch(argv: list[str]) -> int | None:
    """Handle worker/version modes before DPI, mutex, Tk or Monitor imports."""
    if argv == ["--version"]:
        from pet.version import APP_VERSION
        print(APP_VERSION)
        return 0
    if argv and argv[0] == "--deskpet-internal-converter":
        from tools.convert import main as converter_main
        return int(converter_main(argv[1:]))
    return None


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    early = _early_dispatch(args)
    if early is not None:
        return early
    if sys.platform != "win32":
        print("DeskPet 目前仅支持 Windows（含 WSL 内 Agent 的监听）")
        return 1
    _dpi_aware()
    if not _single_instance():
        ctypes.windll.user32.MessageBoxW(
            None, "DeskPet 已经在运行了。如需重启，请先退出已有 DeskPet 进程。",
            "DeskPet", 0x40)
        return 0

    from pet.config import Config
    from pet.app import PetApp

    startup_t0 = time.perf_counter()
    config = Config()
    app = PetApp(config, startup_baseline=startup_t0,
                 config_loaded_at=time.perf_counter())
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
''')

# ---------------------------------------------------------------------------
# 6. Runtime tray icon defaults to LocalAppData; explicit root_dir remains a test seam.
replace_once(
    "pet/icon.py",
    "  * ``assets/icon.ico`` 仍是本机生成产物（gitignored）：启动时缺文件\n",
    "  * ``%LOCALAPPDATA%/DeskPet/icon.ico`` 是本机生成产物：启动时缺文件\n",
)
replace_once(
    "pet/icon.py",
    "import os\n",
    "import os\n\nfrom .runtime_paths import get_runtime_paths\n",
)
replace_once(
    "pet/icon.py",
    '''def icon_ico_path(root_dir: str | None = None) -> str:
    base = root_dir or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "icon.ico")
''',
    '''def icon_ico_path(root_dir: str | None = None) -> str:
    if root_dir is not None:
        # Explicit root is retained for deterministic unit/build tooling.
        return os.path.join(root_dir, "assets", "icon.ico")
    return str(get_runtime_paths().runtime_icon)
''',
)
replace_once(
    "pet/icon.py",
    '    """确保 assets/icon.ico 存在且等于当前小猫绘制。\n',
    '    """确保运行期 LocalAppData icon.ico 存在且等于当前小猫绘制。\n',
)

# ---------------------------------------------------------------------------
# 7. Dashboard: show/open data root; actual config path comes from Config instance.
replace_once(
    "pet/dashboard.py",
    "        from .config import CONFIG_PATH\n        app = self.dash.app\n",
    "        from .runtime_paths import get_runtime_paths\n        app = self.dash.app\n        runtime_paths = get_runtime_paths()\n        config_path = str(getattr(app.config, \"path\", runtime_paths.config_file))\n",
)
replace_once(
    "pet/dashboard.py",
    '                 text=f"配置文件位置（只读展示）：{CONFIG_PATH}",\n',
    '                 text=f"配置文件位置（只读展示）：{config_path}",\n',
)
old_about = '''        abt = SurfacePanel(body)
        abt.pack(fill="x")
        _section_title(abt.body, "关于")
'''
new_about = '''        data = SurfacePanel(body)
        data.pack(fill="x", pady=(0, SECTION_GAP))
        _section_title(data.body, "本地数据")
        tk.Label(data.body, text=str(runtime_paths.data_root),
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(data, 9), anchor="w").pack(fill="x")
        drow = tk.Frame(data.body, bg=LIGHT.surface)
        drow.pack(fill="x", pady=(6, 0))
        ttk.Button(drow, text="打开数据目录",
                   command=self._open_data_root).pack(side="left")
        InfoButton(drow,
                   "配置、导入皮肤与生成缓存统一保存在此目录；普通设置请直接在 Dashboard 修改。",
                   self.dash.tooltip).pack(side="left", padx=(6, 0))

        abt = SurfacePanel(body)
        abt.pack(fill="x")
        _section_title(abt.body, "关于")
'''
replace_once("pet/dashboard.py", old_about, new_about)
replace_once(
    "pet/dashboard.py",
    "    def _toggle_autostart(self):\n",
    '''    def _open_data_root(self):
        from .runtime_paths import open_data_root
        result = open_data_root()
        if not result.ok:
            self.dash.app.toast(
                f"无法打开数据目录：{result.error or result.path}", 5)

    def _toggle_autostart(self):
''',
)

# ---------------------------------------------------------------------------
# 8. Version and source launchers.
replace_once("pet/version.py", 'APP_VERSION = "4.5.0"', 'APP_VERSION = "4.6.0"')
replace_once(
    "pet/version.py", '"""DeskPet 版本常量（v4.5.0）。',
    '"""DeskPet 版本常量（v4.6.0）。')
replace_once(
    "Setup-Desktop.bat",
    "rem DeskPet one-time setup (v4.3.0 release setup): repo-local .venv only.\n",
    "rem DeskPet source/developer setup: optional repo-local .venv.\n",
)
replace_once(
    "Start-Desktop.bat",
    "rem DeskPet launcher (v4.3.0 release start): start only, never install.\n",
    "rem DeskPet source/developer launcher: start only, never install.\n",
)

# ---------------------------------------------------------------------------
# 9. Build dependencies and build/acceptance tools.
write("requirements-build.txt", "Nuitka==4.2.1\n")

write("tools/build_release.py", r'''"""Build DeskPet Windows x64 portable distribution with Nuitka standalone."""
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
        "--lto=yes",
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
''')

write("tools/release_acceptance.py", r'''"""Static and executable acceptance for a built DeskPet standalone dist."""
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
        lowered = {p.lower() for p in path.parts}
        if "tests" in lowered or ".release" in lowered:
            raise SystemExit(f"development-only path leaked into release: {path}")

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

    print("release acceptance: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''')

write("THIRD_PARTY_NOTICES.md", r'''# Third-party components

DeskPet portable builds bundle third-party runtime components. The exact package
versions are pinned by `constraints.txt` / `requirements-build.txt` and must be
verified from the produced distribution before each release.

Runtime families currently include CPython runtime components, Pillow, psutil,
comtypes, NumPy, SciPy, imageio-ffmpeg and the FFmpeg executable supplied through
imageio-ffmpeg. Nuitka is the build tool.

Before publishing a binary release, the release job must retain the final build
report and the actual FFmpeg licensing/build configuration. In particular, an
FFmpeg build containing `--enable-nonfree` must not be published.

This notice is informational and does not replace the license files/notices
shipped by the respective upstream components where redistribution requires them.
''')

# ---------------------------------------------------------------------------
# 10. GitHub release workflow: tag is the durable user distribution channel.
write(".github/workflows/release.yml", r'''name: release

on:
  push:
    tags:
      - "v*.*.*"
  workflow_dispatch:

permissions:
  contents: write

jobs:
  windows-portable:
    runs-on: windows-2022
    env:
      PYTHONUTF8: "1"
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install runtime dependencies
        run: python -m pip install -r requirements.txt -c constraints.txt
      - name: Install build dependencies
        run: python -m pip install -r requirements-build.txt
      - name: Verify tag and source version
        if: startsWith(github.ref, 'refs/tags/')
        shell: pwsh
        run: |
          $version = python -c "from pet.version import APP_VERSION; print(APP_VERSION)"
          $tag = "${{ github.ref_name }}"
          if ($tag -ne "v$version") { throw "tag $tag != v$version" }
      - name: Byte-compile
        run: python -m compileall agents pet actions tools
      - name: Unit tests
        run: python -m unittest discover tests -p "test_*.py"
      - name: Monitor benchmark
        run: python tests/benchmark_monitor.py --ticks 5000 --report benchmark-report.json
      - name: Desktop sources benchmark
        run: python tests/benchmark_desktop_sources.py --ticks 5000 --report desktop-source-benchmark.json
      - name: Presentation benchmark
        run: python tests/benchmark_presentation.py --report presentation-benchmark.json
      - name: UI architecture benchmark
        run: python tests/benchmark_ui_architecture.py --report ui-architecture-benchmark.json
      - name: Build portable standalone
        run: python tools/build_release.py
      - name: Upload build diagnostics
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: deskpet-release-diagnostics
          path: |
            .release/nuitka-report.xml
            benchmark-report.json
            desktop-source-benchmark.json
            presentation-benchmark.json
            ui-architecture-benchmark.json
      - name: Upload portable artifact for manual dispatch
        if: github.event_name == 'workflow_dispatch'
        uses: actions/upload-artifact@v4
        with:
          name: DeskPet-windows-x64-portable
          path: |
            .release/DeskPet-windows-x64-portable.zip
            .release/SHA256SUMS.txt
      - name: Publish GitHub Release
        if: startsWith(github.ref, 'refs/tags/')
        shell: pwsh
        run: |
          gh release create "${{ github.ref_name }}" `
            ".release/DeskPet-windows-x64-portable.zip" `
            ".release/SHA256SUMS.txt" `
            --verify-tag --generate-notes
''')

# ---------------------------------------------------------------------------
# 11. Tests for the new contracts.
write("tests/test_runtime_paths.py", r'''from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pet import runtime_paths


class RuntimePathsTests(unittest.TestCase):
    def tearDown(self):
        runtime_paths._PATHS = None

    def test_env_fallback_never_uses_program_root(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(runtime_paths.os, "name", "posix"), \
             patch.dict(os.environ, {"LOCALAPPDATA": td}, clear=False):
            runtime_paths._PATHS = None
            paths = runtime_paths.get_runtime_paths()
            self.assertEqual(paths.data_root, Path(td) / "DeskPet")
            self.assertNotEqual(paths.data_root, paths.program_root)

    def test_layout_contract(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(runtime_paths.os, "name", "posix"), \
             patch.dict(os.environ, {"LOCALAPPDATA": td}, clear=False):
            runtime_paths._PATHS = None
            p = runtime_paths.get_runtime_paths()
            self.assertEqual(p.config_file, p.data_root / "config.json")
            self.assertEqual(p.config_backup, p.data_root / "config.json.bak")
            self.assertEqual(p.pets_dir, p.data_root / "assets" / "pets")
            self.assertEqual(p.cache_dir, p.data_root / "assets" / "cache")
            self.assertEqual(p.runtime_icon, p.data_root / "icon.ico")

    def test_ensure_asset_dirs_is_lazy_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(runtime_paths.os, "name", "posix"), \
             patch.dict(os.environ, {"LOCALAPPDATA": td}, clear=False):
            runtime_paths._PATHS = None
            p = runtime_paths.ensure_asset_dirs()
            self.assertTrue(p.pets_dir.is_dir())
            self.assertTrue(p.cache_dir.is_dir())
            runtime_paths.ensure_asset_dirs()
''')

write("tests/test_release_entry.py", r'''from __future__ import annotations

import io
import unittest
from unittest.mock import patch

import main


class ReleaseEntryTests(unittest.TestCase):
    def test_version_dispatch_does_not_touch_gui(self):
        out = io.StringIO()
        with patch("sys.stdout", out), patch.object(main, "_dpi_aware") as dpi:
            self.assertEqual(main.main(["--version"]), 0)
        self.assertTrue(out.getvalue().strip())
        dpi.assert_not_called()

    def test_converter_dispatch_happens_before_platform_guard(self):
        with patch("tools.convert.main", return_value=7) as worker, \
             patch.object(main, "_dpi_aware") as dpi:
            rc = main.main(["--deskpet-internal-converter", "x", "y"])
        self.assertEqual(rc, 7)
        worker.assert_called_once_with(["x", "y"])
        dpi.assert_not_called()
''')

# Update existing release-layout expectations instead of deleting its historical checks.
replace_once("tests/test_release_layout.py", 'self.assertEqual(APP_VERSION, "4.5.0")',
             'self.assertEqual(APP_VERSION, "4.6.0")')
replace_once("tests/test_release_layout.py", 'self.assertEqual(APP_LABEL, "DeskPet V4.5.0")',
             'self.assertEqual(APP_LABEL, "DeskPet V4.6.0")')
replace_once(
    "tests/test_release_layout.py",
    "    def test_constraints_file_pins_verified_set(self):\n",
    '''    def test_portable_release_files_present(self):
        self.assertTrue((ROOT / "pet" / "runtime_paths.py").is_file())
        self.assertTrue((ROOT / "requirements-build.txt").is_file())
        self.assertTrue((ROOT / "tools" / "build_release.py").is_file())
        self.assertTrue((ROOT / ".github" / "workflows" / "release.yml").is_file())
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("DeskPet-windows-x64-portable.zip", readme)
        self.assertIn("现有 Python 3.12", readme)
        self.assertIn("%LOCALAPPDATA%\\\\DeskPet", readme)

    def test_constraints_file_pins_verified_set(self):
''',
)

# ---------------------------------------------------------------------------
# 12. README: end-user first, while retaining detailed maintainer contracts.
write("README.md", r'''# DeskPet V4.6.0

轻量、被动、零 Hook 的 Windows / WSL AI Agent 桌宠观察器。

DeskPet 常驻 Windows 桌面，被动观察已经由用户启动的 AI 编码 Agent，
把 Goal、Mode、Thinking / Reading / Coding / Testing / Waiting Approval 等
状态映射为桌宠动画和状态气泡。支持 Codex CLI、Claude Code、Kimi CLI、
pi，以及 Codex Desktop / ChatGPT Desktop Codex 模式和 ZCode Desktop。

DeskPet 不创建、不托管、不控制 Agent：不要求 hooks、MCP、插件或代理
脚本，不注入 Agent 进程，不自动审批，不向 Agent 数据库写数据。

## 下载与启动

DeskPet 同时保留两种运行方式。

### 方式 A：Windows x64 Portable（普通用户推荐）

正式 Release 提供：

**[下载最新 DeskPet Windows x64 Portable](https://github.com/invincible-summer/Deskpet-For-Agents/releases/latest/download/DeskPet-windows-x64-portable.zip)**

使用：

1. 下载 `DeskPet-windows-x64-portable.zip`；
2. 解压到任意普通用户可执行目录；
3. 双击 `DeskPet.exe`。

无需安装 Python、pip、虚拟环境或安装器，也不要求管理员权限。普通用户
请下载 Release asset，不要把 GitHub 自动生成的 `Source code (zip)` 当作
可运行程序。

默认发布采用 Nuitka **standalone** 而不是 onefile：仍然免安装，但避免
常驻小工具每次启动都做 onefile 临时解包和额外磁盘 I/O。

### 方式 B：使用现有 Python 3.12 环境

如果机器已经有 Python 3.12，可以直接使用现有环境；不强制创建 DeskPet
专用 `.venv`：

```bat
python -m pip install -r requirements.txt -c constraints.txt
python main.py
```

希望无控制台窗口时可使用：

```bat
pythonw main.py
```

如果更希望依赖隔离，仓库仍保留可选的 repo-local `.venv` 流程：

```bat
Setup-Desktop.bat
Start-Desktop.bat
```

`Setup-Desktop.bat` 只寻找已有 Python 3.12 并创建 `.venv`；
`Start-Desktop.bat` 只启动，不执行 pip 安装或联网修复。

## 第一次使用

首次启动即使没有用户皮肤，也会显示程序化生成的 `builtin-cat`。并行监听
默认开启，每次启动固定进入单宠聚合模式；没有任何 Agent 时仍保留
`pet-1 idle fallback`，除非用户本次运行中显式隐藏全部桌宠。

```text
启动 DeskPet
  ↓
自己打开 Windows Terminal / WSL / Desktop Agent
  ↓
DeskPet 自动发现并被动观察
  ↓
桌宠动画 + Agent 状态气泡
```

用户可在本次运行内切换 SINGLE / AGGREGATE / FLEET；重启后再次回到
并行监听默认开启 + 单宠聚合。

## 皮肤导入

Dashboard → 外观 → `导入皮肤…`，选择包含五个状态素材的文件夹：

```text
MyPet/
├─ walk.webm      工作中
├─ attack.webm    下达指令
├─ die.webm       等待审批
├─ special.webm   完成
└─ sleep.webm     空闲
```

也支持转换器允许的 mp4/mkv/mov/avi/gif。外部目录只是 import source：
DeskPet 会先校验，再复制到自己的数据目录、生成 manifest、再次校验并原子
发布。因此移动/删除原 Downloads/Desktop 素材目录不会破坏已导入皮肤。

FLEET 模式下每只桌宠可独立选择皮肤；同一套皮肤可以重复使用。

## 本地数据目录

所有用户可变数据统一位于：

```text
%LOCALAPPDATA%\\DeskPet\
├─ config.json
├─ config.json.bak
├─ icon.ico
└─ assets\
   ├─ pets\
   │  └─ MyPet\
   │     ├─ walk.webm
   │     ├─ attack.webm
   │     ├─ die.webm
   │     ├─ special.webm
   │     ├─ sleep.webm
   │     └─ manifest.json
   └─ cache\
      ├─ MyPet@240\
      └─ builtin-cat@240\
```

Dashboard → 设置 → 本地数据会显示实际路径，并提供 **打开数据目录**。
程序目录与用户数据彻底分离，因此替换 portable 程序目录不会删除配置、
皮肤或缓存。

普通配置全部通过 Dashboard 修改：内存立即生效，经 650ms debounce 后由
单一后台 writer 原子保存到 `config.json`。`config.json` 是内部持久化格式，
普通用户无需手工编辑。

## 状态与交互

状态优先级：

```text
ERROR > WAITING > INPUT > WORKING > DONE > IDLE > UNKNOWN
```

Mode 是独立维度，因此 `WAITING + APPROVAL + PLAN` 是合法组合。

- SINGLE：双击桌宠/气泡，唤起对应 Terminal。
- AGGREGATE：多张 Agent 卡片叠在同一只桌宠上；双击卡片只唤起该 Agent，
  双击桌宠 body 只互动。
- FLEET：每个目标有自己的桌宠/气泡并可使用不同皮肤。
- Codex/ZCode Desktop 只承诺恢复并前置宿主应用，不猜测私有会话导航。

## 被动观察、安全与隐私

```text
Process tells us WHO.
Session data tells us WHAT.
Terminal UIA tells us WHAT THE USER IS ASKED.
StateReducer combines evidence.
DeskPet only observes.
```

DeskPet 不发送键盘输入、不自动审批、不写 Agent 数据库、不 checkpoint WAL，
不因为静默而推断 WAITING。PID/HWND/RuntimeId/WT_SESSION/exact key 等运行期
identity 不持久化。终端可见文本只在内存参与状态/归属判断，默认不落盘；
归属证据不足时宁可缺失证据，也不把审批错归给其他 Agent。

Windows Terminal 唤起使用公共 Win32 API，并在操作前校验 HWND + PID +
进程创建时间 + 窗口类；系统拒绝抢前台时只闪烁提醒，不绕过 foreground
policy。WSL 只探测本轮确认正在运行的 distro，不为了监听而启动已停止 WSL。

# 架构与接口合同

以下边界是维护时必须保持的长期合同。发行逻辑不得扩散进 Monitor、状态融合
或 Presentation。

## `main.py` — 进程入口

启动顺序：

```text
early argv dispatch
→ Windows guard
→ DPI awareness
→ single-instance mutex
→ Config
→ PetApp
```

维护接口：

```text
DeskPet.exe --version
```

内部 worker 接口：

```text
DeskPet.exe --deskpet-internal-converter --gated ...
```

internal converter 必须在 mutex、Tk、Monitor 之前 dispatch，否则转换子进程
会被 GUI 单实例保护拦截或加载不必要的常驻组件。

## `pet/runtime_paths.py` — 唯一路径真值

`RuntimePaths` 提供：

```text
program_root
 data_root
 config_file / config_backup
 assets_dir
 pets_dir / cache_dir
 runtime_icon
```

Windows data root 通过 `SHGetKnownFolderPath(FOLDERID_LocalAppData)` 获取，
`LOCALAPPDATA` 仅为 API 失败 fallback；永不回退到程序目录。

`open_data_root()` 负责 lazy mkdir + `ShellExecuteW("open")`，返回结构化结果，
不会把 Win32 异常抛进 Tk event loop。

## `pet/config.py` / `pet/config_save.py`

默认持久化：

```text
%LOCALAPPDATA%\\DeskPet\\config.json
%LOCALAPPDATA%\\DeskPet\\config.json.bak
```

Config 继续负责 schema、migrate、normalize/clamp、revision、dirty 和原子写盘。
测试仍可显式传 `Config(path=temp_file)`，其 backup 跟随 custom path。

运行时保存协议：

```text
Config.set
→ revision++ / dirty
→ ConfigSaveCoordinator 650ms debounce
→ short snapshot
→ <=1 transient writer
→ temp + flush + optional fsync + backup + replace
→ acknowledge revision
```

磁盘写入期间新的 revision 不能被旧 snapshot 错误标记为 clean；同一失败
revision 不无限自动重试。

## `pet/skins.py` / `tools/convert.py`

`SkinBuildManager` 是唯一 skin mutation lane，串行 bootstrap/build/rebuild/
import/maintenance。import 与 cache build 都先写同 filesystem staging，再完整
校验并 atomic directory swap；失败保持旧 live 目录。

source 模式 converter：

```text
python.exe -m tools.convert --gated ...
```

compiled 模式：

```text
DeskPet.exe --deskpet-internal-converter --gated ...
```

二者复用同一个 `tools.convert.main(argv)`。converter 保持独立子进程；
numpy/scipy 在函数内惰性 import，转换结束后峰值内存随子进程退出释放。
Windows 使用 KILL_ON_JOB_CLOSE Job Object + one-byte gate，确保加入 job 之前
不会 spawn ffmpeg，取消/退出可终止整棵 converter→ffmpeg 树。

## `pet/petview.py`

一个 Tk interpreter、N 个 Toplevel PetView。所有 Pet 共享：

- `SharedAnimationCache`
- `AnimationScheduler`
- `SkinBuildManager`
- Monitor / Presentation

增加桌宠数量不得复制这些全局对象；这是 FLEET 仍保持低 CPU/内存的核心。

## `agents/monitor.py`

Monitor 只负责 Agent data plane 编排，输出 revision 驱动的 `AgentTarget`。
它不负责 LocalAppData、Nuitka、GitHub Release、皮肤目录或配置保存。本次 portable
发行不改变 Codex/Claude/Kimi/pi/Codex Desktop/ZCode Desktop 的监听协议。

## `pet/presentation.py`

Presentation 只负责 SINGLE / AGGREGATE / FLEET 和 target→slot/view 映射，
不负责 discovery 或持久化。每次启动固定初始化并行监听 + AGGREGATE；运行时
切换不改变下一次启动默认值。

## `pet/dashboard.py`

Dashboard 是普通用户控制面：设置写入走 Config/AppearanceController +
ConfigSaveCoordinator；皮肤选择只产生 import request，真实复制/校验/发布由
SkinBuildManager 完成；本地数据目录通过 RuntimePaths 显示/打开。

## `pet/autostart.py`

source 模式注册 `pythonw.exe main.py`；compiled 模式注册当前 `DeskPet.exe`。
portable 目录被移动后旧注册项视为 stale，由现有 repair 操作重新登记。不引入
Windows service、Task Scheduler 或安装器专属状态。

# 项目结构

```text
main.py                    GUI / --version / internal converter 入口
pet/runtime_paths.py       LocalAppData 与 program root 唯一边界
pet/config.py              schema / normalize / persistence
pet/config_save.py         async single-writer save coordinator
pet/dashboard.py           用户控制面
pet/skins.py               skin catalog/import/build/cache transaction
pet/petview.py             N PetView + shared cache/scheduler/build
pet/presentation.py        single/aggregate/fleet
agents/                    passive discovery/session/state/terminal/desktop sources
actions/winkeys.py         fail-closed Win32 activation
tools/convert.py           conversion child process
tools/build_release.py     standalone build 唯一入口
tools/release_acceptance.py compiled artifact acceptance
tests/                     unit / benchmark / acceptance contracts
.github/workflows/test.yml source CI
.github/workflows/release.yml tag -> standalone -> GitHub Release
```

# 测试

现有 Python 3.12 环境中：

```bat
python -m unittest discover tests -p "test_*.py"
python tests\benchmark_monitor.py --ticks 5000 --report benchmark-report.json
python tests\benchmark_desktop_sources.py --ticks 5000 --report desktop-source-benchmark.json
python tests\benchmark_presentation.py --report presentation-benchmark.json
python tests\benchmark_ui_architecture.py --report ui-architecture-benchmark.json
python tools\ttfv_probe.py
python tools\real_machine_acceptance.py
```

真实 Windows Terminal 前台策略/UIA 事件仍属于实机 acceptance；CI 只验证纯逻辑、
Win32 调用合同 mock、资源预算和 UI dataflow。

# Portable 构建与 GitHub Release

构建依赖与 runtime 依赖分离：

```text
requirements.txt          runtime/source dependencies
constraints.txt           verified runtime pins
requirements-build.txt    build-only Nuitka pin
```

维护者构建：

```bat
python -m pip install -r requirements.txt -c constraints.txt
python -m pip install -r requirements-build.txt
python tools\build_release.py
```

输出：

```text
.release/
├─ DeskPet.dist/
├─ DeskPet-windows-x64-portable.zip
├─ SHA256SUMS.txt
└─ nuitka-report.xml
```

二进制和 ZIP 不提交 Git history。

正式版本使用 `vX.Y.Z` tag，并强制：

```text
tag == v{pet.version.APP_VERSION}
```

`release.yml` 在 Windows 2022 + Python 3.12 上重新执行单测和 blocking benchmarks，
然后构建 Nuitka standalone、运行 compiled acceptance、生成 ZIP/SHA256，并把：

```text
DeskPet-windows-x64-portable.zip
SHA256SUMS.txt
```

发布为 GitHub Release assets。Actions artifacts 只保存 build report/benchmark 等
诊断数据，不作为长期用户下载入口。

Release 前还必须核验最终发行物中的 FFmpeg license/build configuration，禁止
发布 `--enable-nonfree` 构建；实际依赖族记录于 `THIRD_PARTY_NOTICES.md`。

## 研究资料

Agent 上游合同、实现快照与实证来源见 [SourceLink.md](./SourceLink.md)。
''')

# ---------------------------------------------------------------------------
# 13. .gitignore build outputs.
with (ROOT / ".gitignore").open("a", encoding="utf-8") as f:
    f.write("\n# Nuitka / portable release build\n.release/\n*.build/\n*.dist/\n*.onefile-build/\nnuitka-report.xml\n")

# ---------------------------------------------------------------------------
# 14. Restore generic repository-wide AGENTS.md if absent on current main.
write("AGENTS.md", r'''# DeskPet Repository Development Contract

## Project goal

DeskPet is a lightweight Windows desktop pet that passively observes supported AI
Agent sessions across Windows/WSL terminals and supported desktop clients. Keep the
application low-CPU, low-memory, deterministic and fail-closed. Monitoring must not
require hooks, input injection, automatic approval or writes to Agent-owned data.

## Architecture discipline

Keep boundaries explicit: discovery/session parsing/state reduction belong in
`agents/`; UI/presentation/config/skin runtime belong in `pet/`; public Win32 window
actions belong in `actions/`; probes/build utilities belong in `tools/`. Do not mix
packaging, persistence or UI concerns into Monitor/state reducers.

Prefer one clear implementation over parallel legacy paths. Remove dead code and old
architecture when a replacement is accepted. Do not add abstractions without a real
boundary or test seam. Preserve implementations that already satisfy their contract.

## Plans and implementation

Before substantial work, re-read the current repository and the active plan. Treat the
plan's semantics, interfaces and acceptance criteria as implementation requirements,
not suggestions. If repository reality conflicts with a plan, update the plan explicitly
before changing architecture.

During implementation, continuously compare the code against the plan and avoid scope
creep. Necessary decisions must be documented with the chosen option and rationale.

## Safety and privacy

Observation is passive and least-privilege. Do not add keyboard/clipboard injection,
automatic approval, Agent database writes, WAL checkpointing, hidden control-plane
connections, or durable terminal text. Runtime identities such as PID/HWND/RuntimeId
must not be persisted. Ambiguous attribution fails closed.

## Performance

Do not add high-frequency polling where an event/revision/deadline model exists. Keep
Tk free of blocking I/O. Bound queues, caches, workers, subprocesses and shutdown time.
Do not load heavy conversion dependencies into the resident GUI process. Optimization
changes require benchmark or profiling evidence.

## Tests and acceptance

Every changed contract needs regression coverage. Run the complete unit suite and the
relevant blocking benchmarks. Release work also requires compiled-artifact acceptance
on Windows. A task is not complete while required CI/acceptance is red. Review the
final diff for stale code, duplicated paths, documentation drift and privacy/resource
regressions before declaring completion.

## Documentation

README describes current user behavior and maintained architecture; release notes carry
version history; SourceLink records external research evidence. Keep commands, paths,
versions and file names synchronized with the actual repository.
''')

# ---------------------------------------------------------------------------
# Self-delete staging files: the workflow itself is removed by the shell step after this script.
print("portable patch applied")
