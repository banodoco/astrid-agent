# Pack data storage

Use the workspace Runtime's project Documents API for mutable settings and
state that belong to one pack in one project. Keep pack source as ordinary
files, and keep Runtime as the authority for project, timeline, media, task,
run, and published artifact state. A pack is not a database schema or a
second project store.

## Put each kind of data in its owner

| Data | Owner and location |
| --- | --- |
| Pack code, manifests, schemas, assets, and static defaults | Ordinary files in the pack source tree. Declare files used by the pack in `pack.yaml` resources. |
| Mutable settings or small state for a pack in a project | A project-scoped Runtime document, addressed by project ID, pack-owned kind, and stable document ID. |
| Typed settings that belong to a user across projects | The Runtime user app-state API, using the owning feature's key. For example, RunPod policy lives in `generation` → `machines.runpod`. |
| User or project instructions in prose | Runtime preferences, as Markdown. Preferences are guidance, not a typed settings database or authorization grant. |
| Timelines, project media, tasks, runs, and invocation outputs | Runtime-owned project surfaces and published output references. Do not save these as a pack's config document. |
| Compiled modules, admitted source snapshots, and build receipts | Rebuildable derived artifacts, keyed by exact source and build identity. They may accelerate loading but never replace the selected Runtime document. |
| Local source roots, allowed filesystem roots, compiler commands, cache paths, and credential setup | Native host configuration and its path/credential authority. A Runtime document cannot grant access to an arbitrary local path. |

Static pack defaults belong with the pack as declared resources. When a
project needs an editable copy, initialize a Runtime document explicitly from
those defaults and validate the document on every read and write. Do not
rewrite pack source or preference prose to persist typed project settings.
If `pack.yaml` declares a V3 document format/schema, that is pack contract
metadata; it does not install a Runtime validator or storage backend.

Pack code must explicitly read the settings it needs; the host does not
automatically inject project documents into every invocation. Keep secret
values out of project documents and source resources. Declare required
environment variable names in the relevant capability manifest and use the
existing host-configured credential/secret mechanism for their values.

## RunPod machine storage

RunPod machine preferences are user settings across projects. The authoritative
copy is `machines.runpod` in `/v1/app-state/user/generation`, read and updated
through the versioned Runtime app-state API. It stores GPU order, optional
network volume/datacenter, pinned image, idle timeout, budget ceiling and
credential references. It never stores key bytes or permission to spend.
Use `astrid machines setup` to change it and `astrid machines setup --show` to
inspect sanitized settings plus the selected Runtime data root and realm.
Do not hand-edit Runtime's backing store.

| Data | Location and lifetime |
| --- | --- |
| User machine preferences | Runtime user app-state `generation` → `machines.runpod`; persist across jobs and pod release until explicitly changed. |
| Exact pod custody, paid limits and live lease | Local `<data-root>/runpod/<sha256(account-ref)>/claim.json`; survives host restarts for reconciliation. Keep it until the owner has confirmed cleanup; never delete it to force a new claim. Ended lease history is the sibling `claim.json.ended.json`. |
| Bootstrap source cache | Local `<data-root>/runpod/sources`; clean checkouts pinned by the installed release manifest. Rebuildable source cache, not preferences or custody. |
| Saved network-volume models | `<mount>/astrid/engines/comfyui/models` on the exact selected volume (`/workspace` is the default mount). Persist after pod release; deleting the volume is an explicit owner decision. |
| Pod engine and models without a volume | `/root/astrid/engines/comfyui` and its `models` directory on the pod's container disk. Warm reuse keeps them; pod termination removes them. |
| Pod Runtime and job scratch | `/root/astrid/runtime-data` and `/root/astrid/runs/<run-id>` on container disk. Reset removes prior job folders/attempt scratch and replaces the temporary realm before reuse; pod termination removes the disk. These are not durable outputs. |
| Published outputs and local copies | Runtime owns published output references and durable readback. Generation copies default to caller-local `./outputs/<task-id>/` or `--out`; pod release does not delete those copies. Preserve an unsettled local spool and the original receipt until publication can be recovered. |

