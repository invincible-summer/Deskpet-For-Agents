"""v4.3 Phase 2：per-slot skin / AppearanceController / build waiter set。

覆盖 AC43-SKIN-01..10 与 AC43-ANIM-09（无进程内 converter fallback）。
"""
from __future__ import annotations

import copy
import queue
import sys
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
                 patch("pet.skins.built_gifs", return_value=fake_paths):
                h.real_load_skin(view)
            old_paths = dict(view._skin_paths)
            self.assertEqual(old_paths, fake_paths)   # 皮肤就绪
            # 模拟后续 build 失败
            view.build_result(view._build_key, "err",
                              "RuntimeError('converter failed')",
                              app.pet_manager.build_manager)
            self.assertEqual(view.skin_build_state, "error")
            self.assertEqual(view._skin_paths, old_paths)   # 旧画面保持


class BuildWaiterSetTests(unittest.TestCase):
    """AC43-SKIN-07/08：waiter view set 的去重与撤销。"""

    def _manager(self):
        mgr = SkinBuildManager()
        started = []

        def fake_start(skin, height, fps, log=None):
            started.append((skin, height, fps))
            q = queue.Queue()
            return q
        mgr_start = fake_start
        return mgr, mgr_start, started

    def test_same_key_multiple_views_build_once_and_fanout(self):
        mgr, fake_start, started = self._manager()
        with patch("pet.skins.start_build", side_effect=fake_start):
            k1 = mgr.request("pet-1", "skin-x", 240, 12)
            k2 = mgr.request("pet-2", "skin-x", 240, 12)
            k3 = mgr.request("pet-3", "skin-x", 240, 12)
            self.assertEqual((k1, k2, k3)[0], k1)
            self.assertEqual(k1, k2)
            self.assertEqual(started, [("skin-x", 240, 12)])   # 只启动一次
            self.assertEqual(mgr.waiting_views(k1),
                             {"pet-1", "pet-2", "pet-3"})
            # 完成后 fan-out：一次 poll 返回一个结果给全部等待者
            mgr._queue.put(("ok", {"walk": "w.gif"}))
            results = mgr.poll_results()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][1], "ok")
            # 结果缓存后：后续同 key 请求立即 ok，不再启动 converter
            mgr.request("pet-4", "skin-x", 240, 12)
            self.assertEqual(started, [("skin-x", 240, 12)])
            self.assertEqual(len(mgr.poll_results()), 1)

    def test_view_switch_forgets_old_key_others_keep_waiting(self):
        # AC43-SKIN-08
        mgr, fake_start, started = self._manager()
        with patch("pet.skins.start_build", side_effect=fake_start):
            k_old = mgr.request("pet-1", "old", 240, 12)
            mgr.request("pet-2", "old", 240, 12)
            k_new = mgr.request("pet-3", "new", 240, 12)
            # pet-1 换皮：撤销对 old 的等待，pet-2 仍等待
            mgr.forget("pet-1", k_old)
            self.assertEqual(mgr.waiting_views(k_old), {"pet-2"})
            self.assertEqual(mgr.waiting_views(k_new), {"pet-3"})
            # 当前转换仍进行（old 还有一个等待者），new 仍在 pending
            self.assertTrue(mgr.building())

    def test_last_waiter_forget_removes_pending(self):
        # 最后一个 waiter 撤销时，尚未开始的 pending build 被移除；
        # 已在飞行中的 build（converter 已启动）不中断——结果缓存
        # 仍可能被后续请求复用。
        mgr, fake_start, started = self._manager()
        with patch("pet.skins.start_build", side_effect=fake_start):
            mgr.request("pet-1", "first", 240, 12)   # 立即启动（in-flight）
            k2 = mgr.request("pet-9", "skin-z", 480, 24)   # 排队 pending
            self.assertEqual(mgr.pending_count(), 2)
            mgr.forget("pet-9", k2)
            self.assertEqual(mgr.waiting_views(k2), set())
            self.assertEqual(mgr.pending_count(), 1)   # 只剩 in-flight
            # 没有 waiter 的 key 再次被请求时仍会正常排队
            k2b = mgr.request("pet-8", "skin-z", 480, 24)
            self.assertEqual(k2b, k2)

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
