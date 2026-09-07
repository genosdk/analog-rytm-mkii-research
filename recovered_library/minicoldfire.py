#!/usr/bin/env python3
"""Minimal trap-driven ColdFire/m68k interpreter for AR MKII OS 1.72 bring-up.

This is NOT a complete ColdFire emulator. It implements conventional 68k/ColdFire
integer/control instructions incrementally along the firmware's actual boot path.
Unsupported opcodes stop with full state instead of being guessed.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from collections import Counter
import argparse, json, struct

SDRAM_BASE=0x40000000
SDRAM_SIZE=128*1024*1024
SRAM_BASE=0x80000000
SRAM_PHYS=64*1024
MAIN_LOAD=0x40000400
ENTRY=0x40000870
INITIAL_SP=0x47FFFFE0
PBC1_BASE=0xE0000000
PBC0_BASE=0xF0000000
PIT0_PCSR=0xFC080000
PIT0_PMR=0xFC080002
PIT0_PCNTR=0xFC080004
DTIM0_DTCN=0xFC07000C
DTIM_ACCEL=10000  # accelerated free-running DMA-timer ticks per semantic CPU instruction
FLEX_EXT_BASE=0x4B800000
FLEX_EXT_SIZE=0x00010000
PIT_PCSR_EN=0x0001
PIT_PCSR_RLD=0x0002
PIT_PCSR_PIF=0x0004
PIT_PCSR_PIE=0x0008
PIT0_VECTOR=205
PIT0_LEVEL=1
UART_BASES=(
    0xFC060000,0xFC064000,0xFC068000,0xFC06C000,
    0xEC060000,0xEC064000,0xEC068000,0xEC06C000,0xEC070000,0xEC074000,
)
UART_USR_OFF=0x04
UART_USR_TXRDY=0x04
UART_USR_TXEMP=0x08
UART8_BASE=0xEC070000
UART_DATA_OFF=0x0C
EDMA_BASE=0xFC044000
EDMA_SERQ=EDMA_BASE+0x18
EDMA_CERQ=EDMA_BASE+0x19
EDMA_CINT=EDMA_BASE+0x1C
EDMA_SSRT=EDMA_BASE+0x1E
EDMA_INTH=EDMA_BASE+0x20
EDMA_INTL=EDMA_BASE+0x24
EDMA_TCD_BASE=0xFC045000
EDMA_TCD_STRIDE=0x20
EDMA_CSR_INT_MAJOR=0x0002
EDMA_CSR_D_REQ=0x0008
EDMA_CSR_DONE=0x0080
EDMA_CH_UART8_RX=34
EDMA_CH_UART8_TX=35

class Unsupported(Exception):
    pass
class MemFault(Exception):
    pass

def sx(v,bits):
    m=1<<(bits-1); return (v ^ m)-m

def mask_for(size): return (1<<(size*8))-1

def signbit(size): return 1<<(size*8-1)

@dataclass
class Bus:
    sdram: bytearray = field(default_factory=lambda: bytearray(SDRAM_SIZE))
    sram: bytearray = field(default_factory=lambda: bytearray(SRAM_PHYS))
    flex_ext: bytearray = field(default_factory=lambda: bytearray(FLEX_EXT_SIZE))
    mmio: dict[int,int] = field(default_factory=dict)
    mmio_events: list[dict] = field(default_factory=list)
    flex_events: list[dict] = field(default_factory=list)
    pc_provider: callable|None = None
    step_provider: callable|None = None
    pit0_pcsr: int = 0
    pit0_pmr: int = 0
    pit0_pcntr: int = 0
    pit0_fires: int = 0
    edma_erq: set[int] = field(default_factory=set)
    edma_events: list[dict] = field(default_factory=list)
    pending_irqs: list[tuple[int,int,str]] = field(default_factory=list)
    uart_tx: bytearray = field(default_factory=bytearray)
    uart_rx: bytearray = field(default_factory=bytearray)

    def load_main(self,path:Path):
        b=path.read_bytes(); off=MAIN_LOAD-SDRAM_BASE
        self.sdram[off:off+len(b)]=b

    def _region(self,addr,size,write=False):
        addr &= 0xffffffff
        if SDRAM_BASE <= addr and addr+size <= SDRAM_BASE+SDRAM_SIZE:
            return self.sdram, addr-SDRAM_BASE
        if FLEX_EXT_BASE <= addr and addr+size <= FLEX_EXT_BASE+FLEX_EXT_SIZE:
            return self.flex_ext, addr-FLEX_EXT_BASE
        if 0x80000000 <= addr < 0x8C000000:
            return self.sram, addr & (SRAM_PHYS-1)
        if 0xE0000000 <= addr <= 0xFFFFFFFF:
            return None,addr
        raise MemFault(f'address 0x{addr:08X} size={size}')

    def _mmio_raw_read(self,addr,size):
        v=0
        for i in range(size): v=(v<<8)|self.mmio.get((addr+i)&0xffffffff,0)
        return v

    def _mmio_raw_write(self,addr,size,value):
        bs=(value & mask_for(size)).to_bytes(size,'big')
        for i,x in enumerate(bs): self.mmio[(addr+i)&0xffffffff]=x

    def _tcd_addr(self,ch,off=0):
        return EDMA_TCD_BASE + EDMA_TCD_STRIDE*ch + off

    def _tcd_read(self,ch,off,size):
        return self._mmio_raw_read(self._tcd_addr(ch,off),size)

    def _tcd_write(self,ch,off,size,value):
        self._mmio_raw_write(self._tcd_addr(ch,off),size,value)

    def _queue_edma_irq(self,ch):
        # AR 1.72 programs UART8 RX/TX eDMA channels 34/35 onto INTC1
        # sources 26/27 respectively. INTC1 vector base is 128.
        src=ch-8
        vector=128+src
        icr=self._mmio_raw_read(0xFC04C040+src,1)
        level=icr & 7
        if level==0: level=3
        # Set eDMA interrupt status bit (INTH for channels 32..63).
        if ch>=32:
            cur=self._mmio_raw_read(EDMA_INTH,4)
            self._mmio_raw_write(EDMA_INTH,4,cur | (1<<(ch-32)))
        else:
            cur=self._mmio_raw_read(EDMA_INTL,4)
            self._mmio_raw_write(EDMA_INTL,4,cur | (1<<ch))
        if not any(v==vector for v,_,_ in self.pending_irqs):
            self.pending_irqs.append((vector,level,f'eDMA{ch}'))

    @staticmethod
    def _mod_advance(addr,delta,modbits):
        if not modbits:
            return (addr+delta)&0xffffffff
        mod=1<<modbits; base=addr & ~(mod-1)
        return base | (((addr-base)+delta)&(mod-1))

    def _edma_service(self,ch):
        """Service one complete major loop for the tiny subset needed by AR UART8.

        UART8 TX is permanently request-ready in the current board model, so all
        minor iterations of an enabled TX major loop can complete back-to-back.
        RX remains dormant until bytes are explicitly injected later.
        """
        if ch not in self.edma_erq:
            return False
        if ch==EDMA_CH_UART8_RX and not self.uart_rx:
            return False
        saddr=self._tcd_read(ch,0x00,4); attr=self._tcd_read(ch,0x04,2)
        soff=sx(self._tcd_read(ch,0x06,2),16); nbytes=self._tcd_read(ch,0x08,4)
        slast=sx(self._tcd_read(ch,0x0C,4),32); daddr=self._tcd_read(ch,0x10,4)
        citer=self._tcd_read(ch,0x14,2)&0x7fff; doff=sx(self._tcd_read(ch,0x16,2),16)
        dlast=sx(self._tcd_read(ch,0x18,4),32); biter=self._tcd_read(ch,0x1C,2)&0x7fff
        csr=self._tcd_read(ch,0x1E,2)
        if citer==0: citer=biter
        if citer==0 or nbytes==0: return False
        smod=(attr>>11)&0x1f; dmod=(attr>>3)&0x1f
        initial={'pc':self.pc_provider() if self.pc_provider else 0,'ch':ch,'saddr':saddr,'daddr':daddr,
                 'citer':citer,'biter':biter,'nbytes':nbytes,'csr':csr,'bytes':[]}
        done=0
        while citer>0:
            # For AR UART8 paths NBYTES=1. Keep generic byte loop for completeness.
            payload=[]
            if ch==EDMA_CH_UART8_RX and saddr==UART8_BASE+UART_DATA_OFF:
                for _ in range(nbytes):
                    payload.append(self.uart_rx.pop(0) if self.uart_rx else 0)
            else:
                for j in range(nbytes): payload.append(self.read((saddr+j)&0xffffffff,1))
            if ch==EDMA_CH_UART8_TX and daddr==UART8_BASE+UART_DATA_OFF:
                for x in payload:
                    self.uart_tx.append(x); self.mmio_events.append({'pc':self.pc_provider() if self.pc_provider else 0,
                        'kind':'DMAW','addr':daddr,'size':1,'value':x})
            else:
                for j,x in enumerate(payload): self.write((daddr+j)&0xffffffff,1,x)
            initial['bytes'].extend(payload); done+=len(payload)
            saddr=self._mod_advance(saddr,soff,smod)
            daddr=self._mod_advance(daddr,doff,dmod)
            citer-=1
        saddr=(saddr+slast)&0xffffffff; daddr=(daddr+dlast)&0xffffffff
        self._tcd_write(ch,0x00,4,saddr); self._tcd_write(ch,0x10,4,daddr)
        # After major completion hardware reloads CITER from BITER and marks DONE.
        self._tcd_write(ch,0x14,2,biter)
        self._tcd_write(ch,0x1E,2,csr|EDMA_CSR_DONE)
        if csr & EDMA_CSR_D_REQ: self.edma_erq.discard(ch)
        initial.update({'done_bytes':done,'final_saddr':saddr,'final_daddr':daddr})
        self.edma_events.append(initial)
        if csr & EDMA_CSR_INT_MAJOR: self._queue_edma_irq(ch)
        return True

    def inject_uart8_rx(self,data:bytes):
        self.uart_rx.extend(data)
        if EDMA_CH_UART8_RX in self.edma_erq:
            self._edma_service(EDMA_CH_UART8_RX)

    def read(self,addr,size):
        buf,off=self._region(addr,size)
        if buf is not None:
            # wrap SRAM if needed
            if buf is self.sram and off+size>SRAM_PHYS:
                bs=bytes(self.sram[(off+i)&(SRAM_PHYS-1)] for i in range(size))
            else: bs=buf[off:off+size]
            value=int.from_bytes(bs,'big')
            if buf is self.flex_ext:
                self.flex_events.append({'pc':self.pc_provider() if self.pc_provider else 0,'kind':'R','addr':addr&0xffffffff,'size':size,'value':value})
            return value
        # PIT0 has write-to-clear status semantics and a live counter.
        a=addr&0xffffffff
        if a==PIT0_PCSR and size==2:
            value=self.pit0_pcsr&0xffff
        elif a==PIT0_PMR and size==2:
            value=self.pit0_pmr&0xffff
        elif a==PIT0_PCNTR and size==2:
            value=self.pit0_pcntr&0xffff
        elif a==DTIM0_DTCN and size==4:
            # MAIN assumes the bootloader left DTIM0 as a free-running timebase.
            # Scale semantic instructions so long hardware timeouts complete quickly
            # while preserving monotonic 32-bit wraparound behavior.
            steps=self.step_provider() if self.step_provider else 0
            value=(steps*DTIM_ACCEL)&0xffffffff
        elif size==1 and any(a==base+UART_USR_OFF for base in UART_BASES):
            # Idle UART: transmitter holding register ready and transmitter empty.
            value=UART_USR_TXRDY|UART_USR_TXEMP
            if a==UART8_BASE+UART_USR_OFF and self.uart_rx:
                value |= 0x01  # RXRDY
        elif a==UART8_BASE+UART_DATA_OFF and size==1 and self.uart_rx:
            value=self.uart_rx.pop(0)
        else:
            value=0
            for i in range(size): value=(value<<8)|self.mmio.get((off+i)&0xffffffff,0)
        self.mmio_events.append({'pc':self.pc_provider() if self.pc_provider else 0,'kind':'R','addr':a,'size':size,'value':value})
        return value

    def write(self,addr,size,value):
        value &= mask_for(size)
        buf,off=self._region(addr,size,True)
        bs=value.to_bytes(size,'big')
        if buf is not None:
            if buf is self.sram and off+size>SRAM_PHYS:
                for i,x in enumerate(bs): self.sram[(off+i)&(SRAM_PHYS-1)]=x
            else: buf[off:off+size]=bs
            if buf is self.flex_ext:
                self.flex_events.append({'pc':self.pc_provider() if self.pc_provider else 0,'kind':'W','addr':addr&0xffffffff,'size':size,'value':value})
            return
        a=addr&0xffffffff
        if a==PIT0_PCSR and size==2:
            # PIF is write-one-to-clear; configuration writes must not latch it high.
            self.pit0_pcsr=value & ~PIT_PCSR_PIF
            value_for_store=self.pit0_pcsr
            bs=value_for_store.to_bytes(2,'big')
            for i,x in enumerate(bs): self.mmio[(off+i)&0xffffffff]=x
        elif a==PIT0_PMR and size==2:
            self.pit0_pmr=value&0xffff; self.pit0_pcntr=self.pit0_pmr
            self.pit0_pcsr &= ~PIT_PCSR_PIF
            bs=self.pit0_pmr.to_bytes(2,'big')
            for i,x in enumerate(bs): self.mmio[(off+i)&0xffffffff]=x
        elif a==EDMA_SERQ and size==1:
            ch=value&0x3f; self.edma_erq.add(ch); self._mmio_raw_write(a,1,value)
            # UART8 TX requests are continuously available while TXRDY is set.
            if ch==EDMA_CH_UART8_TX: self._edma_service(ch)
            elif ch==EDMA_CH_UART8_RX and self.uart_rx: self._edma_service(ch)
        elif a==EDMA_CERQ and size==1:
            self.edma_erq.discard(value&0x3f); self._mmio_raw_write(a,1,value)
        elif a==EDMA_CINT and size==1:
            ch=value&0x3f
            if ch>=32:
                cur=self._mmio_raw_read(EDMA_INTH,4); self._mmio_raw_write(EDMA_INTH,4,cur&~(1<<(ch-32)))
            else:
                cur=self._mmio_raw_read(EDMA_INTL,4); self._mmio_raw_write(EDMA_INTL,4,cur&~(1<<ch))
            self.pending_irqs=[x for x in self.pending_irqs if x[0]!=(120+ch)]
            self._mmio_raw_write(a,1,value)
        else:
            for i,x in enumerate(bs): self.mmio[(off+i)&0xffffffff]=x
        self.mmio_events.append({'pc':self.pc_provider() if self.pc_provider else 0,'kind':'W','addr':a,'size':size,'value':value})

    def pit0_enabled(self):
        return bool((self.pit0_pcsr & (PIT_PCSR_EN|PIT_PCSR_PIE)) == (PIT_PCSR_EN|PIT_PCSR_PIE))

    def trigger_pit0(self):
        if not self.pit0_enabled():
            return False
        self.pit0_pcsr |= PIT_PCSR_PIF
        self.pit0_pcntr = self.pit0_pmr
        self.pit0_fires += 1
        if not any(v == PIT0_VECTOR for v, _, _ in self.pending_irqs):
            self.pending_irqs.append((PIT0_VECTOR, PIT0_LEVEL, 'PIT0'))
        # Mirror visible register bytes.
        self.mmio[PIT0_PCSR]= (self.pit0_pcsr>>8)&0xff
        self.mmio[PIT0_PCSR+1]= self.pit0_pcsr&0xff
        return True

@dataclass
class EA:
    cpu:'CPU'; mode:int; reg:int; size:int; ext_pc:int
    kind:str=''; addr:int|None=None; imm:int|None=None; postinc:int=0; predec:int=0
    ext_bytes:int=0

    def resolve(self,for_write=False):
        c=self.cpu; m=self.mode;r=self.reg;s=self.size; pc=self.ext_pc
        if m==0: self.kind='D'; return self
        if m==1: self.kind='A'; return self
        if m==2: self.kind='M'; self.addr=c.a[r]; return self
        if m==3:
            self.kind='M'; self.addr=c.a[r]
            self.postinc=s
            return self
        if m==4:
            inc=s
            c.a[r]=(c.a[r]-inc)&0xffffffff
            self.kind='M'; self.addr=c.a[r]; return self
        if m==5:
            disp=sx(c.bus.read(pc,2),16); self.ext_bytes=2
            self.kind='M'; self.addr=(c.a[r]+disp)&0xffffffff; return self
        if m==6:
            ext=c.bus.read(pc,2); self.ext_bytes=2
            # brief extension. ColdFire uses scale/index forms compatible enough for bring-up.
            idx_is_a=(ext>>15)&1; idx_reg=(ext>>12)&7; idx_long=(ext>>11)&1
            scale=1<<((ext>>9)&3); disp=sx(ext&0xff,8)
            iv=(c.a if idx_is_a else c.d)[idx_reg]
            if not idx_long: iv=sx(iv&0xffff,16)
            self.kind='M'; self.addr=(c.a[r]+disp+iv*scale)&0xffffffff; return self
        if m==7:
            if r==0:
                w=c.bus.read(pc,2); self.ext_bytes=2; self.kind='M'; self.addr=sx(w,16)&0xffffffff; return self
            if r==1:
                self.addr=c.bus.read(pc,4); self.ext_bytes=4; self.kind='M'; return self
            if r==2:
                disp=sx(c.bus.read(pc,2),16); self.ext_bytes=2; self.kind='M'; self.addr=(pc+disp)&0xffffffff; return self
            if r==3:
                ext=c.bus.read(pc,2); self.ext_bytes=2
                idx_is_a=(ext>>15)&1; idx_reg=(ext>>12)&7; idx_long=(ext>>11)&1
                scale=1<<((ext>>9)&3); disp=sx(ext&0xff,8)
                iv=(c.a if idx_is_a else c.d)[idx_reg]
                if not idx_long: iv=sx(iv&0xffff,16)
                self.kind='M'; self.addr=(pc+disp+iv*scale)&0xffffffff; return self
            if r==4:
                self.kind='I'; n=4 if s==4 else 2
                raw=c.bus.read(pc,n); self.ext_bytes=n
                self.imm=raw & mask_for(s); return self
        raise Unsupported(f'EA mode={m} reg={r} size={s}')

    def read(self):
        c=self.cpu;s=self.size
        if self.kind=='D': v=c.d[self.reg]&mask_for(s)
        elif self.kind=='A': v=c.a[self.reg]&mask_for(s)
        elif self.kind=='I': v=self.imm
        else: v=c.bus.read(self.addr,s)
        if self.postinc:
            c.a[self.reg]=(c.a[self.reg]+self.postinc)&0xffffffff; self.postinc=0
        return v
    def write(self,v):
        c=self.cpu;s=self.size;v&=mask_for(s)
        if self.kind=='D':
            old=c.d[self.reg]; m=mask_for(s); c.d[self.reg]=(old&~m)|v
        elif self.kind=='A':
            if s==2: c.a[self.reg]=sx(v,16)&0xffffffff
            else: c.a[self.reg]=v&0xffffffff
        elif self.kind=='M': c.bus.write(self.addr,s,v)
        else: raise Unsupported('write immediate')
        if self.postinc:
            c.a[self.reg]=(c.a[self.reg]+self.postinc)&0xffffffff; self.postinc=0

@dataclass
class CPU:
    bus:Bus
    d:list[int]=field(default_factory=lambda:[0]*8)
    a:list[int]=field(default_factory=lambda:[0]*8)
    pc:int=ENTRY
    sr:int=0x2700
    ctrl:dict[int,int]=field(default_factory=dict)
    # ColdFire enhanced multiply-accumulate unit.  Accumulators are kept as
    # 64-bit bit patterns; the architectural value occupies the low 48 bits.
    macsr:int=0
    mac_mask:int=0xffffffff
    macc:list[int]=field(default_factory=lambda:[0]*4)
    steps:int=0
    calls:int=0
    trace:list[dict]=field(default_factory=list)
    op_counts:Counter=field(default_factory=Counter)
    max_trace:int=200

    # CCR bits
    X=0x10; N=0x08; Z=0x04; V=0x02; C=0x01
    def __post_init__(self):
        self.bus.pc_provider=lambda:self.pc
        self.bus.step_provider=lambda:self.steps
    def rw(self,addr): return self.bus.read(addr,2)
    def rl(self,addr): return self.bus.read(addr,4)
    def ww(self,addr,v): self.bus.write(addr,2,v)
    def wl(self,addr,v): self.bus.write(addr,4,v)
    def fetchw(self): v=self.rw(self.pc); self.pc=(self.pc+2)&0xffffffff; return v
    def fetchl(self): v=self.rl(self.pc); self.pc=(self.pc+4)&0xffffffff; return v
    def pushl(self,v): self.a[7]=(self.a[7]-4)&0xffffffff; self.wl(self.a[7],v)
    def popl(self): v=self.rl(self.a[7]); self.a[7]=(self.a[7]+4)&0xffffffff; return v
    def set_nz(self,v,size,clear_vc=True):
        m=mask_for(size); v&=m
        self.sr &= ~(self.N|self.Z|(self.V|self.C if clear_vc else 0))
        if v==0:self.sr|=self.Z
        if v&signbit(size):self.sr|=self.N
    def _set_add_flags(self,a,b,r,size):
        m=mask_for(size); sb=signbit(size); a&=m; b&=m; r&=m
        self.sr &= ~(self.N|self.Z|self.V|self.C|self.X)
        if r==0:self.sr|=self.Z
        if r&sb:self.sr|=self.N
        if (~(a^b)&(a^r)&sb):self.sr|=self.V
        if a+b>m:self.sr|=self.C|self.X
    def _set_sub_flags(self,a,b,r,size,affect_x=True):
        m=mask_for(size); sb=signbit(size); a&=m; b&=m; r&=m
        clear=self.N|self.Z|self.V|self.C|(self.X if affect_x else 0); self.sr &= ~clear
        if r==0:self.sr|=self.Z
        if r&sb:self.sr|=self.N
        if ((a^b)&(a^r)&sb):self.sr|=self.V
        if b>a:self.sr|=self.C|(self.X if affect_x else 0)
    def cond(self,cc):
        n=bool(self.sr&self.N);z=bool(self.sr&self.Z);v=bool(self.sr&self.V);c=bool(self.sr&self.C)
        return [True,False,not(c or z),c or z,not c,c,not z,z,not v,v,not n,n,n==v,n!=v,(not z and n==v),(z or n!=v)][cc]
    def ea(self,mode,reg,size,pc): return EA(self,mode,reg,size,pc).resolve()
    def addr_ea(self,mode,reg,pc):
        e=self.ea(mode,reg,4,pc)
        if e.kind!='M': raise Unsupported('control EA not memory')
        return e.addr,e.ext_bytes
    def record(self,start,op,desc):
        self.op_counts[desc.split()[0]]+=1
        if len(self.trace)<self.max_trace or self.steps%10000==0:
            self.trace.append({'step':self.steps,'pc':start,'op':op,'desc':desc,'sp':self.a[7]})
            if len(self.trace)>self.max_trace*2: self.trace=self.trace[-self.max_trace:]

    def _mac_clear_flags(self):
        self.macsr &= ~(0x008|0x004|0x002|0x001)

    def _mac_set_flags(self,acc):
        val=self.macc[acc]&0xffffffffffffffff
        if (val&((1<<48)-1))==0:self.macsr|=0x004
        elif val&(1<<47):self.macsr|=0x008
        if self.macsr&(0x100<<acc):self.macsr|=0x002
        signed=sx(val,64)
        if self.macsr&0x020:
            top=signed>>40
            if top not in (0,-1):self.macsr|=0x001
        elif self.macsr&0x040:
            top=signed>>32
            if top not in (0,-1):self.macsr|=0x001
        elif val>>32:self.macsr|=0x001

    def _mac_saturate(self,acc):
        val=self.macc[acc]&0xffffffffffffffff
        signed=sx(val,64)
        if self.macsr&(0x020|0x040):
            result=sx(val&((1<<48)-1),48)
            if result!=signed:self.macsr|=0x002
            if self.macsr&0x002:
                self.macsr|=0x100<<acc
                if self.macsr&0x080:
                    result=(-0x800000000000 if signed<0 else 0x7fffffffffff) if self.macsr&0x020 else (-0x80000000 if signed<0 else 0x7fffffff)
            self.macc[acc]=result&0xffffffffffffffff
        else:
            if val>>48:self.macsr|=0x002
            if self.macsr&0x002:
                self.macsr|=0x100<<acc
                if self.macsr&0x080:val=0 if val>(1<<53) else (1<<48)-1
                else:val&=(1<<48)-1
            self.macc[acc]=val&0xffffffffffffffff

    def _mac_from(self,acc):
        val=self.macc[acc]&0xffffffffffffffff
        if self.macsr&0x020:
            if self.macsr&0x040:
                rem=val&0xffffff; out=(val>>24)&0xffff
                if rem>0x800000 or (rem==0x800000 and out&1):out+=1
            else:
                rem=val&0xff; out=val>>8
                if self.macsr&0x010 and (rem>0x80 or (rem==0x80 and out&1)):out+=1
            return out&0xffffffff
        if not (self.macsr&0x080):return val&0xffffffff
        if self.macsr&0x040:
            sv=sx(val,64)
            return (sv if -0x80000000<=sv<=0x7fffffff else (0x80000000 if sv<0 else 0x7fffffff))&0xffffffff
        return (val if val<=0xffffffff else 0xffffffff)&0xffffffff

    def _set_macsr(self,val):
        # Firmware initializes MACSR before loading its accumulators.  Retain
        # QEMU-compatible mode state; representation conversion is unnecessary
        # for that path and intentionally deferred until a test needs it.
        self.macsr=val&0xffffffff

    def raise_cf_exception(self,index:int):
        # QEMU-compatible ColdFire exception frame used for traps/interrupt bring-up.
        vector=index<<2; ret=self.pc; oldsp=self.a[7]
        fmt=0x40000000 | ((vector&0xfff)<<16) | (self.sr&0xffff) | ((oldsp&3)<<28)
        sp=oldsp&~3; sp=(sp-4)&0xffffffff; self.wl(sp,ret); sp=(sp-4)&0xffffffff; self.wl(sp,fmt); self.a[7]=sp
        self.sr|=0x2000; vbr=self.ctrl.get(0x801,0); self.pc=self.rl((vbr+vector)&0xffffffff)

    def raise_cf_hw_interrupt(self,index:int,level:int):
        # QEMU cf_interrupt_all semantics: frame records pre-interrupt SR, while live SR
        # enters supervisor mode with the accepted interrupt level masked.
        cur_level=(self.sr>>8)&7
        if level<=cur_level:
            return False
        vector=index<<2; ret=self.pc; oldsp=self.a[7]; oldsr=self.sr&0xffff
        fmt=0x40000000 | ((vector&0xfff)<<16) | oldsr | ((oldsp&3)<<28)
        self.sr=(self.sr|0x2000)&~0x0700; self.sr|=(level&7)<<8
        sp=oldsp&~3; sp=(sp-4)&0xffffffff; self.wl(sp,ret); sp=(sp-4)&0xffffffff; self.wl(sp,fmt); self.a[7]=sp
        vbr=self.ctrl.get(0x801,0); self.pc=self.rl((vbr+vector)&0xffffffff)
        return True
    def step(self):
        # Deliver the highest-level pending board interrupt as soon as the live SR mask permits.
        if self.bus.pending_irqs:
            eligible=[x for x in self.bus.pending_irqs if x[1] > ((self.sr>>8)&7)]
            if eligible:
                vec,lev,src=max(eligible,key=lambda x:x[1])
                self.bus.pending_irqs.remove((vec,lev,src))
                self.raise_cf_hw_interrupt(vec,lev)
        start=self.pc; op=self.fetchw(); desc=''
        # ColdFire EMAC register transfers.  These are decoded before the
        # ordinary A-line MAC family, matching QEMU's more-specific patterns.
        if (op&0xF9B0)==0xA180:  # move.l ACCn,Rx; optional clear
            acc=(op>>9)&3; reg=op&7; val=self._mac_from(acc)
            (self.a if op&8 else self.d)[reg]=val
            if op&0x40:
                self.macc[acc]=0; self.macsr&=~(0x100<<acc)
            desc=f'FROM_MAC ACC{acc}'
        elif (op&0xF9FC)==0xA110:  # move accumulator to accumulator
            src=op&3; dst=(op>>9)&3; self.macc[dst]=self.macc[src]
            self.macsr=(self.macsr&~(0x100<<dst))|((self.macsr&(0x100<<src))<<(dst-src) if dst>=src else (self.macsr&(0x100<<src))>>(src-dst))
            self._mac_clear_flags(); self._mac_set_flags(dst); desc=f'MOVE_MAC ACC{src}->ACC{dst}'
        elif (op&0xF9F0)==0xA980:
            reg=op&7; (self.a if op&8 else self.d)[reg]=self.macsr&0xffffffff; desc='FROM_MACSR'
        elif (op&0xFFF0)==0xAD80:
            reg=op&7; (self.a if op&8 else self.d)[reg]=self.mac_mask&0xffffffff; desc='FROM_MASK'
        elif (op&0xFBF0)==0xAB80:
            reg=op&7; base=2 if op&0x400 else 0
            if self.macsr&0x020:
                val=(self.macc[base]&0xff)|((self.macc[base]>>32)&0xff00)|((self.macc[base+1]<<16)&0xff0000)|((self.macc[base+1]>>16)&0xff000000)
            else: val=((self.macc[base]>>32)&0xffff)|((self.macc[base+1]>>16)&0xffff0000)
            (self.a if op&8 else self.d)[reg]=val&0xffffffff; desc='FROM_MEXT'
        elif op==0xA9C0:
            self.sr=(self.sr&~(self.N|self.Z|self.V))|(self.macsr&(self.N|self.Z|self.V)); desc='MACSR_TO_CCR'
        elif (op&0xF9C0)==0xA100:
            acc=(op>>9)&3; ea=self.ea((op>>3)&7,op&7,4,self.pc); self.pc+=ea.ext_bytes; val=ea.read()&0xffffffff
            if self.macsr&0x020:self.macc[acc]=(sx(val,32)<<8)&0xffffffffffffffff
            elif self.macsr&0x040:self.macc[acc]=sx(val,32)&0xffffffffffffffff
            else:self.macc[acc]=val
            self.macsr&=~(0x100<<acc); self._mac_clear_flags(); self._mac_set_flags(acc); desc=f'TO_MAC ACC{acc}'
        elif (op&0xFFC0)==0xA900:
            ea=self.ea((op>>3)&7,op&7,4,self.pc); self.pc+=ea.ext_bytes; self._set_macsr(ea.read()); desc='TO_MACSR'
        elif (op&0xFBC0)==0xAB00:
            base=2 if op&0x400 else 0; ea=self.ea((op>>3)&7,op&7,4,self.pc); self.pc+=ea.ext_bytes; val=ea.read()&0xffffffff
            if self.macsr&0x020:
                for i,shift in ((base,0),(base+1,16)):
                    piece=(val>>shift)&0xffff; hi=sx(piece&0xff00,16); self.macc[i]=((self.macc[i]&0xffffffff00)|(hi<<32)|(piece&0xff))&0xffffffffffffffff
            else:
                lo=val&0xffff; hi=(val>>16)&0xffff
                self.macc[base]=((self.macc[base]&0xffffffff)|(sx(lo,16)<<32 if self.macsr&0x040 else lo<<32))&0xffffffffffffffff
                self.macc[base+1]=((self.macc[base+1]&0xffffffff)|(sx(hi,16)<<32 if self.macsr&0x040 else hi<<32))&0xffffffffffffffff
            desc='TO_MEXT'
        elif (op&0xFFC0)==0xAD00:
            ea=self.ea((op>>3)&7,op&7,4,self.pc); self.pc+=ea.ext_bytes; self.mac_mask=(ea.read()|0xffff0000)&0xffffffff; desc='TO_MASK'
        elif (op&0xF100)==0xA000:
            ext=self.fetchw(); acc=((op>>7)&1)|((ext>>3)&2); dual=bool((op&0x30) and (ext&3))
            if op&0x30:
                # Capture multiply operands before an addressing-mode writeback.
                rx=((self.a if ext&0x8000 else self.d)[(ext>>12)&7])&0xffffffff
                ry=((self.a if ext&8 else self.d)[ext&7])&0xffffffff
                ea=self.ea((op>>3)&7,op&7,4,self.pc); self.pc+=ea.ext_bytes
                if ea.kind!='M':raise Unsupported('EMAC load EA not memory')
                ea.addr&=self.mac_mask; loadval=ea.read()&0xffffffff; acc^=1
            else:
                rx=((self.a if op&0x40 else self.d)[(op>>9)&7])&0xffffffff
                ry=((self.a if op&8 else self.d)[op&7])&0xffffffff; loadval=None
            self._mac_clear_flags()
            if not (ext&0x0800):
                upperx=bool(ext&0x80); uppery=bool(ext&0x40)
                if self.macsr&0x020:
                    rx=(rx&0xffff0000) if upperx else ((rx&0xffff)<<16)
                    ry=(ry&0xffff0000) if uppery else ((ry&0xffff)<<16)
                elif self.macsr&0x040:
                    rx=sx((rx>>16)&0xffff,16) if upperx else sx(rx&0xffff,16)
                    ry=sx((ry>>16)&0xffff,16) if uppery else sx(ry&0xffff,16)
                else:
                    rx=((rx>>16)&0xffff) if upperx else (rx&0xffff)
                    ry=((ry>>16)&0xffff) if uppery else (ry&0xffff)
            if self.macsr&0x040: prod=sx(rx&0xffffffff,32)*sx(ry&0xffffffff,32)
            else: prod=(rx&0xffffffff)*(ry&0xffffffff)
            shift=(ext>>9)&3
            if shift==1:prod<<=1
            elif shift==3:prod=(prod&0xffffffffffffffff)>>1
            targets=[(acc,bool(op&0x100))]
            if dual:targets.append(((ext>>2)&3,bool(ext&2)))
            for anum,subtract in targets:
                cur=sx(self.macc[anum]&0xffffffffffffffff,64)
                self.macc[anum]=(cur-prod if subtract else cur+prod)&0xffffffffffffffff
                self._mac_saturate(anum)
            self._mac_set_flags(targets[-1][0])
            if loadval is not None:
                (self.a if op&0x40 else self.d)[(op>>9)&7]=loadval
            desc='EMAC'
        # NOP / RTS / RTE
        elif op==0x4E71: desc='NOP'
        elif op==0x4E75: self.pc=self.popl(); desc='RTS'
        elif op==0x4E73:  # ColdFire RTE: [fmt:32][pc:32]
            sp=self.a[7]; fmt=self.rl(sp); newpc=self.rl((sp+4)&0xffffffff)
            sp |= (fmt>>28)&3; self.a[7]=(sp+8)&0xffffffff; self.sr=fmt&0xffff; self.pc=newpc; desc='RTE'
        elif 0x4E40 <= op <= 0x4E4F:  # TRAP #n, ColdFire 8-byte frame
            n=op&0xf; index=32+n; self.raise_cf_exception(index); desc=f'TRAP #{n}'
        elif op==0x4E77: # RTR (rare)
            self.sr=(self.sr&0xff00)|self.rw(self.a[7]); self.a[7]+=2; self.pc=self.popl(); desc='RTR'
        # SR/CCR transfers
        elif (op&0xFFC0)==0x40C0:  # MOVE SR,<ea>
            ea=self.ea((op>>3)&7,op&7,2,self.pc); self.pc+=ea.ext_bytes; ea.write(self.sr&0xffff); desc='MOVE SR->EA'
        elif (op&0xFFC0)==0x42C0:  # MOVE CCR,<ea>
            ea=self.ea((op>>3)&7,op&7,2,self.pc); self.pc+=ea.ext_bytes; ea.write(self.sr&0xff); desc='MOVE CCR->EA'
        elif (op&0xFFC0)==0x44C0:  # MOVE <ea>,CCR
            ea=self.ea((op>>3)&7,op&7,2,self.pc); self.pc+=ea.ext_bytes; self.sr=(self.sr&0xff00)|(ea.read()&0xff); desc='MOVE EA->CCR'
        elif (op&0xFFC0)==0x46C0:  # MOVE <ea>,SR
            ea=self.ea((op>>3)&7,op&7,2,self.pc); self.pc+=ea.ext_bytes; self.sr=ea.read()&0xffff; desc='MOVE EA->SR'
        # MOVEC (ColdFire control registers). Extension: reg/control encoded.
        elif op in (0x4E7A,0x4E7B):
            ext=self.fetchw(); regnum=(ext>>12)&0xF; creg=ext&0xFFF
            regs=self.d+self.a
            if op==0x4E7B: self.ctrl[creg]=regs[regnum]&0xffffffff; desc=f'MOVEC R{regnum}->C{creg:03X}'
            else:
                val=self.ctrl.get(creg,0); (self.d if regnum<8 else self.a)[regnum&7]=val; desc=f'MOVEC C{creg:03X}->R{regnum}'
        # ColdFire MVS/MVZ byte/word -> 32-bit data register (ISA B)
        elif (op&0xF1C0) in (0x7100,0x7140,0x7180,0x71C0):
            form=op&0xF1C0; dr=(op>>9)&7; size=1 if form in (0x7100,0x7180) else 2
            ea=self.ea((op>>3)&7,op&7,size,self.pc); self.pc+=ea.ext_bytes; v=ea.read()
            signed=form in (0x7100,0x7140)
            if signed: v=sx(v,8 if size==1 else 16)&0xffffffff
            else: v &= mask_for(size)
            self.d[dr]=v; self.set_nz(v,4); desc=('MVS' if signed else 'MVZ')+('.B' if size==1 else '.W')
        # MOVEQ
        elif (op&0xF100)==0x7000:
            r=(op>>9)&7; val=sx(op&0xff,8)&0xffffffff; self.d[r]=val; self.set_nz(val,4); desc=f'MOVEQ #{sx(op&0xff,8)},D{r}'
        # MOVE / MOVEA
        elif (op&0xC000)==0 and ((op>>12)&3) in (1,2,3):
            smap={1:1,2:4,3:2}; size=smap[(op>>12)&3]
            sm=(op>>3)&7; sr=op&7; dm=(op>>6)&7; dr=(op>>9)&7
            src=self.ea(sm,sr,size,self.pc); self.pc+=src.ext_bytes; val=src.read()
            dst=self.ea(dm,dr,size,self.pc); self.pc+=dst.ext_bytes
            if dm==1:
                # MOVEA does not affect flags; word sign extends
                self.a[dr]=(sx(val,16)&0xffffffff) if size==2 else val&0xffffffff
                desc=f'MOVEA.{"W" if size==2 else "L"} ->A{dr}'
            else:
                dst.write(val); self.set_nz(val,size); desc=f'MOVE.{ {1:"B",2:"W",4:"L"}[size]}'
        # LEA
        elif (op&0xF1C0)==0x41C0:
            dr=(op>>9)&7; mode=(op>>3)&7; reg=op&7; addr,n=self.addr_ea(mode,reg,self.pc); self.pc+=n; self.a[dr]=addr; desc=f'LEA 0x{addr:08X},A{dr}'
        # JSR/JMP
        elif (op&0xFFC0)==0x4E80:
            mode=(op>>3)&7;reg=op&7;addr,n=self.addr_ea(mode,reg,self.pc);self.pc+=n;ret=self.pc;self.pushl(ret);self.pc=addr;self.calls+=1;desc=f'JSR 0x{addr:08X}'
        elif (op&0xFFC0)==0x4EC0:
            mode=(op>>3)&7;reg=op&7;addr,n=self.addr_ea(mode,reg,self.pc);self.pc=addr;desc=f'JMP 0x{addr:08X}'
        # Register-only encodings override the broader PEA/MOVEM families.
        elif (op&0xFFF8)==0x4840:
            r=op&7;v=self.d[r];self.d[r]=((v<<16)&0xffffffff)|(v>>16);self.set_nz(self.d[r],4);desc=f'SWAP D{r}'
        elif (op&0xFFF8)==0x4880:
            r=op&7;v=sx(self.d[r]&0xff,8)&0xffff;self.d[r]=(self.d[r]&0xffff0000)|v;self.set_nz(v,2);desc='EXT.W'
        elif (op&0xFFF8)==0x48C0:
            r=op&7;v=sx(self.d[r]&0xffff,16)&0xffffffff;self.d[r]=v;self.set_nz(v,4);desc='EXT.L'
        # Long multiply MULU.L/MULS.L, including 64-bit two-register form.
        elif (op&0xFFC0)==0x4C00:
            mode=(op>>3)&7; reg=op&7; ext=self.fetchw(); signed=bool(ext&0x0800); wide=bool(ext&0x0400)
            dl=(ext>>12)&7; dh=ext&7
            src=self.ea(mode,reg,4,self.pc); self.pc+=src.ext_bytes; sv=src.read()&0xffffffff; dv=self.d[dl]&0xffffffff
            if signed:
                prod=(sx(sv,32)*sx(dv,32)) & 0xffffffffffffffff
            else:
                prod=(sv*dv)&0xffffffffffffffff
            self.d[dl]=prod&0xffffffff
            if wide:self.d[dh]=(prod>>32)&0xffffffff
            self.set_nz(prod&0xffffffff,4)
            desc=('MULS.L' if signed else 'MULU.L')+(' 64' if wide else '')
        # Long divide DIVU.L/DIVS.L (ColdFire 32-bit divisor, quotient register).
        elif (op&0xFFC0)==0x4C40:
            mode=(op>>3)&7; reg=op&7; ext=self.fetchw(); signed=bool(ext&0x0800)
            qreg=(ext>>12)&7; rreg=ext&7
            src=self.ea(mode,reg,4,self.pc); self.pc+=src.ext_bytes; den=src.read()&0xffffffff
            num=self.d[qreg]&0xffffffff
            if den==0:
                raise Unsupported('DIVL divide by zero exception not yet modeled')
            if signed:
                sn=sx(num,32); sd=sx(den,32)
                aq=abs(sn)//abs(sd); q=-aq if (sn<0)^(sd<0) else aq
                rem=sn-q*sd
                overflow=not (-0x80000000 <= q <= 0x7fffffff)
            else:
                q=num//den; rem=num%den; overflow=q>0xffffffff
            # ColdFire plain DIVL encodes the same register in both fields.
            # Remainder forms use distinct rreg/qreg fields.
            if not overflow:
                self.d[qreg]=q&0xffffffff
                if rreg!=qreg:self.d[rreg]=rem&0xffffffff
            self.sr &= ~(self.N|self.Z|self.V|self.C)
            if overflow:self.sr|=self.V
            else:
                if (q&0xffffffff)==0:self.sr|=self.Z
                if q&0x80000000:self.sr|=self.N
            desc=('DIVS.L' if signed else 'DIVU.L')
        # ColdFire ISA-B signed saturation of a data register after an
        # overflowing arithmetic operation.
        elif (op&0xFFF8)==0x4C80:
            reg=op&7; val=self.d[reg]&0xffffffff
            if self.sr&self.V: val=0x7fffffff if val&0x80000000 else 0x80000000
            self.d[reg]=val; self.set_nz(val,4); desc=f'SATS D{reg}'
        # MOVEM (word/long, register list to/from memory)
        elif (op&0xFB80)==0x4880:
            mem_to_regs=bool(op&0x0400); size=4 if (op&0x0040) else 2
            mode=(op>>3)&7; reg=op&7; regmask=self.fetchw()
            # Resolve base address manually because MOVEM applies address-reg update once.
            if mode==2: addr=self.a[reg]
            elif mode==3: addr=self.a[reg]
            elif mode==4: addr=self.a[reg]
            elif mode==5:
                disp=sx(self.fetchw(),16); addr=(self.a[reg]+disp)&0xffffffff
            elif mode==6:
                ext=self.fetchw(); idx_is_a=(ext>>15)&1; idx_reg=(ext>>12)&7; idx_long=(ext>>11)&1; scale=1<<((ext>>9)&3); disp=sx(ext&0xff,8)
                iv=(self.a if idx_is_a else self.d)[idx_reg]; iv=iv if idx_long else sx(iv&0xffff,16); addr=(self.a[reg]+disp+iv*scale)&0xffffffff
            elif mode==7 and reg in (0,1):
                addr=(sx(self.fetchw(),16)&0xffffffff) if reg==0 else self.fetchl()
            else: raise Unsupported(f'MOVEM mode={mode} reg={reg}')
            regs=self.d+self.a
            if mem_to_regs:
                # memory -> registers: normal mask order D0..A7; postincrement updates base register.
                paddr=addr
                for idx in range(16):
                    if regmask&(1<<idx):
                        v=self.bus.read(paddr,size); paddr=(paddr+size)&0xffffffff
                        if size==2:v=sx(v,16)&0xffffffff
                        (self.d if idx<8 else self.a)[idx&7]=v
                if mode==3:self.a[reg]=paddr
                desc=f'MOVEM.{"L" if size==4 else "W"} M->R'
            else:
                if mode==4:
                    # predecrement mask encoding is reversed: bit0=A7 ... bit15=D0.
                    paddr=addr
                    for bit in range(16):
                        if regmask&(1<<bit):
                            idx=15-bit; paddr=(paddr-size)&0xffffffff; self.bus.write(paddr,size,regs[idx])
                    self.a[reg]=paddr
                else:
                    paddr=addr
                    for idx in range(16):
                        if regmask&(1<<idx): self.bus.write(paddr,size,regs[idx]); paddr=(paddr+size)&0xffffffff
                desc=f'MOVEM.{"L" if size==4 else "W"} R->M'
        # PEA
        elif (op&0xFFC0)==0x4840 and ((op>>3)&7)!=0:
            mode=(op>>3)&7;reg=op&7;addr,n=self.addr_ea(mode,reg,self.pc);self.pc+=n;self.pushl(addr);desc=f'PEA 0x{addr:08X}'
        # LINK.W / UNLK
        elif (op&0xFFF8)==0x4E50:
            r=op&7; disp=sx(self.fetchw(),16); self.pushl(self.a[r]); self.a[r]=self.a[7]; self.a[7]=(self.a[7]+disp)&0xffffffff; desc=f'LINK A{r},{disp}'
        elif (op&0xFFF8)==0x4E58:
            r=op&7; self.a[7]=self.a[r]; self.a[r]=self.popl(); desc=f'UNLK A{r}'
        # Branches
        elif (op&0xF000)==0x6000:
            cc=(op>>8)&0xF; d8=op&0xff; base=(start+2)&0xffffffff
            if d8==0: disp=sx(self.fetchw(),16)
            elif d8==0xff: disp=sx(self.fetchl(),32)
            else: disp=sx(d8,8)
            if cc==1: self.pushl(self.pc); self.pc=(base+disp)&0xffffffff; desc=f'BSR {disp:+d}'
            elif cc==0: self.pc=(base+disp)&0xffffffff; desc=f'BRA {disp:+d}'
            elif self.cond(cc): self.pc=(base+disp)&0xffffffff; desc=f'Bcc{cc:X} taken {disp:+d}'
            else: desc=f'Bcc{cc:X} nottaken'
        # DBcc
        elif (op&0xF0F8)==0x50C8:
            cc=(op>>8)&0xf; r=op&7; base=(start+2)&0xffffffff; disp=sx(self.fetchw(),16)
            if not self.cond(cc):
                w=(self.d[r]-1)&0xffff; self.d[r]=(self.d[r]&0xffff0000)|w
                if w!=0xffff:self.pc=(base+disp)&0xffffffff
            desc=f'DBcc{cc:X} D{r}'
        # ColdFire FF1 Dn: find first one from MSB, encoded as leading-zero count.
        elif (op&0xFFF8)==0x04C0:
            r=op&7; v=self.d[r]&0xffffffff; self.d[r]=(32 if v==0 else 32-v.bit_length()); self.set_nz(self.d[r],4); desc=f'FF1 D{r}'
        # immediate arithmetic/logical
        elif (op&0xF000)==0x0000 and (op&0x0F00) in (0x000,0x200,0x400,0x600,0xA00,0xC00):
            fam=op&0x0F00; sc=(op>>6)&3
            if sc==3: raise Unsupported('immediate size=3')
            size=(1,2,4)[sc]; mode=(op>>3)&7; reg=op&7
            imm=self.fetchl() if size==4 else self.fetchw()&mask_for(size)
            ea=self.ea(mode,reg,size,self.pc); self.pc+=ea.ext_bytes; old=ea.read(); m=mask_for(size)
            names={0x000:'ORI',0x200:'ANDI',0x400:'SUBI',0x600:'ADDI',0xA00:'EORI',0xC00:'CMPI'}
            if fam==0x000: res=old|imm
            elif fam==0x200: res=old&imm
            elif fam==0xA00: res=old^imm
            elif fam==0x600: res=(old+imm)&m
            elif fam in (0x400,0xC00): res=(old-imm)&m
            if fam!=0xC00: ea.write(res)
            if fam in (0x000,0x200,0xA00):
                self.set_nz(res,size)
            elif fam==0x600:
                self._set_add_flags(old,imm,res,size)
            elif fam==0x400:
                self._set_sub_flags(old,imm,res,size,affect_x=True)
            else:  # CMPI does not affect X
                self._set_sub_flags(old,imm,res,size,affect_x=False)
            desc=names[fam]
        # Dynamic bit operations BTST/BCHG/BCLR/BSET Dn,<ea>
        elif (op&0xF100)==0x0100:
            src=(op>>9)&7; which=(op>>6)&3; mode=(op>>3)&7; reg=op&7; bit=self.d[src]&0x1f
            size=4 if mode==0 else 1; ea=self.ea(mode,reg,size,self.pc); self.pc+=ea.ext_bytes; old=ea.read(); b=bit%(32 if mode==0 else 8)
            self.sr &= ~self.Z
            if not (old&(1<<b)): self.sr|=self.Z
            if which==1: ea.write(old^(1<<b))
            elif which==2: ea.write(old&~(1<<b))
            elif which==3: ea.write(old|(1<<b))
            desc=['BTSTd','BCHGd','BCLRd','BSETd'][which]
        # immediate bit operations
        elif (op&0xFF00)==0x0800:
            which=(op>>6)&3; mode=(op>>3)&7; reg=op&7; bit=self.fetchw()&0xff
            size=4 if mode==0 else 1; ea=self.ea(mode,reg,size,self.pc); self.pc+=ea.ext_bytes; old=ea.read(); b=bit%(32 if mode==0 else 8)
            self.sr &= ~self.Z
            if not (old&(1<<b)): self.sr|=self.Z
            if which==1: ea.write(old^(1<<b))
            elif which==2: ea.write(old&~(1<<b))
            elif which==3: ea.write(old|(1<<b))
            desc=['BTST','BCHG','BCLR','BSET'][which]
        # NEG / NOT
        elif (op&0xFF00)==0x4400:
            sc=(op>>6)&3
            if sc==3: raise Unsupported('NEG size3')
            size=(1,2,4)[sc]; ea=self.ea((op>>3)&7,op&7,size,self.pc); self.pc+=ea.ext_bytes; old=ea.read(); res=(-old)&mask_for(size); ea.write(res); self._set_sub_flags(0,old,res,size); desc='NEG'
        elif (op&0xFF00)==0x4600:
            sc=(op>>6)&3
            if sc==3: raise Unsupported('NOT size3')
            size=(1,2,4)[sc]; ea=self.ea((op>>3)&7,op&7,size,self.pc); self.pc+=ea.ext_bytes; res=(~ea.read())&mask_for(size); ea.write(res); self.set_nz(res,size); desc='NOT'
        # CLR
        elif (op&0xFF00)==0x4200:
            sc=(op>>6)&3
            if sc==3:raise Unsupported('CLR size3')
            size=(1,2,4)[sc]; ea=self.ea((op>>3)&7,op&7,size,self.pc);self.pc+=ea.ext_bytes;ea.write(0);self.set_nz(0,size);desc='CLR'
        # TST
        elif (op&0xFF00)==0x4A00:
            sc=(op>>6)&3
            if sc==3:raise Unsupported('TST size3')
            size=(1,2,4)[sc];ea=self.ea((op>>3)&7,op&7,size,self.pc);self.pc+=ea.ext_bytes;v=ea.read();self.set_nz(v,size);desc='TST'
        # SWAP Dn
        elif (op&0xFFF8)==0x4840:
            r=op&7;v=self.d[r];self.d[r]=((v<<16)&0xffffffff)|(v>>16);self.set_nz(self.d[r],4);desc=f'SWAP D{r}'
        # EXT.W / EXT.L
        elif (op&0xFFF8)==0x4880:
            r=op&7;v=sx(self.d[r]&0xff,8)&0xffff;self.d[r]=(self.d[r]&0xffff0000)|v;self.set_nz(v,2);desc='EXT.W'
        elif (op&0xFFF8)==0x48C0:
            r=op&7;v=sx(self.d[r]&0xffff,16)&0xffffffff;self.d[r]=v;self.set_nz(v,4);desc='EXT.L'
        # Register shifts/rotates (AS/LS/ROX/RO; common ColdFire form)
        elif (op&0xF000)==0xE000 and ((op>>6)&3)!=3:
            count_field=(op>>9)&7; left=bool(op&0x0100); sc=(op>>6)&3; size=(1,2,4)[sc]
            by_reg=bool(op&0x0020); typ=(op>>3)&3; dr=op&7
            count=(self.d[count_field]&0x3f) if by_reg else (8 if count_field==0 else count_field)
            m=mask_for(size); sb=signbit(size); v=self.d[dr]&m; last=0
            # Implement AS and LS precisely enough for firmware control flow; ROX/RO supported too.
            for _ in range(count):
                if typ==0:  # arithmetic shift
                    if left:
                        last=1 if (v&sb) else 0; v=(v<<1)&m
                    else:
                        last=v&1; v=((v>>1)|(v&sb))&m
                elif typ==1:  # logical shift
                    if left: last=1 if (v&sb) else 0; v=(v<<1)&m
                    else: last=v&1; v>>=1
                elif typ==2:  # rotate through extend
                    x=1 if (self.sr&self.X) else 0
                    if left: last=1 if (v&sb) else 0; v=((v<<1)|x)&m
                    else: last=v&1; v=(v>>1)|(x*sb)
                    self.sr=(self.sr&~self.X)|(self.X if last else 0)
                else:  # rotate
                    if left: last=1 if (v&sb) else 0; v=((v<<1)|last)&m
                    else: last=v&1; v=(v>>1)|(last*sb)
            oldx=self.sr&self.X; self.sr &= ~(self.N|self.Z|self.V|self.C)
            if v==0:self.sr|=self.Z
            if v&sb:self.sr|=self.N
            if count and last:self.sr|=self.C
            if typ in (0,1) and count:
                self.sr=(self.sr&~self.X)|(self.X if last else 0)
            elif typ==3:self.sr=(self.sr&~self.X)|oldx
            self.d[dr]=(self.d[dr]&~m)|(v&m)
            desc=('AS' if typ==0 else 'LS' if typ==1 else 'ROX' if typ==2 else 'RO')+('L' if left else 'R')
        # OR / AND / ADD / SUB / CMP-EOR data-processing families
        elif (op&0xF000) in (0x8000,0x9000,0xB000,0xC000,0xD000):
            top=op&0xF000; dn=(op>>9)&7; opmode=(op>>6)&7; mode=(op>>3)&7; reg=op&7
            # sizes for data operations
            if opmode in (0,1,2,4,5,6):
                size=(1,2,4)[opmode%4 if opmode<3 else opmode-4]
                # CMP family uses 0..2 as CMP, 4..6 as EOR Dn -> ea
                if top==0xB000 and opmode in (0,1,2):
                    src=self.ea(mode,reg,size,self.pc); self.pc+=src.ext_bytes; sv=src.read(); dv=self.d[dn]&mask_for(size)
                    res=(dv-sv)&mask_for(size); self._set_sub_flags(dv,sv,res,size,affect_x=False); desc='CMP'
                elif top==0xB000 and opmode in (4,5,6):
                    dst=self.ea(mode,reg,size,self.pc); self.pc+=dst.ext_bytes; old=dst.read(); res=old^(self.d[dn]&mask_for(size)); dst.write(res); self.set_nz(res,size); desc='EOR'
                else:
                    ea_to_dn = opmode in (0,1,2)
                    if ea_to_dn:
                        src=self.ea(mode,reg,size,self.pc); self.pc+=src.ext_bytes; sv=src.read(); dv=self.d[dn]&mask_for(size)
                        if top==0x8000: res=dv|sv; self.set_nz(res,size); desc='OR'
                        elif top==0xC000: res=dv&sv; self.set_nz(res,size); desc='AND'
                        elif top==0xD000: res=(dv+sv)&mask_for(size); self._set_add_flags(dv,sv,res,size); desc='ADD'
                        elif top==0x9000: res=(dv-sv)&mask_for(size); self._set_sub_flags(dv,sv,res,size); desc='SUB'
                        else: raise Unsupported('data family')
                        m=mask_for(size); self.d[dn]=(self.d[dn]&~m)|(res&m)
                    else:
                        dst=self.ea(mode,reg,size,self.pc); self.pc+=dst.ext_bytes; old=dst.read(); sv=self.d[dn]&mask_for(size)
                        if top==0x8000: res=old|sv; self.set_nz(res,size); desc='OR'
                        elif top==0xC000: res=old&sv; self.set_nz(res,size); desc='AND'
                        elif top==0xD000: res=(old+sv)&mask_for(size); self._set_add_flags(old,sv,res,size); desc='ADD'
                        elif top==0x9000: res=(old-sv)&mask_for(size); self._set_sub_flags(old,sv,res,size); desc='SUB'
                        else: raise Unsupported('data family')
                        dst.write(res)
            elif opmode in (3,7) and top==0xC000:
                # Classic 16x16 -> 32-bit MULU.W/MULS.W encoding retained by
                # ColdFire. The destination operand is the low word of Dn.
                src=self.ea(mode,reg,2,self.pc); self.pc+=src.ext_bytes; sv=src.read()
                dv=self.d[dn]&0xffff
                signed=opmode==7
                if signed:
                    result=(sx(dv,16)*sx(sv,16))&0xffffffff
                else:
                    result=(dv*sv)&0xffffffff
                self.d[dn]=result; self.set_nz(result,4)
                desc='MULS.W' if signed else 'MULU.W'
            elif opmode in (3,7) and top in (0x9000,0xB000,0xD000):
                # SUBA/CMPA/ADDA: word for opmode3, long for 7
                size=2 if opmode==3 else 4; src=self.ea(mode,reg,size,self.pc); self.pc+=src.ext_bytes; sv=src.read();
                if size==2: sv=sx(sv,16)&0xffffffff
                if top==0xD000: self.a[dn]=(self.a[dn]+sv)&0xffffffff; desc='ADDA'
                elif top==0x9000: self.a[dn]=(self.a[dn]-sv)&0xffffffff; desc='SUBA'
                else:
                    dv=self.a[dn]; res=(dv-sv)&0xffffffff; self._set_sub_flags(dv,sv,res,4,affect_x=False); desc='CMPA'
            else:
                raise Unsupported(f'data op top={top:04x} opmode={opmode}')
        # ADDQ/SUBQ, excluding Scc/DBcc encodings
        elif (op&0xF000)==0x5000 and ((op>>6)&3)!=3:
            q=(op>>9)&7;q=8 if q==0 else q;sub=bool(op&0x0100);sc=(op>>6)&3;size=(1,2,4)[sc]
            mode=(op>>3)&7;reg=op&7;ea=self.ea(mode,reg,size,self.pc);self.pc+=ea.ext_bytes;old=ea.read();res=(old-q if sub else old+q)&mask_for(size);ea.write(res)
            if mode!=1:
                if sub:self._set_sub_flags(old,q,res,size,affect_x=True)
                else:self._set_add_flags(old,q,res,size)
            desc='SUBQ' if sub else 'ADDQ'
        # Scc
        elif (op&0xF0C0)==0x50C0:
            cc=(op>>8)&0xf;ea=self.ea((op>>3)&7,op&7,1,self.pc);self.pc+=ea.ext_bytes;ea.write(0xff if self.cond(cc) else 0);desc=f'Scc{cc:X}'
        else:
            raise Unsupported(f'opcode 0x{op:04X}')

        self.steps+=1; self.record(start,op,desc); return desc

    def run(self,max_steps=1000000):
        stop=None
        try:
            while self.steps<max_steps:
                # Verified AR 1.72 BSS clear loop at 0x4000085A:
                # MOVEM.L D4-D7,(A0); LEA 16(A0),A0; SUBQ #1,D1; BNE.
                # When the loop count is large, perform the exact zero-fill in bulk.
                if self.pc==0x4000085A and (self.d[1]&0xffffffff)>0x1000:
                    n=self.d[1]&0xffffffff; start=self.a[0]; end=(start+n*16)&0xffffffff
                    if start==0x402B5000 and end==0x42FA1170:
                        off=start-SDRAM_BASE; self.bus.sdram[off:off+n*16]=b'\0'*(n*16)
                        self.a[0]=end; self.d[1]=0; self.sr=(self.sr&~(self.N|self.V|self.C|self.X))|self.Z
                        self.steps+=n*4; self.op_counts['BSS_ACCEL']+=1; self.pc=0x40000866
                        continue
                self.step()
        except (Unsupported,MemFault) as e:
            stop={'reason':type(e).__name__,'message':str(e),'pc':self.pc,'opcode_pc':self.trace[-1]['pc'] if self.trace else None}
        return stop

def run_cli():
    ap=argparse.ArgumentParser()
    ap.add_argument('main',nargs='?',type=Path,default=Path('/mnt/data/ar172_workspace/AR172_SLICE16_TRANSACTIONAL_LAB_DO_NOT_FLASH.main.bin'))
    ap.add_argument('--steps',type=int,default=100000)
    ap.add_argument('--json',type=Path,default=Path('/mnt/data/ar172_workspace/emulator/minicoldfire_run.json'))
    a=ap.parse_args()
    bus=Bus();bus.load_main(a.main)
    cpu=CPU(bus); cpu.a[7]=INITIAL_SP
    # Boot-provided long at incoming SP+4.
    bus.write(INITIAL_SP+4,4,0)
    stop=cpu.run(a.steps)
    report={
        'firmware':str(a.main),'entry':hex(ENTRY),'initial_sp':hex(INITIAL_SP),
        'steps':cpu.steps,'calls':cpu.calls,'pc':hex(cpu.pc),'sp':hex(cpu.a[7]),'sr':hex(cpu.sr),
        'stop':stop,'registers':{'d':[hex(x&0xffffffff) for x in cpu.d],'a':[hex(x&0xffffffff) for x in cpu.a]},
        'control_registers':{hex(k):hex(v) for k,v in cpu.ctrl.items()},
        'opcode_families':dict(cpu.op_counts),'mmio_events':bus.mmio_events[-500:],
        'trace_tail':cpu.trace[-100:],
    }
    a.json.write_text(json.dumps(report,indent=2))
    print(json.dumps({k:report[k] for k in ('steps','calls','pc','sp','stop','opcode_families')},indent=2))
    print('report:',a.json)

if __name__=='__main__':run_cli()
