import sys, os, glob, time
sys.path.insert(0, r"D:\mycode\program\deskpet")
from agents.claude import ClaudeFile
from agents.codex import CodexFile
from agents.paths import wsl_roots, list_wsl_users
from agents.models import AgentKind

users = list_wsl_users("\\\\wsl.localhost\\Ubuntu\\home\\")
print("WSL users:", users)
roots = wsl_roots(AgentKind.CLAUDE, "Ubuntu", users)
found = []
for r in roots:
    for path in glob.glob(os.path.join(r, "*", "*.jsonl"))[:300]:
        try:
            found.append((os.path.getmtime(path), path))
        except OSError:
            pass
found.sort(reverse=True)
print("历史 claude 会话文件数:", len(found))

if found:
    path = found[0][1]
    print("回放:", path)
    f = ClaudeFile(path)
    n = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            f.feed(line)
            n += 1
    print(f"  行数={n} title={f.title!r} last_text={f.last_text[:60]!r} 未闭合tool={len(f.open_tools)}")
    st, ap = f.status(time.time(), {"waiting_quiet_sec": 15})
    print("  终态(历史数据->应为IDLE):", st)

croots = wsl_roots(AgentKind.CODEX, "Ubuntu", users)
cfound = []
for r in croots:
    for path in glob.glob(os.path.join(r, "*", "*", "*", "*.jsonl"))[:200]:
        try:
            cfound.append((os.path.getmtime(path), path))
        except OSError:
            pass
cfound.sort(reverse=True)
print("历史 codex rollout 数:", len(cfound))
if cfound:
    path = cfound[0][1]
    print("回放:", path)
    g = CodexFile(path)
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            g.feed(line)
    print(f"  cwd={g.cwd!r} last_text={g.last_text[:60]!r} last_cmd={g.last_cmd[:60]!r}")
