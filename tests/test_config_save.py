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
                cfg.set("scale", 1.25)
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
            cfg.set("scale", 1.25)
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


# ================================================================ DP43-R02
class SaverStateMachineTests(unittest.TestCase):
    """v4.3.1 DP43-R02：worker harvest 清引用、failed revision 门、
    single-writer shutdown、stale snapshot 裁决。"""

    def _coordinator(self, cfg):
        root = FakeRoot()
        saver = ConfigSaveCoordinator(root, cfg)
        return saver, root

    def _debs(self, root):
        return [t for t in root.timers.values()
                if t.delay_ms == SAVE_DEBOUNCE_MS]

    def _fail_result(self, error="denied"):
        return type("R", (), {"ok": False, "path": "", "error": error})()

    def test_worker_reference_is_cleared_after_harvest(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            saver, root = self._coordinator(cfg)
            cfg.set("scale", 1.25)
            saver.request_save()
            root.fire_timer()
            saver._worker.join(2.0)
            self.assertIsNotNone(saver._worker)   # 未收割：引用仍在
            self.assertFalse(saver._worker_busy())  # 但 dead 不算 busy
            saver.poll()
            self.assertIsNone(saver._worker)      # harvest 后清除
            self.assertIsNone(saver._worker_token)

    def test_pending_false_after_completed_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            saver, root = self._coordinator(cfg)
            cfg.set("scale", 1.25)
            saver.request_save()
            root.fire_timer()
            saver._worker.join(2.0)
            saver.poll()
            self.assertFalse(saver.pending())   # dead worker 不再让 pending 恒真
            self.assertFalse(cfg.dirty)

    def test_failed_revision_is_not_retried_forever(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            saver, root = self._coordinator(cfg)
            writes = []

            def fail_write(self_cfg, revision, data, **kw):
                writes.append(revision)
                return self._fail_result()

            cfg.set("scale", 1.25)
            with patch.object(Config, "write_snapshot", fail_write):
                saver.request_save()
                root.fire_timer()
                saver._worker.join(2.0)
                saver.poll()
                # 同一失败 revision 的多轮 poll 不再自动重试
                for _ in range(6):
                    saver.poll()
                    root.fire_timer()
                    if saver._worker is not None:
                        saver._worker.join(1.0)
                        saver.poll()
                self.assertEqual(len(writes), 1)
            self.assertTrue(cfg.dirty)          # 失败：dirty 保持
            self.assertEqual(saver._failed_revision, cfg.revision)
            self.assertFalse(saver.pending())   # bridge 回 idle 档

    def test_new_revision_after_failure_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            saver, root = self._coordinator(cfg)
            real_write = Config.write_snapshot

            def fail_once(self_cfg, revision, data, **kw):
                if len(writes) == 0:
                    writes.append(revision)
                    return self._fail_result()
                return real_write(self_cfg, revision, data)

            writes = []
            cfg.set("scale", 1.25)
            with patch.object(Config, "write_snapshot", fail_once):
                saver.request_save()
                root.fire_timer()
                saver._worker.join(2.0)
                saver.poll()   # 失败收割
                # 用户新修改 → revision 前进 → 重新有保存资格
                cfg.set("speed", 2.0)
                saver.poll()
                self.assertEqual(len(self._debs(root)), 1)
                root.fire_timer()
                saver._worker.join(2.0)
                saver.poll()
            self.assertFalse(cfg.dirty)
            with open(cfg.path, encoding="utf-8") as f:
                on_disk = json.load(f)
            self.assertAlmostEqual(on_disk["speed"], 2.0)
            # plan §6.9：失败门只在"同 revision 成功"时清除；旧失败
            # revision 已被更新 revision 的成功保存越过（revision 单调
            # 递增，旧值不会再挡住新保存）。
            self.assertNotEqual(saver._failed_revision, cfg.revision)
            self.assertFalse(saver.pending())

    def test_explicit_retry_retries_same_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            saver, root = self._coordinator(cfg)
            real_write = Config.write_snapshot

            state = {"failed": True}

            def fail_then_ok(self_cfg, revision, data, **kw):
                if state["failed"]:
                    state["failed"] = False
                    return self._fail_result()
                return real_write(self_cfg, revision, data)

            cfg.set("scale", 1.25)
            revision = cfg.revision
            with patch.object(Config, "write_snapshot", fail_then_ok):
                saver.request_save()
                root.fire_timer()
                saver._worker.join(2.0)
                saver.poll()               # 失败；无新 revision
                saver.poll()               # 自动路径：不重试
                self.assertTrue(cfg.dirty)
                # 用户点击"重试保存"：同 revision 立即重新提交（异步 worker）
                saver.request_save(immediate=True, force=True)
                self.assertIsNotNone(saver._worker)   # 立即启动（无 debounce）
                self.assertEqual(self._debs(root), [])
                saver._worker.join(2.0)
                saver.poll()
            self.assertFalse(cfg.dirty)
            self.assertEqual(saver._worker_revision, None)
            with open(cfg.path, encoding="utf-8") as f:
                self.assertAlmostEqual(json.load(f)["scale"], 1.25)
            self.assertEqual(revision, cfg.revision)  # 同 revision 重试

    def test_only_one_writer_even_with_immediate_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            saver, root = self._coordinator(cfg)
            entered = threading.Event()
            release = threading.Event()
            real_write = Config.write_snapshot

            def slow_write(self_cfg, revision, data, **kw):
                entered.set()
                release.wait(2.0)
                return real_write(self_cfg, revision, data)

            threads = []
            real_start = threading.Thread.start

            def spy_start(self_thread):
                threads.append(self_thread.name)
                real_start(self_thread)

            cfg.set("scale", 1.25)
            with patch.object(Config, "write_snapshot", slow_write), \
                    patch.object(threading.Thread, "start", spy_start):
                saver.request_save()
                root.fire_timer()
                entered.wait(2.0)
                first = saver._worker
                # worker 写盘中：显式 immediate 请求也不创建第二 writer
                cfg.set("speed", 2.0)
                saver.request_save(immediate=True, force=True)
                self.assertIs(saver._worker, first)
                release.set()
                first.join(2.0)
                saver.poll()
                self.assertEqual(len(threads), 1)
                # harvest 后 poll 重新安排最新 revision 的保存
                saver.poll()
                self.assertEqual(len(self._debs(root)), 1)

    def test_shutdown_timeout_does_not_start_second_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            saver, root = self._coordinator(cfg)
            entered = threading.Event()
            release = threading.Event()
            writes = []

            def blocked_write(self_cfg, revision, data, **kw):
                writes.append(revision)
                entered.set()
                release.wait(5.0)   # 模拟磁盘永久阻塞
                return self._fail_result("io stuck")

            cfg.set("scale", 1.25)
            with patch.object(Config, "write_snapshot", blocked_write):
                saver.request_save()
                root.fire_timer()
                entered.wait(2.0)
                first = saver._worker
                # 写期间又出现新 revision（dirty）
                cfg.set("speed", 2.0)
                durable = saver.flush_for_shutdown(0.2)
                self.assertFalse(durable)          # 明确失败，不假装成功
                self.assertEqual(len(writes), 1)   # 没有第二个 writer
                self.assertIs(saver._worker, first)
                release.set()
                first.join(2.0)
                saver._drain_result()

    def test_shutdown_flushes_latest_when_no_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            saver, root = self._coordinator(cfg)
            cfg.set("scale", 1.75)
            saver.request_save()
            threads = []
            real_start = threading.Thread.start

            def spy_start(self_thread):
                threads.append(self_thread.name)
                real_start(self_thread)

            with patch.object(threading.Thread, "start", spy_start):
                durable = saver.flush_for_shutdown(2.0)
            self.assertTrue(durable)
            self.assertEqual(threads, ["deskpet-config-save"])   # 恰一个
            self.assertEqual(self._debs(root), [])
            with open(cfg.path, encoding="utf-8") as f:
                self.assertAlmostEqual(json.load(f)["scale"], 1.75)

    def test_shutdown_returns_within_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            saver, root = self._coordinator(cfg)
            entered = threading.Event()
            release = threading.Event()

            def blocked_write(self_cfg, revision, data, **kw):
                entered.set()
                release.wait(5.0)
                return self._fail_result("io stuck")

            cfg.set("scale", 1.25)
            with patch.object(Config, "write_snapshot", blocked_write):
                saver.request_save()
                root.fire_timer()
                entered.wait(2.0)
                t0 = time.monotonic()
                saver.flush_for_shutdown(0.15)
                elapsed = time.monotonic() - t0
                release.set()
                saver._worker.join(2.0)
            self.assertLessEqual(elapsed, 0.6)   # 有界（含 join 调度余量）

    def test_stale_snapshot_cannot_overwrite_newer_revision(self):
        """写 rev N 期间出现 rev N+1：N 落盘后 dirty 必须保持，随后
        保存最新快照——最终磁盘一定是最新 revision。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _async_config(tmp)
            saver, root = self._coordinator(cfg)
            entered = threading.Event()
            release = threading.Event()
            real_write = Config.write_snapshot

            def slow_write(self_cfg, revision, data, **kw):
                entered.set()
                release.wait(2.0)
                return real_write(self_cfg, revision, data)

            cfg.set("scale", 1.25)
            with patch.object(Config, "write_snapshot", slow_write):
                saver.request_save()
                root.fire_timer()
                entered.wait(2.0)
                cfg.set("speed", 3.0)   # rev N+1
                release.set()
                saver._worker.join(2.0)
                saver.poll()            # ack 旧 revision → dirty 保持
                self.assertTrue(cfg.dirty)
                root.fire_timer()       # 新 debounce → 最新快照
                saver._worker.join(2.0)
                saver.poll()
            with open(cfg.path, encoding="utf-8") as f:
                on_disk = json.load(f)
            self.assertAlmostEqual(on_disk["speed"], 3.0)
            self.assertFalse(cfg.dirty)


# ================================================================ SkinCatalog
class SkinCatalogTests(unittest.TestCase):
    """DP43-R19 §9.4：snapshot 纯内存（永不触发磁盘扫描）；worker
    锁外扫描 → replace() 短临界区 swap + revision++。"""

    def test_snapshot_never_scans_and_replace_bumps_revision(self):
        catalog = SkinCatalog()

        def forbidden():
            raise AssertionError("snapshot() 不得触发磁盘扫描")

        with patch("pet.skins._scan_skins", forbidden):
            first = catalog.snapshot()
            second = catalog.snapshot()
        self.assertIs(first, second)
        self.assertIn("builtin-cat", first)   # 纯内存初始化含 builtin
        rev = catalog.revision
        catalog.replace({"b": {"name": "b"}})
        self.assertEqual(catalog.snapshot(), {"b": {"name": "b"}})
        self.assertEqual(catalog.revision, rev + 1)

    def test_list_skins_uses_singleton_snapshot(self):
        import pet.skins as skins
        catalog = SkinCatalog()
        with patch("pet.skins._catalog", catalog), \
             patch("pet.skins._scan_skins",
                   side_effect=AssertionError("不得扫描")):
            one = skins.list_skins()
            two = skins.list_skins()
        self.assertIn("builtin-cat", one)
        self.assertIs(one, two)


# ================================================================ 导入 lane
class ImportLaneTests(unittest.TestCase):
    """v4.3.1 DP43-R04/R05：单 mutation lane（import/build/rebuild/
    maintenance 共享，worker 只执行 job → bounded 结果队列）。"""

    def _manager(self):
        return SkinBuildManager()

    def _wait_result(self, bm, timeout=3.0):
        """等 active worker 完成并 poll（真实线程；结果很快）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = bm.poll_results()
            if out:
                return out
            if bm._active_job is None and not bm.results_pending():
                return []
            time.sleep(0.01)
        return None

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
                self.assertEqual(bm._active_thread.name, "deskpet-convert")
                out = self._wait_result(bm)
                self.assertIsNotNone(out)
                self.assertFalse(bm.results_pending())
                self.assertEqual(len(out), 1)
                key, kind, payload = out[0]
                self.assertEqual(key, ("import", "myskin"))
                self.assertEqual(kind, "import_ok")
                self.assertEqual(results, [(True, "myskin", "")])
                # manifest 已写入 + 素材齐全（整目录事务）
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
            out = self._wait_result(bm)
            self.assertIsNotNone(out)
            self.assertEqual(out[0][1], "import_err")
            self.assertIn("缺少素材", str(out[0][2]))
            self.assertFalse(results[0][0])

    def test_import_queue_single_slot_overwrites(self):
        bm = self._manager()
        started = []
        release = threading.Event()
        real_prep = "pet.skins.prepare_import"

        def slow_prepare(src, name, cancel=None):
            started.append(name)
            if name == "first":
                release.wait(2.0)

        with patch(real_prep, slow_prepare):
            bm.submit_import("x", "first")
            bm.submit_import("y", "second")   # 排队
            bm.submit_import("z", "third")    # 覆盖排队项
            release.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and len(started) < 2:
                bm.poll_results()   # lane 前进由 poll 驱动（Tk 职责）
                time.sleep(0.02)
            self.assertEqual(started, ["first", "third"])

    def test_build_defers_while_import_running(self):
        bm = self._manager()
        started_builds = []
        release = threading.Event()

        def slow_prepare(src, name, cancel=None):
            release.wait(2.0)

        def fake_build(skin, height, fps, log=None, **kw):
            started_builds.append((skin, height, fps))
            return {s: f"C:/cache/{s}.gif" for s in
                    ("walk", "attack", "die", "special", "sleep")}

        with patch("pet.skins.prepare_import", slow_prepare), \
             patch("pet.skins.build_skin", fake_build):
            bm.submit_import("x", "myskin")
            # 导入运行中请求 build：只入 pending，不启动 converter
            bm.request("pet-1", "someskin", 240, 12)
            self.assertIsNotNone(bm._active_job)
            self.assertEqual(bm._active_job.kind.name, "IMPORT")
            self.assertEqual(started_builds, [])   # 导入期间 0 个 build
            release.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not started_builds:
                bm.poll_results()
                time.sleep(0.02)
            # 导入结束后 build 补位启动（lane 仍互斥）
            self.assertEqual(len(started_builds), 1)
            self._wait_result(bm)

    def test_only_one_active_skin_job(self):
        # DP43-R05 §25.2：build/import/rebuild/maintenance 任意组合，
        # 任意时刻 <= 1 个 job 在执行（结构有界）
        bm = self._manager()
        guard = threading.Lock()
        running = []
        ran = []
        violations = []

        def tracked(tag, fn):
            def wrapper(*a, **kw):
                with guard:
                    if running:
                        violations.append(tag)
                    running.append(tag)
                    ran.append(tag)
                time.sleep(0.05)
                with guard:
                    running.remove(tag)
                return fn(*a, **kw)
            return wrapper

        def fake_prepare(src, name, cancel=None):
            return name

        def fake_build(skin, height, fps, log=None, **kw):
            return {s: f"C:/c/{s}.gif" for s in
                    ("walk", "attack", "die", "special", "sleep")}

        with patch("pet.skins.prepare_import",
                   tracked("import", fake_prepare)), \
             patch("pet.skins.build_skin", tracked("build", fake_build)), \
             patch("pet.skins.run_maintenance",
                   tracked("maint", lambda cancel=None: {
                       "ready": {}, "seen": set()})):
            bm.submit_import("x", "a")
            bm.request("pet-1", "s", 240, 12)
            bm.request("pet-2", "s2", 240, 12)
            bm.request_rebuild("pet-1", "r", 240, 12)
            bm.request_maintenance()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and bm.building():
                bm.poll_results()
                time.sleep(0.001)
        self.assertEqual(violations, [])   # 任意时刻 <= 1 个 job 执行
        # import×1 + build×2 + rebuild×1 + maintenance×1
        self.assertEqual(len(ran), 5)
        self.assertIn("import", ran)
        self.assertIn("maint", ran)

    def test_shutdown_cancels_and_rejects_new_jobs(self):
        # DP43-R05 §13.1：stop 封口 lane；后续 request/submit 拒绝
        bm = self._manager()
        release = threading.Event()
        entered = threading.Event()

        def slow_prepare(src, name, cancel=None):
            entered.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("导入已取消")
                time.sleep(0.01)

        with patch("pet.skins.prepare_import", slow_prepare):
            bm.submit_import("x", "a")
            entered.wait(2.0)
            bm.stop(timeout=1.0)   # 有界等待 + 取消
            self.assertTrue(bm._stopping)
            t0 = time.monotonic()
            bm.request("pet-1", "s", 240, 12)
            bm.submit_import("y", "b")
            bm.request_rebuild("pet-1", "s", 240, 12)
            bm.request_maintenance()
            self.assertLess(time.monotonic() - t0, 0.2)
            self.assertIsNone(bm._active_job)   # 不再启动新 job


if __name__ == "__main__":
    unittest.main()
