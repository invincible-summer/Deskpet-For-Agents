"""v4.2.3 §10/§12 公开发行 layout 与 builtin-cat fallback 测试。

  * AC-REL-01：仓库不含个人绝对路径（个人 miniconda 路径等）与旧 launcher；
  * AC-REL-04：Start 脚本不安装/不联网；Setup 一次性安装 + constraints；
  * AC-REL-05：fresh config 默认 builtin-cat，无用户素材也能生成五状态；
  * AC-REL-06：已有自定义 skin config 不被迁移覆盖。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_PERSONAL_MINICONDA = "D:" + os.sep + "miniconda3"
_PERSONAL_MINICONDA_ALT = "D:/miniconda3"


class ReleaseLayoutTests(unittest.TestCase):
    def test_no_personal_absolute_paths_in_tracked_sources(self):
        # AC-REL-01：仓库不再含个人开发机路径（本测试文件自身除外——
        # 它需要字面量定义被禁 token）。
        # v4.3.1 §20.5：目标扩大到 pet/agents/actions/tools 全部 py、
        # tests 全部 py（不只 test_*.py）、README/SourceLink 与双 launcher。
        banned = (_PERSONAL_MINICONDA, _PERSONAL_MINICONDA_ALT, "启动桌宠.bat")
        targets = (list((ROOT / "pet").glob("*.py"))
                   + list((ROOT / "agents").glob("*.py"))
                   + list((ROOT / "actions").glob("*.py"))
                   + list((ROOT / "tools").glob("*.py"))
                   + list((ROOT / "tests").glob("*.py"))
                   + [ROOT / "README.md", ROOT / "SourceLink.md",
                      ROOT / "main.py", ROOT / "Start-Desktop.bat",
                      ROOT / "Setup-Desktop.bat"])
        targets = [p for p in targets if p != Path(__file__).resolve()]
        for path in targets:
            text = path.read_text(encoding="utf-8", errors="replace")
            for token in banned:
                self.assertNotIn(token, text,
                                 f"{path.name} 仍含 {token}")

    def test_old_launcher_removed_and_new_scripts_present(self):
        # AC-REL-01/02：旧中文 launcher 删除；新双脚本存在
        self.assertFalse((ROOT / "启动桌宠.bat").exists())
        start = ROOT / "Start-Desktop.bat"
        setup = ROOT / "Setup-Desktop.bat"
        self.assertTrue(start.exists())
        self.assertTrue(setup.exists())

    def test_start_script_never_installs(self):
        # AC-REL-04：Start 只启动；有效命令行里不得出现 pip/conda 安装
        text = (ROOT / "Start-Desktop.bat").read_text(encoding="utf-8")
        active = "\n".join(line for line in text.splitlines()
                           if not line.strip().lower().startswith("rem"))
        low = active.lower()
        self.assertNotIn("-m pip", low)
        self.assertNotIn("conda", low)
        self.assertIn(".venv", active)
        self.assertIn('start ""', active)   # quoted empty title 合同

    def test_setup_script_is_one_shot_with_constraints(self):
        text = (ROOT / "Setup-Desktop.bat").read_text(encoding="utf-8")
        self.assertIn("constraints-v4.3.0.txt", text)
        self.assertIn("3.12", text)
        self.assertIn("venv", text.lower())
        self.assertNotIn("conda", text.lower())
        # smoke import 检查存在
        self.assertIn("pet.config", text)

    def test_existing_venv_branch_validates_python_312(self):
        # v4.3.1 DP43-R12：existing .venv 分支也必须验证 Python 3.12
        # （只检查"脚本里出现 3.12"不够——reusing 分支要真正执行探测）
        text = (ROOT / "Setup-Desktop.bat").read_text(encoding="utf-8")
        reusing = text.split("if exist", 1)[1]
        reusing = reusing.split(":createvenv", 1)[0]
        self.assertIn("Reusing existing", reusing)
        self.assertIn("sys.version_info[:2] == (3,12)", reusing)
        self.assertIn("errorlevel 1", reusing)
        # 不自动删除用户环境
        self.assertIn("rename or delete", reusing)

    def test_docs_match_v43_runtime_policy(self):
        # v4.3.1 DP43-R13：README/toast 与 v4.3 runtime policy 一致
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("并发呈现（手动开启）", readme)
        self.assertIn("并行监听默认开启", readme)
        self.assertIn("pet-1 idle fallback", readme)
        app_src = (ROOT / "pet" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("并发需手动开启", app_src)
        self.assertIn("启动默认并行监听 + 单宠聚合", app_src)
        sourcelink = (ROOT / "SourceLink.md").read_text(encoding="utf-8")
        self.assertNotIn("v4.2.3 — SourceLink", sourcelink)
        self.assertIn("39941c2", sourcelink)

    def test_constraints_file_pins_verified_set(self):
        path = ROOT / "constraints-v4.3.0.txt"
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        for pin in ("psutil==7.2.2", "Pillow==12.3.0", "comtypes==1.4.16",
                    "imageio-ffmpeg==0.6.0", "numpy==2.5.3", "scipy==1.18.1"):
            self.assertIn(pin, text)

    def test_version_module_is_single_source(self):
        from pet.version import APP_LABEL, APP_NAME, APP_VERSION
        self.assertEqual(APP_NAME, "DeskPet")
        self.assertEqual(APP_VERSION, "4.3.1")
        self.assertEqual(APP_LABEL, "DeskPet V4.3.1")
        import pet.dashboard as dashboard
        self.assertEqual(dashboard.APP_VERSION, APP_LABEL)


class BuiltinSkinFallbackTests(unittest.TestCase):
    def test_defaults_skin_is_builtin_and_list_includes_it(self):
        from pet.config import DEFAULTS
        from pet.skins import BUILTIN_SKIN, list_skins
        self.assertEqual(DEFAULTS["skin"], BUILTIN_SKIN)
        skins = list_skins()
        self.assertIn(BUILTIN_SKIN, skins)
        self.assertTrue(skins[BUILTIN_SKIN].get("builtin"))

    def test_build_builtin_skin_generates_five_states_without_converter(self):
        # AC-REL-05：无 assets/pets 用户素材也能生成五状态轻量 GIF
        from pet import skins
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(skins, "CACHE_DIR", tmp):
                paths = skins.build_skin(skins.BUILTIN_SKIN, 240, 12)
                self.assertEqual(set(paths), set(skins.STATES))
                for state, gif in paths.items():
                    self.assertTrue(os.path.isfile(gif), state)
                    self.assertTrue(os.path.isfile(gif + ".json"), state)
                    with open(gif + ".json", encoding="utf-8") as f:
                        meta = json.load(f)
                    self.assertEqual(meta["frames"], 1)
                    self.assertGreater(meta["width"], 0)
                # 复用缓存：再次 build 不重复生成（mtime 不变）
                first = os.path.getmtime(paths["walk"])
                paths2 = skins.build_skin(skins.BUILTIN_SKIN, 240, 12)
                self.assertEqual(paths2["walk"], paths["walk"])
                self.assertEqual(os.path.getmtime(paths2["walk"]), first)

    def test_existing_custom_skin_not_overwritten_by_migration(self):
        # AC-REL-06：已有自定义 skin 原样保留
        from pet.config import migrate
        from pet.skins import BUILTIN_SKIN
        loaded = {
            "config_version": 4,
            "skin": "my-own-skin",
            "presentation": {"concurrent": {
                "enabled": True, "mode": "fleet", "max_targets": 3,
                "eligible_kinds": {}, "slots": [
                    {"id": "pet-1", "selector": None, "appearance": None,
                     "placement": {"monitor": "", "u": None, "v": None,
                                   "anchor": None, "manual": False}}]}},
        }
        out, _migrated = migrate(loaded)
        self.assertEqual(out["skin"], "my-own-skin")
        # 旧 "default" 皮肤名 → builtin（不再隐含 amiya）
        out2, _ = migrate({**loaded, "skin": "default"})
        self.assertEqual(out2["skin"], BUILTIN_SKIN)

    def test_petview_fallback_is_builtin_not_amiya(self):
        # desired_build_key 的最终 fallback 是 BUILTIN_SKIN（§10.4）；
        # 生产源码不再出现隐藏 amiya 默认值。
        text = (ROOT / "pet" / "petview.py").read_text(encoding="utf-8")
        self.assertNotIn('"amiya"', text)
        self.assertIn("BUILTIN_SKIN", text)
        app_src = (ROOT / "pet" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn('"amiya"', app_src)
        dash_src = (ROOT / "pet" / "dashboard.py").read_text(encoding="utf-8")
        self.assertNotIn('"amiya"', dash_src)
        cfg_src = (ROOT / "pet" / "config.py").read_text(encoding="utf-8")
        self.assertNotIn('"amiya"', cfg_src)


if __name__ == "__main__":
    unittest.main()
