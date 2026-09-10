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

Parser header `0x3n` selects encoder index `n`; the payload is the encoder's
wrapping 8-bit hardware counter. Firmware mapping shows live encoder indices
0..8. The parser retains the previous counter and derives direction, wrap and
acceleration. The desktop bridge therefore expands a requested delta into unit
counter transitions instead of putting the signed delta directly on the wire.

## Desktop bindings

The desktop panel maps `QWERTYUI` to Trigs 1–8 and `ASDFGHJK` to Trigs
9–16. Key-down and key-up emit the same validated press/release records as the
mouse buttons. Repeated key-down events are suppressed, and losing window
focus releases every held trig so the firmware cannot retain a stuck pad.

Encoders A–I are displayed as virtual knobs. Vertical mouse drag changes one
step per two pixels and the wheel changes one step per notch. Each knob keeps a
host-side value clamped to 0–127. On first grab, it sends a saturating `-127`
sweep followed by its displayed value, establishing an actual absolute firmware
value through native unit changes of the `0x3n` counter. Later movement is
expanded into the same unit counter transitions. Page changes invalidate that
synchronization because A–I then address different functions.

## Current target

Trace UI dispatch case 0 through its state mutation and redraw/presentation calls. The goal is to prove one native panel frame causes a visible change in the presented framebuffer at pointer global `0x4026F474`.

The emulator is not considered standalone-interactive until that round trip is reproducible without manual debugger intervention.
