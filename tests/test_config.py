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
        # V4.3：enabled/mode 不再持久化（session runtime state），
        # 旧值无论是什么都不作为下次启动依据，也不在启动时写盘清除。
        self.assertEqual(cfg.get("config_version"), 5)
        self.assertIsNone(cfg.get("presentation.concurrent.enabled"))
        self.assertIsNone(cfg.get("presentation.concurrent.mode"))
        # eligible 默认继承 discovery 开关
        self.assertFalse(cfg.get(
            "presentation.concurrent.eligible_kinds.claude"))
        self.assertEqual(cfg.get("presentation.concurrent.slots")[0]["id"],
                         "pet-1")
        # v5：slot appearance 规范为 {"skin": None}
        self.assertEqual(
            cfg.get("presentation.concurrent.slots")[0]["appearance"],
            {"skin": None})
        # pinned / gone_grace 被清除
        self.assertIsNone(cfg.get("monitor.pinned"))
        self.assertIsNone(cfg.get("monitor.gone_grace_sec"))

    def test_v5_drops_persisted_enabled_mode_regardless_of_value(self):
        # AC43-PRES-01/AC43-CFG-01：旧 enabled=false / mode=fleet 迁移后
        # 不存在这两个键；slot appearance 规范化且保留未知 future key
        v4 = {
            "config_version": 4,
            "presentation": {"concurrent": {
                "enabled": False, "mode": "fleet", "max_targets": 5,
                "slots": [
                    {"id": "pet-1", "selector": None, "appearance": None,
                     "placement": {"monitor": "", "u": None, "v": None,
                                   "anchor": None, "manual": True}},
                    {"id": "pet-2", "selector": {"kind": "codex"},
                     "appearance": {"skin": "custom-x", "future_key": 7},
                     "placement": {"monitor": "", "u": None, "v": None,
                                   "anchor": None, "manual": False}},
                ]}},
        }
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "config.json")
            cfg = self._load(v4, path)
        self.assertEqual(cfg.get("config_version"), 5)
        self.assertIsNone(cfg.get("presentation.concurrent.enabled"))
        self.assertIsNone(cfg.get("presentation.concurrent.mode"))
        self.assertEqual(cfg.get("presentation.concurrent.max_targets"), 5)
        slots = cfg.get("presentation.concurrent.slots")
        self.assertEqual(slots[0]["appearance"], {"skin": None})
        # appearance dict 只 normalize skin，未知 future key 保留
        self.assertEqual(slots[1]["appearance"]["skin"], "custom-x")
        self.assertEqual(slots[1]["appearance"]["future_key"], 7)
        # selector/placement 不被修改
        self.assertEqual(slots[1]["selector"], {"kind": "codex"})
        self.assertTrue(slots[0]["placement"]["manual"])

    def test_ensure_fleet_slots_extends_never_shrinks(self):
        # AC43-SKIN-05：lowering max_targets 不删除 dormant slot
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "config.json")
            cfg = self._load({"config_version": 5}, path)
            changed = cfg.ensure_fleet_slots(4)
            self.assertTrue(changed)
            ids = [s["id"] for s in cfg.get("presentation.concurrent.slots")]
            self.assertEqual(ids, ["pet-1", "pet-2", "pet-3", "pet-4"])
            # 已存在时不重复、不删除
            self.assertFalse(cfg.ensure_fleet_slots(2))
            ids = [s["id"] for s in cfg.get("presentation.concurrent.slots")]
            self.assertEqual(ids, ["pet-1", "pet-2", "pet-3", "pet-4"])
            # 提高后恢复
            cfg.ensure_fleet_slots(6)
            ids = [s["id"] for s in cfg.get("presentation.concurrent.slots")]
            self.assertEqual(ids, ["pet-1", "pet-2", "pet-3", "pet-4",
                                   "pet-5", "pet-6"])
            # count clamp 1..8
            cfg.ensure_fleet_slots(99)
            self.assertEqual(len(cfg.get("presentation.concurrent.slots")), 8)

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
            self.assertEqual(cfg.get("config_version"), 5)
            self.assertTrue(cfg.get("bubble.enabled"))


