"""DSH 无插件监听 adapter 测试（无插件监听 plan §七 DSH frame 测试）。

全部使用合成 fixture：临时目录内构造上游布局
``sessions/--<projectKey>--/<encodeSegment(id)>/session[.vN].jsonl[.zstd]``；
zstd 帧用真实 zstandard 压缩（库缺失时解码用例自动跳过，结构扫描
用例始终运行）。绝不触碰真实用户数据。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

from agents import dsh, paths
from agents.desktop import SessionClaimKey, claims_cover
from agents.models import (
    AgentInstance,
    AgentKind,
    Confidence,
    DesktopHost,
    Phase,
    Status,
)

try:
    import zstandard  # type: ignore
    _HAVE_ZSTD = True
except ImportError:
    _HAVE_ZSTD = False


def _compress(data: bytes) -> bytes:
    """上游 zstdCompress 等价：单帧、带 checksum（ZSTD_c_checksumFlag=1）。"""
    cctx = zstandard.ZstdCompressor(write_checksum=True)
    return cctx.compress(data)


_NOW_MS = int(time.time() * 1000)


def _header_line(session_id="ses_1", cwd="/repo", version=4, **extra):
    obj = {"type": "session", "version": version, "id": session_id,
           "createdAt": _NOW_MS, "isSeeded": False,
           "delegationDepth": 0}
    if cwd is not None:
        obj["cwd"] = cwd
    obj.update(extra)
    return json.dumps(obj, ensure_ascii=False).encode() + b"\n"


def _event_line(etype, seq, data, ts=None, **extra):
    obj = {"type": etype, "seq": seq,
           "time": _NOW_MS if ts is None else ts, "data": data}
    obj.update(extra)
    return json.dumps(obj, ensure_ascii=False).encode() + b"\n"


class UpstreamPortTests(unittest.TestCase):
    """上游 format.ts / zstd.ts 移植的精确性。"""

    def test_encode_segment_safe_and_escape(self):
        self.assertEqual(dsh.dsh_encode_segment("abc123._-"), "abc123._-")
        self.assertEqual(dsh.dsh_encode_segment("."), "~002E")
        self.assertEqual(dsh.dsh_encode_segment(".."), "~002E~002E")
        self.assertEqual(dsh.dsh_encode_segment("~"), "~007E")
        # 遍历中和：分隔符被转义为 ~XXXX，不可能出现在段内
        seg = dsh.dsh_encode_segment("../evil")
        self.assertNotIn("/", seg)
        self.assertNotIn("\\", seg)
        self.assertEqual(seg, "..~002Fevil")

    def test_encode_segment_surrogate_pairs(self):
        # JS 按 UTF-16 code unit：非 BMP 字符拆成两个 ~XXXX（精确移植）
        seg = dsh.dsh_encode_segment("a\U0001F600b")
        self.assertEqual(seg, "a~D83D~DE00b")

    def test_project_key_layout(self):
        # 上游 projectKey：首部 '-' 被 regex 剥离（有损设计）
        self.assertEqual(dsh.dsh_project_key("/home/u/repo"),
                         "--home-u-repo--")
        # 分隔符 run 折叠 + 盘符冒号 + 空格转义
        self.assertEqual(dsh.dsh_project_key("C:\\work\\My Repo"),
                         "--C-work-My~0020Repo--")
        self.assertEqual(dsh.dsh_project_key("/"), "--root--")
        self.assertEqual(dsh.dsh_project_key("//a//b"), "--a-b--")
        with self.assertRaises(ValueError):
            dsh.dsh_project_key("")

    def test_parse_generation_filename(self):
        self.assertEqual(dsh.parse_generation_filename("session.jsonl"),
                         (0, "none"))
        self.assertEqual(
            dsh.parse_generation_filename("session.v4.jsonl.zstd"),
            (4, "zstd"))
        self.assertEqual(dsh.parse_generation_filename("session.v1.jsonl"),
                         (1, "none"))
        # 非 canonical：临时名/大写/前导零/.v0/压缩不匹配
        self.assertIsNone(dsh.parse_generation_filename("session.tmp"))
        self.assertIsNone(dsh.parse_generation_filename("session.V2.jsonl"))
        self.assertIsNone(dsh.parse_generation_filename("session.v02.jsonl"))
        self.assertIsNone(dsh.parse_generation_filename("session.v0.jsonl"))
        self.assertIsNone(dsh.parse_generation_filename("session.jsonlz"))

    def test_scan_zstd_frames_complete_torn_and_corrupt(self):
        frame_a = _compress(b"hello frame one") if _HAVE_ZSTD else b""
        if not _HAVE_ZSTD:
            self.skipTest("zstandard unavailable")
        frame_b = _compress(b"frame two payload")
        joined = frame_a + frame_b
        frames, torn = dsh.scan_zstd_frames(joined)
        self.assertEqual(frames, [(0, len(frame_a)),
                                  (len(frame_a), len(joined))])
        self.assertIsNone(torn)
        # 半帧：EOF 打断最后一帧
        frames, torn = dsh.scan_zstd_frames(joined + frame_a[:5])
        self.assertEqual(len(frames), 2)
        self.assertEqual(torn, len(joined))
        # 坏 magic
        with self.assertRaises(ValueError):
            dsh.scan_zstd_frames(b"\x00" * 16)
        # 保留 frame-header 位（0x18）
        bad = bytearray(frame_a)
        bad[4] |= 0x08
        with self.assertRaises(ValueError):
            dsh.scan_zstd_frames(bytes(bad))

    def test_decode_zstd_frame_validates_and_caps(self):
        if not _HAVE_ZSTD:
            self.skipTest("zstandard unavailable")
        frame = _compress(b"plaintext")
        self.assertEqual(dsh.decode_zstd_frame(frame), b"plaintext")
        # 篡改内容 → checksum/结构失败（fail closed）
        corrupt = bytearray(frame)
        corrupt[-3] ^= 0xFF
        with self.assertRaises(ValueError):
            dsh.decode_zstd_frame(bytes(corrupt))
        # 超过解压上限的解压炸弹被拒绝
        import agents.dsh as d
        bomb = _compress(b"x" * (d.MAX_FRAME_DECOMPRESSED + 1))
        with self.assertRaises(ValueError):
            dsh.decode_zstd_frame(bomb)


class EventTrackerTests(unittest.TestCase):
    """turn/step/approval 状态机与回放预算。"""

    def _tracker(self, events, now=1700000100.0):
        t = dsh.DshEventTracker()
        wall = now
        for etype, data in events:
            t.feed_event({"type": etype, "seq": 1, "time": int(now * 1000),
                          "data": data}, wall)
        return t

    def test_turn_lifecycle_working_done_idle(self):
        now = 1700000100.0
        t = self._tracker([("turn/start", {"turn": 1})], now)
        obs = t.observation(now)
        self.assertIs(obs.status, Status.WORKING)
        self.assertTrue(obs.turn_active)
        # completed → DONE 窗口 → IDLE 余晖
        t.feed_event({"type": "turn/end", "seq": 2, "time": int(now * 1000),
                      "data": {"turn": 1, "reason": {"kind": "completed"}}},
                     now + 1)
        self.assertIs(t.observation(now + 2).status, Status.DONE)
        self.assertIs(t.observation(now + 30).status, Status.IDLE)
        self.assertIsNone(t.observation(now + 10000))

    def test_turn_error_window(self):
        now = 1700000100.0
        t = self._tracker([
            ("turn/start", {"turn": 1}),
            ("turn/end", {"turn": 1, "reason": {"kind": "error"}}),
        ], now)
        self.assertIs(t.observation(now + 1).status, Status.ERROR)
        self.assertIsNone(t.observation(now + 100))

    def test_crash_orphan_turn_never_permanent_working(self):
        # 打开的 turn 是 durable 事实：短静默保持 WORKING（置信降级），
        # 超过孤儿上限才按 crash orphan 回 UNKNOWN（绝不永久 Working）。
        now = 1700000100.0
        t = self._tracker([("turn/start", {"turn": 7})], now)
        quiet = t.observation(now + dsh.OPEN_TURN_QUIET_SEC + 5)
        self.assertIs(quiet.status, Status.WORKING)
        self.assertIs(quiet.confidence, Confidence.MEDIUM)
        self.assertIs(quiet.phase, Phase.THINKING)
        self.assertIsNone(t.observation(now + dsh.OPEN_TURN_ORPHAN_SEC + 5))

    def test_interrupted_closer_is_error_reminder(self):
        now = 1700000100.0
        t = self._tracker([
            ("turn/start", {"turn": 2}),
            ("turn/end", {"turn": 2, "reason": {"kind": "interrupted"}}),
        ], now)
        self.assertIs(t.observation(now + 1).status, Status.ERROR)

    def test_scalar_turn_end_reason_is_not_treated_as_success(self):
        now = 1700000100.0
        t = self._tracker([
            ("turn/start", {"turn": 2}),
            ("turn/end", {"turn": 2, "reason": "aborted"}),
        ], now)
        self.assertIs(t.observation(now + 1).status, Status.ERROR)

    def test_approval_pairing_within_replay_budget(self):
        now = 1700000100.0
        t = self._tracker([
            ("turn/start", {"turn": 1}),
            ("approval/asked", {"id": "apr_1", "toolName": "bash"}),
        ], now)
        obs = t.observation(now + 1)
        self.assertIs(obs.status, Status.WAITING)
        self.assertIn("bash", obs.waiting_detail)
        # decided 解除
        t.feed_event({"type": "approval/decided", "seq": 3,
                      "time": int((now + 2) * 1000),
                      "data": {"id": "apr_1", "outcome": "allowed-once"}},
                     now + 2)
        self.assertIsNotNone(t.observation(now + 3))

    def test_stale_unpaired_asked_not_waiting(self):
        # 回放预算之外的旧 asked 绝不伪装仍在 WAITING（plan §六）
        now = 1700000100.0
        t = dsh.DshEventTracker()
        t.feed_event({"type": "approval/asked", "seq": 1,
                      "time": int((now - dsh.APPROVAL_REPLAY_SEC - 60) * 1000),
                      "data": {"id": "apr_old", "toolName": "bash"}}, now)
        self.assertIsNone(t.observation(now))

    def test_unknown_required_event_refuses(self):
        now = 1700000100.0
        t = dsh.DshEventTracker()
        t.feed_event({"type": "turn/start", "seq": 1,
                      "time": int(now * 1000), "data": {"turn": 1}}, now)
        self.assertIsNotNone(t.observation(now))
        t.feed_event({"type": "plugin:future/required", "seq": 2,
                      "time": int(now * 1000), "data": {}}, now)
        self.assertIsNone(t.observation(now))
        # ignorable=true 的未知事件安全跳过
        t2 = dsh.DshEventTracker()
        t2.feed_event({"type": "turn/start", "seq": 1,
                       "time": int(now * 1000), "data": {"turn": 1}}, now)
        t2.feed_event({"type": "plugin:future/info", "seq": 2,
                       "time": int(now * 1000), "data": {},
                       "ignorable": True}, now)
        self.assertIs(t2.observation(now).status, Status.WORKING)


class DshLogConsumerTests(unittest.TestCase):
    """明文/zstd 增量消费：半帧、残行、世代版本。"""

    def test_plain_incremental_and_partial_line(self):
        c = dsh.DshLogConsumer(compression="none")
        header = _header_line()
        events = (_event_line("turn/start", 1, {"turn": 1}) +
                  _event_line("assistant/message", 2,
                              {"turn": 1, "step": 1, "message": {},
                               "stream": []}))
        c.consume(header + events[:10], 1.0)
        self.assertTrue(c.header_seen)
        self.assertEqual(c.header.session_id, "ses_1")
        # 残行保留，下次补齐
        c.consume(events[10:], 2.0)
        self.assertEqual(c.tracker.open_turn, 1)

    def test_plain_header_rejected_on_garbage(self):
        c = dsh.DshLogConsumer(compression="none")
        c.consume(b"not json\n", 1.0)
        self.assertEqual(c.refused, "HEADER_NOT_JSON")
        # 非法 header 结构
        c2 = dsh.DshLogConsumer(compression="none")
        c2.consume(b'{"type":"session"}\n', 1.0)
        self.assertEqual(c2.refused, "HEADER_INVALID")

    def test_future_format_version_refused(self):
        c = dsh.DshLogConsumer(compression="none")
        c.consume(_header_line(version=9), 1.0)
        self.assertEqual(c.refused, "FORMAT_VERSION_9")
        self.assertEqual(c.header.session_id, "ses_1")   # 身份仍可用

    def test_zstd_frames_and_torn_tail(self):
        if not _HAVE_ZSTD:
            self.skipTest("zstandard unavailable")
        c = dsh.DshLogConsumer(compression="zstd")
        batch1 = _header_line() + _event_line("turn/start", 1, {"turn": 1})
        frame1 = _compress(batch1)
        c.consume(frame1, 1.0)
        self.assertEqual(c.tracker.open_turn, 1)
        # 批写中的半帧：不消费、不报错，下轮写完整后继续
        batch2 = _event_line("turn/end", 2,
                             {"turn": 1, "reason": {"kind": "completed"}})
        frame2 = _compress(batch2)
        c.consume(frame2[:7], 1.5)
        self.assertEqual(c.tracker.open_turn, 1)
        c.consume(frame2[7:], 2.0)
        self.assertEqual(c.tracker.open_turn, 0)
        self.assertTrue(c.tracker.turn_known_over)

    def test_zstd_corrupt_frame_refused(self):
        if not _HAVE_ZSTD:
            self.skipTest("zstandard unavailable")
        c = dsh.DshLogConsumer(compression="zstd")
        frame = _compress(_header_line())
        corrupt = bytearray(frame)
        corrupt[10] ^= 0xFF
        c.consume(bytes(corrupt), 1.0)
        self.assertTrue(c.refused.startswith("DECODE:"))
        # 拒绝后不再消费
        c.consume(_compress(_event_line("turn/start", 1, {"turn": 1})), 2.0)
        self.assertFalse(c.tracker.records_seen > 0)


class SessionLayoutTests(unittest.TestCase):
    """目录布局 / 世代选择 / 双编码拒绝。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "sessions")
        os.makedirs(self.root, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_session(self, cwd, session_id, events, name="session.v4.jsonl",
                       mtime=None):
        project = dsh.dsh_project_dir(self.root, cwd)
        sdir = os.path.join(project, dsh.dsh_encode_segment(session_id))
        os.makedirs(sdir, exist_ok=True)
        path = os.path.join(sdir, name)
        with open(path, "wb") as f:
            f.write(_header_line(session_id=session_id, cwd=cwd))
            for line in events:
                f.write(line)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_candidates_by_cwd(self):
        cwd = "/repo"
        events = [_event_line("turn/start", 1, {"turn": 1})]
        path = self._write_session(cwd, "ses_a", events)
        cands = dsh.dsh_session_candidates(self.root, cwd, window_sec=3600)
        self.assertEqual([p for _m, p in cands], [path])
        # 其他 cwd 不串扰
        self.assertEqual(dsh.dsh_session_candidates(
            self.root, "/other", window_sec=3600), [])

    def test_generation_selection_prefers_highest(self):
        cwd = "/repo"
        old = self._write_session(cwd, "ses_g", [], name="session.v2.jsonl")
        new = self._write_session(cwd, "ses_g", [], name="session.v4.jsonl")
        cands = dsh.dsh_session_candidates(self.root, cwd, window_sec=3600)
        self.assertEqual([p for _m, p in cands], [new])

    def test_dual_encoding_and_future_version_rejected(self):
        cwd = "/repo"
        # 双编码并存 → 上游 encodingMismatch → 无候选
        self._write_session(cwd, "ses_mixed", [], name="session.v4.jsonl")
        self._write_session(cwd, "ses_mixed", [],
                            name="session.v4.jsonl.zstd")
        self.assertEqual(dsh.dsh_session_candidates(
            self.root, cwd, window_sec=3600), [])
        # 未来版本 → 无候选
        self._write_session(cwd, "ses_future", [], name="session.v9.jsonl")
        self.assertEqual(dsh.dsh_session_candidates(
            self.root, cwd, window_sec=3600), [])

    def test_traversal_session_id_neutralized(self):
        cwd = "/repo"
        # 恶意 session id（含 ../）被 encodeSegment 中和到自己的目录
        path = self._write_session(cwd, "../escape", [])
        norm = os.path.normcase(os.path.abspath(path))
        self.assertNotIn("..", norm.split(os.sep))
        self.assertTrue(norm.startswith(os.path.normcase(
            os.path.abspath(self.root)) + os.sep))


class DshWatcherTests(unittest.TestCase):
    """CLI watcher 绑定：单实例绑定 / 同 cwd 多实例不绑。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "sessions")
        os.makedirs(self.root, exist_ok=True)
        self._old_root = None

    def tearDown(self):
        if self._old_root is not None:
            paths.dsh_sessions_root, self._old_root = self._old_root, \
                paths.dsh_sessions_root
        self._tmp.cleanup()

    def _patch_root(self):
        self._old_root = paths.dsh_sessions_root
        paths.dsh_sessions_root = lambda: self.root

    @staticmethod
    def _inst(pid, cwd, key=None):
        return AgentInstance(kind=AgentKind.DSH, pid=pid, source="windows",
                             cwd=cwd, key=key or f"w|dsh|{pid}|t{pid}")

    def _write_log(self, cwd, session_id, events):
        project = dsh.dsh_project_dir(self.root, cwd)
        sdir = os.path.join(project, dsh.dsh_encode_segment(session_id))
        os.makedirs(sdir, exist_ok=True)
        path = os.path.join(sdir, "session.v4.jsonl")
        with open(path, "wb") as f:
            f.write(_header_line(session_id=session_id, cwd=cwd))
            for line in events:
                f.write(line)
        return path

    def test_single_instance_binds_and_reports_waiting(self):
        self._patch_root()
        cwd = "C:\\work\\proj"
        events = [
            _event_line("turn/start", 1, {"turn": 1}),
            _event_line("approval/asked", 2,
                        {"id": "apr_1", "toolName": "bash"}),
        ]
        self._write_log(cwd, "ses_wait", events)
        watcher = dsh.DshWatcher({})
        out = watcher.poll([self._inst(101, cwd)])
        self.assertEqual(len(out), 1)
        (key, obs), = out.items()
        self.assertEqual(obs.session_id, "ses_wait")
        self.assertIs(obs.status, Status.WAITING)
        self.assertEqual(obs.waiting_detail, "bash")
        # claims 暴露给 desktop 去重
        claims = watcher.terminal_claims()
        self.assertTrue(claims_cover(claims, "dsh", session_id="ses_wait"))

    def test_same_cwd_two_instances_never_bind(self):
        self._patch_root()
        cwd = "/repo"
        self._write_log(cwd, "ses_x", [
            _event_line("turn/start", 1, {"turn": 1})])
        watcher = dsh.DshWatcher({})
        out = watcher.poll([self._inst(201, cwd), self._inst(202, cwd)])
        for obs in out.values():
            self.assertFalse(obs.session_bound)
            self.assertIsNone(obs.status)
        self.assertEqual(watcher.terminal_claims(), frozenset())

    def test_no_instances_clears(self):
        watcher = dsh.DshWatcher({})
        self.assertEqual(watcher.poll([]), {})


class DshDesktopSourceTests(unittest.TestCase):
    """Desktop source：host 绑定、claims 去重、子会话折叠。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "sessions")
        os.makedirs(self.root, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_log(self, cwd, session_id, events, name="session.v4.jsonl"):
        project = dsh.dsh_project_dir(self.root, cwd)
        sdir = os.path.join(project, dsh.dsh_encode_segment(session_id))
        os.makedirs(sdir, exist_ok=True)
        path = os.path.join(sdir, name)
        with open(path, "wb") as f:
            f.write(_header_line(session_id=session_id, cwd=cwd))
            for line in events:
                f.write(line)
        return path

    @staticmethod
    def _host(pid=500):
        return DesktopHost(kind=AgentKind.DSH, source="windows", pid=pid,
                           process_token="1.000", started_at=1.0)

    def test_active_session_emits_target_with_waiting(self):
        cwd = "/repo"
        self._write_log(cwd, "ses_root", [
            _event_line("turn/start", 1, {"turn": 1}),
            _event_line("approval/asked", 2,
                        {"id": "a1", "toolName": "write"}),
        ])
        src = dsh.DshDesktopSource(cfg={}, sessions_root=self.root)
        snap = src.poll((self._host(),), frozenset(), time.time() + 1)
        self.assertTrue(snap.authoritative)
        self.assertEqual(len(snap.instances), 1)
        inst = snap.instances[0]
        self.assertEqual(inst.logical_session_id, "ses_root")
        obs = snap.observations[inst.key]
        self.assertIs(obs.status, Status.WAITING)

    def test_admitted_waiting_log_survives_discovery_window(self):
        cwd = "/repo"
        path = self._write_log(cwd, "ses_old", [
            _event_line("turn/start", 1, {"turn": 1}),
            _event_line("approval/asked", 2,
                        {"id": "old", "toolName": "write"}),
        ])
        src = dsh.DshDesktopSource(cfg={}, sessions_root=self.root)
        first = src.poll((self._host(),), frozenset(), 100.0)
        self.assertEqual(len(first.instances), 1)
        # The log stops growing and falls outside the 180s discovery window;
        # the already-admitted pending session must remain owned and visible.
        os.utime(path, (0, 0))
        src._last_scan = 0.0
        second = src.poll((self._host(),), frozenset(), 400.0)
        self.assertEqual(len(second.instances), 1)
        self.assertEqual(second.instances[0].logical_session_id, "ses_old")

    def test_claim_covered_session_not_duplicated(self):
        cwd = "/repo"
        self._write_log(cwd, "ses_cl", [
            _event_line("turn/start", 1, {"turn": 1})])
        src = dsh.DshDesktopSource(cfg={}, sessions_root=self.root)
        claims = frozenset({SessionClaimKey(kind="dsh",
                                            session_id="ses_cl")})
        snap = src.poll((self._host(),), claims, time.time() + 1)
        self.assertEqual(snap.instances, ())

    def test_child_session_folds_into_root(self):
        cwd = "/repo"
        self._write_log(cwd, "ses_root", [
            _event_line("turn/start", 1, {"turn": 1}),
            _event_line("turn/end", 2,
                        {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        # 子会话：subagent 审批等待应折叠到 root
        self._write_log(cwd, "ses_child", [
            _event_line("approval/asked", 1,
                        {"id": "a_child", "toolName": "bash"}),
        ])
        child_header = _header_line(session_id="ses_child", cwd=cwd,
                                    parentSession="ses_root",
                                    origin="subagent", delegationDepth=1)
        project = dsh.dsh_project_dir(self.root, cwd)
        cdir = os.path.join(project, dsh.dsh_encode_segment("ses_child"))
        path = os.path.join(cdir, "session.v4.jsonl")
        # 重写子会话头部带 parentSession
        body = path
        with open(body, "wb") as f:
            f.write(child_header)
            f.write(_event_line("approval/asked", 1,
                                {"id": "a_child", "toolName": "bash"}))
        src = dsh.DshDesktopSource(cfg={}, sessions_root=self.root)
        snap = src.poll((self._host(),), frozenset(), time.time() + 1)
        self.assertEqual(len(snap.instances), 1)
        inst = snap.instances[0]
        self.assertEqual(inst.logical_session_id, "ses_root")
        self.assertIs(snap.observations[inst.key].status, Status.WAITING)

    def test_no_hosts_clears(self):
        src = dsh.DshDesktopSource(cfg={}, sessions_root=self.root)
        snap = src.poll((), frozenset(), time.time())
        self.assertTrue(snap.authoritative)
        self.assertEqual(snap.instances, ())
        self.assertEqual(src.stats()["dsh_desktop_sessions"], 0)

class NearEndStartTests(unittest.TestCase):
    """v4.4.1：大日志从近端起步时的帧对齐/行对齐/header 引导。

    实机取证（2026-10）：1MB 的 session.v4.jsonl.zstd 若直接从
    size-256KB 起读，起点落在帧中段，第一 4 字节不是 magic，
    scan_zstd_frames 抛 CORRUPT → 整个会话降级 UNKNOWN。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "sessions")
        os.makedirs(self.root, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_zstd(self, cwd, session_id, filler_bytes):
        project = dsh.dsh_project_dir(self.root, cwd)
        sdir = os.path.join(project, dsh.dsh_encode_segment(session_id))
        os.makedirs(sdir, exist_ok=True)
        path = os.path.join(sdir, "session.v4.jsonl.zstd")
        with open(path, "wb") as f:
            f.write(_compress(_header_line(session_id=session_id, cwd=cwd)))
            f.write(_compress(_event_line("turn/start", 1, {"turn": 1})))
            written = 0
            while written < filler_bytes:
                chunk = _compress(_event_line(
                    "step/start", 2, {"turn": 1, "step": 1}))
                f.write(chunk)
                written += len(chunk)
            f.write(_compress(_event_line(
                "turn/end", 9,
                {"turn": 1, "reason": {"kind": "completed"}})))
        return path

    def _write_plain(self, cwd, session_id, filler_bytes):
        project = dsh.dsh_project_dir(self.root, cwd)
        sdir = os.path.join(project, dsh.dsh_encode_segment(session_id))
        os.makedirs(sdir, exist_ok=True)
        path = os.path.join(sdir, "session.v4.jsonl")
        with open(path, "wb") as f:
            f.write(_header_line(session_id=session_id, cwd=cwd))
            f.write(_event_line("turn/start", 1, {"turn": 1}))
            written = 0
            while written < filler_bytes:
                line = _event_line("step/start", 2,
                                   {"turn": 1, "step": 1,
                                    "pad": "x" * 200})
                f.write(line)
                written += len(line)
            f.write(_event_line(
                "turn/end", 9,
                {"turn": 1, "reason": {"kind": "completed"}}))
        return path

    @unittest.skipUnless(_HAVE_ZSTD, "zstandard unavailable")
    def test_large_zstd_starts_on_frame_boundary_not_corrupt(self):
        path = self._write_zstd("/repo", "ses_big", 700 * 1024)
        size = os.path.getsize(path)
        self.assertGreater(size, dsh.MAX_NEAR_END_TAIL)
        f = dsh.DshFile(path)
        f.start_at(size)
        with open(path, "rb") as fh:
            fh.seek(f._tail_pos)
            self.assertEqual(fh.read(4), dsh._ZSTD_MAGIC_BYTES)
        f.last_mtime = 0
        f.poll_lines()
        self.assertEqual(f.consumer.refused, "")
        self.assertTrue(f._header_bootstrapped)
        self.assertEqual(f.session_id, "ses_big")
        self.assertEqual(f.cwd, "/repo")

    def test_large_plain_starts_on_line_boundary_with_header(self):
        path = self._write_plain("/repo", "ses_plain", 700 * 1024)
        size = os.path.getsize(path)
        self.assertGreater(size, dsh.MAX_NEAR_END_TAIL)
        f = dsh.DshFile(path)
        f.start_at(size)
        with open(path, "rb") as fh:
            fh.seek(f._tail_pos - 1)
            self.assertEqual(fh.read(1), b"\n")
        f.last_mtime = 0
        f.poll_lines()
        self.assertEqual(f.consumer.refused, "")
        self.assertEqual(f.session_id, "ses_plain")

    def test_small_log_starts_from_zero(self):
        path = self._write_plain("/repo", "ses_small", 10)
        size = os.path.getsize(path)
        self.assertLess(size, dsh.MAX_NEAR_END_TAIL)
        f = dsh.DshFile(path)
        f.start_at(size)
        self.assertEqual(f._tail_pos, 0)


class WatcherSlotStabilityTests(unittest.TestCase):
    """v4.4.1：bound 文件不被 MAX_TRACKED_FILES 挤出后反复重建。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "sessions")
        os.makedirs(self.root, exist_ok=True)
        self._old = paths.dsh_sessions_root
        paths.dsh_sessions_root = lambda: self.root

    def tearDown(self):
        paths.dsh_sessions_root = self._old
        self._tmp.cleanup()

    @staticmethod
    def _inst(pid, cwd, key=None):
        return AgentInstance(kind=AgentKind.DSH, pid=pid, source="windows",
                             cwd=cwd, key=key or f"w|dsh|{pid}|t{pid}")

    def _write_log(self, cwd, session_id, events):
        project = dsh.dsh_project_dir(self.root, cwd)
        sdir = os.path.join(project, dsh.dsh_encode_segment(session_id))
        os.makedirs(sdir, exist_ok=True)
        path = os.path.join(sdir, "session.v4.jsonl")
        with open(path, "wb") as f:
            f.write(_header_line(session_id=session_id, cwd=cwd))
            for line in events:
                f.write(line)
        return path

    def test_bound_file_survives_newer_candidates(self):
        cwd = "C:\\work\\proj"
        self._write_log(cwd, "ses_bound", [
            _event_line("turn/start", 1, {"turn": 1}),
            _event_line("approval/asked", 2,
                        {"id": "apr_1", "toolName": "bash"}),
        ])
        watcher = dsh.DshWatcher({})
        (key, obs), = watcher.poll([self._inst(101, cwd)]).items()
        self.assertEqual(obs.session_id, "ses_bound")
        self.assertIs(obs.status, Status.WAITING)
        created = 0
        orig_make = watcher.make_state

        def make(path):
            nonlocal created
            created += 1
            return orig_make(path)

        watcher.make_state = make
        for i in range(6):
            self._write_log("C:\\other\\%d" % i, "ses_hist_%d" % i, [
                _event_line("turn/start", 1, {"turn": 1})])
            (key, obs), = watcher.poll(
                [self._inst(101, cwd)]).items()
            self.assertEqual(obs.session_id, "ses_bound")
            self.assertIs(obs.status, Status.WAITING)
        self.assertEqual(created, 0, "bound file must not be recreated")


class ClaimsSkipTailerTests(unittest.TestCase):
    """v4.4.1：被 terminal claim 覆盖的会话不再被 desktop source 解码。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "sessions")
        os.makedirs(self.root, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _inst(pid, cwd, key=None):
        return AgentInstance(kind=AgentKind.DSH, pid=pid, source="windows",
                             cwd=cwd, key=key or f"w|dsh|{pid}|t{pid}")

    def _write_zstd(self, cwd, session_id):
        project = dsh.dsh_project_dir(self.root, cwd)
        sdir = os.path.join(project, dsh.dsh_encode_segment(session_id))
        os.makedirs(sdir, exist_ok=True)
        path = os.path.join(sdir, "session.v4.jsonl.zstd")
        with open(path, "wb") as f:
            f.write(_compress(_header_line(session_id=session_id, cwd=cwd)))
            f.write(_compress(_event_line("turn/start", 1, {"turn": 1})))
            f.write(_compress(_event_line(
                "approval/asked", 2, {"id": "a1", "toolName": "bash"})))
        return path

    @unittest.skipUnless(_HAVE_ZSTD, "zstandard unavailable")
    def test_claimed_session_gets_no_consumer(self):
        cwd = "C:\\work\\proj"
        log = self._write_zstd(cwd, "ses_claim")
        claims = frozenset({SessionClaimKey(
            kind="dsh", session_id="ses_claim", canonical_session_path=log)})
        host = DesktopHost(kind=AgentKind.DSH, pid=1, process_token="t",
                           started_at=time.time())
        src = dsh.DshDesktopSource(sessions_root=self.root)
        snap = src.poll((host,), claims, time.time())
        self.assertEqual(src._logs, {})
        self.assertEqual(snap.instances, ())
        self.assertTrue(snap.authoritative)

    @unittest.skipUnless(_HAVE_ZSTD, "zstandard unavailable")
    def test_unclaimed_session_still_tracked(self):
        cwd = "C:\\work\\proj"
        self._write_zstd(cwd, "ses_free")
        host = DesktopHost(kind=AgentKind.DSH, pid=1, process_token="t",
                           started_at=time.time())
        src = dsh.DshDesktopSource(sessions_root=self.root)
        snap = src.poll((host,), frozenset(), time.time())
        self.assertEqual(len(snap.instances), 1)
        self.assertEqual(snap.instances[0].logical_session_id,
                         "ses_free")


class EventBudgetBackpressureTests(unittest.TestCase):
    """v4.4.2：单轮事件预算耗尽时未消化数据留缓冲，绝不静默丢弃。

    上游一个 zstd 帧=一个追加批次（单帧可含数百事件）；旧实现把超预算
    的行直接丢掉——丢 turn/end 漏 DONE、丢 approval/decided 幻影 WAITING。
    """

    BUDGET = dsh.MAX_EVENTS_PER_POLL

    def _filler(self, seq_start, count):
        out = b""
        for i in range(seq_start, seq_start + count):
            out += _event_line("step/start", i, {"turn": 1, "step": i})
        return out

    def test_plain_over_budget_no_loss(self):
        c = dsh.DshLogConsumer(compression="none")
        payload = (_header_line()
                   + _event_line("turn/start", 1, {"turn": 1})
                   + _event_line("approval/asked", 2,
                                 {"id": "apr_1", "toolName": "bash"})
                   + self._filler(3, self.BUDGET + 8)
                   + _event_line("approval/decided", 4,
                                 {"id": "apr_1", "outcome": "allowed-once"})
                   + _event_line("turn/end", 5,
                                 {"turn": 1, "reason": {"kind": "completed"}}))
        c.consume(payload[:len(payload) // 2], 1.0)
        self.assertGreater(c.tracker.records_seen, 0)
        # 半行残端 + 预算余量都留在缓冲；补上后半（读端分块送达）
        c.consume(payload[len(payload) // 2:], 1.1)
        rounds = 0
        while c._buffer and rounds < 50:
            c.consume(b"", 1.0 + rounds)
            rounds += 1
        self.assertLess(rounds, 50, "backlog must be drainable")
        # 全部事件消化完毕：asked 已被 decided 配对解除、turn 已完成
        self.assertEqual(c.tracker.pending_approvals, {})
        self.assertEqual(c.tracker.open_turn, 0)
        self.assertTrue(c.tracker.turn_known_over)
        self.assertGreater(c.tracker.records_seen, self.BUDGET)
        self.assertEqual(c.refused, "")

    @unittest.skipUnless(_HAVE_ZSTD, "zstandard unavailable")
    def test_zstd_over_budget_keeps_whole_frames(self):
        c = dsh.DshLogConsumer(compression="zstd")
        frame_a = _compress(_header_line()
                            + _event_line("turn/start", 1, {"turn": 1})
                            + self._filler(2, self.BUDGET + 4))
        frame_b = _compress(_event_line(
            "turn/end", 9, {"turn": 1, "reason": {"kind": "completed"}}))
        c.consume(frame_a + frame_b, 1.0)
        # 帧内剩余解码文本与未解码整帧都留在缓冲，本轮一个不丢
        self.assertTrue(c._pending_plain)
        self.assertTrue(c._buffer)
        rounds = 0
        while (c._pending_plain or c._buffer) and rounds < 50:
            c.consume(b"", 1.0 + rounds)
            rounds += 1
        self.assertLess(rounds, 50, "backlog must be drainable")
        self.assertEqual(c.tracker.open_turn, 0)
        self.assertTrue(c.tracker.turn_known_over)
        self.assertEqual(c.refused, "")


class UpstreamVocabularyTests(unittest.TestCase):
    """2026-10 上游 master 新增 job/* 词表：不得把含新事件的会话判 UNKNOWN。"""

    def test_job_events_known_and_not_refused(self):
        self.assertIn("job/registered", dsh.KNOWN_EVENT_TYPES)
        self.assertIn("job/settled", dsh.KNOWN_EVENT_TYPES)
        c = dsh.DshLogConsumer(compression="none")
        c.consume(_header_line()
                  + _event_line("job/registered", 1, {"id": "j1"})
                  + _event_line("turn/start", 2, {"turn": 1}), 1.0)
        self.assertEqual(c.refused, "")
        self.assertEqual(c.tracker.open_turn, 1)


class MergedTrackerIsolationTests(unittest.TestCase):
    """子会话审批折叠不得污染 root tracker（旧 bug：幻影 WAITING 要等
    600s 回放预算才被剪除）。"""

    def test_child_approval_merge_does_not_write_into_root(self):
        root = dsh.DshLogConsumer(compression="none")
        root.tracker.turn_known_over = True
        root.tracker.done_ts = 100.0
        child = dsh.DshLogConsumer(compression="none")
        child.header = dsh.DshHeaderInfo(
            version=4, session_id="ses_child", parent_session="ses_root",
            valid=True)
        child.tracker.pending_approvals["a_child"] = ("bash", 100.0)
        sess = dsh._DshDesktopSession(path="/x", session_id="ses_root",
                                      root_session_id="ses_root")
        merged = dsh._merged_tracker(root, {"c": child}, sess)
        self.assertIn("a_child", merged.pending_approvals)    # 折叠可见
        self.assertEqual(root.tracker.pending_approvals, {})  # root 不被污染
        # 子会话 decided 后合并视图立即解除（无需等回放预算剪除）
        child.tracker.pending_approvals.pop("a_child", None)
        merged2 = dsh._merged_tracker(root, {"c": child}, sess)
        self.assertEqual(merged2.pending_approvals, {})


class ChildSessionExclusionTests(unittest.TestCase):
    """CLI 候选排除子会话：繁忙 subagent 的日志不得抢占 root 绑定。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "sessions")
        os.makedirs(self.root, exist_ok=True)
        self._old = paths.dsh_sessions_root
        paths.dsh_sessions_root = lambda: self.root

    def tearDown(self):
        paths.dsh_sessions_root = self._old
        self._tmp.cleanup()

    @staticmethod
    def _inst(pid, cwd):
        return AgentInstance(kind=AgentKind.DSH, pid=pid, source="windows",
                             cwd=cwd, key=f"w|dsh|{pid}|t{pid}")

    def _write_log(self, cwd, session_id, events, parent="", mtime=None):
        project = dsh.dsh_project_dir(self.root, cwd)
        sdir = os.path.join(project, dsh.dsh_encode_segment(session_id))
        os.makedirs(sdir, exist_ok=True)
        path = os.path.join(sdir, "session.v4.jsonl")
        extra = ({"parentSession": parent, "origin": "subagent"}
                 if parent else {})
        with open(path, "wb") as f:
            f.write(_header_line(session_id=session_id, cwd=cwd, **extra))
            for line in events:
                f.write(line)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_busy_child_log_never_steals_binding(self):
        cwd = "/repo"
        root = self._write_log(cwd, "ses_root", [
            _event_line("turn/start", 1, {"turn": 1}),
        ], mtime=time.time() - 10)
        child = self._write_log(cwd, "ses_child", [
            _event_line("turn/start", 1, {"turn": 1}),
            _event_line("approval/asked", 2,
                        {"id": "a_c", "toolName": "bash"}),
        ], parent="ses_root", mtime=time.time())
        self.assertTrue(dsh.dsh_child_session(child))
        self.assertFalse(dsh.dsh_child_session(root))
        watcher = dsh.DshWatcher({})
        out = watcher.poll([self._inst(301, cwd)])
        (key, obs), = out.items()
        self.assertEqual(obs.session_id, "ses_root")
        self.assertEqual(watcher._instance_files[key], root)

    def test_unreadable_or_invalid_child_header_is_not_admitted_as_root(self):
        cwd = "/repo3"
        project = dsh.dsh_project_dir(self.root, cwd)
        sdir = os.path.join(project, dsh.dsh_encode_segment("unknown"))
        os.makedirs(sdir, exist_ok=True)
        path = os.path.join(sdir, "session.v4.jsonl")
        with open(path, "wb") as f:
            f.write(b"not a valid DSH header\n")
        watcher = dsh.DshWatcher({})
        self.assertIsNone(dsh._dsh_child_session_state(path))
        out = watcher.poll([self._inst(303, cwd)])
        self.assertFalse(next(iter(out.values())).session_bound)

    def test_terminal_claims_fall_back_to_path(self):
        cwd = "/repo2"
        root = self._write_log(cwd, "ses_p", [
            _event_line("turn/start", 1, {"turn": 1})])
        watcher = dsh.DshWatcher({})
        # session_id 尚未解析（未 poll）的绑定也要能按路径发 claim
        watcher.files[root] = watcher.make_state(root)
        key = self._inst(302, cwd).key
        watcher._instance_files[key] = root
        claims = watcher.terminal_claims()
        self.assertTrue(claims_cover(claims, "dsh", canonical_path=root))


class ClaimedConsumerFreezeTests(unittest.TestCase):
    """v4.4.2：claim 建立前已打开的 consumer 冻结增量读，claim 消失恢复。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "sessions")
        os.makedirs(self.root, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _host():
        return DesktopHost(kind=AgentKind.DSH, source="windows", pid=500,
                           process_token="1.000", started_at=1.0)

    def test_open_consumer_frozen_while_claimed(self):
        cwd = "/repo"
        project = dsh.dsh_project_dir(self.root, cwd)
        sdir = os.path.join(project, dsh.dsh_encode_segment("ses_fz"))
        os.makedirs(sdir, exist_ok=True)
        path = os.path.join(sdir, "session.v4.jsonl")
        with open(path, "wb") as f:
            f.write(_header_line(session_id="ses_fz", cwd=cwd))
            f.write(_event_line("turn/start", 1, {"turn": 1}))
        src = dsh.DshDesktopSource(cfg={}, sessions_root=self.root)
        host = self._host()
        now = time.time()
        snap = src.poll((host,), frozenset(), now)
        self.assertEqual(len(snap.instances), 1)    # 先打开 consumer 并 tail
        consumer = src._logs[path]
        frozen_pos = consumer.file_pos
        # 追加完成事件；带 claim 再 poll → 不发 target 也不增量读
        with open(path, "ab") as f:
            f.write(_event_line("turn/end", 9,
                                {"turn": 1, "reason": {"kind": "completed"}}))
        claims = frozenset({SessionClaimKey(kind="dsh",
                                            session_id="ses_fz")})
        snap = src.poll((host,), claims, now + 1)
        self.assertEqual(snap.instances, ())
        self.assertEqual(consumer.file_pos, frozen_pos)
        self.assertEqual(consumer.tracker.open_turn, 1)  # 未读新字节
        # claim 消失 → 恢复增量读，看到完成事件
        snap = src.poll((host,), frozenset(), now + 2)
        self.assertEqual(len(snap.instances), 1)
        self.assertGreater(consumer.file_pos, frozen_pos)
        self.assertEqual(consumer.tracker.open_turn, 0)


if __name__ == "__main__":
    unittest.main()
