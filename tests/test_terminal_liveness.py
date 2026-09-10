"""v4.2.3 §2/§4：interactive-terminal liveness 与 kind-disable 回归测试。

覆盖 AC-PROC-01..07：
  * WSL tty/tpgid 三态分类（纯函数）；
  * canonical group（wrapper+runtime）整组判定；
  * DETACHED 不进入 authoritative snapshot，UNKNOWN 保留；
  * tmux/screen（pgid != tpgid 但 TTY 有效）不误删；
  * Monitor 权威空 → _commit_exit 级联（binding/tailer 同 tick 消失）；
  * native lease：arm 条件、连续 2 个 authoritative generation 才退出；
  * kind-disable 优先于 probe health；
  * DeskPet 不调用 terminate/kill/signal（静态检查）。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from agents.discovery import (
    DistroInventory,
    WslProcessProbe,
    canonical_terminal_attachment,
    classify_ps_terminal_attachment,
)
from agents.models import (
    AgentInstance,
    AgentKind,
    ObservationBindingConfidence,
    Snapshot,
    SourceProbeSnapshot,
    TerminalAttachment,
    TerminalObservationBinding,
    TerminalWindowBinding,
    WindowBindingConfidence,
    WindowIdentity,
)
from agents.monitor import Monitor
from agents.terminal_service import WindowsTerminalService


def snap(source, authoritative, instances=(), gen=1, error=""):
    return SourceProbeSnapshot(
        source=source, generation=gen, observed_at=1000.0,
        authoritative=authoritative, instances=tuple(instances),
        error=error)


def _wsl_ps_row(pid, ppid, pgid, tpgid, tty, args,
                sid=None, uid=1000, etimes=60, stat="S", comm=""):
    return (pid, ppid, sid if sid is not None else ppid, pgid, tpgid,
            tty, uid, etimes, stat, comm or args.split()[0], args)


class MemoryConfig:
    def __init__(self, data=None):
        self.data = data or {
            "monitor": {
                "agents": {kind.value: True for kind in AgentKind},
                "windows_enabled": True,
                "wsl_enabled": True,
                "windows_scan_sec": 3600,
                "wsl_scan_sec": 3600,
                "file_poll_sec": 0.5,
                "session_scan_sec": 3600,
                "activity_grace_sec": 10,
            },
            "privacy": {"goal_max_chars": 120, "summary_max_chars": 160},
        }

    def get(self, path, default=None):
        node = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, path, value):
        node = self.data
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value


# ------------------------------------------------------------- 纯函数三态

class ClassifyAttachmentTests(unittest.TestCase):
    def test_attached_when_tty_and_fg_present(self):
        self.assertIs(classify_ps_terminal_attachment("pts/3", 4242),
                      TerminalAttachment.ATTACHED)

    def test_detached_when_tty_revoked_and_no_fg(self):
        self.assertIs(classify_ps_terminal_attachment("?", -1),
                      TerminalAttachment.DETACHED)
        self.assertIs(classify_ps_terminal_attachment("??", 0),
                      TerminalAttachment.DETACHED)
        self.assertIs(classify_ps_terminal_attachment("", 0),
                      TerminalAttachment.DETACHED)

    def test_unknown_on_contradictory_evidence(self):
        # TTY 被 revoke 但仍有 foreground pg（或反之）：证据矛盾 → UNKNOWN
        self.assertIs(classify_ps_terminal_attachment("?", 4242),
                      TerminalAttachment.UNKNOWN)
        self.assertIs(classify_ps_terminal_attachment("pts/3", -1),
                      TerminalAttachment.UNKNOWN)

    def test_background_group_with_tty_is_attached(self):
        # tmux/screen：Agent 可处于 background process group 但仍持有
        # 有效 controlling TTY（pgid != tpgid 不是 detach 证据）
        self.assertIs(classify_ps_terminal_attachment("pts/0", 999),
                      TerminalAttachment.ATTACHED)


class CanonicalAttachmentTests(unittest.TestCase):
    def _rows(self):
        # wrapper(npm) 1000 → runtime 1001；独立 claude 2000
        return {
            1000: _wsl_ps_row(1000, 900, 10, -1, "?",
                              "node /usr/lib/codex/codex.js run", comm="npm"),
            1001: _wsl_ps_row(1001, 1000, 10, -1, "?", "/usr/bin/codex"),
            2000: _wsl_ps_row(2000, 1, 20, 20, "pts/1", "/usr/bin/claude"),
        }

    def test_whole_group_detached_when_all_members_detached(self):
        # AC-PROC-02：wrapper+runtime 均 detached → 整组 DETACHED
        self.assertIs(canonical_terminal_attachment(1001, (1000,), self._rows()),
                      TerminalAttachment.DETACHED)

    def test_group_kept_when_any_launcher_attached(self):
        rows = self._rows()
        rows[1000] = _wsl_ps_row(1000, 900, 10, 10, "pts/0",
                                 "node /usr/lib/codex/codex.js run",
                                 comm="npm")
        self.assertIs(canonical_terminal_attachment(1001, (1000,), rows),
                      TerminalAttachment.ATTACHED)

    def test_attached_agent_kept_even_if_background(self):
        # AC-PROC-03：pgid != tpgid 的 tmux agent 保留
        rows = {2000: _wsl_ps_row(2000, 1, 20, 999, "pts/1",
                                  "/usr/bin/claude")}
        self.assertIs(canonical_terminal_attachment(2000, (), rows),
                      TerminalAttachment.ATTACHED)

    def test_missing_rows_is_unknown(self):
        self.assertIs(canonical_terminal_attachment(31337, (), {}),
                      TerminalAttachment.UNKNOWN)


# ------------------------------------------------------- WslProcessProbe 过滤

class WslDetachedFilterTests(unittest.TestCase):
    def _scan(self, rows):
        probe = WslProcessProbe()

        def fake_running(self):
            return DistroInventory(("Ubuntu",), True)

        def fake_ps(self, distro, exclude_pids):
            return rows

        def fake_meta(self, distro, pids):
            return {pid: {"cwd": "/home/u/p", "ticks": str(pid), "uid": "1000",
                          "home": "/home/u", "env": {}} for pid in pids}

        with patch.object(WslProcessProbe, "_list_running_distros", fake_running), \
             patch.object(WslProcessProbe, "_ps_scan", fake_ps), \
             patch.object(WslProcessProbe, "_metadata", fake_meta):
            return probe.scan()

    def test_orphan_claude_not_in_authoritative_snapshot(self):
        # AC-PROC-01：PID 仍存在但 tty=?/tpgid=-1 → 不进入 snapshot
        rows = [_wsl_ps_row(2000, 1, 20, -1, "?", "/usr/bin/claude")]
        out = self._scan(rows)
        self.assertEqual(out["wsl:Ubuntu"].instances, ())
        self.assertTrue(out["wsl:Ubuntu"].authoritative)

    def test_attached_agent_in_snapshot_with_attachment_field(self):
        rows = [_wsl_ps_row(2000, 1, 20, 20, "pts/1", "/usr/bin/claude")]
        out = self._scan(rows)
        self.assertEqual(len(out["wsl:Ubuntu"].instances), 1)
        self.assertIs(out["wsl:Ubuntu"].instances[0].terminal_attachment,
                      TerminalAttachment.ATTACHED)

    def test_codex_group_partial_attached_kept_whole(self):
        # AC-PROC-02：wrapper attached、runtime detached → 整组保留
        rows = [
            _wsl_ps_row(1000, 900, 10, 10, "pts/0",
                        "node /usr/lib/codex/codex.js run", comm="npm"),
            _wsl_ps_row(1001, 1000, 10, -1, "?", "/usr/bin/codex"),
        ]
        out = self._scan(rows)
        insts = out["wsl:Ubuntu"].instances
        self.assertEqual(len(insts), 1)
        self.assertEqual(insts[0].pid, 1001)
        self.assertIs(insts[0].terminal_attachment, TerminalAttachment.ATTACHED)

    def test_codex_group_all_detached_filtered(self):
        rows = [
            _wsl_ps_row(1000, 900, 10, -1, "?",
                        "node /usr/lib/codex/codex.js run", comm="npm"),
            _wsl_ps_row(1001, 1000, 10, -1, "?", "/usr/bin/codex"),
        ]
        out = self._scan(rows)
        self.assertEqual(out["wsl:Ubuntu"].instances, ())

    def test_detached_filtered_count_only_counts_non_sensitive_stat(self):
        rows = [_wsl_ps_row(2000, 1, 20, -1, "?", "/usr/bin/claude")]
        probe = WslProcessProbe()

        def fake_running(self):
            return DistroInventory(("Ubuntu",), True)

        def fake_ps(self, distro, exclude_pids):
            return rows

        def fake_meta(self, distro, pids):
            return {pid: {"cwd": "", "ticks": str(pid), "uid": "1000",
                          "home": "/home/u", "env": {}} for pid in pids}

        with patch.object(WslProcessProbe, "_list_running_distros", fake_running), \
             patch.object(WslProcessProbe, "_ps_scan", fake_ps), \
             patch.object(WslProcessProbe, "_metadata", fake_meta):
            probe.scan()
            probe.scan()
        self.assertEqual(probe.detached_filtered_count, 2)


# ------------------------------------------------------- Monitor 级联退出

def _make_monitor(wsl_enabled=True):
    config = MemoryConfig()
    config.set("monitor.wsl_enabled", wsl_enabled)
    monitor = Monitor(config)
    monitor._terminal_service = WindowsTerminalService(None)
    return monitor


class DetachedExitCascadeTests(unittest.TestCase):
    def test_detached_target_exits_with_cascade_in_one_commit(self):
        # AC-PROC-05：终端关闭（WSL 权威 census 已过滤 detached）后，
        # session tailer/window binding/observation binding 同一
        # _commit_exit 级联消失。
        monitor = _make_monitor()
        inst = AgentInstance(AgentKind.CLAUDE, 2000, "wsl:Ubuntu",
                             process_token="2000")
        monitor.instances = {inst.key: inst}
        monitor.snapshots = {inst.key: Snapshot(
            inst.key, inst.kind, inst.source, inst.pid)}
        monitor.window_bindings = {inst.key: TerminalWindowBinding(
            window=WindowIdentity(77, 5, 1.0, "CASCADIA"),
            confidence=WindowBindingConfidence.HIGH, reason="v3-score")}
        monitor.terminal_observation_bindings = {inst.key:
                                                 TerminalObservationBinding(
                                                     inst.key, (1, 2),
                                                     ObservationBindingConfidence.HIGH)}
        watcher = monitor._watchers[AgentKind.CLAUDE]
        watcher._instance_files[inst.key] = "/tmp/fake.jsonl"

        # 权威快照已无该实例（probe 层过滤 detached）
        result = {"wsl:Ubuntu": snap("wsl:Ubuntu", True)}
        with patch.object(monitor._probe, "snapshot", return_value=result):
            monitor._merge_instances(1000.0)

        self.assertNotIn(inst.key, monitor.instances)
        self.assertNotIn(inst.key, monitor.snapshots)
        self.assertNotIn(inst.key, monitor.window_bindings)
        self.assertNotIn(inst.key, monitor.terminal_observation_bindings)
        self.assertNotIn(inst.key, watcher._instance_files)

    def test_non_authoritative_scan_keeps_detached_candidate(self):
        # AC-PROC-04：WSL probe 失败（authoritative=False）时，即使上一轮
        # 已看到 Agent，也不得因“没有新 tty 数据”删除。
        monitor = _make_monitor()
        inst = AgentInstance(AgentKind.CLAUDE, 2000, "wsl:Ubuntu",
                             process_token="2000")
        monitor.instances = {inst.key: inst}
        result = {"wsl:Ubuntu": snap("wsl:Ubuntu", False, [inst],
                                     error="probe failed")}
        with patch.object(monitor._probe, "snapshot", return_value=result):
            monitor._merge_instances(1000.0)
        self.assertIn(inst.key, monitor.instances)

    def test_kind_disabled_exits_even_when_source_not_authoritative(self):
        # plan1 §4：用户关闭 claude kind 必须立即生效，不受 probe 失败影响
        config = MemoryConfig()
        config.set("monitor.agents.claude", False)
        monitor = Monitor(config)
        monitor._terminal_service = WindowsTerminalService(None)
        inst = AgentInstance(AgentKind.CLAUDE, 2000, "windows",
                             process_token="2000")
        monitor.instances = {inst.key: inst}
        result = {"windows": snap("windows", False, [inst],
                                  error="process_iter failed")}
        with patch.object(monitor._probe, "snapshot", return_value=result):
            monitor._merge_instances(1000.0)
        self.assertNotIn(inst.key, monitor.instances)

    def test_kind_still_enabled_keeps_instance_when_not_authoritative(self):
        # 反向保护：kind 未禁用 + probe 失败 → 保留（不因本修复误删）
        monitor = _make_monitor()
        inst = AgentInstance(AgentKind.CLAUDE, 2000, "windows",
                             process_token="2000")
        monitor.instances = {inst.key: inst}
        result = {"windows": snap("windows", False, [inst],
                                  error="process_iter failed")}
        with patch.object(monitor._probe, "snapshot", return_value=result):
            monitor._merge_instances(1000.0)
        self.assertIn(inst.key, monitor.instances)


# ------------------------------------------------------- native lease

def _strong_binding(pid=55, hwnd=77):
    return TerminalWindowBinding(
        window=WindowIdentity(hwnd, pid, 1.0, "CASCADIA_PROCESSING_WINDOW_CLASS"),
        confidence=WindowBindingConfidence.CONFIRMED,
        reason="windows-ancestor")


def _native_inst(pid=100, parent_alive=False):
    inst = AgentInstance(AgentKind.CLAUDE, pid, "windows",
                         process_token=str(pid))
    inst.external_parent_pid = 999
    inst.external_parent_alive = parent_alive
    return inst


class NativeTerminalLeaseTests(unittest.TestCase):
    def _monitor(self):
        monitor = _make_monitor(wsl_enabled=False)
        return monitor

    def test_two_broken_authoritative_generations_commit_exit(self):
        # AC-PROC-06：曾强绑定 + 外部 parent 连续 2 个 authoritative
        # generation 不存在且强绑定未恢复 → 退出
        monitor = self._monitor()
        inst = _native_inst()
        monitor.instances = {inst.key: inst}

        # gen1：强绑定存在 → arm
        monitor.window_bindings = {inst.key: _strong_binding()}
        out = monitor._prune_detached_native(
            {inst.key: inst}, {"windows": snap("windows", True, [inst], gen=1)},
            1000.0)
        self.assertEqual(out, [])
        self.assertIn(inst.key, monitor.instances)

        # gen2：强绑定消失 + parent 不存在 → broken=1，不退出
        monitor.window_bindings = {}
        out = monitor._prune_detached_native(
            {inst.key: inst}, {"windows": snap("windows", True, [inst], gen=2)},
            1030.0)
        self.assertEqual(out, [])
        self.assertIn(inst.key, monitor.instances)

        # 同 gen2 的下一 tick：不得重复计数
        out = monitor._prune_detached_native(
            {inst.key: inst}, {"windows": snap("windows", True, [inst], gen=2)},
            1031.0)
        self.assertEqual(out, [])
        self.assertIn(inst.key, monitor.instances)

        # gen3：第二次断裂 → 退出
        out = monitor._prune_detached_native(
            {inst.key: inst}, {"windows": snap("windows", True, [inst], gen=3)},
            1060.0)
        self.assertEqual(out, [inst.key])
        self.assertNotIn(inst.key, monitor.instances)
        self.assertNotIn(inst.key, monitor.window_bindings)

    def test_single_broken_generation_keeps_agent(self):
        monitor = self._monitor()
        inst = _native_inst()
        monitor.instances = {inst.key: inst}
        monitor.window_bindings = {inst.key: _strong_binding()}
        monitor._prune_detached_native(
            {inst.key: inst}, {"windows": snap("windows", True, [inst], gen=1)},
            1000.0)
        monitor.window_bindings = {}
        # gen2 broken=1；gen3 强绑定恢复 → reset
        monitor._prune_detached_native(
            {inst.key: inst}, {"windows": snap("windows", True, [inst], gen=2)},
            1030.0)
        monitor.window_bindings = {inst.key: _strong_binding()}
        monitor._prune_detached_native(
            {inst.key: inst}, {"windows": snap("windows", True, [inst], gen=3)},
            1060.0)
        monitor.window_bindings = {}
        monitor._prune_detached_native(
            {inst.key: inst}, {"windows": snap("windows", True, [inst], gen=4)},
            1090.0)
        # 只有 gen4 一次断裂 → 保留
        self.assertIn(inst.key, monitor.instances)

    def test_parent_unknown_never_counts(self):
        monitor = self._monitor()
        inst = _native_inst(parent_alive=None)
        monitor.instances = {inst.key: inst}
        monitor.window_bindings = {inst.key: _strong_binding()}
        monitor._prune_detached_native(
            {inst.key: inst}, {"windows": snap("windows", True, [inst], gen=1)},
            1000.0)
        monitor.window_bindings = {}
        for gen in (2, 3, 4, 5):
            monitor._prune_detached_native(
                {inst.key: inst},
                {"windows": snap("windows", True, [inst], gen=gen)},
                1000.0 + gen * 30)
        self.assertIn(inst.key, monitor.instances)

    def test_never_strong_bound_agent_never_pruned(self):
        monitor = self._monitor()
        inst = _native_inst()
        monitor.instances = {inst.key: inst}
        # 从未获得 windows-ancestor CONFIRMED：即使 parent 消失 N 轮也不删
        monitor.window_bindings = {}
        for gen in range(1, 8):
            monitor._prune_detached_native(
                {inst.key: inst},
                {"windows": snap("windows", True, [inst], gen=gen)},
                1000.0 + gen * 30)
        self.assertIn(inst.key, monitor.instances)
        self.assertEqual(monitor._native_terminal_leases, {})

    def test_scan_failure_does_not_count_generation(self):
        monitor = self._monitor()
        inst = _native_inst()
        monitor.instances = {inst.key: inst}
        monitor.window_bindings = {inst.key: _strong_binding()}
        monitor._prune_detached_native(
            {inst.key: inst}, {"windows": snap("windows", True, [inst], gen=1)},
            1000.0)
        monitor.window_bindings = {}
        # 非 authoritative：不递增、不判死
        for gen in (2, 3):
            monitor._prune_detached_native(
                {inst.key: inst},
                {"windows": snap("windows", False, [inst], gen=gen,
                                 error="probe failed")},
                1000.0 + gen * 30)
        lease = monitor._native_terminal_leases.get(inst.key)
        self.assertEqual(lease.broken_generations, 0)
        self.assertIn(inst.key, monitor.instances)

    def test_low_confidence_binding_does_not_arm(self):
        monitor = self._monitor()
        inst = _native_inst()
        monitor.instances = {inst.key: inst}
        monitor.window_bindings = {inst.key: TerminalWindowBinding(
            window=WindowIdentity(77, 55, 1.0, "CASCADIA"),
            confidence=WindowBindingConfidence.HIGH,
            reason="v3-score")}
        for gen in range(1, 5):
            monitor._prune_detached_native(
                {inst.key: inst},
                {"windows": snap("windows", True, [inst], gen=gen)},
                1000.0 + gen * 30)
        self.assertIn(inst.key, monitor.instances)
        self.assertEqual(monitor._native_terminal_leases, {})


# ------------------------------------------------------- 安全不变量

class NoKillInvariantTests(unittest.TestCase):
    def test_agents_never_terminate_or_signal_orphan_processes(self):
        # AC-PROC-07：DeskPet 观察层绝不 kill/terminate/signal 残余进程。
        # v4.3.1 受控例外（plan2 §13.2 / DoD#21）：pet/skins.py 的
        # ConverterJob 必须能终止 DeskPet 自己 spawn 的 converter/
        # ffmpeg 子进程树（Windows Job Object + cancel），否则退出时
        # 遗留孤儿转换进程。该例外只限 ConverterJob 类内部——只管理
        # 自有子进程，绝不触碰用户 Agent 进程。
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        banned = ("TerminateProcess", "terminate(", ".kill(", "os.kill",
                  "send_signal", "taskkill", "GenerateConsoleCtrlEvent")
        for module in ("agents", "pet", "actions"):
            for path in (root / module).glob("*.py"):
                text = path.read_text(encoding="utf-8")
                if path.name == "skins.py":
                    _head, sep, tail = text.partition("class ConverterJob:")
                    self.assertTrue(sep, "skins.py 应包含 ConverterJob")
                    _block, sep2, rest = tail.partition("\ndef build_skin")
                    self.assertTrue(sep2, "ConverterJob 后应有 build_skin")
                    text = _head + rest   # 检查 ConverterJob 之外的全部源码
                for token in banned:
                    self.assertNotIn(
                        token, text,
                        f"{path.name} 引入了 {token}（违反不 kill 合同）")


if __name__ == "__main__":
    unittest.main()
