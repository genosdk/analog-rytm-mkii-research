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

Parser header `0x3n` selects encoder index `n`; the payload is a signed delta. Firmware mapping shows live encoder indices 0..8.

## Desktop bindings

The desktop panel maps `QWERTYUI` to Trigs 1–8 and `ASDFGHJK` to Trigs
9–16. Key-down and key-up emit the same validated press/release records as the
mouse buttons. Repeated key-down events are suppressed, and losing window
focus releases every held trig so the firmware cannot retain a stuck pad.

Encoders A–I are displayed as virtual knobs. Vertical mouse drag changes one
step per two pixels and the wheel changes one step per notch. Each knob keeps a
host-side value clamped to 0–127 and emits only the corresponding signed delta
through the native `0x3n` encoder packet.

The desktop shell is fixed to the hardware's 385:225 panel proportion. Its OLED
bezel and control regions do not resize when firmware pages change. The six
proven page keys retain one active red LED after release; trigger keys show the
combined mouse/QWERTY held state, including focus-loss cleanup. Encoders also
accept arrow keys (one step), Page Up/Down (eight steps), Home/End (0/127) and
double-click reset (64). OLED activity and transient control feedback use
separate status fields so the 30 Hz framebuffer poll cannot erase an input
message immediately.

## Current GUI target

The standalone bridge, native panel round trip and first cohesive desktop shell
are complete. Continue by adding context labels from proven firmware state and
folding the separate Filter 2 laboratory controls into the same shell without
inventing unverified hardware controls.
