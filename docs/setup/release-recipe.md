# Current paired-release and provisioning recipe

> **Developer and release-owner reference:** this document records artifact
> qualification and operator procedures. It is not a supported user install
> path. Users should follow [Getting Started](../getting-started.md) and use
> Astrid at astrid.haus.

Status: **maintained recipe; final release inputs pending producer handoff**.

This is the one setup recipe for the current Astrid publication tuple. It is
an artifact manifest plus operator instructions, not a new installer or
registry. It belongs to I11 and supplies the existing I03/I08/I09/I11 rows:

- `I11-RELEASE-RECIPE`
- `I11-PREDECESSOR-TRANSITION`

The final source union is still active. Until the producer hands off the
validated tuple, every `PENDING_PRODUCER_HANDOFF` value below is a hard stop,
not a value to infer from a checkout, a candidate wheel or a registry lookup.

The five findings from the adopted
`install-launch-coherence-astra-20261009/report.md` are folded into existing
acceptance rows, not added as a gate: (1) paired artifact/bootstrap closure is
I08/I11, (2) one durable root and discovery path is I03/I08/I11, (3) installed
origin and canonical-client parity is I08/I09, (4) app restart identity is
I09, and (5) predecessor transition/update/rollback is I09/I11. The recipe
does not create a new review or completion counter.

## Release manifest

The producer handoff must replace only the pending values with the exact
artifact filenames, versions, source SHA/ref and SHA-256 digests. The shape is
deliberately explicit so a receipt can be compared without repeating a full
source audit:

```yaml
schema: astrid.paired-release-recipe/v1
status: pending_producer_handoff
publication_destinations:
  astrid: https://github.com/banodoco/astrid-agent.git
  runtime: https://github.com/banodoco/astrid-runtime.git
  app: https://github.com/banodoco/astrid-app.git
source_tuple:
  astrid_final_sha: PENDING_PRODUCER_HANDOFF
  runtime_final_sha: PENDING_PRODUCER_HANDOFF
  app_final_sha: PENDING_PRODUCER_HANDOFF
  astrid_version: PENDING_PRODUCER_HANDOFF
  runtime_version: PENDING_PRODUCER_HANDOFF
  schema_digest: PENDING_PRODUCER_HANDOFF
wheelhouse:
  location: PENDING_PRODUCER_HANDOFF
  manifest: PENDING_PRODUCER_HANDOFF
  python_requirements_lock: PENDING_PRODUCER_HANDOFF
  astrid_wheel: PENDING_PRODUCER_HANDOFF
  astrid_wheel_sha256: PENDING_PRODUCER_HANDOFF
  runtime_wheel: PENDING_PRODUCER_HANDOFF
  runtime_wheel_sha256: PENDING_PRODUCER_HANDOFF
canonical_client:
  delivery_owner: astrid-wheel
  import: astrid_runtime_client
  runtime_role: neutral-workspace-service-and-protocol
  final_digest: PENDING_PRODUCER_HANDOFF
resources:
  remotion_bundle: PENDING_PRODUCER_HANDOFF
  timeline_schema_artifact: PENDING_PRODUCER_HANDOFF
  browser_executable: PENDING_PRODUCER_HANDOFF
  fonts_and_provenance: PENDING_PRODUCER_HANDOFF
  ffmpeg: PENDING_PRODUCER_HANDOFF
  ffprobe: PENDING_PRODUCER_HANDOFF
  omp_artifact: PENDING_PRODUCER_HANDOFF
  agent_launcher: PENDING_PRODUCER_HANDOFF
  native_dependencies: PENDING_PRODUCER_HANDOFF
app_public_inputs:
  build: PENDING_PRODUCER_HANDOFF
  catalogs: PENDING_PRODUCER_HANDOFF
  fonts_and_public_assets: PENDING_PRODUCER_HANDOFF
  public_configuration: PENDING_PRODUCER_HANDOFF
optional_provider_diagnostics:
  runpod: PENDING_PRODUCER_HANDOFF
  other: PENDING_PRODUCER_HANDOFF
validation_auxiliary:
  black_frame_checker: contributor_only_not_installed_journey
support_root:
  owner: stable-config-worker
  fresh_home_default: "$HOME/Library/Application Support/Astrid Runtime"
  selected_absolute_root: PENDING_STABLE_CONFIG_HANDOFF
  discovery_path: "$ASTRID_SUPPORT_ROOT/runtime/discovery.json"
  realm_binding: existing-runtime-attach-exact-realm-id-and-realm-root
  relocation: forbidden
  automatic_new_workspace: forbidden
app_identity:
  discovery_path: "$ASTRID_WORKSPACE_DISCOVERY"
  realm_actor_binding: exact-and-preserved
  instance_check: keep-enabled
  astrid_launcher_correction_owner: NativeSol
  installed_host_origins: PENDING_NATIVE_SOL_HEAVY_END
predecessor:
  kind: recorded-i11-current-noneditable-candidate
  astrid_wheel_sha256: 47d56e391f9b41d82a714e0ef7cbd9ab76caa9c0137088e5530bc3fe55a497eb
  runtime_wheel_sha256: 4c3363b3d9f7694a8c63567b659f3299d62f43efb290a56207a0dcf620caced7
```

