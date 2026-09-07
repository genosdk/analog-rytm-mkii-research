#!/usr/bin/env python3
"""Analog Rytm MKII desktop panel / framebuffer bridge.

Current stage: real 128x64 1bpp framebuffer viewer + panel event producer.
Later the CPU backend can update framebuffer.bin and consume panel_events.jsonl.
"""
from __future__ import annotations
import argparse, json, time
from pathlib import Path
import tkinter as tk
from tkinter import filedialog

W,H=128,64
FRAME_BYTES=1024

class App:
    def __init__(self, root, frame_file:Path, event_file:Path, scale:int=5):
        self.root=root; self.frame_file=frame_file; self.event_file=event_file; self.scale=scale
        self.mode='row-msb'; self.last_mtime=0
        root.title('Analog Rytm MKII — firmware framebuffer bridge')
        root.configure(bg='#181818')
        top=tk.Frame(root,bg='#181818'); top.pack(padx=12,pady=12)
        self.canvas=tk.Canvas(top,width=W*scale,height=H*scale,bg='black',highlightthickness=1,highlightbackground='#555')
        self.canvas.grid(row=0,column=0,columnspan=8,pady=(0,10))
        self.photo=tk.PhotoImage(width=W,height=H)
        self.img_id=self.canvas.create_image(0,0,image=self.photo,anchor='nw')
        self.canvas.scale(self.img_id,0,0,scale,scale)  # PhotoImage itself is replaced below with zoom
        self.status=tk.StringVar(value='waiting for framebuffer')
        tk.Label(top,textvariable=self.status,fg='#bbb',bg='#181818').grid(row=1,column=0,columnspan=8,sticky='w')
        tk.Button(top,text='Load frame…',command=self.load_dialog).grid(row=2,column=0,pady=6,sticky='w')
        modes=['row-msb','row-lsb','page-msb','page-lsb']
        for i,m in enumerate(modes):
            tk.Button(top,text=m,command=lambda m=m:self.set_mode(m)).grid(row=2,column=i+1,padx=2)

        controls=tk.Frame(root,bg='#202020'); controls.pack(fill='x',padx=12,pady=(0,12))
        names=['SYN','SMP','FLTR','AMP','LFO','FX','MUTE','PATTERN']
        for i,n in enumerate(names):
            tk.Button(controls,text=n,width=8,command=lambda n=n:self.event('button',n,'tap')).grid(row=0,column=i,padx=2,pady=4)
        for i in range(16):
            tk.Button(controls,text=str(i+1),width=4,command=lambda i=i:self.event('trig',str(i+1),'tap')).grid(row=1,column=i%8,padx=2,pady=4)
        enc=tk.Frame(root,bg='#181818');enc.pack(pady=(0,12))
        for i,n in enumerate(['A','B','C','D','E','F','G','H']):
            f=tk.Frame(enc,bg='#181818');f.grid(row=0,column=i,padx=4)
            tk.Label(f,text=n,fg='white',bg='#181818').pack()
            tk.Button(f,text='−',width=2,command=lambda n=n:self.event('encoder',n,-1)).pack(side='left')
            tk.Button(f,text='+',width=2,command=lambda n=n:self.event('encoder',n,+1)).pack(side='left')

        root.bind('<KeyPress>',self.keypress)
        self.poll()

    def event(self,kind,name,value):
        rec={'t':time.time(),'kind':kind,'name':name,'value':value}
        self.event_file.parent.mkdir(parents=True,exist_ok=True)
        with self.event_file.open('a') as f:f.write(json.dumps(rec)+'\n')
        self.status.set(f'event: {kind} {name} {value}')

    def keypress(self,e):
        if e.char and e.char in '1234567890': self.event('key',e.char,'tap')

    def set_mode(self,m): self.mode=m; self.reload(force=True)
    def load_dialog(self):
        p=filedialog.askopenfilename()
        if p: self.frame_file=Path(p); self.reload(force=True)

    def decode(self,b:bytes):
        pix=[[0]*W for _ in range(H)]
        if self.mode.startswith('row'):
            msb=self.mode.endswith('msb')
            for y in range(H):
                for x in range(W):
                    v=b[y*16+x//8];bit=(7-(x&7)) if msb else (x&7);pix[y][x]=(v>>bit)&1
        else:
            msb=self.mode.endswith('msb')
            for page in range(8):
                for x in range(W):
                    v=b[page*128+x]
                    for yy in range(8):
                        bit=(7-yy) if msb else yy;pix[page*8+yy][x]=(v>>bit)&1
        return pix

    def draw(self,b):
        pix=self.decode(b)
        small=tk.PhotoImage(width=W,height=H)
        # Use black/white only; later palette can mimic OLED.
        for y,row in enumerate(pix):
            runs=[];cur=row[0];start=0
            for x in range(1,W+1):
                v=row[x] if x<W else 1-cur
                if v!=cur:
                    small.put('#f2f2f2' if cur else '#000000',to=(start,y,x,y+1));start=x;cur=v
        self.photo=small.zoom(self.scale,self.scale)
        self.canvas.itemconfigure(self.img_id,image=self.photo)

    def reload(self,force=False):
        try:
            st=self.frame_file.stat()
            if not force and st.st_mtime_ns==self.last_mtime:return
            b=self.frame_file.read_bytes()
            if len(b)<FRAME_BYTES:
                self.status.set(f'{self.frame_file}: {len(b)} bytes; need 1024');return
            self.draw(b[:FRAME_BYTES]);self.last_mtime=st.st_mtime_ns
            self.status.set(f'{self.frame_file.name}: 128×64 1bpp, {self.mode}')
        except FileNotFoundError: pass

    def poll(self):
        self.reload();self.root.after(50,self.poll)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--frame',type=Path,default=Path('framebuffer.bin'));ap.add_argument('--events',type=Path,default=Path('panel_events.jsonl'));ap.add_argument('--scale',type=int,default=5);a=ap.parse_args()
    root=tk.Tk();App(root,a.frame,a.events,a.scale);root.mainloop()
if __name__=='__main__':main()
