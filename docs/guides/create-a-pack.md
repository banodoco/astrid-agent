# When and how to create a pack

Turn something useful into something reusable.

A pack groups callable actions, host contributions, rendering elements, and
supporting documentation with instructions for an agent to use them.

Create a pack when you have a tool or creative approach worth reusing. First check whether an existing pack already does the job; a one-off task does not need a new pack.

## Give it to your agent

```text
Help me turn this idea into an Astrid pack.
Start with Astrid’s Pack Builder skill, check what already exists,
then build and validate the smallest useful version.
```

[Open the Pack Builder skill →](../../astrid/packs/_core/docs/pack-builder/SKILL.md)

The [Tools, Widgets, and Actions vision](frontend-pack-vision.md) is a
design-only naming direction; it does not add a current V3 manifest or SDK
surface.

## Build it yourself

Choose the current V3 declaration that matches the contribution: an `action`
does callable work, `ui` mounts a host contribution, and `rendering` declares
an element or renderer. A one-off experiment belongs in a run; the folder name
does not publish a capability.

From an empty working folder, with Astrid’s environment active:

```bash
python3 -m astrid.core.pack.cli new my_pack
cd my_pack
```

Declare actual public actions, UI, rendering, or document contributions in
`pack.yaml`; support-only packs are valid, and a folder by itself does not
publish anything. Give the pack a useful description or `agent.purpose`, then
explain when and how to use its contribution in the one ordinary
`docs/SKILL.md`, referenced by `documentation: {kind: skill, path: docs/SKILL.md}`.
Add `agent.normal_entrypoints` only as a preferred subset of declared public
contributions or supported aliases. It is optional and does not need to name
actions for UI, rendering, document, or support-only packs. Keep
`agent.required_context` optional and limited to files an agent needs to read.

For authored input/output port lists, include a type and useful description on
each port, and set requiredness on each input. Preserve actual defaults. If a
contract uses inline JSON Schema or a schema reference, retain its own
descriptions, required properties, and defaults instead of flattening it into
ports. Static validation reports incomplete authoring metadata as advisory
warnings; the warnings do not block validation.

Follow [Creating Astrid Packs](../packs/creating-packs.md) for the complete
layouts and examples. For mutable settings that belong to a pack in one
project, follow the [Pack data storage contract](../packs/data-storage.md).
Then validate:

```bash
python3 -m astrid.core.pack.cli validate .
```

Validation checks the pack’s structure; it does not install dependencies or prove its runtime behavior. Follow the authoring guide to register, discover, and try the capability in the intended environment.

[Choosing what to build](creating-tools.md) · [Pack contract](../packs/contract.md)

Have a useful finding rather than a reusable tool? [Contribute knowledge](contributing-knowledge.md).

[Back to Astrid](../../README.md)