The Astrid wheel is the single delivery owner for the canonical generated
Python client. The Runtime wheel owns the neutral service/protocol. I08/I09
must verify one installed `astrid_runtime_client` origin and matching
contract digest; install order must not select a different copy. Generated
client bytes remain owned by the Runtime schema/generator source contract and
are consumed only after that producer handoff.

No public wheel, registry publication, domain ownership or package rename is
implied by this manifest. The current source and publication destinations are
the URLs above. The historical Astrid URL, superseded Runtime revision and
editable checkout recipe are not current setup inputs.

## Obtain and install one artifact bundle

The release owner supplies one obtainable directory or archive containing the
manifest, both noneditable wheels, `python-requirements.lock`, and the named
non-Python artifacts. An operator uses the existing `venv` and `pip` tools:

```bash
python3 -m venv /absolute/path/to/astrid-env
source /absolute/path/to/astrid-env/bin/activate

export ASTRID_WHEELHOUSE=/absolute/path/to/current-wheelhouse
export ASTRID_WHEEL_FILENAME=PENDING_PRODUCER_HANDOFF
export RUNTIME_WHEEL_FILENAME=PENDING_PRODUCER_HANDOFF
test -r "$ASTRID_WHEELHOUSE/$ASTRID_WHEEL_FILENAME"
test -r "$ASTRID_WHEELHOUSE/$RUNTIME_WHEEL_FILENAME"
test -r "$ASTRID_WHEELHOUSE/python-requirements.lock"

python3 -m pip install --no-index --no-deps \
  "$ASTRID_WHEELHOUSE/$ASTRID_WHEEL_FILENAME" \
  "$ASTRID_WHEELHOUSE/$RUNTIME_WHEEL_FILENAME"
python3 -m pip install --no-index --require-hashes \
  -r "$ASTRID_WHEELHOUSE/python-requirements.lock"
```

The final handoff must bind each filename to the manifest's version, SHA-256
and source tuple. Do not install from an editable checkout, add a checkout to
`PYTHONPATH`, or let pip reach a registry. The existing
`scripts/reshape/installed_artifact.py` and packaging receipts are the
installed-artifact boundary; no new build or installer code belongs here.

## Installed identity and artifact verification

At the end of every install, update, and rollback attempt, record the exact
selected wheel filenames and SHA-256 values from the manifest, then verify the
installed distribution metadata and import origins from the same environment:

```bash
python3 -m pip show astrid astrid-runtime
python3 -c 'import importlib.metadata as m, importlib.util; print("astrid", m.version("astrid"), importlib.util.find_spec("astrid").origin); print("astrid-runtime", m.version("astrid-runtime"), importlib.util.find_spec("astrid_runtime").origin); print("astrid_runtime_client", importlib.util.find_spec("astrid_runtime_client").origin)'
shasum -a 256 "$ASTRID_WHEELHOUSE/$ASTRID_WHEEL_FILENAME"
shasum -a 256 "$ASTRID_WHEELHOUSE/$RUNTIME_WHEEL_FILENAME"
```

The receipt must show the selected interpreter, installed versions, import
origins under that environment's site-packages, wheel hashes, and a `sys.path`
with no Astrid or Runtime checkout. The existing installed-artifact harness
and its `import_path`/`wheel_sha256` receipt fields are the verification
contract; do not claim this proof until the final I08/I09 run records it.

## Resource and native artifact inputs

The wheelhouse is paired with, or points to, these exact non-Python inputs:

| Input | Required contract |
| --- | --- |
| Remotion | One bundle matching the published app and Astrid rendering declarations. |
| Timeline schema | The exact canonical schema artifact/digest named by the manifest. |
| Browser | The Chromium/browser executable used by the existing Remotion frame worker. |
| Fonts | The reviewed public fonts plus provenance/license records and public-input paths. |
| `ffmpeg`/`ffprobe` | Matching media tools, with both executable paths recorded. |
| OMP | The exact compatible OMP artifact; do not use ambient `omp`. |
| Agent | The existing official launcher/profile artifact and its native runtime dependencies. |
| Native dependencies | The platform-specific libraries/binaries named by the artifact receipt. |
| App public inputs | Built app, catalogs, public fonts/assets and non-secret public configuration. |

