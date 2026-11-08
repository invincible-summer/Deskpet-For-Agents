"""v4.4.0 DSH 插件分发契约测试（dsh-plugin/）。

插件是 DeskPet 的安装/启动载体，契约要点（AGENTS.md 架构纪律）：
  * AC-DP-01：npm 清单合法且 npm-ready（dsh.bundle.patch、engines.dsh、
    零运行时依赖、非 private），版本与 pet.version.APP_VERSION 单源同步；
  * AC-DP-02：cordis.patch.yml 单条 insert 行 id/name 与包名一致，
    config 键与 index.js 防御性默认一致（autoStart/exePath/releaseTag）；
  * AC-DP-03：index.js 卫生——win32 平台守卫、apply 全程不抛出（上游
    契约：apply 失败会杀死 DSH 宿主进程）、仅 node: 内置模块 import、
    detached 启动、显式传递 DSH_HOME；
  * AC-DP-04：插件绝不触碰 Agent 数据——不出现 .credentials.yaml、
    不读取 sessions 目录；下载工件名与 tools/build_release.py 产物名
    严格一致；
  * AC-DP-05：文档同步——README（方式 C + 监听边界）、CHANGELOG、
    AGENTS.md（上游对齐行 + 架构纪律）、CI node --check 步骤。
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PLUGIN_DIR = ROOT / "dsh-plugin"
PACKAGE_NAME = "dsh-deskpet"
ROW_ID = "deskpet"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


class DshPluginManifestTests(unittest.TestCase):
    """AC-DP-01：package.json 合法、npm-ready、版本单源同步。"""

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(_read(PLUGIN_DIR / "package.json"))

    def test_manifest_core_fields(self):
        m = self.manifest
        self.assertEqual(m["name"], PACKAGE_NAME)
        self.assertEqual(m["type"], "module")
        self.assertEqual(m["main"], "index.js")
        self.assertEqual(m["dsh"]["bundle"]["patch"], "./cordis.patch.yml")
        # 上游 publish.md：用 engines.dsh SemVer 范围做安装期兼容检查
        self.assertRegex(m["engines"]["dsh"], r"^>=\d+\.\d+\.\d+ <\d+\.\d+\.\d+$")

    def test_manifest_npm_ready_and_zero_dependencies(self):
        m = self.manifest
        # npm-ready：非 private；零运行时依赖（纯 Node 内置模块实现）
        self.assertNotEqual(m.get("private", False), True)
        for key in ("dependencies", "devDependencies", "peerDependencies",
                    "optionalDependencies"):
            self.assertNotIn(key, m, f"插件不应声明 {key}（零依赖契约）")
        # 发布白名单包含全部插件文件
        for name in ("index.js", "cordis.patch.yml", "README.md"):
            self.assertIn(name, m["files"])
        for name in m["files"]:
            self.assertTrue((PLUGIN_DIR / name).exists(), f"files 含缺失文件 {name}")
        self.assertIn(".", m["exports"])
        self.assertEqual(m["exports"]["."], "./index.js")

    def test_version_matches_app_version_single_source(self):
        from pet.version import APP_VERSION
        self.assertEqual(self.manifest["version"], APP_VERSION,
                         "插件包版本必须与 pet/version.py::APP_VERSION 同步")


class DshPluginPatchTests(unittest.TestCase):
    """AC-DP-02：cordis.patch.yml 行结构与代码默认一致。"""

    @classmethod
    def setUpClass(cls):
        cls.patch_text = _read(PLUGIN_DIR / "cordis.patch.yml")

    def test_single_insert_row_references_package_name(self):
        # 单条 insert 行；id/name 与包名一致（name 是插件包本身）
        self.assertEqual(self.patch_text.count("- id:"), 1)
        self.assertIn(f"- id: {ROW_ID}", self.patch_text)
        self.assertIn(f"name: '{PACKAGE_NAME}'", self.patch_text)

    def test_config_keys_match_code_defaults(self):
        index = _read(PLUGIN_DIR / "index.js")
        # patch 行声明的 config 键 == index.js readConfig 支持的键
        for key in ("autoStart", "exePath", "releaseTag"):
            self.assertIn(key, self.patch_text)
            self.assertIn(key, index)
        # patch 默认值与代码默认一致
        self.assertIn("autoStart: true", self.patch_text)
        self.assertIn("'latest'", self.patch_text)
        self.assertIn("let releaseTag = 'latest'", index)


class DshPluginCodeHygieneTests(unittest.TestCase):
    """AC-DP-03/04：index.js 防御性与数据面边界。"""

    @classmethod
    def setUpClass(cls):
        cls.index = _read(PLUGIN_DIR / "index.js")

    def test_win32_guard_and_never_throw_contract(self):
        # 上游契约：apply 抛出会杀死 DSH 宿主 → 必须有平台守卫 + 全包 try/catch
        self.assertIn("process.platform !== 'win32'", self.index)
        self.assertRegex(
            self.index,
            r"export function apply\(ctx, rawConfig\) \{\s*\n\s*try \{",
        )
        self.assertIn("// Never propagate: a failing apply kills the DSH host process.",
                      self.index)
        # 失败只经 ctx.logger 输出
        self.assertIn("ctx.logger", self.index)

    def test_only_node_builtin_imports(self):
        # 零依赖契约：所有 import 说明符必须是 node: 内置模块
        specifiers = re.findall(r"from '([^']+)'", self.index)
        specifiers += re.findall(r"await import\('([^']+)'\)", self.index)
        self.assertTrue(specifiers)
        for spec in specifiers:
            self.assertTrue(spec.startswith("node:"),
                            f"非内置模块 import：{spec}")

    def test_detached_launch_propagates_dsh_home(self):
        # DeskPet 常驻：detached + unref，不注册 dispose 杀进程；
        # 显式传递 DSH_HOME，让非默认 home 的宿主也正确解析数据根。
        self.assertIn("detached: true", self.index)
        self.assertIn("child.unref()", self.index)
        self.assertIn("DSH_HOME: home", self.index)
        self.assertIn("dshHomeDir", self.index)

    def test_real_machine_regressions_covered(self):
        # 2026-11-08 实机验收修复的两个 Windows 坑必须有契约锁定：
        # 1) bsdtar 必须走 System32 绝对路径（PATH 可能解析到 Git 的
        #    GNU tar，读不了 ZIP）；且命令行不得出现绝对路径（盘符冒号
        #    会被解析成 rsh 远程主机）。
        # 2) 宿主 stderr 平时不可见，安装过程须留有界文件日志。
        self.assertIn("function windowsTarPath()", self.index)
        self.assertIn("'System32', 'tar.exe'", self.index)
        self.assertIn("path.basename(zipPath), '-C', extractDirName", self.index)
        self.assertNotIn("-xf', zipPath", self.index)
        self.assertIn("const LOG_NAME = 'deskpet-installer.log'", self.index)
        self.assertIn("MAX_LOG_BYTES", self.index)

    def test_never_touches_agent_owned_data(self):
        # AC-DP-04：插件不读 Agent 数据、不碰凭据
        self.assertNotIn(".credentials.yaml", self.index)
        self.assertNotIn("'sessions'", self.index)
        self.assertNotIn('"sessions"', self.index)

    def test_artifact_names_match_release_build(self):
        # 下载工件名必须与 tools/build_release.py 产物名严格一致，防漂移
        build = _read(ROOT / "tools" / "build_release.py")
        zip_name = re.search(r'ZIP_PATH = WORK / "([^"]+)"', build).group(1)
        sums_name = re.search(r'SUMS = WORK / "([^"]+)"', build).group(1)
        self.assertIn(f'const ZIP_NAME = \'{zip_name}\'', self.index)
        self.assertIn(f'const SUMS_NAME = \'{sums_name}\'', self.index)
        self.assertIn(f'const EXE_NAME = \'{zip_name.split("-")[0]}.exe\'',
                      self.index)

    def test_no_personal_absolute_paths(self):
        # 拼接构造，避免本测试文件自身含被禁字面量（AC-REL-01 扫描范围）
        banned = ("D:" + "\\" + "miniconda3", "D:/" + "miniconda3",
                  "D:/" + "mycode", "C:/" + "Users", "32" + "525")
        for token in banned:
            self.assertNotIn(token, self.index)


class DshPluginDocsSyncTests(unittest.TestCase):
    """AC-DP-05：仓库文档与 CI 同步提及插件渠道。"""

    def test_readme_documents_plugin_channel(self):
        readme = _read(ROOT / "README.md")
        self.assertIn("方式 C：通过 DSH 插件安装", readme)
        self.assertIn("dsh-plugin/README.md", readme)
        # 监听边界声明插件不改变被动只读边界
        self.assertIn("不订阅 Host 事件、不读会话数据", readme)

    def test_changelog_and_versioning_synced(self):
        changelog = _read(ROOT / "CHANGELOG.md")
        self.assertIn("v4.4.0 DSH 插件分发渠道", changelog)
        versioning = _read(ROOT / "VERSIONING.md")
        self.assertIn("**4.4.0**", versioning)

    def test_agents_md_alignment_refreshed(self):
        agents = _read(ROOT / "AGENTS.md")
        # 上游对齐行刷新到本轮证据；架构纪律声明插件仅为分发载体
        self.assertIn("fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159", agents)
        self.assertIn("distribution vehicle only", agents)

    def test_ci_runs_node_syntax_check(self):
        ci = _read(ROOT / ".github" / "workflows" / "test.yml")
        self.assertIn("node --check dsh-plugin/index.js", ci)

    def test_plugin_readme_present_and_honest(self):
        text = _read(PLUGIN_DIR / "README.md")
        for needed in ("dsh plugin --profile desktop add", "autoStart",
                       "exePath", "releaseTag", "SHA-256", "不会",
                       "$DSH_HOME/deskpet"):
            self.assertIn(needed, text)


if __name__ == "__main__":
    unittest.main()
