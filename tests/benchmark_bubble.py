"""Compare fixed-card rendering against git HEAD; writes only ignored test artifacts.
Run with Windows Python from the repository root. No agent tasks or terminal input.
"""
import json
from pathlib import Path
import sys
import time
import tkinter as tk
import types
import psutil
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests.test_ui import MemoryConfig
from pet.bubble import BubbleRenderer,BubbleModel

ROOT=Path(__file__).resolve().parents[1]
ARTIFACTS=ROOT/'.test-artifacts'

def main():
    from main import _dpi_aware
    _dpi_aware()
    root=tk.Tk();root.title('DeskPet · 气泡预览');root.geometry('960x650+60+60')
    canvas=tk.Canvas(root,bg='#eef1ed',highlightthickness=0);canvas.pack(fill='both',expand=True)
    cfg=MemoryConfig()
    module=types.ModuleType('legacy_bubble');exec((ARTIFACTS/'legacy_bubble.py').read_text(encoding='utf-8'),module.__dict__)
    rounds=500
    result={}
    for name,klass,model_class in [('before',module.BubbleRenderer,module.BubbleModel),('after',BubbleRenderer,BubbleModel)]:
        renderer=klass(canvas,cfg);model=model_class();model.visible=True;model.text='正在整理气泡布局与缩放方式，检查状态摘要是否清晰自然。';renderer.model=model
        renderer.layout();renderer.draw(10,10,160,160);root.update_idletasks()
        t=time.perf_counter()
        for _ in range(rounds):
            renderer.layout();renderer.draw(10,10,160,160)
        result[name+'_render_ms']=round((time.perf_counter()-t)*1000,2)
        canvas.delete('all')
    result['rounds']=rounds
    result['rss_mb']=round(psutil.Process().memory_info().rss/1024**2,2)
    result['reduction_percent']=round(100*(1-result['after_render_ms']/result['before_render_ms']),1)
    cards=[]
    for i,(status,text,approval) in enumerate([
        ('Codex · 思考中','正在整理气泡布局的改进方案，并检查字体与宠物的缩放关系。',False),
        ('Codex · 等待审批','请求在工作目录运行测试，请确认本次操作。',True),
        ('Claude Code · 编码中','修改配置读取与状态解析，避免长时间思考被误判为空闲。',False),
        ('Codex · 已完成','已完成气泡与安全审批改进，测试全部通过。',False)]):
        cfg2=MemoryConfig();renderer=BubbleRenderer(canvas,cfg2)
        renderer.model=BubbleModel(visible=True,status=status,text=text,footer='目标 · 完善桌宠体验',
                                  approve_label='批准本次' if approval else '',deny_label='拒绝' if approval else '')
        w,h=renderer.layout();x=30+(i%2)*460;y=40+(i//2)*270
        renderer.draw(x,y,x+w//2,y+h+12);cards.append(renderer)
    root.update();root.lift();root.after(200,lambda:None);root.update()
    ARTIFACTS.mkdir(exist_ok=True)
    try:
        from PIL import ImageGrab
        x,y=canvas.winfo_rootx(),canvas.winfo_rooty()
        ImageGrab.grab(bbox=(x,y,x+canvas.winfo_width(),y+canvas.winfo_height())).save(ARTIFACTS/'bubble-preview.png')
    finally:
        root.destroy()
    (ARTIFACTS/'bubble-benchmark.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
