# Desktop skin assets

`photon_panel_neutral.png` is the neutral-state photographic faceplate used by
`desktop_panel.py`. The live OLED, active crops, encoder markers, focus ring,
and mouse hit regions are rendered independently at runtime; the raster must
therefore stay free of illuminated control states.

`photon_panel_active.png` is its coordinate-identical activation sheet. The
frontend copies only the crop belonging to a held/selected key, Trig, or pad,
so multiple controls can illuminate independently without replacing the live
OLED or the neutral chassis.

The checked-in 1400×802 image is a deterministic crop/scale of the approved
1536×1024 source render. Geometry in `desktop_panel.py` is expressed against
the uncropped 1508×864 hardware bounds and projected onto this raster, keeping
future alignment corrections independent of window size.
