# Render

**Executor**: `rendering.render`  
**Status**: implemented  
**Pipeline step**: 12 (terminal)

Renders a hype timeline into opaque `hype.mp4` (or an explicitly
alpha-stamped layer into transparent ProRes 4444 `.mov`) through the backend-neutral
`RenderService`. This executor is the stable facade: it adapts CLI inputs into
a protocol-v1 request while the service resolves a qualified renderer or
planner, validates support and artifacts, performs explicit finalization when
required, and publishes the final video plus provenance. Renderer and planner
selection uses qualified capability ids and fails closed for shorthand.

A timeline containing one built-in `audio-reactive-colour` effect and one
coextensive audio clip can be compiled by the FFmpeg renderer to its dedicated
`sendcmd` specialization. The compact effect remains the editable timeline
representation; service selection and request-sensitive support evidence
choose the implementation.

Normal Astrid usage goes through the admitted SDK capability
(`astrid.sdk.invoke("rendering.render", kind="executor", project=...)`). The
runtime generic host owns the pack child process; callers never invoke the
pack entrypoint or bypass admission.

`rendering.render` has two explicit, mutually exclusive modes. Supply exactly
one of `timeline` or `timeline_ref`. File mode supplies a project-owned JSON
file through normal file-input admission: import it with
`client.media.import_file(...)`, then pass the returned object descriptor; the
host materializes its verified bytes for the renderer. In a parent action, use the existing
producer-file registration and admitted object custody path rather than
treating a path inside JSON as permission to read a file. File mode has no
canonical timeline authority and does not stamp a canonical timeline identity
in provenance. Values such as `timeline="main"` remain file paths and never
gain implicit canonical meaning. `timeline_ref` names a managed kernel slug,
UUID, or ULID and may be paired with `expected_version`. The managed mode
resolves and pins the stream head before admission, materializes immutable
private renderer inputs, and stamps the canonical ID, version, tail hash, and
content hashes in provenance. The product CLI exposes that mode as
`astrid timelines render <ref> --project <project>`.

## Quick-start

Render a timeline with no external media registry:

```python
from pathlib import Path
import astrid.sdk as sdk
from astrid.sdk import AstridClient

with AstridClient.open_from_launcher() as client:
    imported = client.media.import_file(
        project="demo",
        path=Path("./out/hype.timeline.json"),
        idempotency_key="hype-timeline-v1",
    )
    if not imported.ok:
        raise RuntimeError(imported.error)
    timeline = {
        "digest": imported.data["digest"],
        "object_id": imported.data["object_id"],
        "filename": "hype.timeline.json",
    }
    result = sdk.invoke(
        "rendering.render",
        kind="executor",
        project="demo",
        inputs={"timeline": timeline},
        client=client,
    )
```

File inputs must be imported into Runtime-managed storage and supplied as the
returned object descriptor. To render changed bytes, import the changed file
with a new idempotency key for that revision and pass its new descriptor. A
local path inside the action input is not a worker-readable file reference.

Render a timeline with the optional media asset registry produced by
`video_editing.cut`:

```python
from pathlib import Path
import astrid.sdk as sdk
from astrid.sdk import AstridClient

with AstridClient.open_from_launcher() as client:
    timeline_import = client.media.import_file(
        project="demo", path=Path("./out/hype.timeline.json"),
        idempotency_key="hype-timeline-v1",
    )
    registry_import = client.media.import_file(
        project="demo", path=Path("./out/hype.assets.json"),
        idempotency_key="hype-assets-v1",
    )
    if not timeline_import.ok or not registry_import.ok:
        raise RuntimeError("Could not import the timeline or asset registry")
    timeline = {
        "digest": timeline_import.data["digest"],
        "object_id": timeline_import.data["object_id"],
        "filename": "hype.timeline.json",
    }
    assets_registry = {
        "digest": registry_import.data["digest"],
        "object_id": registry_import.data["object_id"],
        "filename": "hype.assets.json",
    }
    result = sdk.invoke(
        "rendering.render",
        kind="executor",
        project="demo",
        inputs={"timeline": timeline, "assets_registry": assets_registry},
        client=client,
    )
```

With a custom theme and strict qualified renderer:

```python
from pathlib import Path
import astrid.sdk as sdk
from astrid.sdk import AstridClient

with AstridClient.open_from_launcher() as client:
    timeline_import = client.media.import_file(
        project="demo", path=Path("./out/hype.timeline.json"),
        idempotency_key="hype-timeline-v1",
    )
    theme_import = client.media.import_file(
        project="demo", path=Path("./out/theme.json"), idempotency_key="render-theme-v1"
    )
    if not timeline_import.ok or not theme_import.ok:
        raise RuntimeError("Could not import the timeline or theme")
    timeline = {
        "digest": timeline_import.data["digest"],
        "object_id": timeline_import.data["object_id"],
        "filename": "hype.timeline.json",
    }
    theme = {
        "digest": theme_import.data["digest"],
        "object_id": theme_import.data["object_id"],
        "filename": "theme.json",
    }
    result = sdk.invoke(
        "rendering.render",
        kind="executor",
        project="demo",
        inputs={
            "timeline": timeline,
            "theme": theme,
            "selector": "rendering.remotion",
        },
        client=client,
    )
```

