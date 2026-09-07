"""DeskPet 入口：DPI 感知、单实例保护、启动桌宠。"""
import ctypes
import sys


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
    return bool(handle) and kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def main():
    if sys.platform != "win32":
        print("DeskPet 目前仅支持 Windows（含 WSL 内 Agent 的监听）")
        return 1
    _dpi_aware()
    if not _single_instance():
        ctypes.windll.user32.MessageBoxW(
            None, "DeskPet 已经在运行了（托盘/任务栏找不到可在任务管理器结束 pythonw）。"
                  "如需重启，请先结束已有进程。", "DeskPet", 0x40)
        return 0
    from pet.config import Config
    from pet.app import PetApp

    config = Config()
    app = PetApp(config)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
