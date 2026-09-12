from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Config docstring: keep a literal escaped backslash in Python source.
p = ROOT / "pet" / "config.py"
text = p.read_text(encoding="utf-8")
text = text.replace("%LOCALAPPDATA%\\DeskPet；程序目录不写配置/素材/cache；",
                    "%LOCALAPPDATA%\\\\DeskPet；程序目录不写配置/素材/cache；")
p.write_text(text, encoding="utf-8", newline="\n")

# Keep SettingsPage short: local-data controls share the existing save panel instead
# of adding an eighth vertically-expensive SurfacePanel.
p = ROOT / "pet" / "dashboard.py"
text = p.read_text(encoding="utf-8")
old = '''        data = SurfacePanel(body)
        data.pack(fill="x", pady=(0, SECTION_GAP))
        _section_title(data.body, "本地数据")
        tk.Label(data.body, text=str(runtime_paths.data_root),
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(data, 9), anchor="w").pack(fill="x")
        drow = tk.Frame(data.body, bg=LIGHT.surface)
        drow.pack(fill="x", pady=(6, 0))
        ttk.Button(drow, text="打开数据目录",
                   command=self._open_data_root).pack(side="left")
        InfoButton(drow,
                   "配置、导入皮肤与生成缓存统一保存在此目录；普通设置请直接在 Dashboard 修改。",
                   self.dash.tooltip).pack(side="left", padx=(6, 0))

'''
if old not in text:
    raise RuntimeError("dashboard local-data panel block not found")
text = text.replace(old, "", 1)
needle = '''        tk.Label(savep.body,
                 text=f"配置文件位置（只读展示）：{config_path}",
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(savep, 9)).pack(anchor="w", pady=(6, 0))
'''
insert = needle + '''        drow = tk.Frame(savep.body, bg=LIGHT.surface)
        drow.pack(fill="x", pady=(5, 0))
        tk.Label(drow, text=f"本地数据：{runtime_paths.data_root}",
                 bg=LIGHT.surface, fg=LIGHT.text_secondary,
                 font=pick_font(savep, 9), anchor="w").pack(side="left", fill="x", expand=True)
        ttk.Button(drow, text="打开数据目录",
                   command=self._open_data_root).pack(side="right")
'''
if needle not in text:
    raise RuntimeError("dashboard config path label not found")
text = text.replace(needle, insert, 1)
p.write_text(text, encoding="utf-8", newline="\n")

# README was produced from a raw Python literal where Windows slashes were doubled.
p = ROOT / "README.md"
text = p.read_text(encoding="utf-8")
while "\\\\" in text:
    text = text.replace("\\\\", "\\")
p.write_text(text, encoding="utf-8", newline="\n")

# Runtime path tests must not mutate os.name on a live Windows interpreter; patch the
# leaf resolver instead, which is the intended test seam.
(ROOT / "tests" / "test_runtime_paths.py").write_text(r'''from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pet import runtime_paths


class RuntimePathsTests(unittest.TestCase):
    def tearDown(self):
        runtime_paths._PATHS = None

    def _paths(self, td: str):
        runtime_paths._PATHS = None
        with patch.object(runtime_paths, "_known_local_app_data",
                          return_value=Path(td)):
            return runtime_paths.get_runtime_paths()

    def test_data_root_never_uses_program_root(self):
        with tempfile.TemporaryDirectory() as td:
            paths = self._paths(td)
            self.assertEqual(paths.data_root, Path(td) / "DeskPet")
            self.assertNotEqual(paths.data_root, paths.program_root)

    def test_layout_contract(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._paths(td)
            self.assertEqual(p.config_file, p.data_root / "config.json")
            self.assertEqual(p.config_backup, p.data_root / "config.json.bak")
            self.assertEqual(p.pets_dir, p.data_root / "assets" / "pets")
            self.assertEqual(p.cache_dir, p.data_root / "assets" / "cache")
            self.assertEqual(p.runtime_icon, p.data_root / "icon.ico")

    def test_ensure_asset_dirs_is_lazy_and_idempotent(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(runtime_paths, "_known_local_app_data",
                          return_value=Path(td)):
            runtime_paths._PATHS = None
            p = runtime_paths.ensure_asset_dirs()
            self.assertTrue(p.pets_dir.is_dir())
            self.assertTrue(p.cache_dir.is_dir())
            runtime_paths.ensure_asset_dirs()
''', encoding="utf-8", newline="\n")

print("post-patch fixes applied")