The app public-input bundle must not contain Runtime bearer credentials,
provider keys or contributor credentials. The existing connector injects
server-side credentials. The app owner supplies the exact build/launch
command in the producer handoff; this document does not invent a new app
launcher.

Optional providers are diagnostics-gated. For example, a RunPod-backed
capability may require its existing optional package and credential, while the
base toolkit must remain usable without either:

```bash
python3 -m astrid doctor --json
astrid-credential set runpod
```

The first command is read-only and should report the missing optional input;
the second is only run when the selected capability requires it. Agent sign-in
does not grant provider access.

## Rendering prerequisites and review loop

The Astrid wheel is the single delivery owner for `astrid_runtime_client`;
Runtime owns the canonical timeline, realm, run and managed output. The final
paired manifest must name and hash the matching Remotion bundle, canonical
timeline schema, browser/Chromium executable, reviewed public fonts and
provenance, `ffmpeg`, `ffprobe`, OMP/agent/native inputs and app public inputs.
Do not use an ambient schema/OMP or place Runtime/provider credentials in app
public inputs. NativeSol owns the actual Astrid App launcher correction and
installed-host-origin evidence; this recipe does not claim an installed
journey.

After Runtime supplies the project and timeline, use the supported V3 route:

```bash
python3 -m astrid projects current --json
python3 -m astrid timelines list --project <project> --json
python3 -m astrid timelines show <timeline> --project <project> --json
python3 -m astrid timelines visualize <timeline> --project <project> \
  --mode inputs --format md --format png --json
python3 -m astrid timelines render <timeline> --project <project> \
  --backend rendering.remotion --wait --json
```

`--mode inputs` is the render-free placement check. Inspect the managed render
receipt/output after rendering. Only after a video is explicitly materialized
for validation, run the existing black-frame checker:

```bash
python3 tools/check_black_frames.py --video <render.mp4> \
  --timeline <timeline.json> --assets <assets.json>
```

The source-tree checker is contributor-only and is not a mandatory installed
recipe input because `tools/` is not packaged in the wheel. If a producer later
supplies it as a checksummed paired auxiliary artifact, its exact manifest row
must be recorded first; otherwise the installed journey uses renderer doctor,
managed output receipt and Runtime-owned output decode/readback instead. The
checker validates an already-produced video and does not render, publish, or
bypass Runtime custody. `tools/render_and_check.py` is retired and must not be
revived. Missing renderer, schema, browser, fonts, media-tool, OMP or native
inputs remain typed diagnostics; no browser/service is started by this
documentation pass.

## Operator distinctions

Keep these four surfaces separate:

1. `python3 -m astrid --help` is toolkit help and does not prove a Runtime or
   agent session.
2. `python3 -m astrid.skills.cli sync` is agent onboarding and installs the
   skill view; it does not install provider credentials.
3. `astrid login`/`status`/`logout`/`revoke` is Hivemind contributor login,
   only for contribution work.
4. `astrid-credential set <provider>` configures an optional creative/provider
   credential and is independent of agent and contributor authentication.

The shipped `astrid` profile is read from installed package resources. A final
direct agent launch/reconnect claim must cite the exact agent artifact,
command, installed origin and receipt; self-description is not route metadata.
The installed console entry is `astrid = astrid.omp_agent:main`, and its
supported direct routes are:

```bash
astrid agent
astrid agent "<one-shot message>"
```

These commands document the registered entry/script; this recipe does not
claim that the installed launch or reconnect journey has already passed.

## Supported predecessor populations → current final release

The recorded I11 noneditable candidate pair is a bounded actual test artifact,
not evidence of a real user population:

| Artifact | Candidate identity |
| --- | --- |
| Astrid | `astrid-0.1.0-py3-none-any.whl`, SHA-256 `47d56e391f9b41d82a714e0ef7cbd9ab76caa9c0137088e5530bc3fe55a497eb` |
| Runtime | `astrid_runtime-0.1.0-py3-none-any.whl`, SHA-256 `4c3363b3d9f7694a8c63567b659f3299d62f43efb290a56207a0dcf620caced7` |

These candidate artifacts are evidence inputs, not published-release claims.
The current final release is the producer-handoff tuple in the manifest above.
No arbitrary older version is supported.

