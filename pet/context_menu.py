"""TkContextMenuController（DP43-R14 §5）：单一 Tk Pet context menu owner。

只负责 Tk context menu 的生命周期与 deferred 语义发布，不懂
Agent/skin/business logic，不做 click-away polling、不绑定全局
FocusOut、不加 timer、不调用任何 Win32 foreground helper。

严格合同（plan §5）：

  1. 同一 Tk interpreter 同时最多一个 App-owned Pet context menu；
  2. show() 前先 dismiss 旧对象；
  3. menu 生命周期固定 create → build → tk_popup → finally
     unpost/release/destroy → clear owner；
  4. deferred() 生成的 menu command 不直接执行业务 command——菜单
     仍处于 native/Tk 交互阶段时只重排 idle，菜单确定性销毁
     （active 为 False）后才通过 root.after_idle 执行一次；
  5. deferred action 运行前检查 is_closing()；closing 状态丢弃
     （quit 本身以 allow_when_closing=True 豁免）；
  6. menu build 失败同样 finally 清理（不留悬浮死菜单）；
  7. shutdown()/dismiss() 幂等。
"""
from __future__ import annotations

import tkinter as tk


class TkContextMenuController:
    """一个 controller，一个 active popup，一套 deferred 语义。"""

    def __init__(self, root, *, is_closing=None):
        self._root = root
        self._is_closing = is_closing or (lambda: False)
        self._menu: tk.Menu | None = None
        self._owner = None

    # ------------------------------------------------------------ 状态
    @property
    def active(self) -> bool:
        """当前是否有本 controller 拥有的 popup menu。"""
        return self._menu is not None

    @property
    def owner(self):
        """当前 popup 的语义 owner（PetView）；无 popup 为 None。"""
        return self._owner

    # ------------------------------------------------------------ deferred
    def deferred(self, command, *args, **kwargs):
        """把一个业务 action 包装成 menu command。

        返回的 callable 作为 Tk menu entry 的 command 使用；被菜单
        调用时只发布一次 after_idle，不在菜单交互阶段同步执行业务。
        """
        allow_when_closing = bool(kwargs.pop("allow_when_closing", False))

        def _publish():
            self._root.after_idle(
                lambda: self._run(command, args, kwargs,
                                  allow_when_closing))

        return _publish

    def _run(self, command, args, kwargs, allow_when_closing):
        if self._menu is not None:
            # 菜单尚未确定性销毁（仍在 native/Tk 交互/teardown 阶段）：
            # 再等一个 idle，绝不在菜单存活期间做生命周期动作
            self._root.after_idle(
                lambda: self._run(command, args, kwargs,
                                  allow_when_closing))
            return
        if self._is_closing() and not allow_when_closing:
            return   # closing 状态丢弃普通 action（quit 豁免）
        command(*args, **kwargs)

    # ------------------------------------------------------------ 生命周期
    def show(self, owner, x_root, y_root, build_fn) -> None:
        """显示一个 popup：先 dismiss 旧的，再 create → build →
        tk_popup → finally destroy → clear owner。"""
        self.dismiss()
        menu = tk.Menu(self._root, tearoff=0)
        self._menu = menu
        self._owner = owner
        try:
            build_fn(menu)
            menu.tk_popup(int(x_root), int(y_root))
        except Exception:
            pass   # build/popup 失败：finally 仍确定性销毁（§16）
        finally:
            self._destroy(menu)
            if self._menu is menu:
                self._menu = None
                self._owner = None

    def dismiss(self) -> None:
        """确定性销毁当前 popup；幂等。"""
        menu = self._menu
        self._menu = None
        self._owner = None
        self._destroy(menu)

    def shutdown(self) -> None:
        """App 退出路径：等价 dismiss（幂等）。"""
        self.dismiss()

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _destroy(menu) -> None:
        """幂等销毁一个 popup menu；任何阶段失败都不抛 TclError。"""
        if menu is None:
            return
        for op in ("grab_release", "unpost", "destroy"):
            try:
                getattr(menu, op)()
            except tk.TclError:
                pass
