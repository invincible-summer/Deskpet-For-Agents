"""v4.3 Phase 2：per-slot skin / AppearanceController / build waiter set。

覆盖 AC43-SKIN-01..10 与 AC43-ANIM-09（无进程内 converter fallback）。
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.models import AgentInstance, AgentKind, Snapshot, Status
from pet.presentation import PresentationMode
from pet.skins import BUILTIN_SKIN, SkinBuildManager

NOW = 1_000_000.0


class FleetConfig:
    def __init__(self, slots):
        self.data = {
            "skin": BUILTIN_SKIN, "scale": 1.0, "speed": 1.0,
            "animated": True, "topmost": False, "tray_enabled": False,
            "pet_pos": None,
            "bubble": {"enabled": True, "font_family": "MS Sans Serif",
                       "font_size": 11, "height": 132, "width": 300,
                       "relative_width": 1.0, "relative_height": 1.0,
                       "relative_font": 1.0},
            "animation_cache_mb": 8,
            "presentation": {"concurrent": {
                "max_targets": 8,
                "eligible_kinds": {"codex": True, "claude": True,
                                   "kimi": True, "pi": True},
                "slots": slots}},
        }
        self.migration_notice = False
        self.saves = 0

    def get(self, path, default=None):
        node = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return copy.deepcopy(node) if isinstance(node, (dict, list)) else node

    def set(self, path, value):
        parts = path.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = copy.deepcopy(value)

    def commit(self):
        self.saves += 1
        return type("R", (), {"ok": True, "path": "", "error": ""})()

    save = commit


def _slot(slot_id, skin=None):
    appearance = {"skin": skin} if skin is not None else {"skin": None}
    return {"id": slot_id, "selector": None, "appearance": appearance,
            "placement": {"monitor": "", "u": None, "v": None,
                          "anchor": None, "manual": False}}


def inst(kind, pid, cwd="/w/p"):
    return AgentInstance(kind, pid, "wsl:Ubuntu", cwd=cwd,
                         process_token=str(pid), started_at=NOW - pid)


def snap(i, status=Status.WORKING):
    return Snapshot(i.key, i.kind, i.source, i.pid, status=status, ts=NOW)


class AppHarness:
    def __init__(self, slots, fleet=True):
        from pet.app import PetApp
        from pet.petview import PetView
        self._real_load_skin = PetView.load_skin
        cfg = FleetConfig(slots)
        with patch.object(PetApp, "_reload_skins", lambda self: None), \
             patch.object(PetView, "load_skin", lambda self, bm: None):
            self.app = PetApp(cfg)
        from agents.terminal_service import WindowsTerminalService
        self.app.monitor._terminal_service = WindowsTerminalService(None)
        if fleet:
            self.app.presentation.set_concurrent_mode(PresentationMode.FLEET)
        self.cfg = cfg

    def real_load_skin(self, view):
        """调用未 patch 的真实 load_skin（需要皮肤运行态的测试用）。"""
        self._real_load_skin(view, self.app.pet_manager.build_manager)

    def put(self, *agents):
        self.app.monitor.instances = {a.key: a for a in agents}
        self.app.monitor.snapshots = {a.key: snap(a) for a in agents}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.app.quit()


class SlotSkinTests(unittest.TestCase):
    def test_each_slot_independent_skin_and_duplicates_allowed(self):
        # AC43-SKIN-01/02：pet-1..pet-4 各自皮肤；同一皮肤可重复选择
        slots = [_slot("pet-1", "alpha"), _slot("pet-2", "beta"),
                 _slot("pet-3", "beta"), _slot("pet-4", None)]
        with AppHarness(slots) as h:
            app = h.app
            agents = [inst(AgentKind.CODEX, i + 1, cwd=f"/w/p{i}")
                      for i in range(4)]
            h.put(*agents)
            app._aggregate()
            self.assertEqual(len(app.pet_manager.views), 4)
            v1 = app.pet_manager.views["pet-1"]
            v2 = app.pet_manager.views["pet-2"]
            v3 = app.pet_manager.views["pet-3"]
            v4 = app.pet_manager.views["pet-4"]
            self.assertTrue(v1.view_config.overrides("skin"))
            self.assertEqual(v1.view_config.get("skin"), "alpha")
            # 重复皮肤不禁用/拒绝
            self.assertEqual(v2.view_config.get("skin"), "beta")
            self.assertEqual(v3.view_config.get("skin"), "beta")
            # skin=None 继承全局
            self.assertFalse(v4.view_config.overrides("skin"))
            self.assertEqual(v4.view_config.get("skin"), BUILTIN_SKIN)

    def test_global_skin_change_only_affects_inheritors(self):
        # AC43-SKIN-03
        slots = [_slot("pet-1", "alpha"), _slot("pet-2", None)]
        with AppHarness(slots) as h:
            app = h.app
            agents = [inst(AgentKind.CODEX, 1), inst(AgentKind.CLAUDE, 2)]
            h.put(*agents)
            app._aggregate()
            loads = []
            for vid in ("pet-1", "pet-2"):
                view = app.pet_manager.views[vid]
                orig = view.load_skin
                view.load_skin = lambda bm, v=orig, name=vid: (
                    loads.append(name), v(bm))[1]
            app.appearance.set_global("skin", "gamma")
            self.assertEqual(loads, ["pet-2"])   # 只 reload 继承者

    def test_slot_skin_change_only_reloads_that_view(self):
        # AC43-SKIN-06
        slots = [_slot("pet-1", None), _slot("pet-2", None)]
        with AppHarness(slots) as h:
            app = h.app
            agents = [inst(AgentKind.CODEX, 1), inst(AgentKind.CLAUDE, 2)]
            h.put(*agents)
            app._aggregate()
            loads = []
            for vid in ("pet-1", "pet-2"):
                view = app.pet_manager.views[vid]
                orig = view.load_skin
                view.load_skin = lambda bm, v=orig, name=vid: (
                    loads.append(name), v(bm))[1]
            app.appearance.set_slot_skin("pet-2", "delta")
            self.assertEqual(loads, ["pet-2"])
            # config slot 已更新且持久化在内存
            slot2 = next(s for s in h.cfg.data["presentation"]["concurrent"]
                         ["slots"] if s["id"] == "pet-2")
            self.assertEqual(slot2["appearance"]["skin"], "delta")

    def test_fleet_aggregate_fleet_preserves_slot_override(self):
        # AC43-SKIN-04：override 在 Fleet→Aggregate→Fleet 后保留
        slots = [_slot("pet-1", "alpha"), _slot("pet-2", None)]
        with AppHarness(slots) as h:
            app = h.app
            a = inst(AgentKind.CODEX, 1)
            h.put(a)
            app._aggregate()
            self.assertEqual(app.pet_manager.views["pet-1"].view_config
                             .get("skin"), "alpha")
            # 切 Aggregate：pet-1 使用全局 skin（override 不生效）
            app.presentation.set_concurrent_mode(PresentationMode.AGGREGATE)
            app._aggregate()
            view = app.pet_manager.views["pet-1"]
            self.assertFalse(view.view_config.overrides("skin"))
            # 切回 Fleet：override 原样恢复
            app.presentation.set_concurrent_mode(PresentationMode.FLEET)
            app._aggregate()
            self.assertEqual(app.pet_manager.views["pet-1"].view_config
                             .get("skin"), "alpha")
            # config 中的 override 从未被删除
            slot1 = next(s for s in h.cfg.data["presentation"]["concurrent"]
                         ["slots"] if s["id"] == "pet-1")
            self.assertEqual(slot1["appearance"]["skin"], "alpha")

    def test_missing_skin_falls_back_to_builtin(self):
        # AC43-SKIN-10：missing configured skin → runtime builtin-cat，
        # config 字符串保留，UI 可见 requested/runtime 状态
        slots = [_slot("pet-1", "deleted-skin")]
        with AppHarness(slots) as h:
            app = h.app
            h.put(inst(AgentKind.CODEX, 1))
            app._aggregate()
            view = app.pet_manager.views["pet-1"]
            with patch("pet.skins.list_skins",
                       return_value={BUILTIN_SKIN: {}}), \
                 patch("pet.skins.built_gifs", return_value=None), \
                 patch("pet.skins.built_gifs_any", return_value=None), \
                 patch.object(SkinBuildManager, "request",
                              return_value=None) as req:
                h.real_load_skin(view)
                self.assertEqual(view.skin_requested_name, "deleted-skin")
                self.assertEqual(view.skin_runtime_name, BUILTIN_SKIN)
                self.assertEqual(view.skin_build_state, "fallback")
                # fallback 请求的是 builtin 的 build key
                skin_arg = req.call_args[0][1]
                self.assertEqual(skin_arg, BUILTIN_SKIN)
            # config 原字符串不被静默覆盖
            slot1 = h.cfg.data["presentation"]["concurrent"]["slots"][0]
            self.assertEqual(slot1["appearance"]["skin"], "deleted-skin")

    def test_build_error_keeps_old_picture(self):
        # AC43-SKIN-09：build error 保留旧画面（不空白/崩溃）
        slots = [_slot("pet-1", None)]
        with AppHarness(slots) as h:
            app = h.app
            h.put(inst(AgentKind.CODEX, 1))
            app._aggregate()
            view = app.pet_manager.views["pet-1"]
            fake_paths = {s: f"C:/cache/{s}.gif"
                          for s in ("walk", "attack", "die", "special",
                                    "sleep")}
            with patch("pet.skins.list_skins",
                       return_value={BUILTIN_SKIN: {}}), \
                 patch.object(SkinBuildManager, "ready_paths",
                              return_value=fake_paths), \
                 patch.object(SkinBuildManager, "nearest_ready_cache",
                              return_value=None):
                h.real_load_skin(view)
            old_paths = dict(view._skin_paths)
            self.assertEqual(old_paths, fake_paths)   # 皮肤就绪
            # 模拟后续 build 失败
            view.build_result(view._build_key, "err",
                              "RuntimeError('converter failed')",
                              app.pet_manager.build_manager)
            self.assertEqual(view.skin_build_state, "error")
            self.assertEqual(view._skin_paths, old_paths)   # 旧画面保持


class PetViewCloseTests(unittest.TestCase):
    """DP43-R01：close 幂等 + forget(view_id, key) 恰好一次 + 迟到
    build 结果不复活已关闭 view。"""

    def _ready_paths(self):
        return {s: f"C:/cache/{s}.gif"
                for s in ("walk", "attack", "die", "special", "sleep")}

    def _real_load(self, h, view):
        with patch("pet.skins.list_skins",
                   return_value={BUILTIN_SKIN: {}}), \
             patch("pet.skins.built_gifs",
                   return_value=self._ready_paths()):
            h.real_load_skin(view)

    def test_close_with_active_build_key(self):
        # 旧实现第二次 forget(key) 少 view_id → active key 下必抛 TypeError
        slots = [_slot("pet-1", None)]
        with AppHarness(slots) as h:
            app = h.app
            h.put(inst(AgentKind.CODEX, 1))
            app._aggregate()
            view = app.pet_manager.views["pet-1"]
            self._real_load(h, view)
            self.assertIsNotNone(view._build_key)
            app.pet_manager.remove_view("pet-1")   # 不抛 = 通过
            self.assertNotIn("pet-1", app.pet_manager.views)

    def test_close_is_idempotent(self):
        slots = [_slot("pet-1", None)]
        with AppHarness(slots) as h:
            app = h.app
            h.put(inst(AgentKind.CODEX, 1))
            app._aggregate()
            view = app.pet_manager.views["pet-1"]
            self._real_load(h, view)
            bm = app.pet_manager.build_manager
            view.close(bm)
            view.close(bm)   # 第二次：无异常、无二次清理
            view.close()     # 无 build_manager：同样幂等

    def test_close_forget_exactly_once(self):
        slots = [_slot("pet-1", None)]
        with AppHarness(slots) as h:
            app = h.app
            h.put(inst(AgentKind.CODEX, 1))
            app._aggregate()
            view = app.pet_manager.views["pet-1"]
            self._real_load(h, view)
            key = view._build_key
            bm = app.pet_manager.build_manager
            calls = []
            real_forget = SkinBuildManager.forget

            def spy_forget(self_bm, view_id, forget_key):
                calls.append((view_id, forget_key))
                return real_forget(self_bm, view_id, forget_key)

            with patch.object(SkinBuildManager, "forget", spy_forget):
                view.close(bm)
                view.close(bm)
            self.assertEqual(calls, [(view.view_id, key)])
            self.assertEqual(len(calls), 1)   # 恰好一次

    def test_late_build_result_does_not_revive_closed_view(self):
        slots = [_slot("pet-1", None)]
        with AppHarness(slots) as h:
            app = h.app
            h.put(inst(AgentKind.CODEX, 1))
            app._aggregate()
            view = app.pet_manager.views["pet-1"]
            self._real_load(h, view)
            key = view._build_key
            old_paths = dict(view._skin_paths)
            bm = app.pet_manager.build_manager
            view.close(bm)
            # 迟到的 build 结果（worker 完成）：不复活、不报错
            view.build_result(key, "ok", {"walk": "NEW.gif"}, bm)
            self.assertEqual(view._skin_paths, old_paths)
            self.assertTrue(view._closed)

    def test_fleet_to_aggregate_reclaims_real_loaded_views(self):
        """Fleet→Aggregate 移除多余 view：真实 _build_key 的 close 不抛
        （回归 DP43-R01 的生产路径）。"""
        slots = [_slot("pet-1", None), _slot("pet-2", None),
                 _slot("pet-3", None)]
        with AppHarness(slots) as h:
            app = h.app
            agents = [inst(AgentKind.CODEX, 1), inst(AgentKind.CLAUDE, 2),
                      inst(AgentKind.KIMI, 3)]
            h.put(*agents)
            app._aggregate()
            self.assertEqual(len(app.pet_manager.views), 3)
            for view in app.pet_manager.views.values():
                self._real_load(h, view)
                self.assertIsNotNone(view._build_key)
            app.presentation.set_concurrent_mode(
                PresentationMode.AGGREGATE)
            app._aggregate()   # 只剩 pet-1；旧实现此处 TypeError
            self.assertEqual(list(app.pet_manager.views), ["pet-1"])


class SkinImportTransactionTests(unittest.TestCase):
    """DP43-R04 §10：import 事务——同名整目录替换、失败保持旧 live、
    名称边界校验。"""

    def _write_sources(self, src, ext=".webm",
                       states=("walk", "attack", "die", "special", "sleep")):
        for s in states:
            Path(src, s + ext).write_bytes(b"x" * 8)

    def test_same_name_reimport_replaces_not_merges(self):
        # 旧 skin 含 die.webm 等；新 source 全 gif 且缺 die → 失败且
        # live 保持 old；补全后整目录替换：无 .webm 残留（不混合代次）
        import pet.skins as skins
        with tempfile.TemporaryDirectory() as pets, \
                tempfile.TemporaryDirectory() as old_src, \
                tempfile.TemporaryDirectory() as new_src:
            self._write_sources(old_src, ".webm")
            self._write_sources(
                new_src, ".gif",
                ("walk", "attack", "special", "sleep"))   # 缺 die
            with patch.object(skins, "PETS_DIR", pets):
                skins.prepare_import(old_src, "mix")
                dst = Path(pets, "mix")
                self.assertTrue((dst / "die.webm").is_file())
                # 缺 die → 失败，live untouched（绝不用 dst 旧文件补缺）
                with self.assertRaises(RuntimeError):
                    skins.prepare_import(new_src, "mix")
                self.assertTrue((dst / "die.webm").is_file())
                self.assertFalse((dst / "walk.gif").exists())
                # 补全 die.gif → 成功：整目录替换
                Path(new_src, "die.gif").write_bytes(b"x" * 8)
                skins.prepare_import(new_src, "mix")
                self.assertTrue((dst / "die.gif").is_file())
                self.assertFalse((dst / "die.webm").exists())
                self.assertFalse((dst / "walk.webm").exists())
                self.assertTrue((dst / "manifest.json").is_file())
                # 无 staging/old 残留
                leftovers = [n for n in os.listdir(pets)
                             if n.startswith(".deskpet-")]
                self.assertEqual(leftovers, [])

    def test_import_copy_failure_keeps_old_live_skin(self):
        import pet.skins as skins
        with tempfile.TemporaryDirectory() as pets, \
                tempfile.TemporaryDirectory() as src, \
                tempfile.TemporaryDirectory() as src2:
            self._write_sources(src, ".webm")
            with patch.object(skins, "PETS_DIR", pets):
                skins.prepare_import(src, "keepme")
                before = sorted(os.listdir(Path(pets, "keepme")))
                # 第二次导入中途 copy 失败
                self._write_sources(src2, ".gif")
                real_copy = shutil.copy2

                def failing_copy(a, b, **kw):
                    if str(b).endswith("special.gif"):
                        raise OSError("disk full")
                    return real_copy(a, b, **kw)

                with patch("shutil.copy2", failing_copy):
                    with self.assertRaises(OSError):
                        skins.prepare_import(src2, "keepme")
                # 旧 live skin 完整 + 无 staging 残留
                self.assertEqual(sorted(os.listdir(Path(pets, "keepme"))),
                                 before)
                self.assertEqual(
                    [n for n in os.listdir(pets)
                     if n.startswith(".deskpet-")], [])

    def test_import_name_validation(self):
        from pet.skins import BUILTIN_SKIN, validate_skin_name
        for bad in ("", " ", ".", "..", "/abs", "a/b", "a\\b", "a:b",
                    "con", "CON", "NUL", "com1", "LPT9", ".hidden",
                    "trailing.", "name<>?", BUILTIN_SKIN):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_skin_name(bad)
        self.assertEqual(validate_skin_name("  my-skin-2 "), "my-skin-2")


class CacheManifestTests(unittest.TestCase):
    """DP43-R04 §11/§12：cache manifest 身份、legacy 有条件接受、
    build/rebuild 事务、ready index 失效。"""

    PATH_STATES = ("walk", "attack", "die", "special", "sleep")

    def _fake_paths(self, d):
        return {s: os.path.join(d, s + ".gif") for s in self.PATH_STATES}

    def _make_source_skin(self, pets, name="s1", ext=".webm"):
        d = Path(pets, name)
        d.mkdir(parents=True, exist_ok=True)
        for s in self.PATH_STATES:
            Path(d, s + ext).write_bytes(b"src" + s.encode())
        return d

    def _make_legacy_cache(self, cache, pets, name="s1", height=240,
                           fps=12, src=None):
        src = src or Path(pets, name)
        d = Path(cache, f"{name}@{height}")
        d.mkdir(parents=True, exist_ok=True)
        for s in self.PATH_STATES:
            gif = Path(d, s + ".gif")
            gif.write_bytes(b"gif")
            st = os.stat(Path(src, s + ".webm"))
            meta = {"frames": 1, "width": 10, "height": height,
                    "delay_ms": 83, "loop": True,
                    "src_mtime": st.st_mtime, "src_size": st.st_size,
                    "height": height, "fps": fps}
            Path(d, s + ".gif.json").write_text(
                json.dumps(meta), encoding="utf-8")
        return d

    def test_builtin_build_writes_manifest_and_fps_identity(self):
        import pet.skins as skins
        with tempfile.TemporaryDirectory() as cache:
            with patch.object(skins, "CACHE_DIR", cache):
                paths = skins.build_skin(skins.BUILTIN_SKIN, 240, 12)
                self.assertTrue(skins.read_cache_manifest(
                    os.path.dirname(paths["walk"])) is not None)
                self.assertIsNotNone(
                    skins.ready_cache_paths(skins.BUILTIN_SKIN, 240, 12))
                # fps 是 cache 身份的一部分：不同 fps 不得复用
                self.assertIsNone(
                    skins.ready_cache_paths(skins.BUILTIN_SKIN, 240, 24))

    def test_legacy_cache_requires_full_proof(self):
        import pet.skins as skins
        with tempfile.TemporaryDirectory() as cache, \
                tempfile.TemporaryDirectory() as pets:
            self._make_source_skin(pets)
            self._make_legacy_cache(cache, pets)
            with patch.object(skins, "CACHE_DIR", cache), \
                 patch.object(skins, "PETS_DIR", pets):
                # meta 完整证明 source+fps+height → legacy ready
                self.assertIsNotNone(skins.legacy_ready_paths("s1", 240, 12))
                # fps 不匹配 → 不信任
                self.assertIsNone(skins.legacy_ready_paths("s1", 240, 24))
                # source mtime 变化 → 不信任
                p = Path(pets, "s1", "walk.webm")
                st = os.stat(p)
                os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
                self.assertIsNone(skins.legacy_ready_paths("s1", 240, 12))

    def test_failed_build_keeps_old_live_cache(self):
        import pet.skins as skins
        with tempfile.TemporaryDirectory() as cache, \
                tempfile.TemporaryDirectory() as pets:
            self._make_source_skin(pets, "s2")
            with patch.object(skins, "CACHE_DIR", cache), \
                 patch.object(skins, "PETS_DIR", pets):
                # 先建立旧 live cache（造一个合法 legacy → build 升级）
                self._make_legacy_cache(cache, pets, "s2")
                old = sorted(os.listdir(Path(cache, "s2@240")))
                # converter 失败的 force rebuild：旧 live cache 保持完整
                with patch.object(skins.ConverterJob, "run",
                                  return_value=False):
                    with self.assertRaises(RuntimeError):
                        skins.build_skin("s2", 240, 12, force=True)
                self.assertEqual(sorted(os.listdir(Path(cache, "s2@240"))),
                                 old)
                # staging 已清理
                self.assertEqual(
                    [n for n in os.listdir(cache)
                     if n.startswith(".deskpet-")], [])

    def test_manager_rebuild_preserves_old_ready_image(self):
        # ready index 有 key；request_rebuild 立即失效 index（新 ready
        # lookup 不得返回旧代次），正在显示的 view 画面不被撕掉
        mgr = SkinBuildManager()
        key = ("s3", 240, 12)
        mgr._ready_index[key] = self._fake_paths("C:/old")
        self.assertIsNotNone(mgr.ready_paths("s3", 240, 12))
        gate = threading.Event()

        def slow_build(skin, height, fps, log=None, **kw):
            gate.wait(2.0)
            return self._fake_paths(f"C:/new")

        with patch("pet.skins.build_skin", slow_build):
            mgr.request_rebuild("pet-1", "s3", 240, 12)
            self.assertIsNone(mgr.ready_paths("s3", 240, 12))  # 失效
            gate.set()
            deadline = time.monotonic() + 2
            out = None
            while time.monotonic() < deadline:
                out = mgr.poll_results()
                if out:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(out)
            self.assertEqual(out[0][1], "ok")
            # 新代次 ready
            self.assertEqual(mgr.ready_paths("s3", 240, 12)["walk"],
                             os.path.join("C:/new", "walk.gif"))

    def test_stale_memo_cannot_return_deleted_cache(self):
        # _last_paths 回归：index 命中后磁盘被删 → maintenance
        # reconciliation 淘汰该条目（memo 不再返回已删除 cache）
        import pet.skins as skins
        mgr = SkinBuildManager()
        with tempfile.TemporaryDirectory() as cache, \
                tempfile.TemporaryDirectory() as pets:
            self._make_source_skin(pets, "s4")
            self._make_legacy_cache(cache, pets, "s4")
            live = Path(cache, "s4@240")
            with patch.object(skins, "CACHE_DIR", cache), \
                 patch.object(skins, "PETS_DIR", pets):
                # BUILD job：legacy ready → 发布 index
                def legacy_build(skin, height, fps, log=None, **kw):
                    return skins.legacy_ready_paths(skin, height, fps)

                with patch("pet.skins.build_skin", legacy_build):
                    mgr.request("pet-1", "s4", 240, 12)
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        if mgr.poll_results():
                            break
                        time.sleep(0.01)
                self.assertIsNotNone(mgr.ready_paths("s4", 240, 12))
                # 磁盘目录被外部删除（模拟手动清理）
                shutil.rmtree(live)
                # maintenance reconcile → 条目淘汰
                with patch.object(skins, "CACHE_DIR", cache), \
                     patch.object(skins, "PETS_DIR", pets):
                    payload = skins.run_maintenance()
                mgr._reconcile_ready_index(payload, mgr._next_job_id)
                self.assertIsNone(mgr.ready_paths("s4", 240, 12))

    def test_nearest_ready_cache_same_skin_and_fps_only(self):
        mgr = SkinBuildManager()
        mgr._ready_index[("s", 240, 12)] = self._fake_paths("A")
        mgr._ready_index[("s", 480, 12)] = self._fake_paths("B")
        mgr._ready_index[("s", 240, 24)] = self._fake_paths("C")
        mgr._ready_index[("other", 240, 12)] = self._fake_paths("D")
        near = mgr.nearest_ready_cache("s", 300, 12)
        self.assertEqual(near["walk"],
                         os.path.join("A", "walk.gif"))   # 240 比 480 更近
        self.assertEqual(mgr.nearest_ready_cache("s", 300, 24)["walk"],
                         os.path.join("C", "walk.gif"))    # fps 必须一致
        self.assertIsNone(mgr.nearest_ready_cache("other2", 300, 12))

    def test_import_invalidates_ready_index_for_skin(self):
        # §10.5：同名 import 成功 → 该皮肤全部 ready 代次失效
        mgr = SkinBuildManager()
        mgr._ready_index[("dup", 240, 12)] = self._fake_paths("X")
        mgr._ready_index[("dup", 480, 12)] = self._fake_paths("Y")
        mgr._ready_index[("keep", 240, 12)] = self._fake_paths("Z")
        with tempfile.TemporaryDirectory() as src:
            for s in self.PATH_STATES:
                Path(src, s + ".gif").write_bytes(b"g")
            with patch("pet.skins.prepare_import",
                       lambda s_, n_, cancel=None: n_), \
                 patch("pet.skins.refresh_skin_catalog", lambda: {}):
                mgr.submit_import(src, "dup")
                deadline = time.monotonic() + 2
                out = []
                while time.monotonic() < deadline:
                    out = mgr.poll_results()
                    if out:
                        break
                    time.sleep(0.01)
        self.assertEqual(out[0][1], "import_ok")
        self.assertIsNone(mgr.ready_paths("dup", 240, 12))
        self.assertIsNone(mgr.ready_paths("dup", 480, 12))
        self.assertIsNotNone(mgr.ready_paths("keep", 240, 12))


class JanitorOffTkTests(unittest.TestCase):
    """DP43-R06 §14.5：maintenance 的 filesystem work 在 lane worker；
    Tk callback（janitor/rebuild 入口）O(1)，不阻塞 after_idle。"""

    def test_janitor_and_rebuild_do_not_block_tk(self):
        slots = [_slot("pet-1", None)]
        with AppHarness(slots) as h:
            app = h.app
            h.put(inst(AgentKind.CODEX, 1))
            app._aggregate()
            done = []

            def slow_maintenance(cancel=None):
                time.sleep(1.0)   # §14.5：人工 rmtree/sleep 1s 场景
                return {"ready": {}, "seen": set()}

            with patch("pet.skins.run_maintenance", slow_maintenance):
                t0 = time.monotonic()
                app._janitor()            # Tk callback：必须立即返回
                janitor_ms = (time.monotonic() - t0) * 1000
                app._rebuild_skin()       # Tk callback：同样 O(1)
                rebuild_ms = (time.monotonic() - t0) * 1000 - janitor_ms
                # Tk 线程仍能正常执行 after_idle（未被 lane 阻塞）
                app.root.after_idle(lambda: done.append(1))
                app.root.update()
                self.assertEqual(done, [1])
            self.assertLess(janitor_ms, 200)
            self.assertLess(rebuild_ms, 200)
            # 100 次 request_maintenance → pending 增量 <= 1（§14.3）
            bm = app.pet_manager.build_manager
            before = bm.pending_count()
            for _ in range(100):
                bm.request_maintenance()
            self.assertLessEqual(bm.pending_count() - before, 1)
            bm.stop(timeout=1.5)


class RealConverterSmokeTests(unittest.TestCase):
    """v4.3.1 DP43-R04/R05 实机子进程冒烟（Windows 真实 converter）：

    - gate 协议 + Job Object + staging 事务 + manifest 原子发布；
    - 取消真实转换树（KILL_ON_JOB_CLOSE 即时终止，且实测被杀进程
      returncode==0——ConverterJob 必须先判 cancelled 再判退出码）。
    """

    def _make_skin_sources(self, src, heavy_states=(), size=60):
        import random
        random.seed(7)
        for s in ("walk", "attack", "die", "special", "sleep"):
            if s in heavy_states:
                from PIL import Image
                frames = [Image.effect_noise((700, 700), 42)
                          for _ in range(240)]
                frames[0].save(Path(src, s + ".gif"), save_all=True,
                               append_images=frames[1:], duration=40,
                               loop=0)
            else:
                from PIL import Image
                Image.new("RGBA", (size, size), (9, 8, 7, 255)).save(
                    Path(src, s + ".gif"))

    def test_real_gated_converter_transaction(self):
        import pet.skins as skins
        with tempfile.TemporaryDirectory() as root:
            pets, cache, src = (Path(root, p) for p in
                                ("pets", "cache", "src"))
            for d in (pets, cache, src):
                d.mkdir()
            self._make_skin_sources(src)
            with patch.object(skins, "PETS_DIR", str(pets)), \
                    patch.object(skins, "CACHE_DIR", str(cache)):
                skins.prepare_import(str(src), "realskin")
                paths = skins.build_skin("realskin", 96, 6)
                self.assertTrue(all(os.path.isfile(p)
                                    for p in paths.values()))
                m = skins.read_cache_manifest(skins.cache_dir("realskin", 96))
                self.assertIsNotNone(m)
                self.assertEqual(m["fps"], 6)
                self.assertEqual(set(m["source_signature"]),
                                 set(skins.STATES))
                # ready 身份：fps 不匹配不得复用
                self.assertIsNone(skins.ready_cache_paths("realskin", 96, 12))
                self.assertIsNotNone(skins.ready_cache_paths("realskin", 96, 6))

    def test_cancelled_real_converter_reports_failure_fast(self):
        import pet.skins as skins
        with tempfile.TemporaryDirectory() as root:
            pets, cache, src = (Path(root, p) for p in
                                ("pets", "cache", "src"))
            for d in (pets, cache, src):
                d.mkdir()
            self._make_skin_sources(src, heavy_states=("walk", "attack"))
            with patch.object(skins, "PETS_DIR", str(pets)), \
                    patch.object(skins, "CACHE_DIR", str(cache)):
                skins.prepare_import(str(src), "heavyskin")
                conv = skins.ConverterJob()
                result = {}

                def run_it():
                    result["ok"] = conv.run(
                        str(pets / "heavyskin"), str(cache / ".t-cancel"),
                        480, 24)

                th = threading.Thread(target=run_it, daemon=True)
                th.start()
                time.sleep(0.8)
                if not th.is_alive():
                    self.skipTest(
                        "本机转换快于取消窗口（无法确定性地测取消）")
                t0 = time.monotonic()
                conv.cancel()
                th.join(6.0)
                latency = time.monotonic() - t0
                th.join(2.0)
                self.assertFalse(result.get("ok"),
                                 "被取消的转换必须报告失败（实测 job-kill "
                                 "进程 returncode==0，不得据退出码判成功）")
                self.assertLess(latency, 1.0)


class BuildWaiterSetTests(unittest.TestCase):
    """AC43-SKIN-07/08：waiter view set 的去重与撤销。v4.3.1 起改为
    lane worker + 结果队列驱动（不再有独立 start_build 线程）。"""

    def _manager(self):
        mgr = SkinBuildManager()
        started = []

        def fake_build(skin, height, fps, log=None, **kw):
            started.append((skin, height, fps))
            return {s: f"C:/cache/{skin}/{s}.gif"
                    for s in ("walk", "attack", "die", "special", "sleep")}
        return mgr, fake_build, started

    def _drain(self, mgr, timeout=2.0):
        """等待 active worker 完成并收割结果（真实线程，快速 fake）。
        返回结果列表；超时返回 None。"""
        import time as _time
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            out = mgr.poll_results()
            if out:
                return out
            if mgr._active_job is None and not mgr.results_pending():
                return []
            _time.sleep(0.01)
        return None

    def test_same_key_multiple_views_build_once_and_fanout(self):
        mgr, fake_build, started = self._manager()
        with patch("pet.skins.build_skin", side_effect=fake_build):
            k1 = mgr.request("pet-1", "skin-x", 240, 12)
            k2 = mgr.request("pet-2", "skin-x", 240, 12)
            k3 = mgr.request("pet-3", "skin-x", 240, 12)
            self.assertEqual(k1, k2)
            self.assertEqual(mgr.waiting_views(k1),
                             {"pet-1", "pet-2", "pet-3"})
            out = self._drain(mgr)
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0][1], "ok")
            self.assertEqual(started, [("skin-x", 240, 12)])   # 只执行一次
            # 结果进 ready index 后：后续同 key 请求立即 ok（无 worker）
            mgr.request("pet-4", "skin-x", 240, 12)
            self.assertEqual(started, [("skin-x", 240, 12)])
            self.assertEqual(len(mgr.poll_results()), 1)

    def test_view_switch_forgets_old_key_others_keep_waiting(self):
        # AC43-SKIN-08
        mgr, _, started = self._manager()
        gate = threading.Event()

        def gated_build(skin, height, fps, log=None, **kw):
            started.append((skin, height, fps))
            gate.wait(2.0)
            return {s: f"C:/c/{s}.gif" for s in
                    ("walk", "attack", "die", "special", "sleep")}

        with patch("pet.skins.build_skin", gated_build):
            k_old = mgr.request("pet-1", "old", 240, 12)
            mgr.request("pet-2", "old", 240, 12)
            k_new = mgr.request("pet-3", "new", 240, 12)
            # pet-1 换皮：撤销对 old 的等待，pet-2 仍等待（不触发取消）
            mgr.forget("pet-1", k_old)
            self.assertEqual(mgr.waiting_views(k_old), {"pet-2"})
            self.assertEqual(mgr.waiting_views(k_new), {"pet-3"})
            # 当前转换仍进行（old 还有一个等待者），new 仍在 pending
            self.assertTrue(mgr.building())
            gate.set()
            self.assertTrue(self._drain(mgr))      # old 完成并 fan-out
            self.assertEqual(mgr.waiting_views(k_old), set())
            self.assertTrue(self._drain(mgr))      # new 补位完成
        self.assertEqual(sorted(started),
                         [("new", 240, 12), ("old", 240, 12)])

    def test_last_waiter_forget_removes_pending(self):
        # 最后一个 waiter 撤销时，尚未开始的 pending build 被移除；
        # active（in-flight）的 build 不因 pending 撤销而移除。
        mgr, _, started = self._manager()
        gate = threading.Event()

        def gated_build(skin, height, fps, log=None, **kw):
            started.append((skin, height, fps))
            gate.wait(2.0)
            return {s: f"C:/c/{s}.gif" for s in
                    ("walk", "attack", "die", "special", "sleep")}

        with patch("pet.skins.build_skin", gated_build):
            mgr.request("pet-1", "first", 240, 12)   # 立即启动（in-flight）
            k2 = mgr.request("pet-9", "skin-z", 480, 24)   # 排队 pending
            self.assertEqual(mgr.pending_count(), 2)
            mgr.forget("pet-9", k2)
            self.assertEqual(mgr.waiting_views(k2), set())
            self.assertEqual(mgr.pending_count(), 1)   # 只剩 in-flight
            # 没有 waiter 的 key 再次被请求时仍会正常排队
            k2b = mgr.request("pet-8", "skin-z", 480, 24)
            self.assertEqual(k2b, k2)
            gate.set()
            self.assertTrue(self._drain(mgr))
            self.assertTrue(self._drain(mgr))   # skin-z 补位执行
        self.assertEqual(sorted(started),
                         [("first", 240, 12), ("skin-z", 480, 24)])

    def test_last_waiter_forget_cancels_active_build(self):
        # v4.3.1 §13.4：active build 的最后一个 waiter 撤销 → 取消
        # obsolete converter，lane 不被旧转换阻塞
        mgr, _, started = self._manager()

        def blocked_build(skin, height, fps, log=None, force=False,
                          converter=None):
            started.append((skin, height, fps))
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if converter is not None and converter.cancelled:
                    raise RuntimeError("cancelled during conversion")
                time.sleep(0.01)
            return {s: f"C:/c/{s}.gif" for s in
                    ("walk", "attack", "die", "special", "sleep")}

        with patch("pet.skins.build_skin", blocked_build):
            key = mgr.request("pet-1", "obsolete", 240, 12)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not started:
                time.sleep(0.01)
            self.assertTrue(started)
            mgr.forget("pet-1", key)   # 最后一个 waiter 撤销 → cancel
            out = self._drain(mgr)
            self.assertIsNotNone(out)
            self.assertEqual(out[0][0], key)
            self.assertEqual(out[0][1], "err")   # 取消 → err
            self.assertFalse(mgr.building())

    def test_no_inprocess_converter_fallback(self):
        # AC43-ANIM-09：converter 失败不把 numpy/scipy 载入主进程
        # （只检查可执行 import 语句；模块 docstring 提及不算）
        import pathlib
        src_path = (pathlib.Path(__file__).resolve().parents[1]
                    / "pet" / "skins.py")
        for line in src_path.read_text(encoding="utf-8").splitlines():
            code = line.strip()
            if code.startswith(("from tools.convert", "import tools.convert")):
                self.fail(f"skins.py 引入进程内 converter: {code}")
        # build_skin 的失败路径必须是 raise，而不是调用 convert_skin
        from pet import skins as skins_mod
        import inspect
        body = inspect.getsource(skins_mod.build_skin)
        self.assertNotIn("convert_skin(", body)
        self.assertIn("raise RuntimeError", body)


if __name__ == "__main__":
    unittest.main()
