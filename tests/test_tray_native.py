"""DP43-R15 Tray 原生协议/生命周期/菜单测试（plan §12.1/§12.2）。

全部用 mock/注入验证协议合同——绝不在 CI 里真弹 native 菜单（AGENTS：
native menu 会交互阻塞）。真机验收走 plan §13 AC431-UI-01/04。
"""
import copy
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pet.config import DEFAULTS


class MemoryConfig:
    def __init__(self):
        self.data = copy.deepcopy(DEFAULTS)
        self.data['tray_enabled'] = False
        self.migration_notice = False

    def get(self, path, default=None):
        node = self.data
        for p in path.split('.'):
            if not isinstance(node, dict) or p not in node:
                return default
            node = node[p]
        return node

    def set(self, path, value):
        node = self.data
        parts = path.split('.')
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value

    def save(self):
        pass


def make_app(cfg=None):
    from pet.app import PetApp
    from pet.petview import PetView
    with\
         patch.object(PetView, 'load_skin', lambda self, bm: None):
        return PetApp(cfg or MemoryConfig())


def drain(icon):
    out = []
    while True:
        try:
            out.append(icon.events.get_nowait())
        except queue.Empty:
            return out


# ================================================================ §12.1 协议
class TrayShellApiTests(unittest.TestCase):
    """NIM_ADD / NIM_SETVERSION 的结果必须进入真实生命周期状态。"""

    def _icon(self):
        from pet.tray import TrayIcon
        icon = TrayIcon.__new__(TrayIcon)
        from pet.tray import TRAY_EVENT_QUEUE_MAX
        icon.events = queue.Queue(maxsize=TRAY_EVENT_QUEUE_MAX)
        icon.dropped_events = 0
        icon.tooltip = "t"
        icon._thread = None
        icon._thread_id = 0
        icon._state_lock = threading.Lock()
        icon._state = None   # 由测试显式设置
        icon._last_error = ""
        icon._ready = threading.Event()
        icon._stopped = threading.Event()
        icon._hwnd = 0x1234
        icon._nid = None
        icon._hicon = None
        icon._owns_hicon = False
        icon._menu_snapshot = None
        icon._menu_open_failures = 0
        return icon

    def test_add_and_setversion_success_reach_ready(self):
        import pet.tray as tray_mod
        from pet.tray import TrayState
        icon = self._icon()
        icon._state = TrayState.STARTING
        calls = []

        def fake_notify(msg, _data):
            calls.append(msg)
            return True

        with patch.object(tray_mod, '_register_hwnd', lambda h, i: None), \
             patch.object(tray_mod.user32, 'CreateWindowExW',
                          return_value=0x1234), \
             patch.object(icon, '_load_icon', return_value=(0x55, False)), \
             patch.object(tray_mod.shell32, 'Shell_NotifyIconW',
                          side_effect=fake_notify):
            ok = icon._add_icon()
        self.assertTrue(ok)
        self.assertEqual(calls, [tray_mod.NIM_ADD, tray_mod.NIM_SETVERSION])
        self.assertEqual(icon._state, TrayState.STARTING)   # 状态由 _run 推进

    def test_add_failure_is_failed_not_ready(self):
        import pet.tray as tray_mod
        from pet.tray import TrayState
        icon = self._icon()
        icon._state = TrayState.STARTING
        with patch.object(icon, '_load_icon', return_value=(0x55, False)), \
             patch.object(tray_mod.shell32, 'Shell_NotifyIconW',
                          return_value=False):
            ok = icon._add_icon()
        self.assertFalse(ok)
        self.assertEqual(icon._state, TrayState.FAILED)
        self.assertIn("NIM_ADD", icon.last_error())

    def test_setversion_failure_rolls_back_with_delete(self):
        import pet.tray as tray_mod
        from pet.tray import TrayState
        icon = self._icon()
        icon._state = TrayState.STARTING
        calls = []

        def fake_notify(msg, _data):
            calls.append(msg)
            return msg != tray_mod.NIM_SETVERSION

        with patch.object(icon, '_load_icon', return_value=(0x55, False)), \
             patch.object(tray_mod.shell32, 'Shell_NotifyIconW',
                          side_effect=fake_notify):
            ok = icon._add_icon()
        self.assertFalse(ok)
        self.assertEqual(calls, [tray_mod.NIM_ADD, tray_mod.NIM_SETVERSION,
                                 tray_mod.NIM_DELETE])
        self.assertEqual(icon._state, TrayState.FAILED)
        self.assertIn("NIM_SETVERSION", icon.last_error())
        self.assertIsNone(icon._nid)   # 回滚：不留语义模糊的 icon

    def test_setversion_writes_version4_to_native_union_bytes(self):
        """regression for Python attribute vs native struct bytes。

        必须从 Shell_NotifyIconW 收到的指针复制 raw bytes 再解析——
        `ver.uVersion == 4` 只证明 Python 属性被写，不能证明传给
        Win32 的结构体内存正确（历史上 union 字段曾被误写成普通
        attribute，native 字节保持 0）。
        """
        import ctypes
        import pet.tray as tray_mod
        from pet.tray import TrayState
        icon = self._icon()
        icon._state = TrayState.STARTING
        versions = []

        def notify(msg, data):
            if msg == tray_mod.NIM_SETVERSION:
                raw = ctypes.string_at(
                    data, ctypes.sizeof(tray_mod.NOTIFYICONDATAW))
                native = tray_mod.NOTIFYICONDATAW.from_buffer_copy(raw)
                versions.append(native.union.uVersion)
            return True

        with patch.object(icon, '_load_icon', return_value=(None, False)), \
             patch.object(tray_mod.shell32, 'Shell_NotifyIconW',
                          side_effect=notify):
            self.assertTrue(icon._add_icon())
        self.assertEqual(versions, [4],
                         "Shell must receive v4 bytes, not a Python attribute")

    def test_add_failure_skips_setversion_and_frees_owned_icon(self):
        """NIM_ADD 失败：不调 SETVERSION；owned HICON 释放；无残留 nid。"""
        import pet.tray as tray_mod
        from pet.tray import TrayState
        icon = self._icon()
        icon._state = TrayState.STARTING
        calls = []

        def fake_notify(msg, _data):
            calls.append(msg)
            return False

        with patch.object(icon, '_load_icon', return_value=(0x55, True)), \
             patch.object(tray_mod.user32, 'DestroyIcon',
                          return_value=True) as destroy, \
             patch.object(tray_mod.shell32, 'Shell_NotifyIconW',
                          side_effect=fake_notify):
            ok = icon._add_icon()
        self.assertFalse(ok)
        self.assertEqual(calls, [tray_mod.NIM_ADD],
                         "ADD 失败绝不能留下 legacy-protocol icon")
        self.assertEqual(icon._state, TrayState.FAILED)
        destroy.assert_called_once_with(0x55)   # owned icon 必须释放
        self.assertIsNone(icon._nid)

    def test_every_readd_sets_version_again(self):
        """版本不是持久 Shell 属性：每次 NIM_ADD 后都要重新 SETVERSION。"""
        import pet.tray as tray_mod
        from pet.tray import TrayState
        icon = self._icon()
        icon._state = TrayState.STARTING
        calls = []

        def fake_notify(msg, _data):
            calls.append(msg)
            return True

        with patch.object(icon, '_load_icon', return_value=(0x55, False)), \
             patch.object(tray_mod.shell32, 'Shell_NotifyIconW',
                          side_effect=fake_notify):
            self.assertTrue(icon._add_icon())
            icon._remove()          # icon 被移除（如 reconcile/Explorer 重启）
            self.assertTrue(icon._add_icon())   # show_icon 重新挂载路径
        self.assertEqual(calls, [tray_mod.NIM_ADD, tray_mod.NIM_SETVERSION,
                                 tray_mod.NIM_DELETE,
                                 tray_mod.NIM_ADD, tray_mod.NIM_SETVERSION])


