---
name: pack-builder
description: "Design and implement reusable Astrid packs and capabilities. Use when deciding whether a missing idea belongs in an existing executor, a new executor, an orchestrator, a visual element, or a timeline rendering extension, and when authoring the corresponding manifests and entrypoints."
metadata:
  short-description: "Build reusable Astrid extension packs"
---

# Pack Builder

Package a repeatable capability as an Astrid extension. Start with the
catalog and reuse what already exists. For a new pack, follow the
[authoring guide](../../../../../docs/guides/create-a-pack.md) and
[manifest reference](../../../../../docs/packs/creating-packs.md).

## Choose what to build

1. **Existing capability:** inspect `astrid.sdk.discover()` and
   `astrid.sdk.get_capability(...)`; compose what fits.
2. **Action or UI:** declare the public contribution in `pack.yaml` and put
   its implementation under the matching role folder.
3. **Rendering:** declare it under `rendering:` and follow the authoring guide.
4. **Shared code:** put private support used by contributions under `shared/`.
5. **No new pack:** keep one-off work in a run directory.

Reuse capabilities with `astrid.sdk.invoke(...)`. Do not open the Runtime
database or invoke `run.py` directly. See [creating tools](../../../../../docs/guides/creating-tools.md)
for contribution choices and [pack data storage](../../../../../docs/packs/data-storage.md)
for mutable project settings.

## Give the pack useful skills

Give the pack one main skill for its common workflow. Add an extra when a
distinct workflow needs separate instructions or should load only on demand.
Move details that crowd the normal path into extra skills or ordinary
references. Declare the main skill under `documentation`; declare each extra
as a `kind: skill` resource. References are files linked from a skill, not
skill resources. See the [skill-writing guide](references/writing-skills.md).

The [Timeline pack](../../../timeline/pack.yaml) shows this layout: its main
skill is `docs/SKILL.md`, its extras are `series`, `narrated`, and `review`,
and its helper guides live in `docs/references/`.

Validate and inspect declarations with the pack CLI. Use the
[pack testing guide](../pack-testing/SKILL.md) for test lanes. Regenerate the
capability catalog after changing a manifest.
