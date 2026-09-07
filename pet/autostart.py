"""开机自启动：HKCU Run 注册表项。"""
import os
import sys
try:
    import winreg
except ImportError:
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "DeskPet"


def _command() -> str:
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.isfile(pyw) else sys.executable
    main_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "main.py")
    return f'"{exe}" "{main_py}"'


def is_enabled() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, VALUE_NAME)
            return True
    except OSError:
        return False


def set_enabled(enable: bool) -> bool:
    """设置开机自启。返回操作后的状态。"""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(k, VALUE_NAME)
                except FileNotFoundError:
                    pass
        return enable
    except OSError:
        return is_enabled()