The SDK invocation writes `./out/hype.mp4` and
`./out/hype.mp4.provenance.json`.

Render the current canonical kernel timeline, failing before admission if the
caller has observed a different version:

```python
result = sdk.invoke(
    "rendering.render",
    kind="executor",
    project="demo",
    inputs={"timeline_ref": "main", "expected_version": 4},
)
```

The equivalent product command is
`astrid timelines render main --project demo --expected-version 4`.

### Render-only tail clock

Managed timelines may declare an explicit render-only tail under
`app.astrid_render_clock`. The authored clip clock remains authoritative for
clip placement and identity; the declared render clock may extend the decoded
output only when it names a runtime-admitted registry asset and declares the
tail as `unmapped_excess_rendered_region`. For example, an authored 297-second
timeline at 30 FPS can declare 8910 authored frames and 9000 rendered frames,
with a source interval `[8910, 9000)` from the immutable tail asset. Managed
validation rejects missing, malformed, shorter, or unbound clocks before
render admission. Remotion renders the tail explicitly, while storage
estimation, filmstrip snapshots, and visualization extents use the rendered
clock and retain authored clip intervals for provenance.

## Inputs

| Name            | Type   | Required | Description |
|-----------------|--------|----------|-------------|
| timeline        | file   | conditional | Project-owned Hype timeline JSON admitted through the normal file-input boundary; mutually exclusive with `timeline_ref`. |
| timeline_ref    | string | conditional | Canonical kernel slug, UUID, or ULID; mutually exclusive with `timeline`. |
| expected_version | integer | no | Positive stream-head CAS pin; valid only with `timeline_ref`. |
| assets_registry | file   | no       | Optional Hype media asset registry JSON. Pass as the `assets_registry` input when the timeline references media assets. If omitted, the runner supplies an empty registry. |
| theme           | file   | no       | Optional theme configuration. |
| selector        | string | no       | Qualified renderer id: `rendering.remotion`, `rendering.ffmpeg`, or `rendering.threejs`. Omit to select `rendering.remotion`. |
| backend_config  | JSON   | no       | Object keyed by qualified implementation id. The service forwards only the selected implementation's namespace. |
| output_name     | string | no       | Plain basename; defaults to `hype.mp4`. `.mov` is admitted only when the timeline has the exact `metadata.astrid_layer.alpha: true` stamp, and an explicit profile must declare MOV/ProRes/`yuva444p12le` plus PCM S16LE/48 kHz/stereo. The video and sidecar outputs use this value. |
| review | boolean | no | Render-only registered shot names/time at top right plus bottom captions from pinned authored `voiceover_script` bindings. Remotion's default review path uses backend `--scale` to fit the authored canvas inside 640x360 with preserved aspect ratio; the timeline props and authored positions remain canonical, and the actual output profile is probed. These exports show the exact `Low Res Render` marker at top left. Explicit profiles and clean renders retain existing resolution behavior. Caption timing uses the shot interval and is not word-aligned unless explicit admitted speech timing is present. Supported by Remotion and Three.js; FFmpeg rejects it. Gaps display `No shot`, overlaps display all active names. Saved timelines are unchanged. |
| keep_previous_renders | boolean | no | Preserve prior provenance-linked sibling render outputs. |

Qualified renderer selection fails closed when that implementation reports the
request unsupported. There is no implicit planner or fallback route; select a
different qualified renderer explicitly.

## Outputs

| Name       | Type | Path                         | Description |
|------------|------|------------------------------|-------------|
| video      | file | `{out}/{output_name}` | Rendered video; default `{out}/hype.mp4`. |
| provenance | file | `{out}/{output_name}.provenance.json` | Sidecar describing render inputs, plan, resolution, and artifacts. |

## Remotion asset materialization

The shared render host accepts only invocation-owned, runtime-materialized
files. Every referenced media or theme file must already be present in the
attempt's materialization directory and must match its declared content hash
before rendering starts. Renderers do not resolve checkout-relative paths,
project-local locators, sibling roots, remote URLs, or caches.

The Remotion backend serves only that staging directory on `127.0.0.1` through
an `InvocationAssetServer` with HTTP Range support. The ephemeral server sends
the local transport CORS headers needed when the Remotion page is hosted on
`localhost` while the asset server binds `127.0.0.1`. The materializer, server,
and stage are cleaned after success or failure.

