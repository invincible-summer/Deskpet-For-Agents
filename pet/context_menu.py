"""Own one pet popup and dispatch its selected action after native tracking ends.

Windows Tk runs a nested native loop inside tk_popup; X11 returns while the
menu is still posted. Neither path may poll with after_idle: Windows services
idle callbacks inside its menu loop, so an idle that requeues itself can starve
input forever. Menu completion is event driven and actions are single use.
"""
from __future__ import annotations

import logging
import tkinter as tk

_log = logging.getLogger(__name__)


class TkContextMenuController:
    def __init__(self, root, *, is_closing=None):
        self._root = root
        self._is_closing = is_closing or (lambda: False)
        self._menu: tk.Menu | None = None
        self._owner = None
        self._posting = False
        self._stopped = False
        self._pending = None
        self._completion_after = None
        self._action_after = None
        self._window_system = root.tk.call("tk", "windowingsystem")

    @property
    def active(self) -> bool:
        return self._menu is not None

    @property
    def posting(self) -> bool:
        """True while tk_popup's native stack has not unwound."""
        return self._posting

    @property
    def owner(self):
        return self._owner

    def deferred(self, command, *args, **kwargs):
        allow_when_closing = bool(kwargs.pop("allow_when_closing", False))
        menu = self._menu
        used = False

        def publish():
            nonlocal used
            if used or self._stopped:
                return
            if menu is not None and self._menu is not menu:
                return  # obsolete menu generation
            if self._is_closing() and not allow_when_closing:
                return
            used = True
            if self._pending is not None:
                return  # one selection per popup, including nested callbacks
            self._pending = (command, args, kwargs, allow_when_closing)
            if self._posting:
                self._end_native_menu()
                # show() finally publishes exactly one idle after native return.
            else:
                self._schedule_completion()

        return publish

    def _schedule_completion(self):
        if self._completion_after is None and not self._stopped:
            self._completion_after = self._root.after_idle(self._complete)

    def close_then(self, command):
        """An explicit app exit supersedes any selection still in tracking."""
        self._pending = (command, (), {}, True)
        if self._posting:
            self._end_native_menu()
        else:
            self._schedule_completion()

    def _complete(self):
        self._completion_after = None
        if self._posting:
            return  # show() owns completion; never spin in an idle callback
        self.dismiss()
        pending, self._pending = self._pending, None
        if pending is not None and not self._stopped:
            def run():
                self._action_after = None
                command, args, kwargs, allow = pending
                if not self._stopped and (allow or not self._is_closing()):
                    command(*args, **kwargs)
            self._action_after = self._root.after_idle(run)

    def show(self, owner, x_root, y_root, build_fn) -> None:
        if (self._stopped or self._is_closing() or self._posting
                or self._action_after is not None
                or self._completion_after is not None):
            return  # native modal loop can reenter Python; never nest popups
        self.dismiss()
        self._pending = None
        menu = tk.Menu(self._root, tearoff=0)
        self._menu, self._owner = menu, owner
        self._posting = True
        failed = False
        try:
            menu.bind("<Unmap>", lambda event: self._on_unmap(menu, event))
            build_fn(menu)
            menu.tk_popup(int(x_root), int(y_root))
        except Exception:
            failed = True
            self._pending = None
            _log.exception("Could not open pet context menu")
        finally:
            self._posting = False
            # X11 retains a mapped popup until Unmap/selection. Windows has
            # already finished native tracking when tk_popup returns.
            if (failed or self._stopped or self._pending is not None
                    or self._window_system != "x11"
                    or not menu.winfo_exists() or not menu.winfo_ismapped()):
                if failed or self._stopped or self._pending is not None:
                    self._complete()
                else:
                    # A real Windows mouse selection posts WM_COMMAND.
                    # TrackPopupMenu may return BEFORE Tk dispatches it.
                    # Keep Tcl commands/Win32 command IDs alive until queued
                    # input has been serviced; menu.invoke tests bypass this.
                    self._schedule_completion()

    def _on_unmap(self, menu, event):
        if event.widget is menu and self._menu is menu and not self._posting:
            self._schedule_completion()

    def _end_native_menu(self):
        if self._posting and self._window_system == "win32":
            # EndMenu is thread-local: this controller always runs on Tk's
            # thread, never on the tray worker. Tk's unpost is a Windows no-op.
            import ctypes
            end_menu = ctypes.windll.user32.EndMenu
            end_menu.argtypes = []
            end_menu.restype = ctypes.c_int
            end_menu()

    def dismiss(self) -> None:
        self._cancel("_completion_after")
        if self._posting:
            self._end_native_menu()
            return  # never destroy a menu still referenced by native tracking
        menu, self._menu = self._menu, None
        self._owner = None
        if menu is not None:
            # Restore Tk's saved focus/grab state as well as the widget. This
            # is also what Tk's normal Escape/click-away handling uses.
            try:
                menu.tk.call("tk::MenuUnpost", menu._w)
            except tk.TclError:
                pass
            self._destroy(menu)

    def shutdown(self) -> None:
        self._stopped = True
        self._pending = None
        self._cancel("_action_after")
        self.dismiss()

    def _cancel(self, attr):
        token = getattr(self, attr)
        setattr(self, attr, None)
        if token is not None:
            try:
                self._root.after_cancel(token)
            except tk.TclError:
                pass

    @staticmethod
    def _destroy(menu) -> None:
        if menu is None:
            return
        for op in ("unpost", "grab_release", "destroy"):
            try:
                getattr(menu, op)()
            except tk.TclError:
                pass