class TrayVersion4CallbackTests(unittest.TestCase):
    """VERSION_4 语义输入映射（§6.2）。"""

    def _icon(self):
        from pet.tray import TrayIcon, TRAY_EVENT_QUEUE_MAX
        icon = TrayIcon("t")
        icon.events = queue.Queue(maxsize=TRAY_EVENT_QUEUE_MAX)
        return icon

    def test_mouse_left_up_exactly_one_restore(self):
        from pet.tray import WM_APP_TRAY, WM_LBUTTONUP, TrayIcon
        icon = self._icon()
        v4 = TrayIcon._ICON_ID << 16
        for _ in range(3):
            icon._handle_message(1, WM_APP_TRAY, 0, v4 | WM_LBUTTONUP)
        self.assertEqual([e.command for e in drain(icon)],
                         ["restore", "restore", "restore"])

    def test_keyboard_select_maps_to_restore(self):
        from pet.tray import WM_APP_TRAY, NIN_SELECT, NIN_KEYSELECT, TrayIcon
        icon = self._icon()
        v4 = TrayIcon._ICON_ID << 16
        icon._handle_message(1, WM_APP_TRAY, 0, v4 | NIN_KEYSELECT)
        icon._handle_message(1, WM_APP_TRAY, 0, v4 | NIN_SELECT)
        self.assertEqual([e.command for e in drain(icon)],
                         ["restore", "restore"])

    def test_keyboard_context_notification_opens_menu_once(self):
        """键盘 context selection 同样发 WM_CONTEXTMENU：一次手势一个菜单。"""
        from pet.tray import WM_APP_TRAY, WM_CONTEXTMENU, TrayIcon
        icon = self._icon()
        v4 = TrayIcon._ICON_ID << 16
        attempts = []
        with patch.object(icon, '_open_native_menu',
                          lambda: attempts.append(1)):
            icon._handle_message(1, WM_APP_TRAY, 0, v4 | WM_CONTEXTMENU)
        self.assertEqual(attempts, [1])
        self.assertTrue(icon.events.empty())   # 开菜单本身不是语义事件

    def test_foreign_icon_id_ignored(self):
        from pet.tray import WM_APP_TRAY, TrayIcon
        icon = self._icon()
        # HIWORD(lParam) 是别的 icon id（不存在）→ 忽略
        icon._handle_message(1, WM_APP_TRAY, 0, (0x77 << 16) | 0x0202)
        icon._handle_message(1, WM_APP_TRAY, 0, (0x77 << 16) | 0x007B)
        self.assertTrue(icon.events.empty())

    def test_mouse_move_and_down_not_semantic(self):
        from pet.tray import WM_APP_TRAY, TrayIcon
        icon = self._icon()
        v4 = TrayIcon._ICON_ID << 16
        icon._handle_message(1, WM_APP_TRAY, 0, v4 | 0x0200)   # LBUTTONDOWN
        icon._handle_message(1, WM_APP_TRAY, 0, v4 | 0x0201)   # LBUTTONDBLCLK
        icon._handle_message(1, WM_APP_TRAY, 0, v4 | 0x0206)   # MOUSEMOVE
        icon._handle_message(1, WM_APP_TRAY, 0, v4 | 0x020A)   # MOUSEMOVE
        self.assertTrue(icon.events.empty())

    def test_unregistered_hwnd_falls_to_defwindowproc(self):
        import pet.tray as tray_mod
        from pet.tray import _shared_wndproc
        sentinel = 0x4242
        with patch.object(tray_mod.user32, 'DefWindowProcW',
                          return_value=sentinel) as dwp:
            r = _shared_wndproc(0x9999, 0x0001, 0, 0)   # 未登记 HWND
        self.assertEqual(r, sentinel)
        dwp.assert_called_once()

    def test_registered_hwnd_routes_to_instance(self):
        import pet.tray as tray_mod
        from pet.tray import WM_APP_TRAY, TrayIcon, _shared_wndproc
        icon = TrayIcon("route")
        v4 = TrayIcon._ICON_ID << 16
        tray_mod._register_hwnd(0x5000, icon)
        try:
            with patch.object(tray_mod.user32, 'DefWindowProcW',
                              return_value=0x77) as dwp:
                _shared_wndproc(0x5000, WM_APP_TRAY, 0, v4 | 0x0202)
            dwp.assert_not_called()
            self.assertEqual([e.command for e in drain(icon)], ["restore"])
        finally:
            tray_mod._unregister_hwnd(0x5000)


