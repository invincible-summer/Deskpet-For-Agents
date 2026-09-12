"""Desktop 宿主 app 级窗口激活（plan2 §10/§14）。

边界（与 terminal activation 的差异）：
  * 只唤醒应用主窗口（capability = APP_ONLY），不承诺跳转到具体
    会话/线程——4.4.0 没有 codex://threads/<id> 或 ZCode 私有导航
    的稳定官方合同，绝不调用；
  * 激活前重新校验宿主进程 incarnation（PID + create_time），窗口
    属主 PID 必须是宿主或同 exe 家族进程；
  * 多窗口无法确定会话归属时按 Z-order 取最近主窗口做 app-level
    激活；
  * SetForegroundWindow 被 OS 拒绝 → FlashWindowEx 提醒，不绕过；
  * 不发送键盘、不点击 UIA、不操作剪贴板（复用 actions/winkeys
    的 fail-closed 原语，不重新实现 Win32 调用）。
"""
import os

from .models import ActivationCode, ActivationResult, AgentInstance, AgentKind, AgentSurface
from actions import winkeys

# Desktop 宿主/同 app 进程的 exe basename 证据（plan2 §4.3/§4.4）；
# Electron 渲染进程与主进程同 exe，因此窗口属主 exe 匹配即可信。
_HOST_EXE_BASENAMES = {
    AgentKind.CODEX: frozenset({"chatgpt.exe", "codex.exe"}),
    AgentKind.ZCODE: frozenset({"zcode.exe"}),
}


def _window_owner_exe_matches(kind: AgentKind, pid: int) -> bool:
    try:
        import psutil
        exe = str(psutil.Process(int(pid)).exe() or "")
    except Exception:
        return False
    return os.path.basename(exe.lower()) in _HOST_EXE_BASENAMES.get(
        kind, frozenset())


class DesktopWindowService:
    """无状态服务：每次激活都完整重校验身份（fail-closed）。"""

    def activate_host(self, instance: AgentInstance) -> ActivationResult:
        host_pid = int(getattr(instance, "host_pid", 0) or 0)
        host_token = str(getattr(instance, "host_process_token", "") or "")
        kind = getattr(instance, "kind", None)
        if not host_pid or not host_token or kind not in _HOST_EXE_BASENAMES:
            return ActivationResult(ActivationCode.NO_BINDING)
        # 1) 宿主进程 incarnation 重校验（PID 复用防护，plan2 §10.1）
        try:
            import psutil
            created = float(psutil.Process(host_pid).create_time())
        except Exception:
            return ActivationResult(
                ActivationCode.AGENT_GONE, detail="host process gone")
        if f"{created:.3f}" != host_token:
            return ActivationResult(
                ActivationCode.AGENT_GONE, detail="host incarnation changed")
        # 2) 顶层可见窗口候选：属主必须是宿主 PID 或同 exe 家族进程
        #    （EnumWindows 返回 Z-order，顶部优先 = 最近使用主窗口）
        candidates = []
        for hwnd, pid, _title, _cls in winkeys.enum_windows():
            if pid == host_pid or _window_owner_exe_matches(kind, pid):
                candidates.append(hwnd)
        if not candidates:
            return ActivationResult(
                ActivationCode.NO_BINDING, detail="no host window")
        # 3) 完整身份建立 + 验证（HWND/PID/create_time/class 四元组）
        identity = winkeys.window_identity(candidates[0])
        if identity is None:
            return ActivationResult(
                ActivationCode.NO_BINDING, detail="window identity failed")
        if not winkeys.validate_window(identity):
            return ActivationResult(
                ActivationCode.STALE_WINDOW, detail="window revalidated")
        # 4) 恢复 + 前置；被拒只闪烁（plan2 §10.6/§10.7）
        winkeys.restore_window(identity.hwnd)
        if not winkeys.try_set_foreground(identity.hwnd):
            winkeys.flash_window(identity.hwnd)
            return ActivationResult(ActivationCode.FOREGROUND_DENIED)
        return ActivationResult(ActivationCode.OK)


def is_desktop_instance(instance) -> bool:
    return (getattr(instance, "surface", AgentSurface.TERMINAL)
            is AgentSurface.DESKTOP)
