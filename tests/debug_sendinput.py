"""调试终端窗口绑定：只检查枚举、身份校验和前台唤起，不发送输入。"""
import sys
import time
sys.path.insert(0, r"D:\mycode\program\deskpet")
from actions import winkeys

windows = winkeys.terminal_candidates()
print("候选终端:", len(windows))
for hwnd, pid, title, cls in windows[:20]:
    print(f"  hwnd={hwnd} pid={pid} class={cls!r} title={title!r}")

if windows:
    hwnd, pid, title, cls = windows[0]
    print("身份:", winkeys.window_identity(hwnd))
    print("唤起结果:", winkeys.raise_window(hwnd))
    time.sleep(0.5)
else:
    print("没有可检查的终端窗口")