class TrayNativeMenuTests(unittest.TestCase):
    """§6.4 原生 popup 生命周期（全部 mock Win32，不真弹）。"""

    def _icon(self):
        from pet.tray import NOTIFYICONDATAW, TrayAgentItem, TrayIcon
        from pet.tray import TrayMenuSnapshot
        icon = TrayIcon("menu")
        icon._hwnd = 0x1234
        icon._nid = NOTIFYICONDATAW()   # _set_focus_tray 需要 byref(nid)
        icon.update_menu_snapshot(TrayMenuSnapshot(
            pet_visible=True,
            agents=(TrayAgentItem("codex|1", "Codex · proj · 工作中"),
                    TrayAgentItem("claude|2", "Claude · proj · 等待"))))
        return icon

    def _menu_env(self, icon, track_result, foreground=0x1234):
        """patch Win32 菜单原语；返回 (记录器, patch 上下文列表)。

        GetCursorPos 不打桩：只读 API，无交互副作用（byref 参数无法
        经 mock 写回）。
        """
        import pet.tray as tray_mod
        log = []
        menus = iter([0xAAA1, 0xAAA2])   # root, agents submenu

        def fake_create():
            log.append(("CreatePopupMenu",))
            return next(menus)

        def fake_append(hmenu, flags, cmd, text):
            log.append(("Append", int(hmenu), int(flags), int(cmd), text))
            return True

        def fake_track(hmenu, flags, x, y, hwnd, params):
            log.append(("Track", int(hmenu), int(flags)))
            return track_result

        def fake_destroy(hmenu):
            log.append(("DestroyMenu", int(hmenu)))
            return True

        def fake_set_focus(msg, _nid):
            log.append(("NIM_SETFOCUS", msg))

        ctx = [
            patch.object(tray_mod.user32, 'CreatePopupMenu',
                         side_effect=fake_create),
            patch.object(tray_mod.user32, 'AppendMenuW',
                         side_effect=fake_append),
            patch.object(tray_mod.user32, 'TrackPopupMenuEx',
                         side_effect=fake_track),
            patch.object(tray_mod.user32, 'DestroyMenu',
                         side_effect=fake_destroy),
            patch.object(tray_mod.shell32, 'Shell_NotifyIconW',
                         side_effect=fake_set_focus),
            patch.object(tray_mod.user32, 'SetForegroundWindow',
                         return_value=True),
            patch.object(tray_mod.user32, 'GetForegroundWindow',
                         return_value=foreground),
        ]
        return log, ctx

    def _run_menu(self, icon, track_result, foreground=0x1234):
        """在完整 Win32 mock 环境下执行一次 _open_native_menu。"""
        log, ctx = self._menu_env(icon, track_result, foreground)
        for c in ctx:
            c.start()
        try:
            icon._open_native_menu()
        finally:
            for c in reversed(ctx):
                c.stop()
        return log

    def test_menu_flow_and_command_mapping(self):
        from pet.tray import _CMD_DASHBOARD
        icon = self._icon()
        log = self._run_menu(icon, _CMD_DASHBOARD)
        kinds = [e[0] for e in log]
        # 生命周期固定：Create → Append* → Track → DestroyMenu → SETFOCUS
        self.assertEqual(kinds[0], "CreatePopupMenu")
        self.assertEqual(kinds[-2], "DestroyMenu")
        self.assertEqual(kinds[-1], "NIM_SETFOCUS")
        self.assertIn("Track", kinds)
        self.assertEqual([e.command for e in drain(icon)], ["dashboard"])

    def test_cancel_result_zero_no_event(self):
        icon = self._icon()
        log = self._run_menu(icon, 0)   # TrackPopupMenuEx cancel
        self.assertTrue(icon.events.empty())
        kinds = [e[0] for e in log]
        self.assertEqual(kinds[-2], "DestroyMenu")   # cancel 也确定性销毁
        self.assertEqual(kinds[-1], "NIM_SETFOCUS")

    def test_agent_command_id_maps_to_exact_agent_key(self):
        from pet.tray import _AGENT_CMD_BASE
        icon = self._icon()
        self._run_menu(icon, _AGENT_CMD_BASE + 1)
        events = drain(icon)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].command, "activate")
        self.assertEqual(events[0].agent_key, "claude|2")

    def test_foreground_denied_opens_no_menu(self):
        icon = self._icon()
        log = self._run_menu(icon, 101, foreground=0xDEAD)
        kinds = [e[0] for e in log]
        self.assertNotIn("Track", kinds)   # 绝不打开无法 dismiss 的菜单
        self.assertEqual(icon.menu_open_failures(), 1)
        self.assertEqual(kinds[-2], "DestroyMenu")
        self.assertEqual(kinds[-1], "NIM_SETFOCUS")
        self.assertTrue(icon.events.empty())

    def test_request_stop_posts_cancelmode_first(self):
        import pet.tray as tray_mod
        from pet.tray import TrayState, WM_APP_QUIT
        icon = self._icon()
        icon._state = TrayState.READY
        icon._thread_id = 0x2222
        posted = []

        def fake_post(hwnd, msg, wparam, lparam):
            posted.append((int(hwnd or 0), int(msg)))

        with patch.object(tray_mod.user32, 'PostMessageW',
                          side_effect=fake_post), \
             patch.object(tray_mod.user32, 'PostThreadMessageW',
                          lambda tid, msg, w, l: posted.append(("tid", msg))):
            icon.request_stop()
        self.assertEqual(posted[0], (0x1234, tray_mod.WM_CANCELMODE))
        self.assertEqual(posted[1], (0x1234, WM_APP_QUIT))
        self.assertEqual(posted[2], ("tid", 0x0012))


