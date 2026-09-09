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

## Next gate

Enumerate the live package-pad nets from configured IOI muxes, including dedicated
`CLKPAD` paths and fixed/branch routing beyond the local `INT_IOI` tile, then rank
the remaining three-input/one-output sets by shared receiver/serializer topology.
Board continuity testing remains the independent hardware confirmation.

This work is read-only. Nothing here is a flashable FPGA or firmware modification.
