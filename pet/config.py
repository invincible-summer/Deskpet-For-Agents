"""配置模块：V3 默认值、加载、保存、v2→v3 迁移（plan.md §44/§45）。"""
import copy
import json
import os
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
ASSETS_DIR = os.path.join(ROOT, "assets")
PETS_DIR = os.path.join(ASSETS_DIR, "pets")
CACHE_DIR = os.path.join(ASSETS_DIR, "cache")

DEFAULTS = {
    "skin": "amiya",
    "scale": 1.0,                # 宠物+气泡整体缩放（改变后会重新生成对应尺寸的 GIF 缓存）
    "speed": 1.0,                # 动画播放速度倍率
    "animated": True,            # 动态 / 静态（静态=只播第 0 帧）
    "topmost": True,             # 窗口置顶
    "tray_enabled": True,        # 托盘图标
    "pet_pos": None,             # [x, y] 锚点（桌宠底部中心）位置
    "bubble": {
        "enabled": True,         # 气泡总开关（右键可暂时关闭只留桌宠）
        "font_family": "Microsoft YaHei UI",
        "font_size": 11,
        "font_color": "#1f2430",
        "bg": "#fffdf8",
        "border": "#d7dfdc",
        "height": 132,
        "relative_width": 1.0,
        "relative_height": 1.0,
        "relative_font": 1.0,
        "width": 300,            # 固定宽度（不再自适应）
        "max_lines": 2,          # 固定行数
        "autohide_sec": 8,
        "always_visible": True,
    },
    "monitor": {
        "agents": {"claude": True, "codex": True, "kimi": True, "pi": True},
        "windows_enabled": True,
        "wsl_enabled": True,
        "windows_scan_sec": 3.0,
        "wsl_scan_sec": 3.0,
        "file_poll_sec": 0.5,
        "session_scan_sec": 3.0,
        "gone_grace_sec": 15.0,       # 进程消失宽限期：扫描抖动不立刻判定退出
        "activity_grace_sec": 10.0,   # 活动型 WORKING 证据的宽限（plan §28）
        "active_file_window_sec": 180,
        "terminal_observer": True,
        "pinned": "",                 # 手动钉住的主绑定实例 key（空=自动跟随）
    },
    "privacy": {
        "terminal_text_to_disk": False,
        "session_text_to_disk": False,
        "goal_max_chars": 120,
        "summary_max_chars": 160,
    },
    "animation_cache_mb": 48,
    "config_version": 3,
    "force_state": "",           # 锁定动画：空=自动；walk/attack/die/special/sleep
    "convert": {"height": 240, "fps": 12},
}

# V2 → V3 迁移时删除的旧键（plan §45）
_LEGACY_KEYS = (
    "connection_mode",
    "managed",
    "auto_approve",
    "keys",
    "approve_restore_focus",
    "window_instances",
    "monitor.session_bindings",
    "monitor.waiting_quiet_sec",
    "monitor.working_hold_sec",
)


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    if not isinstance(override, dict):
        return out
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _drop_legacy(data: dict):
    for key in _LEGACY_KEYS:
        parts = key.split(".")
        node = data
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if isinstance(node, dict):
            node.pop(parts[-1], None)
    if isinstance(data.get("monitor"), dict):
        # 旧 pinned key 不含进程 incarnation token，一律清空（plan §45）。
        data["monitor"]["pinned"] = ""


class Config:
    def __init__(self):
        self._lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        self.migration_notice = False       # v2→v3 一次性提示
        self.load()

    def load(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, ValueError):
            self.data = copy.deepcopy(DEFAULTS)
            return
        if not isinstance(loaded, dict):
            self.data = copy.deepcopy(DEFAULTS)
            return
        version = loaded.get("config_version", 0)
        if not isinstance(version, int) or version < 3:
            # V2 → V3：废弃旧审批/受控配置，清空运行期绑定
            _drop_legacy(loaded)
            loaded["config_version"] = 3
            self.migration_notice = True
        self.data = _deep_merge(DEFAULTS, loaded)
        if self.data.get("skin") == "default":     # 旧版皮肤名迁移
            self.data["skin"] = "amiya"

    def save(self):
        # Same-directory atomic replacement avoids half-written settings on exit.
        with self._lock:
            temporary = None
            try:
                fd, temporary = tempfile.mkstemp(prefix=".deskpet-config-", dir=ROOT)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                os.replace(temporary, CONFIG_PATH)
            except OSError:
                pass
            finally:
                if temporary and os.path.exists(temporary):
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass

    # 便捷访问
    def get(self, path, default=None):
        with self._lock:
            node = self.data
            for part in path.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return copy.deepcopy(node) if isinstance(node, (dict, list)) else node

    def set(self, path, value):
        with self._lock:
            node = self.data
            parts = path.split(".")
            for part in parts[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = {}
                    node[part] = child
                node = child
            node[parts[-1]] = copy.deepcopy(value)
