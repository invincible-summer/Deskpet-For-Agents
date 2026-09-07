"""回放真实历史会话文件，人工核对 watcher 的状态推导。"""
import glob
import os
import sys
import time

sys.path.insert(0, r"D:\mycode\program\deskpet")
from agents.claude import ClaudeFile
from agents.codex import CodexFile
from agents.kimi import KimiFile
from agents.paths import instance_roots, session_files, wsl_unc
from agents.models import AgentInstance, AgentKind

users_home = "\\\\wsl.localhost\\Ubuntu\\home\\"
try:
    users = [e.name for e in os.scandir(users_home) if e.is_dir(follow_symlinks=False)]
except OSError:
    users = []
print("WSL users:", users)

inst = AgentInstance(AgentKind.CLAUDE, 0, "wsl:Ubuntu", process_token="0", home=f"/home/{users[0]}" if users else "")


def replay(kind, label, globs, cls):
    found = []
    for pattern in globs:
        for path in glob.glob(pattern)[:300]:
            try:
                found.append((os.path.getmtime(path), path))
            except OSError:
                pass
    found.sort(reverse=True)
    print(f"历史 {label} 会话文件数: {len(found)}")
    if found:
        path = found[0][1]
        print("回放:", path)
        f = cls(path)
        n = 0
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                f.feed(line)
                n += 1
        obs = f.observation(time.time(), {"activity_grace_sec": 10})
        state = (f"{obs.status.value} phase={obs.phase.value if obs.phase else ''}"
                 f" conf={obs.confidence.value}") if obs else "None(→UNKNOWN)"
        print(f"  行数={n} goal={getattr(f, 'goal', '')[:50]!r}")
        print(f"  终态(历史数据): {state}")
        print(f"  mode={getattr(f, 'mode', '')!r} cwd={f.cwd!r}")


home = users[0] if users else "u"
replay(AgentKind.CLAUDE, "claude", [wsl_unc("Ubuntu", f"/home/{home}") + "\\projects\\*\\*.jsonl"], ClaudeFile)
replay(AgentKind.CODEX, "codex", [wsl_unc("Ubuntu", f"/home/{home}") + "\\sessions\\*\\*\\*\\*.jsonl"], CodexFile)
replay(AgentKind.KIMI, "kimi", [wsl_unc("Ubuntu", f"/home/{home}") + "\\.kimi-code\\sessions\\*\\*\\agents\\main\\wire.jsonl",
                                wsl_unc("Ubuntu", f"/home/{home}") + "\\.kimi\\sessions\\*\\*\\wire.jsonl"], KimiFile)
