"""Read-only sessions can activate their terminal, never synthesize approval keys."""
from . import winkeys


def send_approval(config,snapshot,action,saved_title=None,restore_focus=None):
    return False,'只读会话不能批复，请打开终端处理；可控会话通过官方协议批复'


def raise_terminal(config,snapshot,saved_title=None):
    hwnd=winkeys.find_terminal_window(snapshot.key,snapshot.pid,snapshot.source,[],saved_title)
    if not hwnd:
        return False,'无法唯一定位终端，请选择并绑定窗口（多标签终端仅定位宿主窗口）'
    ok=winkeys.raise_window(hwnd)
    return ok,('已唤起终端' if ok else 'Windows 未允许切换焦点，已提醒任务栏；请点击目标终端')
