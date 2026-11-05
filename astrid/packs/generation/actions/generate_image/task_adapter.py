"""Pack-owned TaskHandler adapter around ``generate_image.run_sdk``.

Plan step 6's boundary: the adapter is **pack code** — kernel code never
imports it, and the adapter never imports kernel executor internals. It
implements the injected kernel handler protocol
the runtime task-handler protocol by duck typing and
runs the real image-generation pipeline in-process:

1. **Immutable task spec.** The task's immutable ``spec`` carries the
   generation request (``model``, ``mode``, ``execution``, ``prompt``,
   ``count``, ``seed``, plus optional features such as ``negative_prompt``,
   ``size``, ``guidance_scale``, ``steps``, ``strength``, ``image_ref``,
   ``quality``, ``background``, ``timeout``, and ``loras``). The adapter
   validates and translates it into capability argv — it never mutates the
   spec.
2. **Sanctioned in-process invocation.** ``run_sdk`` is imported lazily
   inside :func:`astrid.core.pack.entrypoint.canonical_runtime_entrypoint`
   (the canonical runtime capability context); no project-tree environment is
   consulted and the assigned staging root is the only filesystem boundary.
   The real ``generate_core`` pipeline executes end to end — model/mode
   validation, backend dispatch through the injected
   ``load_default_generation_backend_registry``, sequential N=1 generation,
   PNG metadata embedding, and the on-disk generation manifest.
3. **Confinement to the assigned staging root.** ``--out`` is the assigned
   staging directory, so every generated image under ``images/`` and the
   pack's own ``manifest.json`` land inside it. The adapter never writes
   anywhere else.
4. **Universal result manifest.** The adapter returns a valid universal
   result manifest whose outputs are exactly the pack's ``manifest.json``
   (the single primary ``result``) plus every generated image (ordered
   secondary ``output`` files) — byte SHA-256 identity recomputed from the
   concrete files (the generation manifest's hashes are recorded before PNG
   metadata embedding, so the adapter never trusts them), unique ordinals,
   no directory identities. The kernel service validates it strictly and
   prepares media descriptors from it.

Import direction is exact: the adapter imports only kernel *public* helpers
(``astrid.core.pack.entrypoint``, ``astrid.core.generation``) and its own
pack's ``run`` module; it never imports a local task executor and
nothing in the kernel imports this module.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from astrid.core.generation import GENERATION_RESULT_KEY

PACK_ID = "generation.generate_image"
"""The canonical capability id the adapter wraps and reports in manifests."""

_RESULT_MANIFEST_REL = "manifest.json"
"""The pack's own generation manifest, the single primary output."""

_INTEGER_FIELDS = frozenset({"count", "seed", "timeout", "steps"})
_FLOAT_FIELDS = frozenset({"strength", "guidance_scale", "upscale_factor", "noise_scale"})
_OPTIONAL_STRING_FIELDS = frozenset(
    {
        "negative_prompt",
        "size",
        "image_ref",
        "quality",
        "background",
        "loras",
        "upscale_mode",
        "target_resolution",
    }
)
_REQUIRED_STRING_FIELDS = ("model", "mode", "execution", "prompt")

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_RECIPE_KEYS = frozenset({
    "schema", "project_id", "shot_id", "target_role", "prompt_binding",
    "generator", "inputs", "parent_media_id", "parent_content_sha256",
})
_RECIPE_BINDING_KEYS = frozenset({"id", "head", "media_id", "content_sha256"})
_RECIPE_GENERATOR_KEYS = frozenset({"capability_id", "model", "backend", "mode", "settings"})
_RECIPE_INPUT_KEYS = frozenset({"ordinal", "role", "reference_id", "media_id", "content_sha256"})
_RECIPE_SETTING_KEYS = frozenset({
    "count", "seed", "timeout", "steps", "strength", "guidance_scale",
    "negative_prompt", "size", "image_ref", "quality", "background", "loras",
    "upscale_mode", "upscale_factor", "target_resolution", "noise_scale", "env_file",
})


