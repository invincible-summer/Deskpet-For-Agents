"""DeskPet entry: early worker dispatch, DPI, single instance, GUI."""
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
