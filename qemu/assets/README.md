# Desktop skin assets

`photon_panel_neutral.png` is the neutral-state photographic faceplate used by
`desktop_panel.py`. The live OLED, press/focus outlines, LEDs, and mouse hit
regions are rendered independently at runtime; the raster must therefore stay
free of illuminated control states.

The checked-in 1400×802 image is a deterministic crop/scale of the approved
1536×1024 source render. Geometry in `desktop_panel.py` is expressed against
the uncropped 1508×864 hardware bounds and projected onto this raster, keeping
future alignment corrections independent of window size.