# ================================================================ §6.3 queue
class TrayEventQueueTests(unittest.TestCase):
    """语义事件队列：有界、满不阻塞 wndproc、quit 语义不可丢。"""

    def test_bounded_queue_drops_and_counts(self):
        from pet.tray import (TRAY_EVENT_QUEUE_MAX, WM_APP_TRAY, TrayEvent,
                              TrayIcon)
        icon = TrayIcon.__new__(TrayIcon)   # 不启动线程：只测 wndproc 语义
        icon.events = queue.Queue(maxsize=TRAY_EVENT_QUEUE_MAX)
        icon.dropped_events = 0
        icon.quit_requested = threading.Event()
        for _ in range(TRAY_EVENT_QUEUE_MAX):
            icon.events.put_nowait(TrayEvent("restore"))
        with self.assertRaises(queue.Full):
            icon.events.put_nowait(TrayEvent("restore"))
        # 满队列下 1000 次 wndproc 调用全部立即返回（丢弃计数，不阻塞）。
        v4 = TrayIcon._ICON_ID << 16
        for _ in range(1000):
            self.assertEqual(
                icon._handle_message(1, WM_APP_TRAY, 0, v4 | 0x0202), 0)
        self.assertEqual(icon.dropped_events, 1000)
        self.assertEqual(icon.events.qsize(), TRAY_EVENT_QUEUE_MAX)

    def test_quit_latch_cannot_be_lost_behind_full_queue(self):
        from pet.tray import TrayEvent, TrayIcon
        icon = TrayIcon()
        for _ in range(icon.events.maxsize):
            icon._enqueue(TrayEvent("restore"))
        icon._enqueue(TrayEvent("quit"))
        self.assertTrue(icon.quit_requested.is_set())
        self.assertEqual(icon.dropped_events, 1)
        self.assertEqual(icon.events.qsize(), icon.events.maxsize)


