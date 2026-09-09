"""配置模块：V4.1 schema（config_version=4）、原子保存、备份、normalize。

v4plan §9 合同：
  * 继续使用项目内路径（config.json / assets/pets / assets/cache），
    不迁 %LOCALAPPDATA%；
  * save 永不静默吞错：commit() 返回 ConfigSaveResult；
  * 临时文件 → flush → 可选 fsync → backup → os.replace；
  * 主文件损坏 → 尝试 config.json.bak → DEFAULTS；
  * 加载后对用户可修改字段 clamp；未知键保留（向前兼容）；
  * 绝不持久化 runtime identity（PID/HWND/RuntimeId/WT_SESSION/
    exact key）；V3 的 monitor.pinned / gone_grace_sec 被清除。
"""
import copy
import json
import os
import tempfile
import threading
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
BACKUP_PATH = os.path.join(ROOT, "config.json.bak")
ASSETS_DIR = os.path.join(ROOT, "assets")
PETS_DIR = os.path.join(ASSETS_DIR, "pets")
CACHE_DIR = os.path.join(ASSETS_DIR, "cache")

# skins.py 在其顶部（路径 import 之前）先定义 BUILTIN_SKIN，
# 因此这里反向 import 不会循环失败。
from .skins import BUILTIN_SKIN  # noqa: E402

CONFIG_VERSION = 5

DEFAULTS = {
    # fresh install 默认程序化原创 fallback（v4.2.3 §10.4）：公开源码
    # 包不假定用户本机存在版权素材；已有用户 config 原样保留。
    "skin": BUILTIN_SKIN,
    "scale": 1.0,                # 宠物+气泡整体缩放（改变后重新生成 GIF 缓存）
    "speed": 1.0,                # 动画播放速度倍率
    "animated": True,            # 动态 / 静态（静态=只播第 0 帧）
    "topmost": True,             # 窗口置顶
    "tray_enabled": True,        # 托盘图标
    "pet_pos": None,             # [x, y] 锚点（桌宠底部中心）
    "bubble": {
        "enabled": True,
        "font_family": "Microsoft YaHei UI",
        "font_size": 11,
        "font_color": "#1f2430",
        "bg": "#fffdf8",
        "border": "#d7dfdc",
        "height": 132,
        "relative_width": 1.0,
        "relative_height": 1.0,
        "relative_font": 1.0,
        "width": 300,
        "max_lines": 2,
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
        "activity_grace_sec": 10.0,   # 活动型 WORKING 证据的宽限（活 Agent）
        "active_file_window_sec": 180,
        "terminal_observer": True,
        # V4.1 删除：gone_grace_sec（authoritative absence 立即退出）、
        # pinned（runtime focus 不持久化）
    },
    "presentation": {
        "concurrent": {
            # v4.3 §8.1：enabled/mode 不再持久化——每次进程启动由
            # PresentationController 固定初始化为 True + aggregate
            # （session runtime state），不恢复上次退出时的选择。
            "max_targets": 3,        # 展示上限 1..8（不是 Monitor 发现上限）
            "eligible_kinds": {"codex": True, "claude": True,
                               "kimi": True, "pi": True},
            "slots": [
                {"id": "pet-1", "selector": None,
                 "appearance": {"skin": None},
                 "placement": {"monitor": "", "u": None, "v": None,
                               "anchor": None, "manual": False}},
            ],
        },
    },
    "privacy": {
        "terminal_text_to_disk": False,
        "session_text_to_disk": False,
        "wsl_root_metadata_fallback": False,
        "goal_max_chars": 120,
        "summary_max_chars": 160,
    },
    "animation_cache_mb": 48,     # 全进程共享预算（v4plan §11）
    "config_version": CONFIG_VERSION,
    "force_state": "",            # 锁定动画：空=自动
    "convert": {"height": 240, "fps": 12},
}

# V2 → V3 迁移时删除的旧键（保留历史兼容）
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
    # V3 → V4.1 删除（v4plan §21）
    "monitor.pinned",
    "monitor.gone_grace_sec",
)

_KIND_KEYS = ("claude", "codex", "kimi", "pi")


@dataclass(frozen=True)
class ConfigSaveResult:
    ok: bool
    path: str
    error: str = ""


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


def _drop_path(data: dict, dotted: str):
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return
        node = node[part]
    if isinstance(node, dict):
        node.pop(parts[-1], None)


def _clamp(value, lo, hi):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def _normalize_slot_appearance(slot: dict) -> None:
    """v4.3 §7.1/§8.1：slot appearance 规范化。

    appearance None → {"skin": None}；dict → 只 normalize `skin`
    （必须为 str 或 None），未知 future keys 原样保留；其他类型重置。
    """
    appearance = slot.get("appearance")
    if appearance is None:
        slot["appearance"] = {"skin": None}
    elif isinstance(appearance, dict):
        skin = appearance.get("skin")
        appearance["skin"] = (str(skin).strip()
                              if isinstance(skin, str) and skin.strip()
                              else None)
    else:
        slot["appearance"] = {"skin": None}


