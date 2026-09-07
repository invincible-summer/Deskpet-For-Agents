"""批复目标模拟：真实控制台窗口里等待单键确认（与真实 CLI 的 TUI 审批一致）。
用法：python approve_target.py <结果文件路径>
y=approved, n/Esc=rejected。打印 PROMPT READY 供测试轮询。"""
import sys, time, msvcrt

out = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\32525\AppData\Local\Temp\ap_result.txt"
print("PROMPT READY", flush=True)
print("approve? (y/n): ", flush=True)
result = "timeout"
for _ in range(600):          # 最多等 60s
    k = msvcrt.getch()
    if k in (b"y", b"Y"):
        result = "approved"
        break
    if k in (b"n", b"N", b"\x1b"):
        result = "rejected"
        break
with open(out, "w", encoding="utf-8") as f:
    f.write(result)
print("RESULT WRITTEN", flush=True)
time.sleep(2)