def _recipe_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GenerateImageAdapterError(f"shot_generation_recipe field {field!r} must be an object")
    return value


def _reject_unknown(mapping: Mapping[str, Any], allowed: frozenset[str], field: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise GenerateImageAdapterError(
            f"shot_generation_recipe field {field!r} contains unknown key(s): {', '.join(unknown)}"
        )


def _recipe_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GenerateImageAdapterError(f"shot_generation_recipe field {field!r} must be a non-empty string")
    return value


def _recipe_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise GenerateImageAdapterError(f"shot_generation_recipe field {field!r} must be a SHA-256 hash")
    return value


def validate_shot_generation_recipe(
    value: object,
    *,
    project_id: str | None = None,
    model: str | None = None,
    mode: str | None = None,
    execution: str | None = None,
    capability_id: str = PACK_ID,
    resolved_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and deep-copy a closed, immutable shot-generation recipe."""
    recipe = _recipe_mapping(value, "recipe")
    _reject_unknown(recipe, _RECIPE_KEYS, "recipe")
    if recipe.get("schema") != "astrid.shot-generation-recipe/v1":
        raise GenerateImageAdapterError("shot_generation_recipe.schema must be 'astrid.shot-generation-recipe/v1'")
    recipe_project = _recipe_string(recipe.get("project_id"), "project_id")
    if project_id is not None and recipe_project != project_id:
        raise GenerateImageAdapterError("shot_generation_recipe.project_id does not match the task project")
    _recipe_string(recipe.get("shot_id"), "shot_id")
    if _recipe_string(recipe.get("target_role"), "target_role") != "primary_visual":
        raise GenerateImageAdapterError("shot_generation_recipe.target_role must be 'primary_visual'")

    binding = _recipe_mapping(recipe.get("prompt_binding"), "prompt_binding")
    _reject_unknown(binding, _RECIPE_BINDING_KEYS, "prompt_binding")
    _recipe_string(binding.get("id"), "prompt_binding.id")
    head = binding.get("head")
    if isinstance(head, bool) or not isinstance(head, int) or head < 0:
        raise GenerateImageAdapterError("shot_generation_recipe.prompt_binding.head must be a non-negative integer")
    _recipe_string(binding.get("media_id"), "prompt_binding.media_id")
    _recipe_hash(binding.get("content_sha256"), "prompt_binding.content_sha256")

    generator = _recipe_mapping(recipe.get("generator"), "generator")
    _reject_unknown(generator, _RECIPE_GENERATOR_KEYS, "generator")
    if _recipe_string(generator.get("capability_id"), "generator.capability_id") != capability_id:
        raise GenerateImageAdapterError("shot_generation_recipe.generator.capability_id does not match the generation capability")
    for field, expected in (("model", model), ("mode", mode), ("backend", execution)):
        actual = _recipe_string(generator.get(field), f"generator.{field}")
        if expected is not None and actual != expected:
            raise GenerateImageAdapterError(f"shot_generation_recipe.generator.{field} does not match the resolved generation input")
    settings = _recipe_mapping(generator.get("settings"), "generator.settings")
    _reject_unknown(settings, _RECIPE_SETTING_KEYS, "generator.settings")
    try:
        json.dumps(settings)
    except (TypeError, ValueError) as exc:
        raise GenerateImageAdapterError(f"shot_generation_recipe.generator.settings must be JSON-safe: {exc}") from exc
    if resolved_settings is not None:
        for name, expected in settings.items():
            if name not in resolved_settings or resolved_settings[name] != expected:
                raise GenerateImageAdapterError(f"shot_generation_recipe.generator.settings does not match resolved setting {name!r}")

    references = recipe.get("inputs")
    if not isinstance(references, list):
        raise GenerateImageAdapterError("shot_generation_recipe.inputs must be an ordered array")
    seen_reference_ids: set[str] = set()
    seen_media_ids: set[str] = set()
    for ordinal, raw_reference in enumerate(references):
        reference = _recipe_mapping(raw_reference, f"inputs[{ordinal}]")
        _reject_unknown(reference, _RECIPE_INPUT_KEYS, f"inputs[{ordinal}]")
        actual_ordinal = reference.get("ordinal")
        if isinstance(actual_ordinal, bool) or not isinstance(actual_ordinal, int) or actual_ordinal != ordinal:
            raise GenerateImageAdapterError("shot_generation_recipe.inputs ordinals must be contiguous from zero")
        reference_id = _recipe_string(reference.get("reference_id"), f"inputs[{ordinal}].reference_id")
        media_id = _recipe_string(reference.get("media_id"), f"inputs[{ordinal}].media_id")
        _recipe_string(reference.get("role"), f"inputs[{ordinal}].role")
        _recipe_hash(reference.get("content_sha256"), f"inputs[{ordinal}].content_sha256")
        if reference_id in seen_reference_ids or media_id in seen_media_ids:
            raise GenerateImageAdapterError("shot_generation_recipe.inputs reference and media ids must be unique")
        seen_reference_ids.add(reference_id)
        seen_media_ids.add(media_id)

    parent_media = recipe.get("parent_media_id")
    parent_hash = recipe.get("parent_content_sha256")
    if (parent_media is None) != (parent_hash is None):
        raise GenerateImageAdapterError("shot_generation_recipe parent_media_id and parent_content_sha256 must be supplied together")
    if parent_media is not None:
        _recipe_string(parent_media, "parent_media_id")
        _recipe_hash(parent_hash, "parent_content_sha256")
    return copy.deepcopy(dict(recipe))


class GenerateImageAdapterError(RuntimeError):
    """Raised when the real generator cannot produce a valid result manifest.

    The kernel execution service routes any handler exception through the
    fenced repository failure command, so raising here surfaces a typed
    ``failed`` execution outcome instead of corrupting state.
    """


@dataclass(frozen=True, slots=True)
class GenerationInputs:
    """Validated generator inputs decoded from the task's immutable spec."""

    model: str
    mode: str
    execution: str
    prompt: str
    count: int
    seed: int | None
    features: tuple[tuple[str, str | int | float], ...]
    shot_generation_recipe: dict[str, Any] | None = None

    def to_argv(self, staging_dir: Path) -> list[str]:
        """Translate the decoded spec into ``run_sdk`` capability argv."""
        argv: list[str] = [
            "--model", self.model,
            "--mode", self.mode,
            "--execution", self.execution,
            "--count", str(self.count),
            "--out", str(staging_dir),
        ]
        if self.prompt:
            argv.extend(["--prompt", self.prompt])
        if self.seed is not None:
            argv.extend(["--seed", str(self.seed)])
        if self.shot_generation_recipe is not None:
            argv.extend(["--shot-generation-recipe", json.dumps(self.shot_generation_recipe)])
        for name, value in self.features:
            argv.extend([f"--{name.replace('_', '-')}", str(value)])
        return argv


def _require_non_empty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GenerateImageAdapterError(
            f"task spec field {field!r} must be a non-empty string, got {value!r}"
        )
    return value.strip()


def _coerce_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GenerateImageAdapterError(
            f"task spec field {field!r} must be an integer, got {value!r}"
        )
    if value < 0:
        raise GenerateImageAdapterError(
            f"task spec field {field!r} must be non-negative, got {value!r}"
        )
    return value


def _coerce_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GenerateImageAdapterError(
            f"task spec field {field!r} must be a number, got {value!r}"
        )
    return float(value)


def _decode_inputs(
    spec: Mapping[str, Any], *, project_id: str | None = None, capability_id: str = PACK_ID
) -> GenerationInputs:
    """Decode and validate the adapter's inputs from one immutable spec."""
    if not isinstance(spec, Mapping):
        raise GenerateImageAdapterError(
            f"task spec must be an object, got {type(spec).__name__}"
        )
    model = _require_non_empty_string(spec.get("model"), "model")
    mode = _require_non_empty_string(spec.get("mode"), "mode")
    execution = _require_non_empty_string(spec.get("execution"), "execution")
    raw_prompt = spec.get("prompt", "")
    if mode == "upscale":
        if raw_prompt is None:
            raw_prompt = ""
        if not isinstance(raw_prompt, str):
            raise GenerateImageAdapterError("task spec field 'prompt' must be a string")
        prompt = raw_prompt.strip()
    else:
        prompt = _require_non_empty_string(raw_prompt, "prompt")
    count = _coerce_integer(spec.get("count", 1), "count")
    if count < 1:
        raise GenerateImageAdapterError(
            f"task spec field 'count' must be at least 1, got {count!r}"
        )
    raw_seed = spec.get("seed")
    seed: int | None = None
    if raw_seed is not None:
        seed = _coerce_integer(raw_seed, "seed")

    features: list[tuple[str, str | int | float]] = []
    for name in sorted(_OPTIONAL_STRING_FIELDS):
        value = spec.get(name)
        if value is None:
            continue
        features.append((name, _require_non_empty_string(value, name)))
    for name in sorted(_INTEGER_FIELDS - {"count", "seed"}):
        value = spec.get(name)
        if value is None:
            continue
        features.append((name, _coerce_integer(value, name)))
    for name in sorted(_FLOAT_FIELDS):
        value = spec.get(name)
        if value is None:
            continue
        features.append((name, _coerce_float(value, name)))
    recipe = None
    if spec.get("shot_generation_recipe") is not None:
        recipe = validate_shot_generation_recipe(
            spec["shot_generation_recipe"], project_id=project_id, model=model,
            mode=mode, execution=execution, capability_id=capability_id,
            resolved_settings=spec,
        )
    return GenerationInputs(
        model=model,
        mode=mode,
        execution=execution,
        prompt=prompt,
        count=count,
        seed=seed,
        features=tuple(features),
        shot_generation_recipe=recipe,
    )


class GenerateImageAdapter:
    """The injected pack-owned handler for ``generation.generate_image``.

    The caller injects the instance into :meth:`ExecutionService.execute`.
    The task's assigned staging directory is the sole filesystem boundary.
    """

    def execute(
        self, *, task: Any, staging_dir: Path
    ) -> Mapping[str, Any]:
        """Run the real generator and return a universal result manifest.

        ``task`` is the kernel :class:`TaskReadModel` (duck-typed: only
        ``task.spec``, ``task.id``, and ``task.created_at`` are read);
        ``staging_dir`` is the assigned staging root the kernel service
        created. All writes land under ``staging_dir``.
        """
        staging_dir = Path(staging_dir)
        inputs = _decode_inputs(
            task.spec, project_id=getattr(task, "project_id", None),
            capability_id=getattr(task, "capability", PACK_ID),
        )
        argv = inputs.to_argv(staging_dir)

        # Lazy import inside the sanctioned canonical runtime context: the
        # pack's run module guards direct invocation, and the context is the
        # in-process equivalent of ASTRID_INTERNAL_INVOCATION=1. Importing
        # here (not at module import) keeps the adapter importable without
        # the generator's heavy dependency chain.
        from astrid.pack_api import canonical_runtime_entrypoint

        with canonical_runtime_entrypoint(PACK_ID):
            from astrid.packs.generation.actions.generate_image.run import run_sdk

            result = run_sdk(argv)

        returncode = result.get("returncode")
        if returncode != 0:
            error = result.get("error")
            message = (
                error.get("message")
                if isinstance(error, Mapping)
                else f"generator returned {returncode!r}"
            )
            raise GenerateImageAdapterError(
                f"{PACK_ID} generation failed: {message}"
            )
        generation_result = result.get(GENERATION_RESULT_KEY)
        if generation_result is None:
            raise GenerateImageAdapterError(
                f"{PACK_ID} returned no {GENERATION_RESULT_KEY!r} payload"
            )
        return self._build_manifest(task, inputs, staging_dir, generation_result)

    # -- manifest construction --------------------------------------------

    def _build_manifest(
        self,
        task: Any,
        inputs: GenerationInputs,
        staging_dir: Path,
        generation_result: Any,
    ) -> Mapping[str, Any]:
        """Build the universal result manifest from the generated files.

        The pack's own ``manifest.json`` (written by ``run_sdk`` at the
        staging root) is the single primary ``result``; every generated
        image under the staging root is an ordered secondary ``output``.
        Hashes and byte sizes are recomputed from the concrete files — the
        generation manifest records them before PNG metadata embedding, so
        they are never trusted. Only concrete, staging-contained files
        participate: no directory identities, no derived index JSON.
        """
        manifest = getattr(generation_result, "manifest", None)
        if not isinstance(manifest, Mapping):
            raise GenerateImageAdapterError(
                "generator returned no manifest mapping"
            )

        # Declared output paths (staging-relative, from the generation
        # manifest) are the authoritative file list; every one must be a
        # concrete file inside the assigned staging root.
        raw_outputs = manifest.get("outputs")
        if not isinstance(raw_outputs, list):
            raise GenerateImageAdapterError(
                "generator manifest declares no outputs list"
            )
        image_rels: list[str] = []
        for index, entry in enumerate(raw_outputs):
            if not isinstance(entry, Mapping):
                raise GenerateImageAdapterError(
                    f"generator manifest outputs[{index}] must be an object"
                )
            raw_path = entry.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise GenerateImageAdapterError(
                    f"generator manifest outputs[{index}].path must be "
                    "a non-empty string"
                )
            candidate = Path(raw_path)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise GenerateImageAdapterError(
                    f"generator output {raw_path!r} escapes the assigned "
                    "staging directory"
                )
            resolved = (staging_dir / candidate).resolve()
            try:
                resolved.relative_to(staging_dir.resolve())
            except ValueError:
                raise GenerateImageAdapterError(
                    f"generator output {raw_path!r} escapes the assigned "
                    "staging directory"
                ) from None
            if not resolved.is_file():
                raise GenerateImageAdapterError(
                    f"declared generator output is not a concrete file: "
                    f"{raw_path!r}"
                )
            image_rels.append(candidate.as_posix())

        primary_rel = _RESULT_MANIFEST_REL
        ordered = [primary_rel, *sorted(set(image_rels))]

        outputs: list[dict[str, Any]] = []
        for ordinal, rel in enumerate(ordered):
            path = staging_dir / rel
            if not path.is_file():
                raise GenerateImageAdapterError(
                    f"declared generator output is not a concrete file: {rel}"
                )
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            outputs.append(
                {
                    "path": rel,
                    "content_hash": f"sha256:{digest}",
                    "bytes": path.stat().st_size,
                    "ordinal": ordinal,
                    "is_primary": ordinal == 0,
                    "role": "result" if ordinal == 0 else "output",
                    "label": Path(rel).name,
                }
            )

        created = getattr(task, "created_at", None)
        if not isinstance(created, str) or not created:
            created = manifest.get("created", PACK_ID)
        return {
            "schema_version": 1,
            "kind": PACK_ID,
            "inputs": {
                "task_id": task.id,
                "model": inputs.model,
                "mode": inputs.mode,
                "execution": inputs.execution,
                "prompt": inputs.prompt,
                "count": inputs.count,
                "seed": inputs.seed,
                "features": dict(inputs.features),
                **(
                    {"shot_generation_recipe": copy.deepcopy(inputs.shot_generation_recipe)}
                    if inputs.shot_generation_recipe is not None
                    else {}
                ),
            },
            "outputs": outputs,
            "created": created,
            "warnings": [],
        }