def normalize(data: dict) -> dict:
    """加载后 clamp 用户可修改字段（v4plan §9.5）；未知键保留。"""
    data["scale"] = round(_clamp(data.get("scale", 1.0), 0.5, 2.0), 2)
    data["speed"] = round(_clamp(data.get("speed", 1.0), 0.1, 3.0), 2)
    data["animation_cache_mb"] = int(_clamp(
        data.get("animation_cache_mb", 48), 8, 256))
    bubble = data.get("bubble")
    if isinstance(bubble, dict):
        bubble["font_size"] = int(_clamp(bubble.get("font_size", 11), 8, 24))
        bubble["width"] = int(_clamp(bubble.get("width", 300), 160, 520))
        bubble["height"] = int(_clamp(bubble.get("height", 132), 112, 220))
        bubble["relative_width"] = _clamp(
            bubble.get("relative_width", 1.0), 0.7, 1.6)
        bubble["relative_height"] = _clamp(
            bubble.get("relative_height", 1.0), 0.8, 1.6)
    monitor = data.get("monitor")
    if isinstance(monitor, dict):
        agents = monitor.get("agents")
        if not isinstance(agents, dict):
            monitor["agents"] = {k: True for k in _KIND_KEYS}
        else:
            monitor["agents"] = {k: bool(agents.get(k, True))
                                 for k in _KIND_KEYS}
        # 节奏类配置的代码级 clamp（v4.1.1 §11）：配置文件手改异常值
        # 也不能制造高频 loop / 高频扫描。
        monitor["windows_scan_sec"] = _clamp(
            monitor.get("windows_scan_sec", 3.0), 1.0, 60.0)
        monitor["wsl_scan_sec"] = _clamp(
            monitor.get("wsl_scan_sec", 3.0), 1.0, 120.0)
        monitor["file_poll_sec"] = _clamp(
            monitor.get("file_poll_sec", 0.5), 0.2, 5.0)
        monitor["session_scan_sec"] = _clamp(
            monitor.get("session_scan_sec", 3.0), 1.0, 60.0)
        monitor["activity_grace_sec"] = _clamp(
            monitor.get("activity_grace_sec", 10.0), 1.0, 60.0)
        monitor["active_file_window_sec"] = _clamp(
            monitor.get("active_file_window_sec", 180), 30, 3600)
    concurrent = ((data.get("presentation") or {}).get("concurrent"))
    if isinstance(concurrent, dict):
        # v4.3 §8.1：enabled/mode 不再是持久化字段（runtime session
        # state 由 PresentationController 每次启动固定初始化）。
        concurrent.pop("enabled", None)
        concurrent.pop("mode", None)
        concurrent["max_targets"] = int(_clamp(
            concurrent.get("max_targets", 3), 1, 8))
        eligible = concurrent.get("eligible_kinds")
        if not isinstance(eligible, dict):
            concurrent["eligible_kinds"] = {k: True for k in _KIND_KEYS}
        else:
            concurrent["eligible_kinds"] = {
                k: bool(eligible.get(k, True)) for k in _KIND_KEYS}
        slots = concurrent.get("slots")
        if not isinstance(slots, list) or not slots:
            slots = [copy.deepcopy(DEFAULTS["presentation"]["concurrent"]
                                   ["slots"][0])]
        seen = set()
        clean_slots = []
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            slot_id = str(slot.get("id") or "").strip()
            if not slot_id or slot_id in seen:
                continue   # slot id 必须唯一非空
            seen.add(slot_id)
            _normalize_slot_appearance(slot)
            clean_slots.append(slot)
        concurrent["slots"] = clean_slots or [copy.deepcopy(
            DEFAULTS["presentation"]["concurrent"]["slots"][0])]
    return data


def migrate(loaded: dict) -> tuple[dict, bool]:
    """V3 → V4.1 → V4.3 迁移；返回 (data, migrated)。"""
    migrated = False
    version = loaded.get("config_version", 0)
    if not isinstance(version, int) or version < 3:
        migrated = True   # V2 → V3 路径由调用方提示
    for key in _LEGACY_KEYS:
        _drop_path(loaded, key)
    # 运行期 identity 绝不加载（纵深防御：外部注入的也清掉）
    for banned in ("pinned", "gone_grace_sec"):
        _drop_path(loaded, f"monitor.{banned}")
    concurrent = ((loaded.get("presentation") or {}).get("concurrent"))
    if not isinstance(concurrent, dict):
        concurrent = {}
        migrated = True
    # v4.3 §8.1：删除旧 persisted enabled/mode。无论旧值是什么
    # （false/single/fleet/aggregate），都不作为下次启动初始模式依据；
    # 只修改内存中的 normalized config，不为清这两个字段在启动时写盘。
    for stale in ("enabled", "mode"):
        if stale in concurrent:
            concurrent.pop(stale, None)
            migrated = True
    concurrent.setdefault("max_targets", 3)
    if "eligible_kinds" not in concurrent:
        agents = ((loaded.get("monitor") or {}).get("agents")
                  if isinstance(loaded.get("monitor"), dict) else None)
        concurrent["eligible_kinds"] = (
            dict(agents) if isinstance(agents, dict)
            else {k: True for k in _KIND_KEYS})
        migrated = True
    if "slots" not in concurrent:
        concurrent["slots"] = [copy.deepcopy(
            DEFAULTS["presentation"]["concurrent"]["slots"][0])]
        migrated = True
    loaded.setdefault("presentation", {})["concurrent"] = concurrent
    loaded["config_version"] = CONFIG_VERSION
    if loaded.get("skin") == "default":     # 旧版皮肤名迁移（v4.2.3：
        loaded["skin"] = BUILTIN_SKIN       # 不再隐含 amiya 默认值）
    return loaded, migrated


