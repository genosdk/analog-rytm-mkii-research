#!/usr/bin/env python3
"""Summarize AR-MK2 MMIO logs emitted by the experimental QEMU machine."""
from __future__ import annotations
import argparse, json, re
from collections import Counter, defaultdict
from pathlib import Path

RX = re.compile(
    r"AR-MK2 MMIO (?P<kind>[RW]) pc=(?P<pc>[0-9a-fA-F]+) "
    r"addr=(?P<addr>[0-9a-fA-F]+) size=(?P<size>\d+) "
    r"(?:value=(?P<value>[0-9a-fA-F]+) )?module=(?P<module>\S+)"
)

def parse(path: Path):
    events=[]
    for line in path.read_text(errors='replace').splitlines():
        m=RX.search(line)
        if not m: continue
        d=m.groupdict()
        events.append({
            'kind': d['kind'], 'pc': int(d['pc'],16), 'addr': int(d['addr'],16),
            'size': int(d['size']), 'value': int(d['value'],16) if d['value'] else None,
            'module': d['module'],
        })
    return events

def summarize(events):
    by_addr=Counter((e['addr'], e['kind'], e['module']) for e in events)
    by_pc=Counter(e['pc'] for e in events)
    by_module=Counter(e['module'] for e in events)
    pcs=defaultdict(list)
    for e in events:
        if e['pc'] not in pcs[e['addr']]: pcs[e['addr']].append(e['pc'])
    return {
        'event_count': len(events),
        'modules': [{'module':k,'count':v} for k,v in by_module.most_common()],
        'addresses': [
            {'addr':hex(a),'kind':k,'module':m,'count':n,
             'pcs':[hex(x) for x in pcs[a][:16]]}
            for (a,k,m),n in by_addr.most_common()
        ],
        'hot_pcs': [{'pc':hex(pc),'count':n} for pc,n in by_pc.most_common(50)],
        'tail': [
            {**e, 'pc':hex(e['pc']), 'addr':hex(e['addr']),
             'value':hex(e['value']) if e['value'] is not None else None}
            for e in events[-100:]
        ],
    }

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('log',type=Path)
    ap.add_argument('--json',type=Path)
    args=ap.parse_args()
    ev=parse(args.log); r=summarize(ev)
    print(f"MMIO events: {r['event_count']}")
    print('Top modules:')
    for x in r['modules'][:20]: print(f"  {x['count']:8d}  {x['module']}")
    print('Top addresses:')
    for x in r['addresses'][:30]:
        print(f"  {x['count']:8d}  {x['kind']}  {x['addr']:>12}  {x['module']:<16} {', '.join(x['pcs'][:4])}")
    if args.json: args.json.write_text(json.dumps(r,indent=2))

if __name__=='__main__': main()
