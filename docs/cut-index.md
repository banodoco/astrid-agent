# The derived cut index

`astrid.core.timeline.cut_index` owns cut derivation and selection. Readers use
`bundle_cut_index(bundle)`, `snapshot_cut_index(snapshot, fps=...)`, or
`checkout.cut_index`. Legacy `picture_cuts`, `find_cut`, and `_cut_groups` remain
compatibility entry points; production consumers cannot call them directly.
The AST guard in `tests/timeline/test_cut_index.py` enforces this across Astrid.
`tests/parity/cut-consumers.json` pins the admitted source inventory.

The index preserves actual picture windows, gaps and overlaps. Named sequence
steps share a window even when another cut opens between those steps. A named
cut whose picture is missing remains discoverable through its surviving members.
No persistent cut record is added. Display ordinals resolve through the index;
edit commands retain stable ids.

`rows` carries visual and audio intersections. Table rows expose these as
`intersections`; the sheet's `cut_intersections` and visualize cut selection use
the same projection. Cut-free music, VO and SFX intersect windows without joining
structural membership. Timeline-owned sound does not create a picture cut.
The bounded digest cache stores detached derivations and rebinds clip references
for each reader, including after checkout restoration or direct bundle edits.

`groups(checkout)` projects authored membership for structural commands.
`timing_spans(checkout)` projects the compiler's next-opening boundaries.
`spans()` projects actual named coverage for addresses and views. Timing
boundaries must not be used to draw picture coverage: doing so loses gaps,
overlaps and sequences spanning another cut.

Split copies and trims the picture, retains all existing layer identities, and
moves layers starting at or after the split to the new cut. Existing reservations
allocate the new id and retain retired ids through undo and reload.

Lift is available through `cut.lift`, `tl.cut("c2").lift(layers="stay")`,
`timelines lift TL c2 --layers stay`, and `timelines edit TL --lift c2`.
`layers=remove` removes that cut's layers. The preceding picture holds across the
lifted span; chapter starts move to the preceding cut. Cut-free sound remains
independent. Lifting the first cut is refused because there is no preceding
picture. Public picture removal directs the caller to lift or set the asset.
Narrated removal accepts `timelines edit TL --remove-line n22 --visuals remove`
or `--visuals orphan` (the default). The index identifies affected picture cuts,
including a cut opened on the removed words whose tail spans beyond the line.
The existing music policy still governs ripple sound edits.
A sheet fragment omitting a picture preserves it; an explicit sheet remove
command receives the same refusal as CLI and Python.

For a replacement or footage-generation placeholder, clear the existing picture
slot's asset and later replace it; the slot retains its cut identity. Lift merges
the cut into its predecessor. It does not promise a separate empty cut after all
members have gone. The synthetic demonstrations do not establish a need for a
new stored cut record or prove parity with unavailable historical films.