class Config:
    """V4.1 配置：dotted get/set、dirty 跟踪、commit 返回结果。"""

    def __init__(self, path: str = ""):
        self._lock = threading.RLock()
        self.path = path or CONFIG_PATH
        self.data = copy.deepcopy(DEFAULTS)
        self.migration_notice = False
        self._dirty = False
        self._last_save: ConfigSaveResult | None = None
        self.load()

    # ------------------------------------------------------------ 加载
    def load(self):
        loaded = self._load_json(self.path)
        notice = False
        if loaded is None:
            backup = BACKUP_PATH if self.path == CONFIG_PATH else (
                self.path + ".bak")
            loaded = self._load_json(backup)
            if loaded is None:
                self.data = copy.deepcopy(DEFAULTS)
                return
            notice = True   # 主文件损坏 → backup 兜底
        if not isinstance(loaded, dict):
            self.data = copy.deepcopy(DEFAULTS)
            return
        old_version = loaded.get("config_version", 0)
        loaded, migrated = migrate(loaded)
        self.data = normalize(_deep_merge(DEFAULTS, loaded))
        self.migration_notice = notice or migrated or (
            not isinstance(old_version, int) or old_version < CONFIG_VERSION)
        self._dirty = False

    @staticmethod
    def _load_json(path: str):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    # ------------------------------------------------------------ 保存
    def save(self) -> ConfigSaveResult:
        """兼容入口：等价 commit()（V3 调用点逐步迁移）。"""
        return self.commit()

    def commit(self, fsync: bool = False) -> ConfigSaveResult:
        """原子保存：temp → flush →（可选 fsync）→ backup → replace。

        绝不静默吞错（v4plan §9.4）；成功才清 dirty。
        """
        with self._lock:
            directory = os.path.dirname(self.path) or "."
            temporary = None
            try:
                fd, temporary = tempfile.mkstemp(
                    prefix=".deskpet-config-", dir=directory)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    if fsync:
                        os.fsync(f.fileno())
                # 已有有效主文件 → 先备份（last-known-good）
                if os.path.isfile(self.path):
                    existing = self._load_json(self.path)
                    if existing is not None:
                        backup_path = (BACKUP_PATH
                                       if self.path == CONFIG_PATH
                                       else self.path + ".bak")
                        try:
                            if os.path.isfile(backup_path):
                                os.unlink(backup_path)
                            os.replace(self.path, backup_path)
                        except OSError:
                            pass   # 备份失败不阻塞主保存
                os.replace(temporary, self.path)
                temporary = None
                self._dirty = False
                self._last_save = ConfigSaveResult(ok=True, path=self.path)
                return self._last_save
            except OSError as exc:
                result = ConfigSaveResult(ok=False, path=self.path,
                                          error=str(exc))
                self._last_save = result
                return result
            finally:
                if temporary and os.path.exists(temporary):
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass

    def set_and_commit(self, path, value) -> ConfigSaveResult:
        self.set(path, value)
        return self.commit()

    def update_many(self, values: dict) -> None:
        """批量 set（一次 commit，v4plan §9.6 集中写入）。"""
        for path, value in values.items():
            self.set(path, value)

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def last_save_result(self) -> ConfigSaveResult | None:
        return self._last_save

    # ------------------------------------------------------------ 访问
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
            self._dirty = True

    def ensure_fleet_slots(self, count: int) -> bool:
        """保证 pet-1...pet-count 均有持久化 slot（v4.3 §7.2）。

        只扩展，不因 count 降低删除已有更高 slot 的
        selector/appearance/placement（未来重新提高时恢复）；
        normalize 保证 slot id 唯一。返回是否发生修改（调用方据此
        决定是否随下一次正常保存落盘）。
        """
        count = max(1, min(8, int(count)))
        with self._lock:
            concurrent = self.data.setdefault("presentation", {}) \
                .setdefault("concurrent", {})
            slots = concurrent.get("slots")
            if not isinstance(slots, list):
                slots = []
            existing = {str(s.get("id") or "") for s in slots
                        if isinstance(s, dict)}
            changed = False
            for i in range(1, count + 1):
                slot_id = f"pet-{i}"
                if slot_id in existing:
                    continue
                slots.append({
                    "id": slot_id, "selector": None,
                    "appearance": {"skin": None},
                    "placement": {"monitor": "", "u": None, "v": None,
                                  "anchor": None, "manual": False},
                })
                changed = True
            if changed:
                concurrent["slots"] = slots
                self._dirty = True
            return changed
