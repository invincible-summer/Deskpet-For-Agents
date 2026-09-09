"""配置非阻塞保存 + 皮肤目录/异步导入测试（v4.3 §8.2 / §9）。

  * Config revision/snapshot 协议（快照 deepcopy、ack 新鲜度裁决）；
  * ConfigSaveCoordinator：650ms debounce 合并、恒 ≤1 worker、
    写期间新 revision → 重新保存最新快照、flush_for_shutdown 有界；
  * SkinCatalog：list_skins 只读内存快照，refresh bump revision；
  * SkinBuildManager 导入 lane：deskpet-convert 单 job、与 build 互斥、
    结果经 poll_results 收割（worker 线程绝不触碰 Tk）。
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pet.config import Config
from pet.config_save import (FLUSH_TIMEOUT_SEC, SAVE_DEBOUNCE_MS,
                             ConfigSaveCoordinator)
from pet.skins import SkinBuildManager, SkinCatalog


class FakeTimer:
    def __init__(self, token, delay_ms, callback, idle=False):
        self.token = token
        self.delay_ms = delay_ms
        self.callback = callback


class FakeRoot:
    def __init__(self):
        self.timers = {}
        self._next = 0

    def after(self, ms, cb=None, *args):
        self._next += 1
        self.timers[self._next] = FakeTimer(self._next, ms, cb)
        return self._next

    def after_cancel(self, token):
        self.timers.pop(token, None)

    def fire_timer(self, predicate=None):
        tokens = [t for t, timer in self.timers.items()
                  if predicate is None or predicate(timer)]
        for token in tokens:
            timer = self.timers.pop(token)
            timer.callback()


class AsyncConfig(Config):
    """真实 Config + 可观测 write_snapshot（临时路径，不碰用户配置）。"""


def _async_config(tmp: str) -> AsyncConfig:
    return AsyncConfig(os.path.join(tmp, "config.json"))


# ================================================================ Config 协议
class ConfigSnapshotProtocolTests(unittest.TestCase):
    def test_set_bumps_revision_and_snapshot_deep_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            rev0 = cfg.revision
            cfg.set("scale", 1.5)
            self.assertEqual(cfg.revision, rev0 + 1)
            revision, snap = cfg.snapshot_for_save()
            self.assertEqual(revision, cfg.revision)
            snap["scale"] = 9.9   # 深拷贝：不回写 Config
            self.assertEqual(cfg.get("scale"), 1.5)

    def test_write_snapshot_writes_disk_without_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            cfg.set("scale", 1.25)
            revision, snap = cfg.snapshot_for_save()
            cfg.set("speed", 2.0)   # 快照之后又改（模拟写期间新修改）
            result = cfg.write_snapshot(revision, snap)
            self.assertTrue(result.ok, result.error)
            with open(cfg.path, encoding="utf-8") as f:
                on_disk = json.load(f)
            self.assertEqual(on_disk["scale"], 1.25)
            self.assertNotEqual(on_disk.get("speed"), 2.0)
            # write_snapshot 不改实例状态：dirty 仍在
            self.assertTrue(cfg.dirty)

    def test_acknowledge_freshness_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            cfg.set("scale", 1.25)
            revision, snap = cfg.snapshot_for_save()
            ok = type("R", (), {"ok": True, "path": "", "error": ""})()
            # ack 旧 revision（写期间又有新修改）→ 保持 dirty
            cfg.set("speed", 2.0)
            cfg.acknowledge_save(revision, ok)
            self.assertTrue(cfg.dirty)
            # ack 最新 revision → 清 dirty
            revision2, _ = cfg.snapshot_for_save()
            cfg.acknowledge_save(revision2, ok)
            self.assertFalse(cfg.dirty)


# ================================================================ coordinator
class ConfigSaveCoordinatorTests(unittest.TestCase):
    def _coordinator(self, cfg):
        root = FakeRoot()
        saver = ConfigSaveCoordinator(root, cfg)
        return saver, root

    def _debs(self, root):
        return [t for t in root.timers.values()
                if t.delay_ms == SAVE_DEBOUNCE_MS]

    def test_rapid_requests_coalesce_into_one_debounce(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            saver, root = self._coordinator(cfg)
            for _ in range(20):
                cfg.set("scale", 1.0)
                saver.request_save()
            self.assertEqual(len(self._debs(root)), 1)   # 恰一个 timer

    def test_single_worker_and_latest_snapshot_saved(self):
        """20 次快速修改 → 1 个 worker、1 次写盘、写的是最新快照。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            saver, root = self._coordinator(cfg)
            for i in range(20):
                cfg.set("scale", 1.0 + i * 0.01)
            saver.request_save()
            threads = []
            real_start = threading.Thread.start

            def spy_start(self_thread):
                threads.append(self_thread.name)
                real_start(self_thread)

            with patch.object(threading.Thread, "start", spy_start):
                root.fire_timer()   # debounce 到期 → 1 个 worker
            self.assertEqual(threads, ["deskpet-config-save"])
            saver._worker.join(2.0)
            saver.poll()
            with open(cfg.path, encoding="utf-8") as f:
                on_disk = json.load(f)
            self.assertAlmostEqual(on_disk["scale"], 1.19)
            self.assertFalse(cfg.dirty)

    def test_worker_busy_defers_and_poll_resaves_new_revision(self):
        """写期间又有新 revision：poll 收割后重新 debounce 保存最新。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            saver, root = self._coordinator(cfg)
            cfg.set("scale", 1.0)
            saver.request_save()
            entered = threading.Event()
            release = threading.Event()
            real_write = Config.write_snapshot

            def slow_write(self, revision, data):
                entered.set()
                release.wait(2.0)
                return real_write(self, revision, data)

            with patch.object(Config, "write_snapshot", slow_write):
                root.fire_timer()
                entered.wait(2.0)
                self.assertTrue(saver._worker_busy())
                # worker 写 rev N 期间用户又改了配置（rev N+1）
                cfg.set("speed", 3.0)
                # debounce 到期但 worker 忙 → 不启动第二个 worker
                saver._fire()
                worker = saver._worker
                release.set()
                worker.join(2.0)
                # bridge poll：收割旧结果 + 看到 dirty → 重新安排
                saver.poll()
                self.assertTrue(cfg.dirty)
                self.assertEqual(len(self._debs(root)), 1)
            # 第二轮保存最新快照
            root.fire_timer()
            saver._worker.join(2.0)
            saver.poll()
            with open(cfg.path, encoding="utf-8") as f:
                on_disk = json.load(f)
            self.assertEqual(on_disk["speed"], 3.0)
            self.assertFalse(cfg.dirty)

    def test_flush_for_shutdown_bounded_and_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            saver, root = self._coordinator(cfg)
            cfg.set("scale", 1.75)
            saver.request_save()
            saver.flush_for_shutdown(0.5)
            self.assertEqual(self._debs(root), [])   # timer 已取消
            self.assertFalse(cfg.dirty)             # 已落盘（同步兜底）
            with open(cfg.path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["scale"], 1.75)

    def test_legacy_config_falls_back_to_sync_save(self):
        """无 snapshot API 的（测试 fake）Config：debounce 到期同步 save。"""
        saved = []

        class LegacyConfig:
            dirty = True

            def save(self):
                saved.append(1)
                return type("R", (), {"ok": True})()

        root = FakeRoot()
        saver = ConfigSaveCoordinator(root, LegacyConfig())
        saver.request_save()
        saver.request_save()
        root.fire_timer()
        self.assertEqual(saved, [1])   # 合并后恰一次
        self.assertFalse(saver.pending())


# ================================================================ SkinCatalog
class SkinCatalogTests(unittest.TestCase):
    def test_catalog_caches_scan_and_refresh_bumps_revision(self):
        catalog = SkinCatalog()
        calls = []

        def fake_scan():
            calls.append(1)
            return {"a": {"name": "a"}}

        with patch("pet.skins._scan_skins", fake_scan):
            first = catalog.snapshot()
            second = catalog.snapshot()
        self.assertIs(first, second)
        self.assertEqual(len(calls), 1)   # 只扫一次磁盘
        rev = catalog.revision
        with patch("pet.skins._scan_skins", lambda: {"b": {"name": "b"}}):
            refreshed = catalog.refresh_from_disk()
        self.assertEqual(refreshed, {"b": {"name": "b"}})
        self.assertEqual(catalog.revision, rev + 1)

    def test_list_skins_uses_singleton_snapshot(self):
        import pet.skins as skins
        catalog = SkinCatalog()
        with patch("pet.skins._catalog", catalog), \
             patch("pet.skins._scan_skins", lambda: {"x": {"name": "x"}}):
            one = skins.list_skins()
            two = skins.list_skins()
        self.assertIn("x", one)
        self.assertIs(one, two)


# ================================================================ 导入 lane
class ImportLaneTests(unittest.TestCase):
    def _manager(self):
        return SkinBuildManager()

    def test_submit_import_runs_in_convert_thread_and_polls_result(self):
        bm = self._manager()
        results = []
        bm.on_import_result = lambda ok, name, error: results.append(
            (ok, name, error))
        with tempfile.TemporaryDirectory() as src, \
                tempfile.TemporaryDirectory() as pets:
            for state in ("walk", "attack", "die", "special", "sleep"):
                Path(src, state + ".gif").write_bytes(b"g")
            with patch("pet.skins.PETS_DIR", pets):
                bm.submit_import(src, "myskin")
                self.assertTrue(bm.building())   # lane 占用
                bm._import_thread.join(2.0)
                self.assertFalse(bm._import_running())
                self.assertTrue(bm.results_pending())
                out = bm.poll_results()
                self.assertEqual(len(out), 1)
                key, kind, payload = out[0]
                self.assertEqual(key, ("import", "myskin"))
                self.assertEqual(kind, "import_ok")
                self.assertEqual(results, [(True, "myskin", "")])
                # manifest 已写入 + 素材齐全
                self.assertTrue(os.path.isfile(
                    os.path.join(pets, "myskin", "manifest.json")))

    def test_import_error_reports_without_raising(self):
        bm = self._manager()
        results = []
        bm.on_import_result = lambda ok, name, error: results.append(
            (ok, name, error))
        with tempfile.TemporaryDirectory() as src:
            # 空目录 → 缺素材
            bm.submit_import(src, "broken")
            bm._import_thread.join(2.0)
            out = bm.poll_results()
            self.assertEqual(out[0][1], "import_err")
            self.assertIn("缺少素材", str(out[0][2]))
            self.assertFalse(results[0][0])

    def test_import_queue_single_slot_overwrites(self):
        bm = self._manager()
        started = []
        release = threading.Event()
        real_prep = "pet.skins.prepare_import"

        def slow_prepare(src, name):
            started.append(name)
            if name == "first":
                release.wait(2.0)

        with patch(real_prep, slow_prepare):
            bm.submit_import("x", "first")
            bm.submit_import("y", "second")   # 排队
            bm.submit_import("z", "third")    # 覆盖排队项
            release.set()
            deadline = time.time() + 3
            while time.time() < deadline and len(started) < 2:
                time.sleep(0.02)
            self.assertEqual(started, ["first", "third"])

    def test_build_defers_while_import_running(self):
        import queue as queue_mod
        bm = self._manager()
        started_builds = []

        class _IdleQueue:
            def get_nowait(self):
                raise queue_mod.Empty

        def fake_start_build(*args, **kwargs):
            started_builds.append(args)
            return _IdleQueue()

        release = threading.Event()
        with patch("pet.skins.prepare_import",
                   lambda s, n: release.wait(2.0)), \
             patch("pet.skins.start_build", fake_start_build):
            bm.submit_import("x", "myskin")
            # 导入运行中请求 build：只入 pending，不启动 converter
            bm.request("pet-1", "someskin", 240, 12)
            self.assertTrue(bm._import_running())
            self.assertEqual(started_builds, [])   # 导入期间 0 个 build
            release.set()
            bm._import_thread.join(2.0)
            deadline = time.time() + 3
            while time.time() < deadline and not started_builds:
                time.sleep(0.02)
            # 导入结束后 build 补位启动（lane 仍互斥）
            self.assertEqual(len(started_builds), 1)
            self.assertIsNotNone(bm._queue)


if __name__ == "__main__":
    unittest.main()
