"""配置模块：默认值、加载、保存（config.json 放在项目根目录）。"""
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
        "wsl_enabled": True,
        "windows_scan_sec": 3.0,
        "wsl_scan_sec": 5.0,
        "file_poll_sec": 0.6,
        "waiting_quiet_sec": 15.0,      # 静默多少秒后推测"等待批复"
        "active_file_window_sec": 180,  # 会话文件多久内更新过才算活跃
        "working_hold_sec": 90.0,       # WORKING 保持：活动后静默这么久才允许转空闲（防闪跳）
        "gone_grace_sec": 45.0,         # 进程消失宽限期：扫描抖动不立刻判定退出
        "pinned": "",                   # 手动钉住的主绑定实例 key（空=自动选择）
    },
    "connection_mode": "hybrid",
    "managed": {"command": "codex", "history_limit": 300, "experimental": False},
    "animation_cache_mb": 48,
    "config_version": 2,
    "force_state": "",           # 锁定动画：空=自动；walk/attack/die/special/sleep
    "keys": {
        "claude": {"approve": "Return", "deny": "Escape"},
        "codex":  {"approve": "y",      "deny": "Escape"},
        "kimi":   {"approve": "Return", "deny": "Escape"},
        "pi":     {"approve": "Return", "deny": "Escape"},
    },
    "convert": {"height": 240, "fps": 12},
    "window_instances": {},      # 批复用：agent 实例 key -> 手动绑定的终端窗口标题
    "auto_approve": {"enabled": False},   # 旧版设置，仅用于迁移提示；新设置按受控会话保存
    "approve_restore_focus": True,        # 批复发送后把焦点还给原先窗口
}

# 待批复状态显示用的键名映射（气泡上提示用户会发送什么）
KEY_LABEL = {"Return": "Enter", "Escape": "Esc", "space": "空格", "y": "y", "n": "n"}


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


class Config:
    def __init__(self):
        self._lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        self.migration_notice = False
        self.load()

    def load(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                self.data = _deep_merge(DEFAULTS, json.load(f))
        except (OSError, ValueError):
            self.data = copy.deepcopy(DEFAULTS)
        legacy_auto = self.data.get("auto_approve")
        self.migration_notice = bool(
            isinstance(legacy_auto, dict) and legacy_auto.get("enabled")
        )
        self.data["auto_approve"] = {"enabled": False}
        self.data["config_version"] = 2
        if self.data.get("connection_mode") not in ("hybrid", "readonly"):
            self.data["connection_mode"] = "hybrid"
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
