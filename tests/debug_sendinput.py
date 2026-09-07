"""调试 SendInput：对比 UNICODE 与 VK 两种方式，检查 GUI 线程焦点。"""
import sys, time, subprocess, ctypes
sys.path.insert(0, r"D:\mycode\program\deskpet")
from actions import winkeys
import ctypes.wintypes as wt

user32 = ctypes.windll.user32
proc = subprocess.Popen(["notepad.exe"])
time.sleep(3)

target = None
for hwnd, pid, title, cls in winkeys.enum_windows():
    if title.strip() and ("记事本" in title or "Notepad" in title):
        target = hwnd
        break
print("hwnd:", target)
winkeys.focus_and_send(target, [])  # 仅聚焦
time.sleep(0.5)

# GUI 线程焦点检查
class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND),
                ("hwndFocus", wt.HWND), ("hwndCapture", wt.HWND),
                ("hwndMenuOwner", wt.HWND), ("hwndMoveSize", wt.HWND),
                ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]
gti = GUITHREADINFO()
gti.cbSize = ctypes.sizeof(GUITHREADINFO)
tid = user32.GetWindowThreadProcessId(target, None)
ok = user32.GetGUIThreadInfo(tid, ctypes.byref(gti))
print("GetGUIThreadInfo ok:", bool(ok), "focus hwnd:", gti.hwndFocus)

# 方式1：VK 'Y'
winkeys._press_vk(0x59)
time.sleep(0.8)
print("VK Y 已发送（请看记事本：应出现 y）")
