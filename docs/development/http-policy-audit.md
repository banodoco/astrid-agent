# Managed-media HTTP policy audit — 21 November

The managed-media ffprobe helper in `astrid/core/media.py:291` restricts protocols to `file,pipe` in local-only mode and refuses UNC and extended-UNC paths. This is a protocol-level control. A network drive mapped or mounted by the user can appear as a local path; these checks neither identify its backing store nor isolate it.

The node probe opens literal IPv4 loopback with redirects and environment proxies disabled. It makes one GET per attempt, at most two attempts per engine and four across one restart.

The following other Agent FFmpeg and ffprobe invocation sites are **protocol whitelist not applied or not audited**. This list records the known gap; it does not extend the managed-media assurance to these calls. Line numbers identify the source at the containment candidate and include command construction or execution sites:

- `astrid/core/media.py:392`; `astrid/core/execution/thumbnails.py:80,145`; `astrid/core/execution/reconciler.py:129`; `astrid/core/rendering/delivery_audio.py:119,132`.
- `astrid/packs/rendering/rendering/renderers/ffmpeg/command.py:402,465`; `astrid/packs/rendering/rendering/renderers/ffmpeg/run.py:277`; `astrid/packs/rendering/rendering/renderers/ffmpeg/audio_reactive_colour.py:278,313`.
- `astrid/packs/rendering/actions/timeline_visualize/thumbnails.py:227,275,399,459`; `audio_analysis.py:105,289`; `fast_lane.py:361`; `filmstrip_execution.py:370,818`; `filmstrip_cards.py:600,1213`; `render_review.py:55,100,114,172` (the last six paths are in the same `timeline_visualize` directory).
- `astrid/packs/rendering/rendering/finalizers/ffmpeg/run.py:512,636,860,1297`; `astrid/packs/rendering/rendering/finalizers/compositor/run.py:432,622`.
- `astrid/packs/vibecomfy/invocation_preflight.py:102,131,153`; `astrid/packs/h3_av/extensions/vibecomfy.workflows/hooks.py:60`; `astrid/packs/h3_av/shared/compile.py:447,455`; `astrid/packs/h3_av/shared/compose.py:521,530,576,633,835,979,1013,1017,1036,1067,1091,1097,1162,1172,1285`.
- `astrid/packs/training/actions/dataset_build/filter_stages/near_duplicate.py:120`; `astrid/packs/training/actions/dataset_build/filter_stages/black_frame.py:90`; `astrid/packs/training/actions/dataset_build/media.py:49`; `astrid/packs/generation/actions/generate_speech/run.py:225`.
- `astrid/packs/rendering/actions/sprite_sheet/upscale.py:198`; `astrid/packs/rendering/actions/sprite_sheet/web_outputs.py:36,80,107,141,185,246,283,317,444`; `astrid/core/dev/journey.py:801,914,920`; `astrid/core/dev/reference_realm.py:39,185`; `astrid/core/dev/cross_client.py:54`; `astrid/installer/tools.py:492,498,499,504,510,526,530,772`.

The run evidence `run/ffmpeg-boundary-map.md` describes the known gap and its source-audit limits. Release 14 ships the ffprobe-only containment as a known limit. The [Release 15 follow-up, astrid-dev #1247](https://github.com/banodoco/astrid-dev/issues/1247), and [Plan 59 follow-up](https://github.com/banodoco/astrid-dev/pull/1208) cover the remaining FFmpeg boundaries and mount or sandbox isolation.
