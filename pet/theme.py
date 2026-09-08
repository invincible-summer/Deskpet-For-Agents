"""Dashboard 主题 token（v4plan §13.1）。禁止到处硬编码颜色。

状态永远同时有文字/icon 表达，不只靠颜色。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    # 表面
    page: str = "#F7F8F6"
    surface: str = "#FFFFFF"
    surface_subtle: str = "#F0F3F1"
    border: str = "#DCE3DF"

    # 文字
    text: str = "#202522"
    text_secondary: str = "#66716C"

    # 强调
    accent: str = "#4D7C6B"

    # 状态色（同时必须配文字/icon）
    working: str = "#4D7C6B"
    done: str = "#487F73"
    waiting: str = "#995F24"
    error: str = "#A55353"
    unknown: str = "#6D7772"

    # 控件
    nav_active_bg: str = "#E7EDEA"
    nav_active_fg: str = "#2F5D4F"
    control_radius: int = 4
    card_radius: int = 8

    # 字体族
    font_family: str = "Segoe UI Variable Text"
    font_fallbacks: tuple = ("Segoe UI", "Microsoft YaHei UI")
    mono_family: str = "Consolas"


LIGHT = Theme()


_FAMILIES_CACHE: set | None = None


def _available_families(root) -> set:
    global _FAMILIES_CACHE
    import tkinter.font as tkfont
    if _FAMILIES_CACHE is None:
        try:
            _FAMILIES_CACHE = set(tkfont.families(root))
        except Exception:
            _FAMILIES_CACHE = set()
    return _FAMILIES_CACHE


def pick_font(root, size: int = 10, bold: bool = False, mono: bool = False):
    """按主题字体族创建 tkfont；首个可用族生效（族列表进程内缓存）。"""
    import tkinter.font as tkfont
    theme = LIGHT
    if mono:
        return tkfont.Font(root=root, family=theme.mono_family, size=size)
    families = None
    try:
        available = _available_families(root)
        for family in (theme.font_family,) + theme.font_fallbacks:
            if family in available:
                families = family
                break
    except Exception:
        families = None
    return tkfont.Font(
        root=root,
        family=families or theme.font_fallbacks[-1],
        size=size,
        weight="bold" if bold else "normal")


STATUS_COLOR = {
    "working": LIGHT.working,
    "done": LIGHT.done,
    "waiting": LIGHT.waiting,
    "input": LIGHT.waiting,
    "error": LIGHT.error,
    "idle": LIGHT.unknown,
    "unknown": LIGHT.unknown,
}