If `assets_registry` is omitted, the facade creates a temporary empty registry.
This is valid for timelines that do not reference registry media.
In `timeline_ref` mode the registry cannot be overridden: Astrid uses the
canonical stored registry of runtime-managed object IDs and content hashes.
The host supplies verified bytes into an attempt-owned staging root. Private snapshots live under
`<project>/.astrid/render-snapshots/<authority-hash>/`; unchanged heads reuse
one directory. They are immutable derived inputs, not timeline authority, and
can be regenerated from the kernel. Retention tooling must retain directories
referenced by retryable tasks before removing unreferenced versions.

## Theme support

The host may pin one schema-validated, runtime-materialized `theme.json`
document. The selected backend merges its visual data with per-run
`theme_overrides` and passes `{id, visual}` to Remotion as props. When no
document is pinned, the intentional in-process `banodoco-default` style is
used; no workspace theme directory or environment-selected theme is read.

## Local effect assets

Element manifests may declare static files needed by an effect, animation, or
transition with optional top-level asset syntax:

```yaml
assets:
  badge: assets/badge.png
  palette: assets/palette.json
```

Asset paths are relative to the element root, must stay inside that root, and
must point to existing files. During render, only declared assets for elements
actually used by the timeline are copied into:

```bash
remotion/public/astrid-effects/<render-hash>/<effect-id>/
```

The renderer injects Remotion-static-file-relative paths into the clip params
under the reserved key `params.__astridAssets`, for example:

```json
{
  "__astridAssets": {
    "badge": "astrid-effects/<render-hash>/my-effect/badge.png"
  }
}
```

The staging directory and temporary props file are cleaned up after Remotion
exits.

## Provenance sidecar

Canonical shot renders pin each referenced shot’s name, version, and canonical text-binding rows under `timeline_authority.expansion.shots`. A binding’s `head`, immutable `media_id`, and `content_hash` identify the exact narration or other attached text at admission. Rebinding text changes render identity even when the visual timeline stays unchanged. This records source provenance; it does not generate speech or draw captions. The bound text remains inspectable through the canonical shot text-binding and media APIs.

Shot occurrence placements are pinned for every managed render, regardless of
the optional `review` flag. `review` controls only the burned-in visual labels;
the immutable occurrence envelope is also consumed by filmstrip and timeline
visualizer tooling to map each admitted shot binding to its rendered interval.

Every successful facade render writes `<output>.provenance.json`. Core owns its
routing and identity fields: request digest, requested policy, planner, ordered
segments and renderer resolution, finalizer, manifest/input/artifact hashes,
trust and support evidence, artifact profiles, audio ownership, normalization,
attachments, and publication output. Backend-private data is preserved only
under `backend_fragments[qualified-id]`. The sidecar also retains the existing
v1 projections for active packs/theme, element resolution, staging, and
specialized render metadata when applicable.

## Authoring a renderer behind this facade

The facade never changes when a new backend appears: a pack contributes a
qualified renderer/planner/finalizer through
`extensions.rendering.{renderers,planners,finalizers}` and `RenderService`
discovers and invokes it. To author one, scaffold the canonical four-file pack
with the internal rendering CLI (`python3 -m astrid.core.rendering.cli create <name> <dest>`), implement
`render.py`, run the generated `test_renderer.py`, then `validate` →
trusted `install` via the internal pack CLI (`python3 -m astrid.core.pack.cli`)
→ `replay <bundle-dir>`
for captured failure bundles (the golden
path in `docs/contracts/render-backend-v1.md`). `render.py` may parse the raw
v1 file protocol or use the public rendering SDK (`astrid.renderer_main` as
the manifest command, `astrid.RenderContext` inside the implementation — see
`docs/reference/sdk.md`). A failed invocation retains a self-contained replay
bundle — resolved request, localized inputs, configuration, redacted logs,
partial result, and the exact replay command — so backend authors can
reproduce failures without rerunning the editorial pipeline.

## Runtime boundary

The pack command is an internal child-process target of the generic host. It
has no supported direct-debug invocation: all real renders go through the
admitted SDK capability, which supplies the canonical timeline reference and
keeps materialization, execution, publication, and settlement in one runtime
boundary.

## Pipeline position

Step 12 — the terminal step of the editorial pipeline. Runs after
`video_editing.cut` and produces the final rendered video. This is the
last step before optional YouTube upload.

## Depends on

- `editorial.transcribe`
- `editorial.scenes`
- `editorial.quality_zones`
- `editorial.shots`
- `editorial.triage`
- `understanding.scene_describe`
- `editorial.quote_scout`
- `training.pool_build`
- `training.pool_merge`
- `editorial.arrange`
- `video_editing.cut`
- `editorial.refine`

## Dependencies

- **Remotion** — requires server-owned `ASTRID_REMOTION_PROJECT_DIR` and
  `ASTRID_NODE_EXECUTABLE`; the locked project-local CLI is invoked directly
- **Node.js / npm** — `npm install` must have been run in the Remotion project
- **ffmpeg/ffprobe** — required by Remotion's render pipeline
