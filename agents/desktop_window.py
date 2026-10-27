"""Worker-owned, bounded app-window activation; no navigation or input.

Tray-hidden Electron content must be shown by the application's own lifecycle.
External ShowWindow leaves Aura content hidden behind an inert old frame.
"""
from dataclasses import dataclass
import os
import time

from actions import winkeys
from actions.desktop_launch import start_desktop_reopen
from .models import (ActivationCode, ActivationResult, AgentInstance, AgentKind,
                     AgentSurface, WindowIdentity)


_HOST_EXE_BASENAMES = {
    AgentKind.CODEX: frozenset({"chatgpt.exe", "codex.exe"}),
    AgentKind.ZCODE: frozenset({"zcode.exe"}),
    AgentKind.OPENCODE: frozenset({
        "opencode.exe", "opencode dev.exe", "opencode beta.exe"}),
    AgentKind.DSH: frozenset({"deepseek harness.exe"}),
}


def _pid_belongs_to_host(pid: int, host_pid: int) -> bool:
    if pid == host_pid:
        return True
    try:
        import psutil
        current = psutil.Process(pid)
        for _ in range(12):
            current = current.parent()
            if current is None:
                return False
            if current.pid == host_pid:
                return True
    except Exception:
        pass
    return False


@dataclass
class _Activation:
    window: WindowIdentity
    deadline: float
    launcher: object = None


class DesktopWindowService:
    """Only called by Monitor's worker. Pending jobs never wait on a child."""

    def __init__(self):
        self._pending: dict[tuple, _Activation] = {}

    @staticmethod
    def _key(instance):
        return (instance.kind, instance.host_pid, instance.host_process_token)

    @staticmethod
    def _dispose(job):
        # Only our disposable second-instance bootstrap, never the live host.
        if job is not None and job.launcher is not None:
            job.launcher.cancel()

    def cancel(self, instance) -> None:
        self._dispose(self._pending.pop(self._key(instance), None))

    def close(self) -> None:
        for key in list(self._pending):
            self._dispose(self._pending.pop(key))

    def activate_host(self, instance: AgentInstance) -> ActivationResult:
        host_pid = int(getattr(instance, "host_pid", 0) or 0)
        token = str(getattr(instance, "host_process_token", "") or "")
        kind = instance.kind
        if not host_pid or not token or kind not in _HOST_EXE_BASENAMES:
            return ActivationResult(ActivationCode.NO_BINDING)
        key = self._key(instance)
        try:
            import psutil
            host = psutil.Process(host_pid)
            if f"{host.create_time():.3f}" != token:
                raise ProcessLookupError("host incarnation changed")
            if os.path.basename(host.exe().lower()) not in _HOST_EXE_BASENAMES[kind]:
                self.cancel(instance)
                return ActivationResult(ActivationCode.NO_BINDING)
        except Exception:
            self.cancel(instance)
            return ActivationResult(ActivationCode.AGENT_GONE)

        job = self._pending.get(key)
        if job is not None:
            if not winkeys.validate_window(job.window):
                self.cancel(instance)
                return ActivationResult(ActivationCode.STALE_WINDOW)
            if time.monotonic() >= job.deadline:
                self.cancel(instance)
                winkeys.flash_window(job.window.hwnd)
                return ActivationResult(ActivationCode.NO_BINDING,
                                        detail="app restore timed out")
            if job.launcher is not None:
                exited = job.launcher.poll()
                if exited is None:
                    return ActivationResult(ActivationCode.RESTORE_PENDING)
                if exited != 0:
                    self.cancel(instance)
                    return ActivationResult(ActivationCode.NO_BINDING,
                                            detail="app reopen failed")
            if not winkeys.window_ready(job.window.hwnd):
                return ActivationResult(ActivationCode.RESTORE_PENDING)
            self._pending.pop(key)
            return self._foreground(job.window)

        # Matching an exe name alone is insufficient; even known helper PIDs
        # must belong to this live host's process tree. Titleless main is valid.
        candidates = []
        for hwnd, pid, _title, cls in winkeys.enum_windows(include_hidden=True):
            if cls != "Chrome_WidgetWin_1" or not winkeys.is_app_window(hwnd):
                continue
            if not _pid_belongs_to_host(int(pid), host_pid):
                continue
            if int(pid) != host_pid:
                try:
                    owner = psutil.Process(int(pid))
                    exe = owner.exe()
                    if any(arg.startswith("--type=")
                           for arg in owner.cmdline()[1:]):
                        continue
                except Exception:
                    continue
                if os.path.basename(exe.lower()) not in _HOST_EXE_BASENAMES[kind]:
                    continue
            candidates.append((int(pid) != host_pid,
                               -winkeys.window_area(hwnd), int(hwnd), int(pid)))
        invalid_identity = False
        for _, _, hwnd, pid in sorted(candidates):
            identity = winkeys.window_identity(hwnd)
            if (identity is None or identity.pid != pid
                    or identity.window_class != "Chrome_WidgetWin_1"
                    or (pid == host_pid
                        and f"{identity.process_created:.3f}" != token)
                    or not winkeys.validate_window(identity)):
                invalid_identity = True
                continue
            if not winkeys.window_visible(hwnd):
                if len(self._pending) >= 8:
                    return ActivationResult(ActivationCode.NO_BINDING,
                                            detail="activation capacity")
                launcher = start_desktop_reopen(kind, host_pid, token)
                if launcher is None:
                    winkeys.flash_window(hwnd)
                    return ActivationResult(ActivationCode.NO_BINDING,
                                            detail="no verified app reopen entry")
                self._pending[key] = _Activation(
                    identity, time.monotonic() + 8.0, launcher)
                return ActivationResult(ActivationCode.RESTORE_PENDING)
            if not winkeys.window_ready(hwnd):
                if len(self._pending) >= 8:
                    return ActivationResult(ActivationCode.NO_BINDING)
                winkeys.restore_window(hwnd, allow_hidden=False)
                self._pending[key] = _Activation(identity, time.monotonic() + 3.0)
                return ActivationResult(ActivationCode.RESTORE_PENDING)
            return self._foreground(identity)
        return ActivationResult(ActivationCode.STALE_WINDOW if invalid_identity
                                else ActivationCode.NO_BINDING,
                                detail="no verified app main window")

    @staticmethod
    def _foreground(identity) -> ActivationResult:
        if not winkeys.validate_window(identity):
            return ActivationResult(ActivationCode.STALE_WINDOW)
        if winkeys.try_set_foreground(identity.hwnd):
            return ActivationResult(ActivationCode.OK)
        winkeys.flash_window(identity.hwnd)
        return ActivationResult(ActivationCode.FOREGROUND_DENIED)


def is_desktop_instance(instance) -> bool:
    return (getattr(instance, "surface", AgentSurface.TERMINAL)
            is AgentSurface.DESKTOP)