class TrayHiconOwnershipTests(unittest.TestCase):
    """§6.8 HICON ownership。"""

    def test_shared_icon_never_destroyed(self):
        import pet.tray as tray_mod
        icon = tray_mod.TrayIcon("shared")
        icon._hicon = 0x77
        icon._owns_hicon = False
        with patch.object(tray_mod.user32, 'DestroyIcon') as destroy:
            icon._destroy_owned_hicon()
        destroy.assert_not_called()
        self.assertIsNone(icon._hicon)

    def test_owned_icon_destroyed_exactly_once(self):
        import pet.tray as tray_mod
        icon = tray_mod.TrayIcon("owned")
        icon._hicon = 0x88
        icon._owns_hicon = True
        with patch.object(tray_mod.user32, 'DestroyIcon') as destroy:
            icon._destroy_owned_hicon()
            icon._destroy_owned_hicon()   # 幂等：第二次不再 Destroy
        destroy.assert_called_once_with(0x88)

    def test_load_icon_marks_ownership(self):
        import pet.tray as tray_mod
        icon = tray_mod.TrayIcon("load")
        with patch('pet.icon.ensure_icon_ico',
                   return_value='fake.ico'), \
             patch('os.path.isfile', return_value=True), \
             patch.object(tray_mod.user32, 'LoadImageW',
                          return_value=0x99) as load_image:
            hicon, owns = icon._load_icon()
        self.assertEqual(hicon, 0x99)
        self.assertTrue(owns)
        load_image.assert_called_once()


