"""AppearanceController：global/slot appearance 的唯一运行期修改接口（v4.3 §7.4）。

规则：
  * 先验证/normalize value；
  * config 内存立即更新（运行期生效不等保存）；
  * 调 PetViewManager.apply_appearance_change() 做定向 runtime apply；
  * 通过注入的 request_save 回调请求持久化（Phase 5 起为
    ConfigSaveCoordinator.request_save()；此前由 app 提供立即 commit）；
  * 不直接 commit 磁盘、不调用 Tk message loop nested update、
    不直接操作 PetView 私有字段。

§14 reset 语义：reset_all 恢复 DEFAULTS 的外观字段并清空所有 slot
skin override；绝不触碰 Monitor/concurrency/placement/selector/
tray/autostart/privacy。batch apply + 一次 save。
"""
from __future__ import annotations

from .config import DEFAULTS
from .petview import PetViewManager

# §14 "重置全部外观" 恢复的全局字段（不含 monitor/presentation/tray 等）
RESET_FIELDS = (
    "skin",
    "scale",
    "speed",
    "animated",
    "force_state",
    "bubble.enabled",
    "bubble.font_family",
    "bubble.font_size",
    "bubble.font_color",
    "bubble.bg",
    "bubble.border",
    "bubble.height",
    "bubble.relative_width",
    "bubble.relative_height",
    "bubble.relative_font",
    "bubble.width",
    "bubble.max_lines",
    "bubble.autohide_sec",
    "bubble.always_visible",
)

_NUMERIC_BOUNDS = {
    "scale": (0.5, 2.0),
    "speed": (0.1, 3.0),
    "bubble.font_size": (8, 24),
    "bubble.width": (160, 520),
    "bubble.height": (112, 220),
    "bubble.relative_width": (0.7, 1.6),
    "bubble.relative_height": (0.8, 1.6),
    "bubble.relative_font": (0.5, 2.0),
    "bubble.max_lines": (1, 6),
    "bubble.autohide_sec": (0, 3600),
}
_BOOL_FIELDS = {
    "animated", "bubble.enabled", "bubble.always_visible",
}
_FORCE_STATES = ("", "walk", "attack", "die", "special", "sleep")


class AppearanceController:
    """UI 不直接操作 PetView 私有字段；所有外观修改走这里。"""

    def __init__(self, config, pet_manager: PetViewManager,
                 request_save=None):
        self.config = config
        self.pet_manager = pet_manager
        self._request_save = request_save or self._default_save

    # ------------------------------------------------------------ 内部
    def _default_save(self):
        try:
            self.config.commit()
        except Exception:
            pass

    def _save(self):
        self._request_save()

    def _normalize(self, path: str, value):
        """验证/normalize；非法值返回 None（调用方丢弃，不抛异常）。"""
        if path in _BOOL_FIELDS:
            return bool(value)
        if path in _NUMERIC_BOUNDS:
            lo, hi = _NUMERIC_BOUNDS[path]
            try:
                num = float(value)
            except (TypeError, ValueError):
                return None
            return round(max(lo, min(hi, num)), 2)
        if path == "skin":
            text = str(value or "").strip()
            return text if text else None
        if path == "force_state":
            text = str(value or "").strip()
            return text if text in _FORCE_STATES else ""
        if path == "bubble.font_family":
            text = str(value or "").strip()
            return text or None
        if path in ("bubble.font_color", "bubble.bg", "bubble.border"):
            text = str(value or "").strip()
            return text if text.startswith("#") and len(text) in (4, 7) else None
        return value

    # ------------------------------------------------------------ global
    def set_global(self, path: str, value) -> None:
        value = self._normalize(path, value)
        if value is None:
            return
        self.config.set(path, value)
        self.pet_manager.apply_appearance_change(
            scope="global", changed_paths={path})
        self._save()

    # ------------------------------------------------------------ slot skin
    def set_slot_skin(self, slot_id: str, skin: str | None) -> None:
        """slot appearance.skin：None/"" = 恢复跟随全局；不做 uniqueness
        check（同一 skin 可被任意多个 slot 重复选择，§7.1）。"""
        skin = self._normalize("skin", skin)
        slots = list(self.config.get("presentation.concurrent.slots") or [])
        slot = next((s for s in slots
                     if isinstance(s, dict) and s.get("id") == slot_id), None)
        if slot is None:
            return
        appearance = slot.get("appearance")
        if not isinstance(appearance, dict):
            appearance = {}
            slot["appearance"] = appearance
        appearance["skin"] = skin
        self.config.set("presentation.concurrent.slots", slots)
        self.pet_manager.apply_appearance_change(
            scope=slot_id, changed_paths={"skin"})
        self._save()

    # ------------------------------------------------------------ reset
    def reset_all(self) -> None:
        """§14 重置全部外观：全局默认 + 所有 slot skin override 清空。

        一个 batch apply + 一次 save，不逐字段产生 N 次 redraw/build。
        """
        values = {}
        for path in RESET_FIELDS:
            parts = path.split(".")
            node = DEFAULTS
            for part in parts:
                node = node[part]
            values[path] = node
        self.config.update_many(values)
        slots = list(self.config.get("presentation.concurrent.slots") or [])
        for slot in slots:
            if isinstance(slot, dict):
                appearance = slot.get("appearance")
                if isinstance(appearance, dict):
                    appearance["skin"] = None
                else:
                    slot["appearance"] = {"skin": None}
        self.config.set("presentation.concurrent.slots", slots)
        self.pet_manager.refresh_all_slot_configs()
        self.pet_manager.apply_appearance_change(
            scope="global", changed_paths=set(RESET_FIELDS))
        self._save()

    def reset_slot(self, slot_id: str) -> None:
        """单 slot 恢复跟随全局（只清该 slot appearance.skin）。"""
        self.set_slot_skin(slot_id, None)