class NormalizeTests(unittest.TestCase):
    def test_clamps(self):
        data = normalize({
            "scale": 99.0, "speed": 0.0, "animation_cache_mb": 9999,
            "bubble": {"font_size": 3, "width": 10, "height": 9999,
                       "relative_width": 9},
            "presentation": {"concurrent": {
                "enabled": True, "mode": "bogus", "max_targets": 99,
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
        # v4.3：enabled/mode 不是持久化字段，normalize 直接移除
        self.assertNotIn("enabled", conc)
        self.assertNotIn("mode", conc)
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


class SchemaHardeningTests(unittest.TestCase):
    """v4.3.1 DP43-R10 §18.7：malformed-but-valid JSON 不让 Config load
    崩溃；schema 恢复；config_version==5；runtime keys 仍被清除。"""

    BAD_VALUES = (None, "bad", [], 123, True)

    def _load(self, payload) -> Config:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            return Config(path)

    def test_wrong_container_types_never_crash_and_schema_restored(self):
        cases = [
            ("presentation", "bad"),
            {"presentation": "bad"},
            {"bubble": []},
            {"monitor": "x"},
            {"monitor": {"agents": 12}},
            {"presentation": {"concurrent": 123}},
            {"presentation": {"concurrent": {"eligible_kinds": "all"}}},
            {"privacy": 12},
            {"convert": True},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                cfg = self._load(payload)   # 不抛 = 通过
                data = cfg.data
                self.assertIsInstance(data["bubble"], dict)
                self.assertIsInstance(data["monitor"], dict)
                self.assertIsInstance(data["monitor"]["agents"], dict)
                self.assertIsInstance(data["presentation"], dict)
                self.assertIsInstance(
                    data["presentation"]["concurrent"], dict)
                self.assertIsInstance(
                    data["presentation"]["concurrent"]["eligible_kinds"],
                    dict)
                self.assertIsInstance(
                    data["presentation"]["concurrent"]["slots"], list)
                self.assertIsInstance(data["privacy"], dict)
                self.assertIsInstance(data["convert"], dict)
                self.assertEqual(data["config_version"],
                                 cfgmod.CONFIG_VERSION)
                # runtime keys 仍被清除（不因异常路径复活）
                self.assertNotIn("enabled",
                                 data["presentation"]["concurrent"])
                self.assertNotIn("mode",
                                 data["presentation"]["concurrent"])
                self.assertNotIn("pinned", data["monitor"])

    def test_nested_field_bad_values_clamped_to_bounds(self):
        for bad in self.BAD_VALUES:
            with self.subTest(bad=bad):
                cfg = self._load({
                    "config_version": 5,
                    "bubble": {"relative_font": bad, "max_lines": bad,
                               "autohide_sec": bad, "font_family": bad,
                               "enabled": bad if bad is not None else None},
                    "force_state": bad,
                    "convert": {"fps": bad, "height": bad},
                    "animated": bad,
                })
                b = cfg.data["bubble"]
                # None/"bad"/[] → 下界；123/True → clamp/强制类型后的
                # 合法 in-bounds 数值：任何输入都不越界
                self.assertGreaterEqual(b["relative_font"], 0.5)
                self.assertLessEqual(b["relative_font"], 2.0)
                self.assertGreaterEqual(b["max_lines"], 1)
                self.assertLessEqual(b["max_lines"], 6)
                self.assertGreaterEqual(b["autohide_sec"], 0)
                self.assertLessEqual(b["autohide_sec"], 3600)
                self.assertIsInstance(b["font_family"], str)
                self.assertTrue(b["font_family"])
                self.assertIsInstance(b["enabled"], bool)
                self.assertEqual(cfg.data["force_state"], "")
                self.assertGreaterEqual(cfg.data["convert"]["fps"], 1)
                self.assertLessEqual(cfg.data["convert"]["fps"], 30)
                self.assertGreaterEqual(cfg.data["convert"]["height"], 96)
                self.assertLessEqual(cfg.data["convert"]["height"], 960)
                self.assertIsInstance(cfg.data["animated"], bool)

    def test_valid_unknown_future_keys_preserved_in_mappings(self):
        cfg = self._load({
            "config_version": 5,
            "bubble": {"future_bubble_key": {"keep": 1}},
            "privacy": {"future_priv": "keep"},
        })
        self.assertEqual(cfg.data["bubble"]["future_bubble_key"],
                         {"keep": 1})
        self.assertEqual(cfg.data["privacy"]["future_priv"], "keep")

    def test_force_state_valid_values_preserved(self):
        for good in ("", "walk", "attack", "die", "special", "sleep"):
            cfg = self._load({"config_version": 5, "force_state": good})
            self.assertEqual(cfg.data["force_state"], good)
        cfg = self._load({"config_version": 5, "force_state": "hacked"})
        self.assertEqual(cfg.data["force_state"], "")


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
