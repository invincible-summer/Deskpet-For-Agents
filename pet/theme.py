"""Dashboard 主题 token（v4plan §13.1）。禁止到处硬编码颜色。

状态永远同时有文字/icon 表达，不只靠颜色。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    # 页面表面：冷白底 + 明确的卡片层级，避免整页都是同一种灰。
    page: str = "#F4F6F9"
    surface: str = "#FFFFFF"
    surface_subtle: str = "#EDF0F5"
    surface_alt: str = "#F8FAFC"
    border: str = "#DEE4ED"
    border_strong: str = "#C6D0DE"

    # 文字
    text: str = "#202A3B"
    text_secondary: str = "#647086"
    text_muted: str = "#8993A3"

    # 强调
    accent: str = "#4268CE"
    accent_soft: str = "#EDF2FF"

    # 左侧导航使用深色面，正文和导航有清晰空间边界。
    nav: str = "#1C2534"
    nav_surface: str = "#253146"
    nav_text: str = "#BDC8DA"
    nav_hover_bg: str = "#29374D"
    nav_selected_bg: str = "#304463"
    nav_selected_fg: str = "#F4F7FF"

    # 状态色（同时必须配文字/icon）
    working: str = "#2F7D62"
    done: str = "#3D8A76"
    waiting: str = "#A2672B"
    error: str = "#B35C5C"
    unknown: str = "#78867F"

    # 控件
    nav_active_bg: str = "#E5EBF7"
    nav_active_fg: str = "#3254AF"
    control_radius: int = 4
    card_radius: int = 8

    # 字体族
    font_family: str = "Segoe UI"
    font_fallbacks: tuple = ("Microsoft YaHei UI", "Microsoft YaHei")
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
    owner = root._root()
    cache = getattr(owner, "_deskpet_fonts", None)
    if cache is None:
        cache = owner._deskpet_fonts = {}
    key = (size, bold, mono)
    if key in cache:
        return cache[key]
    if mono:
        font = tkfont.Font(root=root, family=theme.mono_family, size=size)
        cache[key] = font
        return font
    families = None
    try:
        available = _available_families(root)
        for family in (theme.font_family,) + theme.font_fallbacks:
            if family in available:
                families = family
                break
    except Exception:
        families = None
    font = tkfont.Font(
        root=root,
        family=families or theme.font_fallbacks[-1],
        size=size,
        weight="bold" if bold else "normal")
    cache[key] = font
    return font


STATUS_COLOR = {
    "working": LIGHT.working,
    "done": LIGHT.done,
    "waiting": LIGHT.waiting,
    "input": LIGHT.waiting,
    "error": LIGHT.error,
    "idle": LIGHT.unknown,
    "unknown": LIGHT.unknown,
}


def configure_dashboard_styles(root):
    """Named ttk styles keep dashboard controls consistent without changing the app theme."""
    from tkinter import ttk
    style = ttk.Style(root)
    font = pick_font(root, 10)
    # Retain font objects for the lifetime of the window.
    root._dashboard_control_font = font
    for kind in ("TButton", "TCheckbutton", "TRadiobutton", "TCombobox", "TSpinbox"):
        name = "Dashboard." + kind
        style.configure(name, font=font, foreground=LIGHT.text,
                        background=LIGHT.surface, padding=(10, 7),
                        borderwidth=1, relief="flat", focusthickness=1)
        # State maps must never change control geometry. In particular, native
        # themes may otherwise inherit active font/padding/border definitions.
        style.map(name, font=[], padding=[], borderwidth=[],
                  relief=[("pressed", "flat"), ("active", "flat")])
        style.map(name, foreground=[("disabled", LIGHT.text_secondary)],
                  background=[("pressed", LIGHT.nav_active_bg),
                              ("active", LIGHT.surface_subtle)])
    style.configure("Dashboard.Primary.TButton", font=font,
                    foreground="#FFFFFF", background=LIGHT.accent,
                    padding=(12, 7), borderwidth=1, relief="flat")
    style.map("Dashboard.Primary.TButton", font=[], padding=[], borderwidth=[],
              relief=[("pressed", "flat"), ("active", "flat")])
    style.map("Dashboard.Primary.TButton",
              foreground=[("disabled", LIGHT.text_muted),
                          ("pressed", "#FFFFFF"), ("active", "#FFFFFF")],
              background=[("disabled", LIGHT.surface_subtle),
                          ("pressed", LIGHT.nav_active_fg),
                          ("active", LIGHT.nav_active_fg)])
    style.configure("Dashboard.Horizontal.TScale", background=LIGHT.surface,
                    troughcolor=LIGHT.surface_subtle, borderwidth=0,
                    lightcolor=LIGHT.accent, darkcolor=LIGHT.accent)
    # Native Windows scale ignores palette colors; use only clam's scale
    # elements, preserving the native theme for all other application widgets.
    if "clam" in style.theme_names():
        # Own the button element tree instead of inheriting a Windows theme's
        # active-state drawing. The same metrics apply to normal/hover/focus.
        for part in ("border", "focus", "padding", "label"):
            name = "Dashboard.Button." + part
            if name not in style.element_names():
                style.element_create(name, "from", "clam", "Button." + part)
        button_layout = [("Dashboard.Button.border", {
            "sticky": "nswe", "children": [("Dashboard.Button.focus", {
                "sticky": "nswe", "children": [("Dashboard.Button.padding", {
                    "sticky": "nswe", "children": [
                        ("Dashboard.Button.label", {"sticky": "nswe"})]})]})]})]
        for name in ("Dashboard.TButton", "Dashboard.Primary.TButton"):
            style.layout(name, button_layout)
            style.configure(name, bordercolor=LIGHT.border,
                            lightcolor=LIGHT.border, darkcolor=LIGHT.border)
        style.configure("Dashboard.Primary.TButton", bordercolor=LIGHT.accent,
                        lightcolor=LIGHT.accent, darkcolor=LIGHT.accent)
        for part in ("trough", "slider"):
            name = "Dashboard.Horizontal.Scale." + part
            if name not in style.element_names():
                style.element_create(name, "from", "clam", "Horizontal.Scale." + part)
        style.layout("Dashboard.Horizontal.TScale", [
            ("Dashboard.Horizontal.Scale.trough", {"sticky": "ew", "children": [
                ("Dashboard.Horizontal.Scale.slider", {"side": "left", "sticky": ""})]})])
        style.configure("Dashboard.Horizontal.TScale", background=LIGHT.accent,
                        bordercolor=LIGHT.accent, troughcolor=LIGHT.surface_subtle,
                        sliderlength=16, sliderthickness=16, gripcount=0)
        style.map("Dashboard.Horizontal.TScale",
                  background=[("active", LIGHT.nav_active_fg)])