Machine listing reads local custody, not the provider account. The owning
host's timers enforce idle and paid expiry only while it runs. A stopped host
or sleeping computer can leave a pod billing past the scheduled release;
the journal records custody but is not a provider-side spending cap. See the
[RunPod machine skill](../../astrid/packs/runpod/docs/SKILL.md#runpod-generation-machines)
for consent, keep and release commands.

## Identity, content schema, and concurrency

For a pack's ordinary per-project configuration, use the exact namespaced
kind `<pack_id>.config`, such as the editorial pack's `config` kind. For independent named
collections, pass a stable collection name as the helper's third argument;
do not append every pack's state to one growing realm-wide document.

Use Astrid's existing [stable document ID helper](../../astrid/sdk/documents.py#L23)
with the canonical Runtime project ID. Keep Runtime's document version
separate from the version of the content your pack understands:

- Kind and document ID are stable identity. Do not change them to rename a
  payload schema.
- Put a schema marker such as `editorial.config/v1` in the JSON content.
- Runtime `version` is the compare-and-swap (CAS) version for writes. It is
  not the payload schema version.
- The owning pack validates content and performs any explicit schema
  migration. It must reject an unknown newer schema without overwriting it.
- Create defaults only during an explicit initialization action. Inspecting or
  running a pack must not create a document or seed over an existing one.
- Update with the version you just read and an idempotency key. If Runtime
  reports a conflict, read the current document, validate it, and reconcile
  deliberately; never retry a stale replacement or merge silently.

For known settings, compute the stable ID and fetch that document directly.
Do not list every document or every pack config to find one record. With
1,000 packs, each pack still has its own project-scoped record in the same
generic Runtime store; there is no global mutable pack index, one database per
pack, or all-packs JSON file. Listing is for explicit inspection and should
remain paginated; it is not the lookup path for a known record.

Here is a create-or-read and CAS update using the existing SDK. Replace the
project placeholder with the canonical Runtime project ID resolved by the
host. This example is for an ordinary pack configuration; the owning pack's
real validator should check all required fields and value constraints.

```python
import uuid

from astrid.sdk import AstridClient
from astrid.sdk.documents import stable_document_id

project_id = "<canonical-runtime-project-id>"
pack_id = "my_pack"
kind = f"{pack_id}.config"
document_id = stable_document_id(project_id, kind, "default")
schema = f"{kind}/v1"
defaults = {"schema": schema, "quality": "balanced"}
init_key = uuid.uuid4().hex  # Keep this key if the same create must be retried.

with AstridClient.open_from_launcher(start_pack_host=False) as client:
    current = client.documents.show(document_id, project=project_id)

    if current.ok:
        record = current.data
    elif current.error.code == "not_found":
        created = client.documents.create(
            project=project_id,
            kind=kind,
            document_id=document_id,
            content=defaults,
            idempotency_key=init_key,
        )
        if created.ok:
            record = created.data
        elif created.error.code == "conflict":
            # Another initializer may have won. Read it; do not replace it.
            winner = client.documents.show(document_id, project=project_id)
            if not winner.ok:
                raise RuntimeError(winner.error.message)
            record = winner.data
        else:
            raise RuntimeError(created.error.message)
    else:
        raise RuntimeError(current.error.message)

    if record["kind"] != kind:
        raise RuntimeError("Configuration document kind does not match this pack")
    config = record["content"]
    if not isinstance(config, dict) or config.get("schema") != schema:
        raise RuntimeError("Unsupported pack configuration schema; preserve and migrate it explicitly")

    # Only run this update in response to an explicit request to change quality.
    next_config = {**config, "quality": "high"}
    update_key = uuid.uuid4().hex  # Reuse for retries of this exact update.
    saved = client.documents.update(
        document_id,
        project=project_id,
        content=next_config,
        expected_version=record["version"],
        idempotency_key=update_key,
    )
    if not saved.ok:
        if saved.error.code == "conflict":
            raise RuntimeError("Settings changed concurrently; fetch, validate, and reconcile before retrying")
        raise RuntimeError(saved.error.message)
    print("Saved Runtime document version", saved.data["version"])
```

For a retry of the same logical write after an uncertain network result, reuse
that operation's idempotency key. Use a new key for a new write. A concurrent
create conflict is safe to accept only after the winning record's project,
kind, stable identity, and content schema are checked. Do not treat the
Runtime's generic JSON acceptance as validation of your pack schema.

The `documents` CLI family can create, inspect, and edit the same records.
Compute the ID from the SDK helper, then use checkout/check-in for a
conflict-safe manual edit:

```bash
PROJECT_ID="<canonical-runtime-project-id>"
INIT_KEY="<unique-key-for-this-create-operation>"
DOCUMENT_ID="$(python3 -c 'import sys; from astrid.sdk.documents import stable_document_id; print(stable_document_id(sys.argv[1], "my_pack.config", "default"))' "$PROJECT_ID")"
astrid documents create --project "$PROJECT_ID" --kind my_pack.config --format json \
  --file ./my_pack.config.json --document-id "$DOCUMENT_ID" --idempotency-key "$INIT_KEY"
astrid documents show "$DOCUMENT_ID" --project "$PROJECT_ID" --json
astrid documents checkout "$DOCUMENT_ID" --project "$PROJECT_ID" --file ./my_pack.config.json
# Edit and validate the JSON with the owning pack.
astrid documents checkin ./my_pack.config.json
```

The CLI's generic document commands do not validate `<pack_id>.config/v1`;
validation and migration belong to the owning pack. Check-in preserves the
local file on a version conflict. Fetch the latest version to a different
path, inspect and reconcile both versions, then submit a new update against
the observed Runtime version.

## Astrid project-pack binding record

Astrid's project-draft-pack coordinator is the first core adopter of this
contract. It uses kind `astrid.project-pack`, with default ID
`stable_document_id(canonical_project_id, "astrid.project-pack", "default")`
and content schema `astrid.project-pack/v1`. This core-owned binding record
contains the stable pack ID, a portable source-binding key and relative
manifest path, plus admitted element references and their source snapshot
digests. The shape is small and project-scoped:

```json
{
  "schema": "astrid.project-pack/v1",
  "pack_id": "project_<stable-id>",
  "source_binding_key": "project-packs",
  "relative_manifest": "<realm>/<project>/packs/project_<stable-id>/pack.yaml",
  "admissions": [
    {
      "id": "thirty-block-reveal",
      "kind": "effect",
      "revision": "sha256:<64-hex-source-revision>",
      "snapshot_digest": "<64-hex-snapshot-digest>"
    }
  ]
}
```

Separate named collections use separate IDs. Compiler receipts and compiled
modules remain derived artifacts. The binding record does not store source
code, absolute machine paths, compiler commands, preferences, or credentials.

The native host resolves a portable source-binding key to a permitted local
source root and supplies compiler/cache configuration. A stored key is
identity, not a filesystem grant. Draft code remains editable files outside
generated caches; successful admission refers to verified immutable derived
inputs. Missing local source after a Runtime restore is an explicit
source-unavailable condition: Runtime backup covers the binding/config
document, not an external source tree. Do not recreate missing code from an
admission record or choose a different source root silently.

Persist the selected `pack_id`; do not silently derive a replacement when a
project or realm is restored or moved.
