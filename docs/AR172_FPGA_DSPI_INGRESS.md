# AR MKII OS 1.72 — FPGA DSPI ingress inventory

## Result

The stock XC3S200A/VQ100 image has been decoded at every one of the 68 BOND57
user pins using Project Combine's exact Spartan-3A IOB configuration coordinates.

The initial direction/locality-only ranking placed the following quartet first:

| VQ100 pin | FPGA IOB | Stock mode | DSPI role status |
|---|---|---|---|
| P28 | `IOB_S3_0` | input (`CMOS_VCCO`) | PCS0/SCK/SOUT, unassigned |
| P29 | `IOB_S3_1` | bidirectional (`CMOS_VCCO`, OE=`01`) | strongest SIN candidate |
| P30 | `IOB_S5_0` | input (`CMOS_VCCO`) | PCS0/SCK/SOUT, unassigned |
| P31 | `IOB_S5_1` | input (`CMOS_VCCO`) | PCS0/SCK/SOUT, unassigned |

That quartet is now rejected. Configuration-level decoding shows no selected
`OUT_FAN`/`OUT_SEC` first-hop consumer for P28, P29, P30, or P31 in their stock
`INT_IOI_S3A_SN` tiles. P29's actual `IOI[1].MUX_O` is `NONE`, so it is not the
live SIN return through the normal user-I/O path. The earlier `OUTPUT_ENABLE=01`
observation described an IOB configuration field; treating every nonzero value as
proof of a live fabric output was too strong.

The other locality-ranked quartets are likewise only heuristics. They are retained
in the original inventory for reproducibility, but no candidate should now be
treated as a signal assignment until its configured route is decoded.

## Extracted inventory

Stock direction counts:

- 15 input pins
- 2 bidirectional pins
- 8 output pins
- 37 unused general IOBs
- 6 unused input-only positions

The only south-edge output-enabled pins are P29 / `IOB_S3_1` and P44 /
`IOB_S13_1`. Both retain enabled input buffers, so both are bidirectional in the
stock configuration. The eight output-only pins are on the north edge.

Artifacts:

- `research/fpga_iob_mode_inventory.py` — reproducible read-only extractor
- `research/AR172_FPGA_IOB_MODE_INVENTORY.json` — full bit evidence and rankings
- `research/AR172_FPGA_IOB_MODE_INVENTORY.csv` — compact 68-pin inventory
- `research/fpga_dspi_net_trace.py` — reproducible IOI/INT first-hop decoder
- `research/AR172_FPGA_DSPI_FIRST_HOP_TRACE.json` — negative gate rejecting P28-P31

## Geometry and validation

Project Combine maps the relevant IOB classes as follows:

- W/E: `IOB_S3A_W4/E4`, four `Vertical (rev 2, rev 64)` rectangles in the
  west/east termination frames (`term_w_frame=4`, `term_e_frame=348`).
- S/N: `IOB_S3A_S2/N2`, two `Vertical (rev 19, rev 6)` rectangles in the owning
  column frames at physical bit bases 0 and 2192.
- BRAM and BRAM-continuation columns use Project Combine's post-allocation
  `col_frame` fixups; the extractor carries those exact physical frame bases.

The stock `IBUF_MODE` population is 51 `NONE`, 12 `CMOS_VCCO`, and 5 `DIFF`.
Output-enable values are 52 `00`, 2 `01`, 8 `11`, plus six physical input-only
positions with no output-enable bits. The IBUF enum exhausts all three-bit patterns,
so enum legality alone is not a validation discriminator. The useful mechanical
cross-check is the highly coherent topology produced by the extraction: W/E are
entirely unused, enabled inputs concentrate on the south edge, the only two
south-edge return-capable pins decode as bidirectional, and eight pure outputs
concentrate on the north edge.

The coordinates are pinned to Project Combine commit
`234343d23e737e57f2727630e19008b509d7d522`.

## Renderer payload classification

The recommended boundary sweep is complete. Stock MAIN's table at `0x40277FE8`
contains 53 renderer entry points. For each entry, the probe installs that pointer
for physical voice 0 immediately before the stock dispatch at `0x4011CA50`, then
runs the same note-60, synth-live, one-callback fixture.

Across the 492 asserted-PCS0 payload positions (queue word indices `1..492`):

