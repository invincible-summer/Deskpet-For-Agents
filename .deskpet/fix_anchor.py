from pathlib import Path
import sys

p = Path(sys.argv[1])
s = p.read_text(encoding="utf-8")

patches = [
    (
"""replace_once('agents/monitor.py',
'''        self._last_windows = 0.0
        self._last_wsl = 0.0
''',
'''        from .zcode_remote import ZCodeRemoteReader
        self._zcode_remote = ZCodeRemoteReader()
        self._zcode_remote_snapshots: dict[str, object] = {}
        self._last_windows = 0.0
        self._last_wsl = 0.0
''')
""",
"""replace_once('agents/monitor.py',
'''        self._wsl = WslProcessProbe(
            allow_root_metadata=bool(config.get(
                \"privacy.wsl_root_metadata_fallback\", False)))
        self._last_windows = 0.0
        self._last_wsl = 0.0
''',
'''        self._wsl = WslProcessProbe(
            allow_root_metadata=bool(config.get(
                \"privacy.wsl_root_metadata_fallback\", False)))
        from .zcode_remote import ZCodeRemoteReader
        self._zcode_remote = ZCodeRemoteReader()
        self._zcode_remote_snapshots: dict[str, object] = {}
        self._last_windows = 0.0
        self._last_wsl = 0.0
''')
"""
    ),
    (
"""replace_once('agents/monitor.py',
'''    def _tick(self):
''',
'''    def _mark_zcode_remote_stale(self, reason: str, now: float) -> None:
""",
"""replace_once('agents/monitor.py',
'''    def _tick(self):
        cfg_m = self.config.get(\"monitor\") or {}
        now = time.time()
''',
'''    def _mark_zcode_remote_stale(self, reason: str, now: float) -> None:
"""
    ),
    (
"""        return instances, observations, claims

    @staticmethod
    def _root_of''')
replace_once('agents/zcode_desktop.py',
""",
"""        return instances, observations, claims

''')
replace_once('agents/zcode_desktop.py',
"""
    ),
]

for index, (old, new) in enumerate(patches):
    count = s.count(old)
    if count != 1:
        raise SystemExit(f"apply script anchor patch {index} mismatch: {count}")
    s = s.replace(old, new)

needle = """    def _tick(self):
''')
# Clear remote snapshots when Windows source is explicitly disabled.
"""
replacement = """    def _tick(self):
        cfg_m = self.config.get(\"monitor\") or {}
        now = time.time()
''')
# Clear remote snapshots when Windows source is explicitly disabled.
"""
count = s.count(needle)
if count != 1:
    raise SystemExit(f"worker tick tail patch mismatch: {count}")
s = s.replace(needle, replacement)

p.write_text(s, encoding="utf-8")
