"""Normal single-instance reopen for supported installed Electron desktop apps.

``start_desktop_reopen(kind, pid, token)`` is a worker-only action.  It returns
an owned ``DesktopReopen`` handle or ``None`` when identity/capability cannot
be proved.  The caller must coalesce requests, poll/reap the launcher without
blocking, and verify that the original host/window was restored before reporting
success.  This function never waits, restores an HWND, or invokes a private API.

An Electron tray hide also hides its Aura content.  Showing its HWND externally
does not undo that hide; the application's own single-instance show path must
run.  Only the three individually verified providers below support this action.
Custom command lines, portable/development profiles and unavailable process
metadata fail closed.  Runtime identity and environment stay in memory only.
"""
from __future__ import annotations

import math
import os
import subprocess

import psutil

from agents.models import AgentKind


_WINDOWS = os.name == "nt"
_HOST_EXES = {
    AgentKind.CODEX: frozenset({"chatgpt.exe", "codex.exe"}),
    AgentKind.ZCODE: frozenset({"zcode.exe"}),
    AgentKind.DSH: frozenset({"deepseek harness.exe"}),
}
# These launch modes can select a different app/profile or execute startup code
# before the ordinary application's single-instance gate.  Do not replay them.
_UNSUPPORTED_ENV = frozenset({
    "PORTABLE_EXECUTABLE_DIR", "PORTABLE_EXECUTABLE_FILE",
    "PORTABLE_EXECUTABLE_APP_FILENAME", "VSCODE_PORTABLE",
    "ELECTRON_OVERRIDE_DIST_PATH", "ELECTRON_NO_ASAR", "ELECTRON_IS_DEV",
    "ELECTRON_USER_DATA_DIR", "CHROME_USER_DATA_DIR",
    "CODEX_USER_DATA_DIR", "CODEX_APP_USER_DATA_DIR",
    "CODEX_ELECTRON_USER_DATA_PATH",  # Changes Codex's singleton profile key.
    "CODEX_ELECTRON_AGENT_RUN_ID", "BUILD_FLAVOR",
    "ZCODE_USER_DATA_DIR", "ZCODE_PROFILE", "DSH_DESKTOP_DEV_APP",
    "NODE_OPTIONS", "NODE_PATH",
})
_MAX_ENVIRONMENT_CHARS = 1024 * 1024


class DesktopReopen:
    """One launcher owned by this action, never an observed Agent process.

    The handle exposes no PID-based cancellation.  ``cancel`` only terminates
    the exact ``Popen`` created by ``start_desktop_reopen`` and never waits;
    callers retain the handle and poll again to reap an asynchronous exit.
    """

    def __init__(self, process: subprocess.Popen):
        self._process = process
        self._cancelled = False

    def poll(self) -> int | None:
        return self._process.poll()

    def cancel(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        try:
            if self._process.poll() is None:
                self._process.terminate()
        except OSError:
            pass  # The short-lived launcher may have exited between the calls.


def _canonical_path(path: str) -> str:
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return ""
    return os.path.normcase(os.path.realpath(path))


def _host_executable(kind: AgentKind, pid: int, token: str) -> str:
    """Read a fresh process object on every validation (create_time is cached)."""
    process = psutil.Process(pid)
    created = float(process.create_time())
    if not math.isfinite(created) or created <= 0 or f"{created:.3f}" != token:
        return ""
    executable = process.exe()
    canonical = _canonical_path(executable)
    if not canonical or os.path.basename(canonical).lower() not in _HOST_EXES[kind]:
        return ""
    arguments = process.cmdline()
    # Even harmless-looking switches are rejected: profiles, helper roles and
    # positional Electron app/script entries must never become a fresh instance.
    if len(arguments) != 1 or _canonical_path(arguments[0]) != canonical:
        return ""
    if not os.path.isfile(canonical):
        return ""
    # Distinguish the installed Electron GUI from a same-named standalone CLI.
    if not os.path.isfile(os.path.join(
            os.path.dirname(canonical), "resources", "app.asar")):
        return ""
    return canonical


def start_desktop_reopen(kind: AgentKind, host_pid: int,
                         host_process_token: str) -> DesktopReopen | None:
    """Start one zero-argument app reopen for an exact, still-live desktop host.

    ``host_process_token`` uses the monitor's ``f"{create_time:.3f}"`` format.
    No executable or arguments are accepted from a caller.  Reusing the host's
    environment preserves its user/profile roots instead of inheriting DeskPet's
    possibly different APPDATA/CODEX_HOME.  ``ELECTRON_RUN_AS_NODE`` is removed so
    an Electron-parent launch cannot turn the desktop executable into a helper.
    A process disappearing/replacing itself during validation returns ``None``.
    There remains an unavoidable host-exit race after the last OS identity read;
    callers must validate the original incarnation again after launch.
    """
    if (not _WINDOWS or kind not in _HOST_EXES or isinstance(host_pid, bool)
            or not isinstance(host_pid, int) or host_pid <= 0
            or not isinstance(host_process_token, str) or not host_process_token):
        return None
    try:
        executable = _host_executable(kind, host_pid, host_process_token)
        if not executable:
            return None
        environment = dict(psutil.Process(host_pid).environ())
        if not environment or sum(
                len(key) + len(value) + 2 for key, value in environment.items()
                ) > _MAX_ENVIRONMENT_CHARS:
            return None
        for key, value in tuple(environment.items()):
            upper = key.upper()
            if upper == "ELECTRON_RUN_AS_NODE":
                del environment[key]
            elif upper in _UNSUPPORTED_ENV and value:
                return None
        # Re-read metadata after reading the environment; never let a reused PID
        # or an updater's changed executable inherit the old launch permission.
        if _host_executable(kind, host_pid, host_process_token) != executable:
            return None
        process = subprocess.Popen(
            [executable], cwd=os.path.dirname(executable), env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, shell=False, close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
        return DesktopReopen(process)
    except (psutil.Error, OSError, ValueError, TypeError):
        return None
