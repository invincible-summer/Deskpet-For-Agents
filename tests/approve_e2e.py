"""只读会话审批安全回归：确认不会向现有终端注入按键。

受控 Codex 的真实审批链路由 ``tests.test_managed`` 覆盖；现有终端只读
监听没有可验证的后台响应通道，``approver.send_approval`` 必须明确拒绝。
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from actions import approver
from agents.models import AgentKind, Snapshot, Status


def main() -> int:
    snapshot = Snapshot(
        key="windows|codex|readonly",
        kind=AgentKind.CODEX,
        source="windows",
        pid=4242,
        status=Status.WAITING,
    )
    with patch.object(approver.winkeys, "raise_window") as raise_window:
        ok, message = approver.send_approval(
            None, snapshot, "approve", saved_title="DeskPetAP"
        )
    passed = (not ok and "只读" in message and not raise_window.called)
    print(f"  [{'PASS' if passed else 'FAIL'}] 只读会话拒绝按键批复 {message}")
    print(f"  [{'PASS' if not raise_window.called else 'FAIL'}] 未调用终端输入路径")
    return 0 if passed and not raise_window.called else 1


if __name__ == "__main__":
    raise SystemExit(main())
