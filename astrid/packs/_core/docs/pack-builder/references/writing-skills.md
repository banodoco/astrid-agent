# Writing pack skills

Give a pack one main skill for its common workflow. Add an extra skill when a
distinct workflow needs separate instructions or should load only on demand.
Put supporting detail in ordinary reference files linked from a skill. Split
content out when it crowds the normal path.

Point `documentation` to the main skill, usually `docs/SKILL.md`. Declare
each extra under `resources` with `kind: skill` and its `SKILL.md` path.
References are ordinary files; link to them, but do not declare them as
skills.

The [Timeline pack](../../../../timeline/pack.yaml) uses one main skill and
three extras: `series`, `narrated`, and `review`. Its guides live under
`docs/references/` and link from the [main skill](../../../../timeline/docs/SKILL.md).
These five lines are copied from its manifest:

```yaml
  - kind: skill
    path: docs/series/SKILL.md
  - kind: skill
    path: docs/narrated/SKILL.md
```

- Put the normal path first; show exceptions after it.
- Teach one way to do each thing. Put alternatives in a reference.
- Show a short example, then give at most one sentence of explanation.
- Say each thing once, from the skill reader's view.
- Use short sentences, plain words, and consistent terms.
- Use tables only to show real choices.
- Keep each skill to one screen. Check that examples run as written.
