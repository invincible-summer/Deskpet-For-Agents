"""Config V4.1 测试（v4plan §18.5）：迁移、原子保存、backup、clamp、
silent-save 禁止、runtime identity 不落盘。"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pet.config as cfgmod
from pet.config import Config, ConfigSaveResult, normalize


class MigrationTests(unittest.TestCase):
    def _load(self, data: dict, path: str) -> Config:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return Config(path)

    def test_v3_config_migrates_preserving_preferences(self):
        v3 = {
            "config_version": 3,
            "skin": "amiya", "scale": 0.75, "speed": 1.5,
            "animated": False, "topmost": False, "tray_enabled": True,
            "pet_pos": [100, 200],
            "bubble": {"enabled": False, "font_size": 13, "width": 320},
            "monitor": {
                "agents": {"codex": True, "claude": False, "kimi": True,
                           "pi": True},
                "windows_enabled": True, "wsl_enabled": False,
                "windows_scan_sec": 4.0, "wsl_scan_sec": 6.0,
                "session_scan_sec": 2.0, "activity_grace_sec": 8.0,
                "terminal_observer": False,
                "pinned": "wsl:Ubuntu|codex|123|tok",
                "gone_grace_sec": 45.0,
            },
            "privacy": {"wsl_root_metadata_fallback": True},
            "animation_cache_mb": 64, "force_state": "walk",
            "convert": {"height": 300, "fps": 15},
        }
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "config.json")
            cfg = self._load(v3, path)
        # 长期偏好保留
        self.assertEqual(cfg.get("skin"), "amiya")
        self.assertEqual(cfg.get("scale"), 0.75)
        self.assertEqual(cfg.get("speed"), 1.5)
        self.assertFalse(cfg.get("animated"))
        self.assertFalse(cfg.get("topmost"))
        self.assertEqual(cfg.get("pet_pos"), [100, 200])
        self.assertFalse(cfg.get("bubble.enabled"))
        self.assertEqual(cfg.get("bubble.font_size"), 13)
        self.assertFalse(cfg.get("monitor.agents.claude"))
        self.assertFalse(cfg.get("monitor.wsl_enabled"))
        self.assertEqual(cfg.get("monitor.activity_grace_sec"), 8.0)
        self.assertFalse(cfg.get("monitor.terminal_observer"))
        self.assertTrue(cfg.get("privacy.wsl_root_metadata_fallback"))
        self.assertEqual(cfg.get("animation_cache_mb"), 64)
        self.assertEqual(cfg.get("force_state"), "walk")
        # V4.1 新增：并发默认关闭（不改变 V3 视觉习惯）
        self.assertEqual(cfg.get("config_version"), 4)
        self.assertFalse(cfg.get("presentation.concurrent.enabled"))
        self.assertEqual(cfg.get("presentation.concurrent.mode"),
                         "aggregate")
        # eligible 默认继承 discovery 开关
        self.assertFalse(cfg.get(
            "presentation.concurrent.eligible_kinds.claude"))
        self.assertEqual(cfg.get("presentation.concurrent.slots")[0]["id"],
                         "pet-1")
        # pinned / gone_grace 被清除
        self.assertIsNone(cfg.get("monitor.pinned"))
        self.assertIsNone(cfg.get("monitor.gone_grace_sec"))

    def test_corrupt_primary_falls_back_to_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{broken json")
            with open(path + ".bak", "w", encoding="utf-8") as f:
                json.dump({"config_version": 4, "skin": "backup-skin"}, f)
            cfg = Config(path)
            self.assertEqual(cfg.get("skin"), "backup-skin")

    def test_both_invalid_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("not json")
            cfg = Config(path)
            self.assertEqual(cfg.get("config_version"), 4)
            self.assertTrue(cfg.get("bubble.enabled"))


class NormalizeTests(unittest.TestCase):
    def test_clamps(self):
        data = normalize({
            "scale": 99.0, "speed": 0.0, "animation_cache_mb": 9999,
            "bubble": {"font_size": 3, "width": 10, "height": 9999,
                       "relative_width": 9},
            "presentation": {"concurrent": {
                "mode": "bogus", "max_targets": 99,
                "eligible_kinds": "junk",
                "slots": [{"id": ""}, {"id": "a"}, {"id": "a"}, "junk"]}},
        })
        self.assertEqual(data["scale"], 2.0)
        self.assertEqual(data["speed"], 0.1)
        self.assertEqual(data["animation_cache_mb"], 256)
        self.assertEqual(data["bubble"]["font_size"], 8)
        self.assertEqual(data["bubble"]["width"], 160)
        self.assertEqual(data["bubble"]["height"], 220)
        self.assertEqual(data["bubble"]["relative_width"], 1.6)
        conc = data["presentation"]["concurrent"]
        self.assertEqual(conc["mode"], "aggregate")
        self.assertEqual(conc["max_targets"], 8)
        # slot id 唯一非空；非法项丢弃后至少保一个默认
        slot_ids = [s["id"] for s in conc["slots"]]
        self.assertEqual(len(slot_ids), len(set(slot_ids)))
        self.assertIn("a", slot_ids)

    def test_monitor_interval_clamps(self):
        """v4.1.1 §11：配置手改异常值不能制造高频 loop/扫描。"""
        data = normalize({
            "monitor": {"file_poll_sec": -1, "windows_scan_sec": 0,
                        "wsl_scan_sec": 999, "session_scan_sec": 0.1,
                        "activity_grace_sec": 0, "active_file_window_sec": 1},
        })
        m = data["monitor"]
        self.assertEqual(m["file_poll_sec"], 0.2)
        self.assertEqual(m["windows_scan_sec"], 1.0)
        self.assertEqual(m["wsl_scan_sec"], 120)
        self.assertEqual(m["session_scan_sec"], 1.0)
        self.assertEqual(m["activity_grace_sec"], 1.0)
        self.assertEqual(m["active_file_window_sec"], 30)
        # 高于下限的合法值保持不变
        data2 = normalize({"monitor": {"file_poll_sec": 1.5,
                                       "windows_scan_sec": 10.0}})
        self.assertEqual(data2["monitor"]["file_poll_sec"], 1.5)
        self.assertEqual(data2["monitor"]["windows_scan_sec"], 10.0)

    def test_runtime_identity_never_loaded(self):
        # 生产管线：load → migrate（drop）→ normalize（clamp）
        data, _ = cfgmod.migrate({
            "config_version": 4,
            "monitor": {"pinned": "wsl:U|codex|1|tok", "gone_grace_sec": 9},
        })
        data = normalize(data)
        self.assertNotIn("pinned", data["monitor"])
        self.assertNotIn("gone_grace_sec", data["monitor"])


class CommitTests(unittest.TestCase):
    def _cfg(self, temp) -> Config:
        path = os.path.join(temp, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"config_version": 4}, f)
        return Config(path)

    def test_commit_writes_and_clears_dirty(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = self._cfg(temp)
            cfg.set("scale", 1.25)
            self.assertTrue(cfg.dirty)
            result = cfg.commit()
            self.assertTrue(result.ok, result.error)
            self.assertFalse(cfg.dirty)
            with open(cfg.path, encoding="utf-8") as f:
                on_disk = json.load(f)
            self.assertEqual(on_disk["scale"], 1.25)

    def test_failed_commit_reports_and_keeps_dirty(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = self._cfg(temp)
            cfg.set("scale", 1.5)
            # 目录变只读（Windows 上用占用句柄模拟失败）
            with patch("pet.config.tempfile.mkstemp",
                       side_effect=OSError("disk full")):
                result = cfg.commit()
            self.assertFalse(result.ok)
            self.assertIn("disk full", result.error)
            self.assertTrue(cfg.dirty)          # dirty 保留：可重试
            self.assertIsInstance(cfg.last_save_result, ConfigSaveResult)

    def test_backup_created_on_valid_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = self._cfg(temp)
            cfg.set("skin", "new")
            cfg.commit()
            self.assertTrue(os.path.isfile(cfg.path + ".bak"))
            with open(cfg.path + ".bak", encoding="utf-8") as f:
                backup = json.load(f)
            self.assertNotIn("skin", backup)    # backup 是上一版内容

    def test_set_and_commit_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = self._cfg(temp)
            result = cfg.set_and_commit("topmost", False)
            self.assertTrue(result.ok)
            self.assertEqual(Config(cfg.path).get("topmost"), False)

    def test_update_many_single_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = self._cfg(temp)
            saves = []
            orig = cfg.commit
            cfg.commit = lambda *a, **k: (saves.append(1), orig(*a, **k))[1]
            cfg.update_many({"scale": 1.0, "speed": 2.0})
            cfg.commit()
            self.assertEqual(len(saves), 1)


class GitIgnoreTests(unittest.TestCase):
    def test_local_artifacts_are_git_ignored(self):
        """config/backup/temp/cache/skin 都不入库（v4plan §18.5-12）。"""
        root = Path(__file__).resolve().parents[1]
        gitignore = (root / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("config.json", "config.json.bak",
                        ".deskpet-config-", "assets/pets/*",
                        "assets/pets/README.md", "assets/cache/"):
            self.assertIn(pattern, gitignore,
                          f".gitignore 缺少 {pattern}")


if __name__ == "__main__":
    unittest.main()
