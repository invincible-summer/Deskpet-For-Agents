"""Approve 全链路 E2E v2：真实控制台窗口（ConPTY）+ 桌宠批复路径 + 自动批复。
1) start 一个标题为 DeskPetAP 的控制台跑 approve_target.py
2) 伪造 WAITING 快照，手动批复 → 校验结果文件 == approved
3) 开启自动批复 + 第二个等待批复快照 → 校验自动发送
"""
import sys, os, time, subprocess
sys.path.insert(0, r"D:\mycode\program\deskpet")
import tempfile

from pet.config import Config
from pet.app import PetApp
from actions import approver, winkeys
from agents.models import AgentInstance, AgentKind, ApprovalRequest, Snapshot, Status

TMP = os.path.join(tempfile.gettempdir(), "deskpet_ap_e2e")
os.makedirs(TMP, exist_ok=True)
RESULT = os.path.join(TMP, "result.txt")
BAT = os.path.join(TMP, "run_target.bat")
TITLE = "DeskPetAP"
KEY = "windows|codex|4242"

with open(BAT, "w", encoding="utf-8") as f:
    f.write('@echo off\r\ntitle DeskPetAP\r\n"D:\\miniconda3\\envs\\deskpet\\python.exe" '
            '-X utf8 "D:\\mycode\\program\\deskpet\\tests\\approve_target.py" '
            f'"{RESULT}"\r\n')

cfg = Config()
cfg.set("pet_pos", [2100, 1300])
cfg.set("auto_approve.enabled", False)
app = PetApp(cfg)
app.monitor.stop()
app.monitor._tick = lambda: None

results = []
def step(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}", flush=True)
    results.append(cond)

def inject(key, pid):
    inst = AgentInstance(kind=AgentKind.CODEX, pid=pid, source="windows")
    snap = Snapshot(key=key, kind=AgentKind.CODEX, source="windows", pid=pid,
                    status=Status.WAITING, title="E2E 测试",
                    last_line="等待批复：Bash: npm test",
                    mode="按需审批·工作区写入",
                    approval=ApprovalRequest(summary="Bash: npm test", exact=True))
    with app.monitor.lock:
        app.monitor.instances[key] = inst
        app.monitor.snapshots[key] = snap
    app._last_text_sig = ""

def t1():
    # 启动目标控制台（CREATE_NEW_CONSOLE + bat 内 title 命令，避免 start 标题解析坑）
    if os.path.exists(RESULT):
        os.remove(RESULT)
    subprocess.Popen(["cmd", "/c", BAT], creationflags=subprocess.CREATE_NEW_CONSOLE)
    def wait_title(tries=0):
        found = None
        for hwnd, pid, title, cls in winkeys.enum_windows():
            if TITLE in (title or "") and cls != "#32770":
                found = (hwnd, title)
                break
        if not found and tries < 15:
            app.root.after(600, lambda: wait_title(tries + 1))
            return
        if not found:
            step("找到目标控制台窗口", False)
            finish()
            return
        cfg.set("window_instances", {KEY: found[1]})
        inject(KEY, found[0] & 0xFFFF or 4242)
        step("找到目标控制台窗口", True, repr(found[1]))
        def do_manual():
            ok, msg = approver.send_approval(cfg, app.monitor.snapshots[KEY],
                                             "approve", found[1])
            step("手动批复发送成功", ok, msg)
            def check():
                got = open(RESULT, encoding="utf-8").read() if os.path.exists(RESULT) else ""
                step("目标控制台收到批准键(结果=approved)", got == "approved", f"got={got!r}")
                t2()
            app.root.after(1500, check)
        app.root.after(1500, do_manual)
    wait_title()

def t2():
    # 自动批复：换一个目标（重写结果文件），注入第二个 WAITING 快照
    if os.path.exists(RESULT):
        os.remove(RESULT)
    BAT2 = os.path.join(TMP, "run_target2.bat")
    with open(BAT2, "w", encoding="utf-8") as f:
        f.write('@echo off\r\ntitle DeskPetAP2\r\n"D:\\miniconda3\\envs\\deskpet\\python.exe" '
                '-X utf8 "D:\\mycode\\program\\deskpet\\tests\\approve_target.py" '
                f'"{RESULT}"\r\n')
    subprocess.Popen(["cmd", "/c", BAT2], creationflags=subprocess.CREATE_NEW_CONSOLE)
    def wait_title(tries=0):
        found = None
        for hwnd, pid, title, cls in winkeys.enum_windows():
            if "DeskPetAP2" in (title or "") and cls != "#32770":
                found = (hwnd, title)
                break
        if not found and tries < 15:
            app.root.after(600, lambda: wait_title(tries + 1))
            return
        if not found:
            step("自动批复：找到目标2", False)
            finish()
            return
        KEY2 = "windows|codex|4243"
        cfg.set("window_instances", {**cfg.get("window_instances", {}), KEY2: found[1]})
        cfg.set("auto_approve.enabled", True)
        inject(KEY2, 4243)
        step("自动批复：目标2就绪", True)
        def check_auto():
            got = open(RESULT, encoding="utf-8").read() if os.path.exists(RESULT) else ""
            step("自动批复已发送(结果=approved)", got == "approved", f"got={got!r}")
            finish()
        app.root.after(6000, check_auto)
    wait_title()

def finish():
    print("SUMMARY:", sum(results), "/", len(results), flush=True)
    cfg.set("auto_approve.enabled", False)
    app.quit()

app.root.after(2500, t1)
app.run()
