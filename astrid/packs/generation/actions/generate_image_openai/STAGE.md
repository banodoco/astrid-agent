# Generate Image (OpenAI) Executor

Use `generation.generate_image_openai` when an agent needs bitmap image assets from
OpenAI GPT Image models for timelines, collages, pitch frames, visual treatments,
or fallback art packs.

> **Sprint 01 note:** This is the original OpenAI-only executor, renamed from
> its previous ID.  The new multi-backend image generation executor (which
> reclaimed the `generation.generate_image` executor ID) supports both local
> (vibecomfy) and cloud (fal) execution.  Prefer the new multi-backend
> executor for new work unless you specifically need OpenAI GPT Image models.

This executor wraps `astrid.packs.generation.actions.generate_image_openai.run` and expects a
prompt file. Put one prompt per line, or provide a JSON/JSONL list accepted by the
underlying CLI.

## Usage

Dry-run:

```python
import astrid.sdk as sdk
result = sdk.invoke("generation.generate_image_openai",
    kind="executor", project="demo",
    inputs={"prompts_file": "runs/example-images/prompts.txt"},
    dry_run=True)
```

Run:

```python
result = sdk.invoke("generation.generate_image_openai",
    kind="executor", project="demo",
    inputs={"prompts_file": "runs/example-images/prompts.txt"})
```

## Outputs

- Images are written under `{out}/images`.
- The generation manifest is written to `{out}/manifest.json`.

## Presets

Pass `--preset <name>` to use a canned prompt and behaviour bundle. Currently:

- `saint-peter-of-banodoco` — onboarding portrait of the maker as Saint Peter
  of Banodoco. The preset is not a public executor input: the public executor
  manifest accepts prompt files, so pass the preset's prompt through
  `generation.generate_image_openai` with `inputs={"prompt": ...}` (or a prompt
  file) via the SDK. Direct module invocation is rejected by the
  canonical-entrypoint guard.

  Add `dry_run=True` to the SDK call to print the planned API call without
  spending tokens.

## Requirements

Requires `OPENAI_API_KEY` in the environment or a supported local env file.

If one prompt is rejected by the image API, the current underlying CLI stops at
that failure. For batch work, prefer smaller prompt files so a single blocked
prompt does not waste earlier planning.
