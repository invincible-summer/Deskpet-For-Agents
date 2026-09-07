"""气泡渲染：Canvas 自绘圆角气泡，固定尺寸（宽度/行数由配置固定，不自适应）。

流程：app 设置 model → layout()（固定尺寸计算）→ app 调整窗口 → draw()（纯绘制）。
"""
import math
import tkinter as tk
import tkinter.font as tkfont


def round_rect_points(x0, y0, x1, y1, r=10) -> list[float]:
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, 270), (x1 - r, y1 - r, 0),
                       (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for k in range(0, 91, 15):
            rad = math.radians(a0 + k)
            pts.append(cx + r * math.cos(rad))
            pts.append(cy + r * math.sin(rad))
    return pts


class BubbleModel:
    def __init__(self):
        self.text = ""                 # 两行文本（本地摘要）
        self.approve_label = ""        # 非空则显示按钮
        self.deny_label = ""
        self.visible = False


class BubbleRenderer:
    PAD_X = 12
    PAD_Y = 8
    LINE_GAP = 4
    TAIL = 12
    BTN_H = 26
    BTN_W = 74

    def __init__(self, canvas: tk.Canvas, config):
        self.canvas = canvas
        self.config = config
        self.model = BubbleModel()
        self.btn_boxes: dict[str, tuple[int, int, int, int]] = {}
        self._items: list[int] = []
        self._font: tkfont.Font | None = None
        self._font_key = None
        self.w = 0
        self.h = 0
        self.disp_lines: list[str] = []

    def font(self) -> tkfont.Font:
        cfg = self.config.get("bubble")
        key = (cfg["font_family"], int(cfg["font_size"]))
        if self._font is None or self._font_key != key:
            self._font = tkfont.Font(family=cfg["font_family"], size=int(cfg["font_size"]))
            self._font_key = key
        return self._font

    def invalidate(self):
        self._font = None
        self._font_key = None
        self.w = self.h = 0
        self.disp_lines = []

    def fixed_size(self) -> tuple[int, int]:
        """固定尺寸：宽 = 配置宽度，高 = 固定行数（批复按钮时加高）。"""
        cfg = self.config.get("bubble")
        font = self.font()
        ls = font.metrics("linespace")
        lines = max(1, int(cfg.get("max_lines", 2)))
        w = int(cfg.get("width", 300)) + self.PAD_X * 2 + 2
        h = lines * ls + (lines - 1) * self.LINE_GAP + self.PAD_Y * 2 + 2
        if self.model.approve_label:
            h += self.BTN_H + 8
        return w, h

    def layout(self) -> tuple[int, int]:
        """固定尺寸 + 按固定行数截断文本。"""
        cfg = self.config.get("bubble")
        font = self.font()
        lines_n = max(1, int(cfg.get("max_lines", 2)))
        inner_w = int(cfg.get("width", 300))
        self.w, self.h = self.fixed_size()
        # 逐字符换行；行数固定，未消费完说明有截断
        text = " ".join(str(self.model.text or "").split())
        lines: list[str] = []
        cur = ""
        truncated = False
        for ch in text:
            if font.measure(cur + ch) > inner_w and cur:
                lines.append(cur)
                cur = ch
                if len(lines) == lines_n:
                    truncated = True
                    break
            else:
                cur += ch
        if not truncated:
            lines.append(cur)
        while len(lines) < lines_n:
            lines.append("")
        if truncated and lines:
            lines[-1] = lines[-1][: max(0, len(lines[-1]) - 1)] + "…"
        self.disp_lines = [l if l else " " for l in lines]
        return self.w, self.h

    def draw(self, ox: int, oy: int, pet_cx: int, pet_top: int):
        """在 (ox, oy) 处绘制已布局的气泡，尾巴指向 (pet_cx, pet_top)。"""
        c = self.canvas
        for it in self._items:
            c.delete(it)
        self._items = []
        self.btn_boxes.clear()
        if not self.model.visible or not self.disp_lines:
            return
        cfg = self.config.get("bubble")
        font = self.font()
        w, h = self.w, self.h
        x0, y0 = ox, oy
        x1, y1 = ox + w, oy + h

        pts = round_rect_points(x0, y0, x1, y1, 10)
        tail_y = y1 - 2
        pts += [(pet_cx - 9, tail_y), (pet_cx, pet_top - 2), (pet_cx + 9, tail_y)]
        self._items.append(c.create_polygon(
            pts, smooth=False, fill=cfg["bg"], outline=cfg["border"], width=2))
        body = round_rect_points(x0, y0, x1, y1, 10)
        self._items.append(c.create_polygon(
            body, smooth=True, fill=cfg["bg"], outline=cfg["border"], width=2))

        ty = y0 + self.PAD_Y
        for line in self.disp_lines:
            self._items.append(c.create_text(
                x0 + self.PAD_X, ty, text=line, anchor="nw",
                fill=cfg["font_color"], font=font))
            ty += font.metrics("linespace") + self.LINE_GAP

        if self.model.approve_label:
            by = y1 - self.BTN_H - 6
            for i, (label, tag, color) in enumerate((
                    (self.model.approve_label, "approve", "#2e9e5b"),
                    (self.model.deny_label, "deny", "#c0392b"))):
                bx = x0 + self.PAD_X + i * (self.BTN_W + 14)
                bb = (bx, by, bx + self.BTN_W, by + self.BTN_H)
                self.btn_boxes[tag] = bb
                bpts = round_rect_points(*bb, 7)
                self._items.append(c.create_polygon(
                    bpts, smooth=True, fill=color, outline=""))
                self._items.append(c.create_text(
                    (bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2, text=label,
                    fill="#ffffff", font=font))

    def hit_button(self, x: int, y: int) -> str | None:
        for tag, (x0, y0, x1, y1) in self.btn_boxes.items():
            if x0 <= x <= x1 and y0 <= y <= y1:
                return tag
        return None

    def clear_items(self):
        self._items = []
        self.btn_boxes.clear()
