"""开机自启动：HKCU Run 注册表项 + 健康验证/修复（v4plan §10）。

不引入管理员权限、服务或 Task Scheduler。注册表值存在 ≠ 可用：
repo/python 路径移动后 value 变 stale，必须能区分并一键修复。

校验依据（Microsoft Learn, Run and RunOnce Registry Keys）：
  * value 是 command line，最长 260 字符；
  * 我们比较 registered command 与当前 expected command。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import Enum

try:
    import winreg
except ImportError:
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "DeskPet"
MAX_COMMAND_LEN = 260   # Run key 官方上限


class AutostartState(str, Enum):
    HEALTHY = "healthy"     # 已注册且与当前路径一致
    MISSING = "missing"     # 未注册
    STALE = "stale"         # 已注册但路径/解释器与当前不一致
    UNAVAILABLE = "unavailable"   # 无 winreg（非 Windows）


@dataclass(frozen=True)
class AutostartStatus:
    state: AutostartState
    registered: bool
    healthy: bool
    expected_command: str
    registered_command: str
    reason: str = ""


@dataclass(frozen=True)
class AutostartResult:
    ok: bool
    enabled: bool
    reason: str = ""


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def expected_command() -> str:
    """当前 DeskPet 的期望启动命令（v4plan §10.2）。

    源码模式：pythonw.exe（无则 python.exe）+ 绝对 main.py；
    未来 frozen exe 经 sys.frozen feature-detect。
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.isfile(pyw) else sys.executable
    main_py = os.path.join(_repo_root(), "main.py")
    return f'"{exe}" "{main_py}"'


def _validate_command(command: str) -> str:
    """expected command 自检；返回错误描述（空 = 合法）。"""
    if not command:
        return "empty command"
    if len(command) > MAX_COMMAND_LEN:
        return f"command exceeds {MAX_COMMAND_LEN} chars"
    if command.count('"') % 2 != 0:
        return "unbalanced quotes"
    parts = [p for p in command.split('"') if p.strip()]
    if not parts:
        return "no executable"
    exe = os.path.expanduser(parts[0].strip())
    if not os.path.isfile(exe):
        return f"interpreter missing: {exe}"
    if not getattr(sys, "frozen", False):
        if len(parts) < 2:
            return "main.py missing from command"
        main_py = os.path.expanduser(parts[1].strip())
        if not os.path.isfile(main_py):
            return f"main.py missing: {main_py}"
    return ""


def _read_registered() -> str | None:
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as k:
            value, _type = winreg.QueryValueEx(k, VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def status() -> AutostartStatus:
    """三态健康：healthy / missing / stale（v4plan §10.3）。"""
    expected = expected_command()
    if winreg is None:
        return AutostartStatus(
            state=AutostartState.UNAVAILABLE, registered=False, healthy=False,
            expected_command=expected, registered_command="",
            reason="winreg unavailable")
    registered = _read_registered()
    if registered is None:
        return AutostartStatus(
            state=AutostartState.MISSING, registered=False, healthy=False,
            expected_command=expected, registered_command="",
            reason="value missing")
    if registered == expected:
        problem = _validate_command(expected)
        if problem:
            return AutostartStatus(
                state=AutostartState.STALE, registered=True, healthy=False,
                expected_command=expected, registered_command=registered,
                reason=problem)
        return AutostartStatus(
            state=AutostartState.HEALTHY, registered=True, healthy=True,
            expected_command=expected, registered_command=registered)
    return AutostartStatus(
        state=AutostartState.STALE, registered=True, healthy=False,
        expected_command=expected, registered_command=registered,
        reason="stale-command")


def is_enabled() -> bool:
    """兼容旧调用：注册表值存在（不验证健康）。"""
    return _read_registered() is not None


def set_enabled(enable: bool) -> AutostartResult:
    """设置开机自启；写入后重新读取验证，绝不只返回请求值（§10.3）。"""
    if winreg is None:
        return AutostartResult(ok=False, enabled=False,
                               reason="winreg unavailable")
    if enable:
        expected = expected_command()
        problem = _validate_command(expected)
        if problem:
            return AutostartResult(ok=False, enabled=False, reason=problem)
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, expected)
        except OSError as exc:
            return AutostartResult(ok=False, enabled=is_enabled(),
                                   reason=f"write failed: {exc}")
        # 写入后重新读取确认
        got = _read_registered()
        if got != expected:
            return AutostartResult(ok=False, enabled=bool(got),
                                   reason="verify-after-write failed")
        return AutostartResult(ok=True, enabled=True)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            try:
                winreg.DeleteValue(k, VALUE_NAME)
            except FileNotFoundError:
                pass
    except OSError as exc:
        return AutostartResult(ok=False, enabled=is_enabled(),
                               reason=f"delete failed: {exc}")
    return AutostartResult(ok=True, enabled=False)


def repair() -> AutostartResult:
    """一键修复 stale 注册（v4plan §10.3）。"""
    return set_enabled(True)


def toggle() -> AutostartResult:
    """按当前健康状态取反（stale 视为开启并修复）。"""
    st = status()
    if st.state == AutostartState.STALE:
        return repair()
    return set_enabled(st.state != AutostartState.HEALTHY)
