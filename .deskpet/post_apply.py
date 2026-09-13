from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    data = p.read_bytes()
    newline = "\r\n" if b"\r\n" in data else "\n"
    old_b = old.replace("\n", newline).encode("utf-8")
    new_b = new.replace("\n", newline).encode("utf-8")
    count = data.count(old_b)
    if count != 1:
        raise SystemExit(f"{path}: expected one match, found {count}: {old!r}")
    p.write_bytes(data.replace(old_b, new_b, 1))


replace_once(
    "agents/discovery.py",
    '''        if "/.zcode/server/agents/glm/zcode.cjs" in low:\n            return "agent"\n        if "/.zcode/server/zcode-server.cjs" in low:\n''',
    '''        if ("/.zcode/server/agents/glm/zcode.cjs" in low\n                or "/.zcode/server/agents/glm/zcode-agent" in low):\n            return "agent"\n        if "/.zcode/server/zcode-server.cjs" in low:\n''',
)

replace_once(
    "tests/test_zcode_wsl_remote.py",
    '''        self.assertEqual(p._match_zcode_remote_runtime(\n            "node", "/home/u/.zcode/server/agents/glm/zcode.cjs"), "agent")\n        self.assertIsNone(p._match_zcode_remote_runtime(\n''',
    '''        self.assertEqual(p._match_zcode_remote_runtime(\n            "node", "/home/u/.zcode/server/agents/glm/zcode.cjs"), "agent")\n        self.assertEqual(p._match_zcode_remote_runtime(\n            "zcode-agent", "/home/u/.zcode/server/agents/glm/zcode-agent"), "agent")\n        self.assertIsNone(p._match_zcode_remote_runtime(\n''',
)

replace_once(
    "tests/test_release_layout.py",
    '        self.assertEqual(APP_VERSION, "4.0.0")\n        self.assertEqual(APP_LABEL, "DeskPet V4.0.0")\n',
    '        self.assertEqual(APP_VERSION, "4.0.1")\n        self.assertEqual(APP_LABEL, "DeskPet V4.0.1")\n',
)
