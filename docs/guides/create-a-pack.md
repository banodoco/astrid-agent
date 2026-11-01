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

Describe the pack in `pack.yaml`. Add the declared implementation and explain
when and how to use it in the pack's ordinary `docs/SKILL.md`, referenced by
`documentation: {kind: skill, path: docs/SKILL.md}`.

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
