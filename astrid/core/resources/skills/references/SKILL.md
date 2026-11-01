---
name: astrid-references
description: Create and manage reusable Astrid character, place, object, clothing, and other references, with canonical media and generation associations.
---

# References

Use references to preserve reusable subjects and their media across creative work.
References are a runtime-owned product mount under media. Their commands are
`python3 -m astrid media references --help`.

## Find or create a reference

Start with the owning project and inspect existing references before creating one:

```bash
python3 -m astrid media references list --project demo --json
python3 -m astrid media references show "Character Name" --project demo --json
```

For a new reference, import its canonical media with `media import`, then use the
returned exact same-project media id:

```bash
python3 -m astrid media import ./character.png --project demo --json
python3 -m astrid media references create --project demo --kind character \
  --name "Character Name" --media MEDIA_ID --json
```

Kinds are `character`, `place`, `object`, `clothing`, and `other`. Use description
and metadata for additional meaning. Resolve ambiguous names by the returned id.

## Use and maintain

- `associate` records media as `canonical`, `depicts`, `inspired_by`, or
  `used_as_input`; the last requires `--context-task` to preserve task lineage.
- `set-primary` changes the canonical media; `update` changes descriptive fields.
- `link` connects references using `belongs_to`, `wears`, `located_in`,
  `associated_with`, or `related_to`. Read its help for direction and id flags.
- `archive` and `recover` handle reversible retirement. Use
  `list --include-archived` when returning to paused work.

Inspect the chosen verb's `--help` for its required arguments. References use
runtime media identities; do not write a local reference database or substitute
file paths for media ids. A reference association records lineage; actual model
inputs must also be supplied through the chosen generation capability.

The current public media surface has no user-facing export or download route:
`media show` returns metadata, while runtime byte materialization is an
attempt-host concern. `generation.generate_image` therefore requires
`image_ref` to be an existing absolute or invocation-relative image path for
`i2i`/`edit`; do not pass a reference id or media object id as that value. If
the original local file is still available, pass that path to generation and
keep the runtime reference association for lineage. If only the managed media
id remains, report the typed limitation instead of inventing a download,
filesystem path, or local cache fallback.

For generating new variants, follow the [generation skill](../../generation/docs/SKILL.md).
For placing media in an existing video timeline, follow the [video editing
existing-timeline route](../../video_editing/docs/SKILL.md); rendering is
downstream evidence.
Return to [creative work](../../_core/docs/creative-work/SKILL.md) for capability selection.

`references.set_primary` only changes which associated media is canonical for
this reusable reference. It does not promote a generation variant and does not
change a timeline. Generation promotion uses the generation facade's explicit
`variant_of` and `primary="promote"` publication controls; object provenance
uses `client.media.relate(kind="variant_of")` and remains independent.
