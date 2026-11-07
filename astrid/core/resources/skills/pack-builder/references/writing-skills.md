# Writing pack skills

Give a pack one main skill for its common workflow. Add an extra skill when a
distinct workflow needs separate instructions or should load only on demand.
Put supporting detail in ordinary reference files linked from a skill. Split
content out when it crowds the normal path.

Point `documentation` to the main skill, usually `docs/SKILL.md`. Declare
each extra under `resources` with `kind: skill` and its `SKILL.md` path.
References are ordinary files; link to them, but do not declare them as
skills.

For example, a pack can declare one extra workflow:

```yaml
resources:
  - kind: skill
    path: docs/workflow/SKILL.md
```

- Put the normal path first; show exceptions after it.
- Teach one way to do each thing. Put alternatives in a reference.
- Show a short example, then give at most one sentence of explanation.
- Say each thing once, from the skill reader's view.
- Use short sentences, plain words, and consistent terms.
- Use tables only to show real choices.
- Keep each skill to one screen. Check that examples run as written.
