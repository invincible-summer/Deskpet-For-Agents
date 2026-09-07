"""summarize 单元测试 + 导入自检。"""
import sys
sys.path.insert(0, r"D:\mycode\program\deskpet")
from agents.summarize import shorten, fmt_command

assert shorten("# 标题**加粗**", 50) == "标题加粗", shorten("# 标题**加粗**", 50)
assert shorten("第一行\n第二行", 50) == "第一行"
assert shorten("", 50) == ""
long_text = "这是一条很长的输出" * 20
s = shorten(long_text, 40)
assert len(s) <= 41 and s.endswith("…"), (len(s), s)
assert fmt_command(["bash", "-lc", "npm test"]) == "bash -lc npm test"
c = fmt_command(["bash", "-lc", "x" * 200], 30)
assert len(c) <= 30 and "…" in c, (len(c), c)
import agents.claude, agents.codex, agents.kimi, agents.pi  # noqa
import agents.summarize as S  # noqa
import pet.app, pet.tray, pet.autostart  # noqa
print("summarize + imports OK")
