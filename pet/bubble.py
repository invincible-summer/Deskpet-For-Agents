"""Fixed, scalable status card. Layout and canvas items change only when needed.

V3：气泡不再有 [批准]/[拒绝] 按钮（plan §41），底行是 footer
（Goal / 环境提示），点击气泡 = 打开终端。
"""
import math
from dataclasses import dataclass
import tkinter as tk
import tkinter.font as tkfont


def round_rect_points(x0, y0, x1, y1, r=10):
    r = min(r, (x1-x0)/2, (y1-y0)/2)
    pts = []
    for cx, cy, a0 in ((x1-r,y0+r,270),(x1-r,y1-r,0),(x0+r,y1-r,90),(x0+r,y0+r,180)):
        for k in range(0,91,15):
            a = math.radians(a0+k)
            pts.extend((cx+r*math.cos(a),cy+r*math.sin(a)))
    return pts


@dataclass
class BubbleModel:
    text: str = ""
    status: str = ""
    footer: str = "打开终端"
    accent: str = "#487f73"
    visible: bool = False


def metrics(config, dpi=1.0):
    """All dimensions are logical pixels, scaled exactly once for display DPI."""
    cfg = config.get('bubble') or {}
    try: scale = max(.5, min(2., float(config.get('scale', 1))))
    except (TypeError, ValueError): scale = 1.0
    def number(name, fallback):
        try: return float(cfg.get(name, fallback))
        except (TypeError, ValueError): return fallback
    rw = max(.7, min(1.6, number('relative_width', 1)))
    rh = max(.8, min(1.6, number('relative_height', 1)))
    base_w = max(160, min(520, number('width', 300)))
    base_h = max(112, min(220, number('height', 132)))
    factor = scale*dpi
    font = max(10, number('font_size', 11) * 4 / 3 * scale * min(rw, rh) * number('relative_font', 1))
    return dict(w=round(base_w*factor*rw),h=round(base_h*factor*rh),
                font=max(1,round(font*dpi)),pad=max(4,round(12*factor)),
                radius=max(3,round(12*factor)),gap=max(2,round(5*factor)),
                button=max(round(25*factor),round(14*dpi)))


def fit_text(text, width, measure):
    if measure(text) <= width:
        return text
    lo,hi=0,len(text)
    while lo<hi:
        mid=(lo+hi+1)//2
        if measure(text[:mid]+'…')<=width: lo=mid
        else: hi=mid-1
    return text[:lo]+'…' if measure('…')<=width else ''


def wrap_text(text, width, measure, count=2):
    text=' '.join(str(text or '')[:512].split())
    lines=[]
    for _ in range(count):
        if measure(text)<=width:
            lines.append(text); text=''; break
        line=fit_text(text,width,measure).removesuffix('…')
        if not line: break
        if len(lines)==count-1:
            lines.append(fit_text(text,width,measure)); text=''; break
        lines.append(line); text=text[len(line):].lstrip()
    return (lines+['']*count)[:count]


class BubbleRenderer:
    def __init__(self,canvas:tk.Canvas,config):
        self.canvas,self.config=canvas,config
        self.model=BubbleModel()
        self.btn_boxes={}
        self._items=[]
        self._font=None
        self._font_key=None
        self._layout_key=None
        self._draw_key=None
        self.w=self.h=0
        self.disp_lines=[]

    def _metrics(self):
        try: dpi=float(self.canvas.winfo_fpixels('1i'))/96
        except (AttributeError,tk.TclError): dpi=1.
        return metrics(self.config,dpi)

    def font(self):
        m=self._metrics()
        key=(self.config.get('bubble.font_family') or 'Microsoft YaHei UI',m['font'])
        if key!=self._font_key:
            self._font=tkfont.Font(root=self.canvas,family=key[0],size=-key[1])
            self._font_key=key
        return self._font

    def invalidate(self):
        self._font_key=self._layout_key=self._draw_key=None

    def fixed_size(self):
        m=self._metrics()
        return m['w'],m['h']

    def layout(self):
        m=self._metrics(); self.w,self.h=m['w'],m['h']
        font=self.font()
        key=(self.model.text,self._font_key,tuple(m.items()))
        if key!=self._layout_key:
            available=self.h-2*m['pad']-m['button']-font.metrics('linespace')-2*m['gap']
            count=max(1, min(2, available // max(1, font.metrics('linespace'))))
            self.disp_lines=wrap_text(self.model.text,self.w-2*m['pad'],font.measure,count)
            self._layout_key=key
        return self.w,self.h

    def draw(self,ox,oy,pet_cx,pet_top):
        cfg=self.config.get('bubble') or {}; m=self._metrics()
        bg = cfg.get('bg', '#fffdf8'); border = cfg.get('border', '#d7dfdc')
        font_color = cfg.get('font_color', '#1f2430')
        key=(ox,oy,pet_cx,pet_top,repr(self.model),self._layout_key,
             bg,border,font_color)
        if key==self._draw_key: return
        self._draw_key=key
        c=self.canvas
        for item in self._items: c.delete(item)
        self._items=[]; self.btn_boxes.clear()
        if not self.model.visible: return
        font=self.font(); pad=m['pad']; x1=ox+self.w; y1=oy+self.h
        def add(item): self._items.append(item)
        tail=min(m['radius'],self.w//8)
        add(c.create_polygon(pet_cx-tail,y1-1,pet_cx,pet_top,pet_cx+tail,y1-1,
                             fill=bg,outline=border))
        add(c.create_polygon(round_rect_points(ox,oy,x1,y1,m['radius']),smooth=True,
                             fill=bg,outline=border,width=1))
        title=fit_text(self.model.status or 'DeskPet',self.w-2*pad,font.measure)
        add(c.create_text(ox+pad,oy+pad,text=title,anchor='nw',font=font,fill=self.model.accent))
        ty=oy+pad+font.metrics('linespace')+m['gap']
        for line in self.disp_lines:
            add(c.create_text(ox+pad,ty,text=line,anchor='nw',font=font,fill=font_color))
            ty+=font.metrics('linespace')
        by=y1-pad-m['button']
        self.btn_boxes['details']=(ox+pad,by,x1-pad,y1-pad)
        add(c.create_text(ox+pad,by+m['button']/2,anchor='w',font=font,
                          text=fit_text(self.model.footer,self.w-2*pad,font.measure),fill='#6a7c75'))

    def hit_button(self,x,y):
        return next((tag for tag,(x0,y0,x1,y1) in self.btn_boxes.items() if x0<=x<=x1 and y0<=y<=y1),None)

    def clear_items(self):
        self._items=[]; self.btn_boxes.clear(); self._draw_key=None