# ================================================================ §12.2 generation
class TrayGenerationMatrixTests(unittest.TestCase):
    """reconcile 状态矩阵（§6.7）——mock TrayIcon 的生产路径模拟。"""

    def test_off_on_off_rapid_single_generation(self):
        from pet.tray import TrayState
        created = []

        class FakeTray:
            def __init__(self, tooltip=''):
                created.append(self)
                self.events = queue.Queue()
                self.dropped_events = 0
                self._state = TrayState.STARTING
                self._terminal = threading.Event()
                self.stops = 0

            def status(self):
                return self._state

            def start(self):
                self._state = TrayState.READY

            def request_stop(self):
                self.stops += 1
                self._state = TrayState.STOPPING

            def join_for_shutdown(self, timeout):
                return self._terminal.is_set()

            def terminate(self):
                self._terminal.set()
                self._state = TrayState.STOPPED

            def show_icon(self):
                pass

            def update_menu_snapshot(self, snapshot):
                pass

            def last_error(self):
                return ""

            def menu_open_failures(self):
                return 0

        with patch('pet.tray.TrayIcon', FakeTray):
            app = make_app()
            try:
                app.set_tray_enabled(True)     # ON
                self.assertEqual(len(created), 1)
                a = created[0]
                # OFF → ON → OFF 快速切换
                app.set_tray_enabled(False)
                app.set_tray_enabled(True)
                app.set_tray_enabled(False)
                self.assertEqual(len(created), 1,
                                 "STOPPING 期间绝不创建 replacement")
                self.assertIs(app.tray, a)
                self.assertGreaterEqual(a.stops, 1)
                a.terminate()
                app._reconcile_tray_runtime()  # terminal → reap
                self.assertIsNone(app.tray)
            finally:
                app.quit()

    def test_failed_generation_no_auto_retry_same_session(self):
        from pet.tray import TrayState
        created = []

        class FailingTray:
            def __init__(self, tooltip=''):
                created.append(self)
                self.events = queue.Queue()
                self.dropped_events = 0
                self._state = TrayState.FAILED

            def status(self):
                return self._state

            def start(self):
                pass

            def request_stop(self):
                pass

            def join_for_shutdown(self, timeout):
                return True

            def update_menu_snapshot(self, snapshot):
                pass

            def last_error(self):
                return "boom"

            def menu_open_failures(self):
                return 0

        with patch('pet.tray.TrayIcon', FailingTray):
            app = make_app()
            try:
                app.set_tray_enabled(True)
                self.assertEqual(len(created), 1)
                app._reconcile_tray_runtime()   # FAILED → reap
                self.assertTrue(app._tray_generation_failed)
                for _ in range(10):
                    app._reconcile_tray_runtime()
                self.assertEqual(len(created), 1,
                                 "FAILED 同一 desired session 不自动重建")
                # 用户显式重新开启 → 受控 retry
                app.set_tray_enabled(False)
                app.set_tray_enabled(True)
                self.assertEqual(len(created), 2)
            finally:
                app.quit()

    def test_hidden_pet_recovery_lease_keeps_tray(self):
        """pet 隐藏时即使 tray_enabled=False 也保留托盘恢复入口。"""
        from pet.tray import TrayState
        created = []

        class FakeTray:
            def __init__(self, tooltip=''):
                created.append(self)
                self.events = queue.Queue()
                self.dropped_events = 0
                self._state = TrayState.READY

            def status(self):
                return self._state

            def start(self):
                pass

            def request_stop(self):
                self._state = TrayState.STOPPING

            def join_for_shutdown(self, timeout):
                return True

            def update_menu_snapshot(self, snapshot):
                pass

            def last_error(self):
                return ""

            def menu_open_failures(self):
                return 0

        with patch('pet.tray.TrayIcon', FakeTray):
            app = make_app()
            try:
                app.hide_pet()   # tray_enabled=False，但恢复入口必须建立
                self.assertEqual(len(created), 1)
                # pet 恢复 + tray preference 仍为 false → tray 停止
                app.restore_pet_from_tray()
                app._reconcile_tray_runtime()
                self.assertEqual(created[0].status(), TrayState.STOPPING)
            finally:
                app.quit()

    def test_quit_event_dispatches_quit(self):
        from pet.tray import TrayEvent, TrayState

        class FakeTray:
            def __init__(self):
                self.events = queue.Queue()
                self.dropped_events = 0

            def status(self):
                return TrayState.READY

            def last_error(self):
                return ""

            def menu_open_failures(self):
                return 0

            def request_stop(self):
                pass

            def show_icon(self):
                pass

            def update_menu_snapshot(self, snapshot):
                pass

        app = make_app()
        try:
            fake = FakeTray()
            app.tray = fake
            fake.events.put_nowait(TrayEvent("quit"))
            with patch.object(app, "quit") as quit_mock:
                app._poll_tray_events()
            quit_mock.assert_called_once()
        finally:
            app.tray = None
            app.quit()


if __name__ == '__main__':
    unittest.main()
