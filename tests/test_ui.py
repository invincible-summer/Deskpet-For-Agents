"""Headless model checks and real Tk smoke tests (Windows Python). No real approvals."""
import copy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from main import _dpi_aware
_dpi_aware()
from pet.config import DEFAULTS
from pet.bubble import BubbleRenderer, BubbleModel, metrics, wrap_text, fit_text
from pet.animator import Animation, Animator
from actions import approver, winkeys


class MemoryConfig:
    def __init__(self):self.data=copy.deepcopy(DEFAULTS);self.data['tray_enabled']=False;self.migration_notice=False
    def get(self,path,default=None):
        node=self.data
        for p in path.split('.'):
            if not isinstance(node,dict) or p not in node:return default
            node=node[p]
        return node
    def set(self,path,value):
        node=self.data;parts=path.split('.')
        for p in parts[:-1]:node=node.setdefault(p,{})
        node[parts[-1]]=value
    def save(self):pass


class GeometryTests(unittest.TestCase):
    def test_joint_scale_and_relative_font(self):
        cfg=MemoryConfig();a=metrics(cfg)
        cfg.set('scale',2);b=metrics(cfg)
        self.assertEqual((b['w'],b['h']),(a['w']*2,a['h']*2))
        self.assertLessEqual(abs(b['font']-2*a['font']),1)
        cfg.set('bubble.relative_width',1.3);cfg.set('bubble.relative_height',1.2)
        c=metrics(cfg);self.assertGreater(c['font'],b['font']);self.assertGreater(c['w'],b['w'])
        d=metrics(cfg,1.5);self.assertLessEqual(abs(d['w']-c['w']*1.5),1)

    def test_wrap_pixel_bound_cjk_and_long_token(self):
        measure=lambda text:sum(14 if ord(ch)>127 else 7 for ch in text)
        for text in ('正在整理气泡布局' * 200,'unbroken_identifier_'*300,'hello\nworld'):
            lines=wrap_text(text,120,measure,2)
            self.assertEqual(len(lines),2)
            self.assertTrue(all(measure(line)<=120 for line in lines))
        self.assertEqual(fit_text('abc',0,measure),'')

    def test_readonly_approval_never_sends_keys(self):
        self.assertFalse(approver.send_approval(None,None,'approve')[0])
        self.assertFalse(hasattr(winkeys,'send_key'))

    def test_stale_binding_and_ambiguous_parent(self):
        with patch.object(winkeys,'enum_windows',return_value=[(1,10,'Codex','console'),(2,10,'Codex','console')]),patch.object(winkeys,'_ancestor_pids',return_value={10}):
            self.assertIsNone(winkeys.find_terminal_window('',10,'windows',[],None))
            self.assertIsNone(winkeys.find_terminal_window('',10,'windows',[],'Codex'))
            self.assertIsNone(winkeys.find_terminal_window('',10,'windows',[],{'hwnd':3}))

    def test_activation_failure_does_not_retry_or_inject(self):
        class User:
            def __init__(self):self.calls=[]
            def IsWindow(self,h):return True
            def IsIconic(self,h):return True
            def ShowWindow(self,*args):self.calls.append('restore')
            def SetForegroundWindow(self,h):self.calls.append('activate')
            def GetForegroundWindow(self):return 99
            def FlashWindowEx(self,*args):self.calls.append('flash')
        user=User()
        with patch.object(winkeys,'user32',user):self.assertFalse(winkeys.raise_window(1))
        self.assertEqual(user.calls,['restore','activate','flash'])


class TkTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root=tk.Tk();self.root.withdraw();self.cfg=MemoryConfig()
        self.canvas=tk.Canvas(self.root,width=650,height=400);self.canvas.pack()
        self.bubble=BubbleRenderer(self.canvas,self.cfg)

    def tearDown(self):self.root.destroy()

    def test_fixed_card_cached_items_and_buttons_at_scales(self):
        for scale in (.5,.75,1,1.5,2):
            self.cfg.set('scale',scale);self.bubble.invalidate()
            self.bubble.model=BubbleModel(visible=True,status='Codex · 思考中',text='正在分析气泡布局与缩放方式')
            normal=self.bubble.layout();self.bubble.draw(0,0,normal[0]//2,normal[1]+8)
            items=self.canvas.find_all()
            for _ in range(30):
                self.bubble.layout();self.bubble.draw(0,0,normal[0]//2,normal[1]+8)
            self.assertEqual(items,self.canvas.find_all())
            self.bubble.model.approve_label='批准本次';self.bubble.model.deny_label='拒绝'
            self.assertEqual(normal,self.bubble.layout())
            self.bubble.draw(0,0,normal[0]//2,normal[1]+8)
            bb=self.bubble.btn_boxes['approve'];self.assertEqual(self.bubble.hit_button((bb[0]+bb[2])/2,(bb[1]+bb[3])/2),'approve')
            self.assertLessEqual(bb[2],normal[0]);self.assertLessEqual(bb[3],normal[1])
            self.bubble.model.submitting=True;self.bubble.draw(0,0,normal[0]//2,normal[1]+8)
            self.assertEqual(self.bubble.btn_boxes,{})

    def test_hidden_bubble_removes_old_buttons(self):
        b=self.bubble;b.model=BubbleModel(visible=True,text='a',approve_label='批准',deny_label='拒绝')
        b.layout();b.draw(0,0,150,140);self.assertTrue(b.btn_boxes)
        b.model.visible=False;b.draw(0,0,150,140)
        self.assertEqual(len(self.canvas.find_all()),0);self.assertFalse(b.btn_boxes)

    def test_animation_byte_budget_and_pause(self):
        import tkinter as tk
        animator=Animator(self.root);anim=Animation('unused',{'width':10,'height':10,'frames':10})
        animator._pool['unused']=anim;animator.current=anim;animator.cache_bytes=800
        photo_factory=tk.PhotoImage
        with patch('pet.animator.tk.PhotoImage',side_effect=lambda **_:photo_factory(master=self.root,width=10,height=10)):
            for i in range(10):animator._idx=i;animator.frame_image()
        self.assertLessEqual(len(anim._frames),2)
        animator._schedule();self.assertIsNotNone(animator._after_id)
        animator.set_paused(True);self.assertIsNone(animator._after_id)
        animator.set_paused(False);self.assertIsNotNone(animator._after_id)
        animator.stop()


class AppTests(unittest.TestCase):
    def test_primary_snapshot_and_dashboard_smoke(self):
        from pet.app import PetApp
        from agents.models import AgentInstance,AgentKind,Snapshot,Status
        cfg=MemoryConfig()
        with patch.object(PetApp,'_apply_skin',lambda self:None):app=PetApp(cfg)
        try:
            one=AgentInstance(AgentKind.CODEX,101,'windows');two=AgentInstance(AgentKind.CLAUDE,102,'windows')
            app.monitor.instances={one.key:one,two.key:two}
            a=Snapshot(one.key,one.kind,one.source,one.pid,status=Status.WORKING,phase='thinking',summary='正在分析布局',goal='完善气泡')
            b=Snapshot(two.key,two.kind,two.source,two.pid,status=Status.WAITING)
            app.monitor.snapshots={a.key:a,b.key:b};app.monitor.set_primary(a.key)
            self.assertEqual(app._pick_focus_snapshot().key,a.key)
            model=app._bubble_model();self.assertIn('思考',model.status);self.assertEqual(model.text,'正在分析布局')
            app.open_dashboard();app.dashboard.open_session(a.key);app.root.update()
            self.assertIn('只读',app.dashboard.session_title.get())
            self.assertEqual(str(app.dashboard.send_button['state']),'disabled')
            app.hide_pet();self.assertTrue(app.animator.paused)
        finally:app.quit()


if __name__=='__main__':unittest.main()
