# AR MKII framebuffer double buffering

The firmware does not use a single framebuffer pointer.

## Pointer globals

- `0x4026F474` — presented/front buffer after a swap.
- `0x4026F478` — working/back buffer after a swap.
- Each buffer is 1024 bytes (`128 x 64 x 1 bpp`).
- In the observed run the two banks were `0x41906644` and `0x41906A44`, exactly 1024 bytes apart.

## Swap routines

The full-transfer path around `0x40092A0C` finishes by swapping the two globals:

- `0x40092A6C`: load `0x4026F478`
- `0x40092A72`: load `0x4026F474`
- `0x40092A7C`: store former back pointer into `0x4026F474`
- `0x40092A82`: store former front pointer into `0x4026F478`

The differential-transfer path around `0x40092A8A` performs the same swap at
`0x40092B08..0x40092B1E` after comparing/sending changed display blocks.

## Emulator implication

A framebuffer exporter that follows `0x4026F478` after presentation will read the next
working/back buffer, which may be blank. For the visible frame, export the front pointer
from `0x4026F474`, or model the present/swap operation explicitly.

This explains why the first successful firmware render could be proven in bank A while
the pointer previously used by the desktop bridge had already moved to blank bank B.
