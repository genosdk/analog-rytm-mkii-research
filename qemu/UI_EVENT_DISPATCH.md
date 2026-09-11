# AR MKII OS 1.72 — live UI event dispatch

This note records the currently proven firmware-level input path used by the desktop-emulation work. It contains no proprietary firmware bytes.

## Live queue

After normal UI initialization the queue pointer global switches to the real UI queue at `0x4192A9D0`.

The UI task dequeues an event at `0x400A1126` by calling `0x40001444`. The dequeue primitive blocks while the queue is empty, advances the ring read index when data is available, returns one 32-bit event pointer/value, and decrements the queue count.

After dequeue the UI task performs the following logical sequence:

- returned event pointer -> `A2`
- event byte 0 -> event type
- reject types >= 40
- dispatch types 0..39 through the 40-entry switch table rooted at `0x400A1148`

## Button event format

The front-panel byte parser is installed at `0x4007F30C`.

For button-group packets, header `0x2n` selects group `n` and the following byte is the group's 8-bit button bitmap. Firmware diffs the bitmap against prior group state and creates individual press/release events.

Groups 3 and 2 map consecutively onto event IDs 0..7 and 8..15, strongly identifying the 16 trig keys.

Known native parser frames:

- Trig 1 press: `23 01`
- Trig 1 release: `23 00`

`0x4007EFC8` constructs the resulting event object and enqueues it through the queue pointer global at `0x4026D4C8`.

The event object observed for this path has:

- byte 0 = `0` (button-event type)
- +4 = control/button object pointer
- +8 = press/release state
- +12 = source/context pointer

Therefore Trig 1 reaches **UI dispatch case 0**.

## Encoder format

Parser header `0x3n` selects encoder index `n`; the payload is a signed 8-bit
movement delta. Firmware mapping shows live encoder indices 0..8. A native
read watchpoint at `0x418B7644` proved that the handler adds `abs(payload)` to
its movement accumulator: payloads 1..10 produced 1, 3, 6 ... 55, rejecting
the earlier wrapping-counter model.

## Desktop bindings

The desktop panel maps `QWERTYUI` to Trigs 1–8 and `ASDFGHJK` to Trigs
9–16. Key-down and key-up emit the same validated press/release records as the
mouse buttons. Repeated key-down events are suppressed, and losing window
focus releases every held trig so the firmware cannot retain a stuck pad.

Encoders A–H are displayed as the eight page-function knobs. Vertical mouse
drag changes one step per two pixels and the wheel changes one step per notch;
movement uses native signed `0x3n` deltas. The previous `-127`/target endpoint
guess has been removed: the stock acceleration gate can suppress or scale those
two frames, so they do not establish an absolute value.

QEMU instead exports the firmware's 42-word live track bank from `0x8000E5B0`.
The frontend selects the proven per-page A–H offsets and decodes each big-endian
Q8 word's high byte as the authoritative 0–127 value. SYN, SMP, FLTR, AMP, and
LFO are mapped. TRIG uses a different owner and remains readback-open. Native
encoder index 8 is the separate Level/Data control, not a ninth page-function
knob.

## Current target

Resolve the TRIG-page owner and separate Level/Data readback path without
regressing the now-proven five-page Q8 export.
