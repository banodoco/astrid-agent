# The `vibecomfy.workflows` point

A pack contributes a workflow family with one YAML file in
`<pack>/extensions/vibecomfy.workflows/<key>.yaml`. The point is declared in
`astrid/packs/vibecomfy/extensions/points/workflows.yaml` (version 1, side `agent`,
cardinality many, effects `none`).

| field | required | meaning |
| --- | --- | --- |
| `ready_root` | yes | Folder of ready templates, relative to the manifest, kept inside the pack. Registered with `plugin_api().register_ready_root()` for each contribution. |
| `entries` | no | Thin entries in VibeComfy's shape `{id, kind, modes, bundle, needs, binds, defaults}`. Plugin templates are absent from the static template index, so the entry is declared here. |
| `inputs` | no | The entry's own interface: `[{name, type, required, description}]`, `type` one of `string integer number boolean file file_map json object array`. When present they replace the bundle's widgets in `describe`. Astrid's field, stripped from the thin entry VibeComfy sees. |
| `default_mode` | no | The mode used when the caller names none and the graph does not depend on it (h3's `build` picks the graph from the request). |
| `build` | no | `file.py:fn`. `build(inputs, files, out) -> {workflow, bindings, plan}`. Runs on the machine, on CPU, deterministically. `requirements()` reads the built graph's rows; the dry run calls it and prints `plan`. |
| `after_outputs` | no | `file.py:fn`. `after_outputs(job_dir, inputs, files, outputs) -> outputs`. Runs on the same machine after execute and before copy-back. If it raises, the job fails. It must not re-run prepare or compile: it reads what `build` left in the shared job directory. |

**`after_outputs` is not VibeComfy's template finalize** (`templates.py:1381`). That one runs while a
template is being assembled; this one runs once per job, after the engine has produced files.

**`build` must be pure and deterministic.** It runs on the controller, through `requirements()`,
before any machine exists, and may run more than once per job (`requirements()`, the dry run, then
the run). The same inputs and files must give the same graph, bindings and plan, with no network,
clock or random input. `build` and `after_outputs` share one directory: the job's `job_dir`, else its
`out`.

A hook that declares an `engine` keyword parameter receives the engine's facts
(`astrid.core.generation.engine_facts.EngineFacts`: pinned ComfyUI commit, model lock, asset
manifest, atomic JSON write, source checks). That is how a contribution stays free of Astrid
imports; h3_av's AST test (`tests/packs/h3_av/test_no_astrid_imports.py`) pins it. `build` may
also return `assets` (`{binding: path}`, the media the graph binds) and `bundle` (the folder of
the chosen graph) beside `workflow`, `bindings` and `plan`.

`needs` is a list of objects, as VibeComfy's strict entry validator requires
(`vibecomfy/entries.py`: exactly `id kind modes bundle needs binds defaults`).

Core never names `build` or `after_outputs`. `astrid.core.generation.workflows` reads the data and hands
the callables to the job; the comfyui engine's run site (`ComfyUIEngine.prepare`, `.finish`,
`.dry_run`) is the one place that calls them.

## File-map inputs

An entry input of type `file_map` is `{id: path}`. It ships in the managed-assets archive, one
binding per id named `<input>/<id>` (`asset_manifest.file_map_bindings`); `file_map_members`
reads the map back from a validated archive by input name.

## Node packs

No hook is needed for node packs: a template's own node pins win over the lock
(VibeComfy `custom_node_refs.py:241-247`).