| Classification | Positions |
|---|---:|
| Renderer-sensitive in the common-state sweep | 119 |
| Invariant in the common-state sweep | 373 |

All 53 renderers execute successfully and produce 41 distinct packet hashes
grouped into 32 distinct position-difference signatures. Completing the sweep
also exposed and fixed an emulator decode-order bug: ColdFire `EXT.B` (`0x49C0`)
was previously captured by the broader `LEA` mask.

The strongest structured clusters are `229..244`, `255..276`, and `309..332`.
They contain repeated tagged groups such as `0x80014000`, `0x80017F80`,
`0x8001B018`, and `0x80018000`; `309..320` also alternates tagged control words
and renderer-dependent values. This is consistent with a framed multi-register
analog-control protocol, but it is not proof of the selected off-chip device or
of the values' physical units. The already calibrated pitch pair at words 46/47
is renderer-sensitive as expected.

The classification is deliberately bounded: swapping a function pointer does not
install each renderer's authentic machine descriptor or preset state. A word that
is invariant here may still vary with a parameter or a later runtime phase.

The first authentic-selector correlation is also complete. The stock routing
table at `0x40278A44` is `[0,4,1,5,8,6,10,2]`; track 6 therefore reaches physical
voice 5. Seeding machine selector `0x8000EA06` with ID 10 makes stock dispatch
select renderer `0x40110B18` without replacing a function pointer. Across all 128
MIDI notes, pitch is encoded as six interleaved tag/value pairs:

| Channel | Tag word | Value word |
|---:|---:|---:|
| 0 | 309 | 310 |
| 1 | 311 | 312 |
| 2 | 313 | 314 |
| 3 | 315 | 316 |
| 4 | 317 | 318 |
| 5 | 319 | 320 |

The tag low byte begins at `0x40` and increments when the 16-bit value rolls
over. Reconstructing each channel as
`((tag_low8 - 0x40) << 16) | value_low16` produces six monotonic 128-note curves.
Five channels obey the exact octave residual set `{0,1}` from notes 30..127;
channel 1 uses `{0,1,2}`, with the lone `+2` rounding case beginning at note 49.
This establishes a six-channel pitch-derived control group. It still does not
identify the receiving chip, electrical units, or physical channel pins.

Artifacts:

- `research/renderer_payload_sweep.py` — reproducible common-state sweep
- `research/AR172_DSPI1_RENDERER_PAYLOAD_MAP.json` — all 492 classifications,
  renderer results, hashes, values, and signature families
- `research/authentic_renderer_pitch_probe.py` — stock selector and 128-note sweep
- `research/AR172_DSPI1_AUTHENTIC_RENDERER_PITCH.json` — six-channel pitch laws

## Configuration-bus boundary

The configuration-pin reuse gate changes the search boundary. The established
board routes are DSPI1 SCK -> FPGA P53/CCLK and DSPI1 SOUT -> FPGA P51/D0
(DIN in slave-serial mode). Direct stock-image extraction shows both P53 and P51
parked after configuration: `IBUF_MODE=NONE`, input disabled, output disabled.
The BR/control stream's use of DSPI1 SCK/SOUT therefore does not establish that
the live FPGA fabric consumes it. `PCS0` can select a separate shared-bus device
or board glue while the FPGA configuration pads ignore post-configuration traffic.

The CPU8251D component-side photographs corroborate the physical topology: U1 is
the ColdFire CPU, U11 is the XC3S200A, memory/storage packages sit near U1, and no
obvious dedicated Xilinx configuration PROM is adjacent to U11. They do not prove
continuity because relevant traces disappear into vias and inner layers, and the
CPU-board solder side is not shown.

Next, vary one renderer-10 sound parameter at a time against the dense
`229..240` cluster, using the now-proven track-6 selector path. Correlate those
changes with the non-FPGA serial/control
devices visible on CPU8251D. Continue generic live-pad routing only where it
answers a specific signal question; do not use direction/locality alone to
relabel DSPI1 as a live FPGA application bus. Board continuity testing remains
the independent hardware confirmation.

The reproducible join is `research/fpga_config_bus_reuse_probe.py`, with committed
output `research/AR172_FPGA_CONFIG_BUS_REUSE.json`.

This work is read-only. Nothing here is a flashable FPGA or firmware modification.
