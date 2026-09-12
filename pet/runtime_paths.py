"""DeskPet runtime path boundary.

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
