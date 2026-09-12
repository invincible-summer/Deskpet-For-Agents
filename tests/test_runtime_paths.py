from __future__ import annotations

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
