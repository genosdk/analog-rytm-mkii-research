#!/usr/bin/env python3
"""Read-only Railway dashboard for the AR MKII OS 1.72 research repository."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os


STATUS = {
    "project": "Analog Rytm MKII OS 1.72 Research",
    "phase": "offline reverse engineering / hardware-unverified",
    "active_target": "close hardware timing and nonzero final-mix coverage for the post-BR Filter 2 hook",
    "completed": [
        "byte-identical SysEx decode/re-encode",
        "ELE3 parse and MAIN extraction",
        "corrected executable cave at 0x402B4200",
        "Slice16 transactional machine-code emulation",
        "SRR static validation",
        "PIT0 and ColdFire EMAC emulator support",
        "BR descriptor, bridge, control-frame, and consumer traces",
        "LFO2 and Filter 2 deterministic reference models",
        "all three audio-interface READY polls under emulation",
        "TCD30 CSR bit 0x10 poll transition",
        "BR terminal read plus 16-pass stock loop execution",
        "signed Q31 quantizer equation: 256 / 256 runtime matches",
        "8 x 32-word post-BR voice slab and renderer address permutation",
        "TCD30 input direction and TCD42 256-byte outbound DMA handoff",
        "in-memory Filter 2 bypass is exact through a nonzero renderer frame",
        "disabled bypass overhead is one semantic instruction per 32-frame block",
    ],
    "next": [
        "measure the full callback and exact board clock on hardware",
        "drive nonzero mixer coefficients through the outbound DMA trace",
        "run staged hardware acceptance protocol when the unit arrives",
    ],
}


PAGE = """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>AR MKII Research</title><style>
:root{color-scheme:dark;--bg:#090b0d;--panel:#11161b;--line:#26313b;--ink:#edf3f7;--muted:#8fa0ad;--orange:#ff7a18;--cyan:#52d3ff}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 85% 0,#1b2933 0,transparent 34%),var(--bg);color:var(--ink);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}main{width:min(1100px,calc(100% - 40px));margin:auto;padding:56px 0}.eyebrow{color:var(--orange);letter-spacing:.17em;font-size:12px}h1{font-size:clamp(38px,7vw,86px);letter-spacing:-.06em;line-height:.92;margin:28px 0 16px}.sub{color:var(--muted);max-width:760px;line-height:1.6}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:46px}.card{background:linear-gradient(145deg,#151b21,#0e1216);border:1px solid var(--line);padding:24px;border-radius:8px}h2{font-size:13px;color:var(--cyan);letter-spacing:.1em;margin:0 0 22px}ul{padding-left:18px;margin:0}li{color:var(--muted);margin:11px 0;line-height:1.45}.target{grid-column:1/-1;border-color:#6b3b18}.target code{color:var(--orange);font-size:clamp(16px,2vw,22px)}footer{color:#586976;margin-top:30px;font-size:11px}@media(max-width:700px){.grid{grid-template-columns:1fr}.target{grid-column:auto}}
</style></head><body><main><div class='eyebrow'>PRIVATE RESEARCH STATUS // OS 1.72</div><h1>ANALOG RYTM<br>MKII LAB</h1><p class='sub'>ColdFire firmware research, host emulation, and staged custom-feature validation. No proprietary firmware binaries are served or stored here.</p><section class='grid'><article class='card target'><h2>ACTIVE TARGET</h2><code>EXACT BYPASS PROVEN → FULL CALLBACK TIMING → NONZERO FINAL MIX</code></article><article class='card'><h2>PROVEN OFFLINE</h2><ul>""" + "".join(f"<li>{x}</li>" for x in STATUS["completed"]) + """</ul></article><article class='card'><h2>NEXT GATES</h2><ul>""" + "".join(f"<li>{x}</li>" for x in STATUS["next"]) + """</ul></article></section><footer>STATIC ANALYSIS + EMULATION ONLY · HARDWARE UNVERIFIED · DO NOT FLASH OUT OF ORDER</footer></main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        if self.path == "/api/status":
            body = json.dumps(STATUS).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE.encode())
            return
        self.send_error(404)

    def log_message(self, format, *args):
        print(format % args)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()