An existing editable-checkout installation with an already-selected support
root and realm is also a supported transition population, but only for an
attach-in-place migration through the current CLI. It is not a claim that all
editable checkouts or all historical source states are supported. Preserve the
selected support root, exact realm ID/root, source-profile identity and Runtime
data before removing the editable install. Use the existing backup/restore
contract before any uninstall or replacement, then attach the same realm with
the `--attach --apply` command above; do not create a new realm or relocate the
data.

For an existing predecessor installation:

1. Stop active work through the existing lifecycle boundary and create a
   Runtime-owned backup:

   ```bash
   python3 -m astrid backup create /absolute/path/to/pre-update-backup --json
   ```

2. Preserve the selected root, realm identity, predecessor manifest and
   backup receipt. Place the current final wheelhouse beside the installation.
3. Replace the exact selected wheels from that wheelhouse with a forced,
   dependency-neutral reinstall; do not replace the root or create a new
   workspace:

   ```bash
   python3 -m pip install --no-index --force-reinstall --no-deps \
     "$ASTRID_WHEELHOUSE/$ASTRID_WHEEL_FILENAME" \
     "$ASTRID_WHEELHOUSE/$RUNTIME_WHEEL_FILENAME"
   ```

   Reinstall the hash-locked dependency set only from the paired wheelhouse.
   Do not claim the update from a version-string change: both current source
   projects remain `0.1.0` until producer handoff.
4. Run the existing bounded upgrade wrapper:

   ```bash
   astrid-upgrade --data-root "$ASTRID_RUNTIME_DATA_ROOT" --json
   ```

   Runtime owns schema/storage migration. The wrapper stops the idle Astrid
   host, refuses active work or an ambiguous realm, activates the selected
   destination, checks the realm identity and relaunches normally.
5. Run the installed identity commands above, then the existing doctor, output
   and readback checks. Confirm project IDs, media hashes, preferences/
   documents, credentials, managed output identity and app readback remain on
   the same root. The installed hash/origin receipt is required evidence, not
   an optional note.

Rollback is deliberately split:

- **Reverting packages:** if activation fails before a schema/data transition,
  use `--force-reinstall --no-deps` with the exact predecessor wheel artifacts
  and locked dependencies from its manifest, preserve the failure receipt,
  verify installed hashes/origins again, and restart through the same root.
- **Reverting data/schema:** after Runtime has activated a transition, restore
  the verified Runtime backup with the existing `astrid backup restore` /
  Runtime restore contract into an explicit destination, then verify realm
  identity. A package reinstall or Git revert does not downgrade a database.

The recipe does not promise arbitrary old binaries can open newer state. A
failed or unsupported transition must refuse with an actionable diagnostic and
retain the backup/activation receipt.

## Heavy-end proof, deferred until the tuple is final

I08/I09/I11 will execute this recipe from a fresh home and unrelated working
directory only after the producer handoff:

1. install the exact paired wheelhouse and capture installed origins;
2. start Runtime through the selected root and record launch/discovery identity;
3. perform one admitted action, materialize its Runtime-owned output, and read
   it back through the existing client/app route;
4. exercise the owned update procedure and the applicable package/data rollback
   path; and
5. capture optional-dependency diagnostics, shutdown/restart state and all
   receipt hashes.

This recipe documents the commands and required evidence inputs now. It does
not claim that this heavy-end run has passed, and it does not add an acceptance
gate. Existing I03/I08/I09/I11 remain the owners of those checks.

## Evidence inputs and handoff pointers

The recipe reuses the existing packaging, rendering-resource, OMP and
publication fragments through:

`receiving/coordination/evidence/v3-manager-dispatch-20261009/source-interface-artifact-index.json`

The release-recipe evidence root is:

`receiving/coordination/evidence/v3-manager-dispatch-20261009/release-recipe`

The route/session evidence is authoritative from the launch record and
`turn_context` model/effort fields. Model self-description is not metadata and
no nested model probe is used. The integration index, root README and Makefile
remain owners of their existing hand-pointer fragments.

## Naming follow-on (planning only)

NAM-00 through NAM-11 remain a planning-only follow-on after validated current
publication. They do not rename repositories, packages, CLIs, imports,
storage, domains or credentials during this release. Use current publication
destinations now, then consult the evidence:

- [NAM report](../../../../receiving/coordination/evidence/astrid-naming-migration-20261009/report.md)
- [NAM plan](../../../../receiving/coordination/evidence/astrid-naming-migration-20261009/plan.md)

No naming task is an I03/I08/I09/I11 gate.
