#!/usr/bin/env python3
"""Extract the current AR MKII 128x64 framebuffer from a guest SDRAM snapshot."""
from __future__ import annotations
import argparse, struct
from pathlib import Path

SDRAM_BASE = 0x40000000
FB_PTR_GLOBAL = 0x4026F478
FRAME_BYTES = 0x400

def read_be32(blob: bytes, guest_addr: int, base: int) -> int:
    off = guest_addr - base
    if off < 0 or off + 4 > len(blob):
        raise ValueError(f'guest 0x{guest_addr:08x} outside snapshot')
    return struct.unpack_from('>I', blob, off)[0]

def extract(blob: bytes, base: int = SDRAM_BASE):
    ptr = read_be32(blob, FB_PTR_GLOBAL, base)
    off = ptr - base
    if off < 0 or off + FRAME_BYTES > len(blob):
        raise ValueError(f'framebuffer pointer 0x{ptr:08x} outside snapshot')
    return ptr, blob[off:off+FRAME_BYTES]

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('snapshot',type=Path)
    ap.add_argument('--base',type=lambda x:int(x,0),default=SDRAM_BASE)
    ap.add_argument('--out',type=Path,default=Path('framebuffer.bin'))
    args=ap.parse_args()
    blob=args.snapshot.read_bytes()
    ptr,frame=extract(blob,args.base)
    args.out.write_bytes(frame)
    print(f'frame pointer: 0x{ptr:08x}')
    print(f'wrote {len(frame)} bytes to {args.out}')

if __name__=='__main__': main()
