"""experiment_import — Import unmanaged/legacy run roots into an experiment.

Walks an unmanaged run root (for example the Discord-command POC directory of
timestamped subdirectories), synthesizes a universal manifest per submission,
and emits an ``experiment.json`` plus an ``import.report.json``.

Rules honoured (see the architecture plan, Phase 4):

- Never rewrite historical directories. Source evidence is read-only; media
  is materialized as independent copy-on-write clones when supported, never
  writable hardlinks or eager large-media copies. No absolute source path
  is ever persisted — only the portable source subdirectory name survives.
- Associate prompts using exact run evidence first (the ``/gen prompt:`` text
  recovered from ``responsePreview``); mark ambiguous instead of guessing.
- Screenshot-only submissions remain ``unknown`` (status ``draft``) unless a
  terminal provider response is recovered.
- Deduplicate recovery fetches by response message id and content hash.
- Manual mappings take precedence over derived ones and are recorded as such.
- Idempotent and byte-stable: rerunning over the same source produces
  byte-identical artifacts.
- Signed provider URLs are never persisted; only non-secret counts survive.
"""

from __future__ import annotations

from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("iteration.experiment_import")
import argparse  # noqa: E402
import ctypes  # noqa: E402
import errno  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Mapping  # noqa: E402

from astrid.pack_api import write_manifest  # noqa: E402
from astrid.pack_api import read_result_json, sanitize_portable, synthesize_discord_manifest
from astrid.pack_api import derive_ulid  # noqa: E402
from astrid.pack_api import ExperimentValidationError, validate_experiment, validate_import_report
from astrid.pack_api import sha256_file  # noqa: E402

_CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _slugify(name: str) -> str:
    """Lowercase, replace non-[a-z0-9] runs with '-', trim separators."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not s:
        s = "sub"
    if s[0].isdigit() or s[0].isalpha():
        return s
    return "n-" + s


def _same_inode(a: Path, b: Path) -> bool:
    """Return True if *a* and *b* are the same inode (best-effort, OSError→False)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _resolve_contained_source(path: Path, root: Path) -> Path | None:
    """Resolve *path* only when its target remains inside the import root."""
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


def _resolve_submission_dir(path: Path, root: Path) -> Path | None:
    """Return a contained real submission directory, rejecting symlink roots."""
    if path.is_symlink():
        return None
    resolved = _resolve_contained_source(path, root)
    if resolved is None or not resolved.is_dir():
        return None
    return resolved


def _resolve_regular_source(path: Path, root: Path) -> Path | None:
    """Return a contained regular source file, rejecting file symlinks."""
    if path.is_symlink():
        return None
    resolved = _resolve_contained_source(path, root)
    if resolved is None or not resolved.is_file():
        return None
    return resolved


def _safe_unlink(path: Path) -> None:
    """Unlink *path* only if it exists; swallow missing-file errors."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # Last-resort: never let cleanup raise over the real operation.
        return


def _unique_temp_link_path(dst: Path) -> Path:
    """A unique sibling path used to stage a copy-on-write clone."""
    suffix = secrets.token_hex(4)
    return dst.parent / f".{dst.name}.link-tmp.{os.getpid()}.{suffix}"


def _clonefile(src: Path, dst: Path) -> None:
    """Create an APFS copy-on-write clone or raise ``OSError``."""
    clonefile = ctypes.CDLL(None, use_errno=True).clonefile
    clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    clonefile.restype = ctypes.c_int
    rc = clonefile(os.fsencode(src), os.fsencode(dst), 0)
    if rc != 0:
        raise OSError(ctypes.get_errno(), "clonefile failed")


def _hardlink_media(src: Path, dst: Path) -> bool:
    """Materialize *src* as a separate copy-on-write file at *dst*.

    The historical source and imported review artifact must never share an
    inode: a writable hardlink lets later review tooling mutate the source
    evidence.  On macOS/APFS ``clonefile(2)`` creates an independent inode
    backed by copy-on-write extents, so large media is not copied eagerly.
    Where cloning is unavailable (for example HFS+, which exposes the macOS
    clonefile symbol but rejects the operation), a regular copy is used.  The
    copy is still staged and verified before replacement, so it has an
    independent inode and retains the same atomic publication contract.

    The clone is staged at a unique sibling and atomically replaced only after
    its bytes and inode independence are verified.  Existing destinations are
    left untouched on failure.
    """
    if src.is_symlink() or not src.is_file():
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    # Idempotent fast path: an independent destination with identical bytes.
    if dst.is_file() and not dst.is_symlink() and not _same_inode(src, dst):
        try:
            if src.stat().st_size == dst.stat().st_size:
                from astrid.pack_api import hash_artifact
                if hash_artifact(src) == hash_artifact(dst):
                    return True
        except OSError:
            pass
    tmp = _unique_temp_link_path(dst)
    try:
        try:
            _clonefile(src, tmp)
        except OSError as exc:
            # clonefile(2) is present on macOS but unsupported by HFS+ and
            # other filesystems.  Only those explicit capability errors may
            # fall back to a byte copy; injected/permission/cross-device
            # failures remain honest failures and preserve the destination.
            unsupported = {
                code
                for code in (
                    getattr(errno, "ENOTSUP", None),
                    getattr(errno, "EOPNOTSUPP", None),
                    getattr(errno, "ENOSYS", None),
                )
                if code is not None
            }
            if exc.errno not in unsupported:
                raise
            shutil.copyfile(src, tmp)
        except AttributeError:
            # Non-macOS Python has no clonefile symbol.  The destination is a
            # sibling path, so copyfile remains safe and atomic after verify.
            shutil.copyfile(src, tmp)
        if _same_inode(src, tmp):
            _safe_unlink(tmp)
            return False
        from astrid.pack_api import hash_artifact
        if hash_artifact(src) != hash_artifact(tmp):
            _safe_unlink(tmp)
            return False
        os.replace(tmp, dst)
        return True
    except (AttributeError, OSError):
        # Unsupported filesystem / permission / injected failure: leave dst untouched,
        # clean only the exact temp path we created.
        _safe_unlink(tmp)
        return False


def _load_manual_mappings(path: Path | None) -> dict[str, dict[str, Any]]:
    """Load optional manual mapping file, keyed by subdir name.

    Accepted shapes::

        {"mappings": [{"subdir": "...", "prompt": "...", "seed": 1, "label": "..."}]}
        {"mappings": {"<subdir>": {"prompt": "...", "seed": 1}}}
        {"<subdir>": {"prompt": "...", "seed": 1}}

    The object-valued ``mappings`` form is the importer's own persisted
    ``manual-mappings.json`` shape, so that artifact must round-trip as a valid
    ``--mapping`` input without losing any human classifications.
    """
    if path is None:
        return {}
    if not path.is_file():
        raise AstridError(
            f"mapping file not found: {path}",
            recovery_command="verify the --mapping path points to an existing JSON file",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AstridError(
            f"cannot read mapping file: {exc}",
            recovery_command="provide a JSON file with manual subdir mappings",
        ) from exc
    mappings: dict[str, dict[str, Any]] = {}
    if not isinstance(data, Mapping):
        return mappings

    wrapped = data.get("mappings")
    if isinstance(wrapped, list):
        for entry in wrapped:
            if isinstance(entry, Mapping) and isinstance(entry.get("subdir"), str):
                mappings[entry["subdir"]] = dict(entry)
    else:
        source = wrapped if isinstance(wrapped, Mapping) else data
        for key, value in source.items():
            if isinstance(key, str) and isinstance(value, Mapping):
                mappings[key] = dict(value)
    return mappings


def _default_rubric() -> list[dict[str, Any]]:
    return [
        {
            "id": "usable",
            "label": "Usable output",
            "description": "Retrospective import rubric: 1 if the submission produced usable output, 0 otherwise.",
            "scale": {"min": 0, "max": 1},
        }
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import an unmanaged run root into a provider-independent experiment."
    )
    parser.add_argument(
        "--root",
        required=True,
        help="Unmanaged run root (e.g. the Discord-command POC directory).",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output directory for experiment.json, import.report.json, and runs/.",
    )
    parser.add_argument(
        "--mapping",
        default=None,
        help="Optional JSON file of manual subdir → {prompt,seed,label} mappings.",
    )
    parser.add_argument(
        "--project-slug",
        default=None,
        help="Project slug for the imported experiment (defaults to the root dir name).",
    )
    parser.add_argument(
        "--experiment-id",
        default=None,
        help="Experiment id (defaults to a slug derived from the root dir name).",
    )
    parser.add_argument("--title", default=None, help="Experiment title.")
    parser.add_argument("--question", default=None, help="Experiment question.")
    parser.add_argument(
        "--rubric-file",
        default=None,
        help="Optional JSON file with a rubric array. Defaults to a generic 0/1 rubric.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)

        root = Path(args.root).resolve()
        out = Path(args.out).resolve()
        if not root.is_dir():
            raise AstridError(
                f"root not found or not a directory: {root}",
                recovery_command="verify the --root path points to an existing directory",
            )
        # Refuse to import into (or onto) the source root.  An output nested
        # inside the root is self-ingested on rerun — the importer would walk
        # its own prior output (experiment.json, import.report.json, runs/) as
        # fresh submissions.  Resolved paths catch trailing-slash / relative
        # disguises; checked before anything is written.
        if out == root or root in out.parents:
            raise AstridError(
                f"--out ({out}) must not be inside or equal to --root ({root}); "
                f"the importer would ingest its own output on rerun",
                recovery_command="point --out at a directory outside the import root",
            )

        # Forbidden absolute source paths redacted from every persisted string
        # so a manual mapping or CLI field that echoes the import root (or its
        # parent) cannot leak it into a portable artifact.
        forbidden_paths = (str(root), str(root.parent))

        project_slug = args.project_slug or _slugify(root.name)
        experiment_id = args.experiment_id or f"{project_slug}-import"
        if not _CASE_ID_RE.fullmatch(experiment_id):
            raise AstridError(
                f"experiment_id {experiment_id!r} is not a valid slug",
                recovery_command="pass --experiment-id matching ^[a-z0-9][a-z0-9._-]*$",
            )

        manual = _load_manual_mappings(Path(args.mapping) if args.mapping else None)
        rubric = _load_rubric(args.rubric_file)

        subdirs = sorted(
            (
                resolved
                for candidate in root.iterdir()
                if not candidate.name.startswith(".")
                for resolved in [_resolve_submission_dir(candidate, root)]
                if resolved is not None
            ),
            key=lambda p: p.name,
        )

        # First pass: synthesize a manifest per subdir.
        synthesized: list[dict[str, Any]] = []
        for sub in subdirs:
            entry = _synthesize_entry(sub, root, manual)
            synthesized.append(entry)

        # Deduplicate by response_message_id (recovery fetches of the same bot
        # response).  Keep the primary (most outputs, then earliest name).
        cases, dedup_count = _dedupe_to_cases(synthesized)

        if not cases:
            # Empty root: no submission subdirectories at all.  Emit one honest
            # placeholder case so the experiment stays schema-valid and the
            # report records that nothing was found.
            cases = [{
                "subdir": "(empty-root)",
                "source_root": root.name,
                "run_id": derive_ulid(f"{root.name}/(empty-root)"),
                "manifest": {
                    "schema_version": 1,
                    "kind": "discord_browser.generate",
                    "inputs": {"source_root": root.name},
                    "outputs": [],
                    "created": "1970-01-01T00:00:00Z",
                    "warnings": [],
                    "status": "draft",
                    "capture_gaps": [{
                        "kind": "missing_manifest",
                        "detail": "No submission subdirectories found in the import root",
                    }],
                    "provider_extension": {"provider": "discord_browser", "empty_root": True},
                },
                "status": "draft",
                "seed": None,
                "prompt": None,
                "ambiguous_prompt": True,
                "label": f"Empty import root: {root.name}",
                "source_kind": "empty",
                "manual_mapping": False,
                "response_message_id": None,
                "_source_dir": root,
                "_manifest": {
                    "schema_version": 1,
                    "kind": "discord_browser.generate",
                    "inputs": {"source_root": root.name},
                    "outputs": [],
                    "created": "1970-01-01T00:00:00Z",
                    "warnings": [],
                    "status": "draft",
                    "capture_gaps": [{
                        "kind": "missing_manifest",
                        "detail": "No submission subdirectories found in the import root",
                    }],
                    "provider_extension": {"provider": "discord_browser", "empty_root": True},
                },
                "_outcome": "draft",
                "_has_screenshots": False,
                "_media_count": 0,
            }]

        # Build experiment.json
        outcome_values = sorted({c["_outcome"] for c in cases}) or ["draft"]
        experiment = _build_experiment(
            experiment_id=experiment_id,
            project_slug=project_slug,
            title=args.title or f"Imported runs from {root.name}",
            question=args.question or "What do these legacy runs show?",
            rubric=rubric,
            cases=cases,
            outcome_values=outcome_values,
        )

        try:
            experiment = validate_experiment(experiment)
        except ExperimentValidationError as exc:
            raise AstridError(
                f"internal error: built invalid experiment: {exc}",
                recovery_command="report this bug — importer produced an invalid experiment",
            ) from exc

        # Materialize imported run tree (manifests + copy-on-write media).
        runs_dir = out / "runs"
        co_location_failures = 0
        for case in cases:
            run_id = case["run_id"]
            run_dir = runs_dir / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            manifest = case["_manifest"]
            # Co-locate media via independent COW clones so review can
            # verify/play without a writable alias to historical evidence.
            # The required-output list is rebuilt from the hardlinks that
            # actually succeeded: a failed item is removed from ``outputs[]``
            # (it would otherwise declare an absent required path), its provider
            # evidence is preserved only in a non-local diagnostic, and an
            # honest capture gap + report warning record the failure.  A
            # pre-existing destination that replacement failed over is left
            # intact (``_hardlink_media`` never unlinks it) but is NOT claimed
            # as this source's output unless it was verified to share the
            # source's inode.
            materialized: list[dict[str, Any]] = []
            ordered_inputs = manifest.get("inputs", {}).get("ordered_artifacts", [])
            if isinstance(ordered_inputs, list):
                for input_entry in ordered_inputs:
                    if not isinstance(input_entry, Mapping):
                        continue
                    rel = input_entry.get("path")
                    if not isinstance(rel, str):
                        continue
                    src = case["_source_dir"] / Path(rel).name
                    dst = run_dir / Path(rel).name
                    resolved_src = _resolve_regular_source(src, root)
                    if (
                        resolved_src is None
                        or not _hardlink_media(resolved_src, dst)
                    ):
                        manifest.setdefault("capture_gaps", []).append({
                            "kind": "missing_input_hash",
                            "detail": (
                                f"Declared input {Path(rel).name!r} could not be "
                                "co-located as an independent COW clone"
                            ),
                        })
            for out_entry in list(manifest.get("outputs", [])):
                rel = out_entry.get("path")
                if not isinstance(rel, str):
                    continue
                basename = Path(rel).name
                src = case["_source_dir"] / basename
                dst = run_dir / basename
                resolved_src = _resolve_regular_source(src, root)
                if (
                    resolved_src is not None
                    and _hardlink_media(resolved_src, dst)
                ):
                    materialized.append(out_entry)
                    continue
                # Failure: drop from required local outputs; keep provider
                # evidence only in a non-local diagnostic.
                provider_ext = manifest.setdefault("provider_extension", {})
                provider_ext.setdefault("unmaterialized_outputs", []).append({
                    "path": basename,
                    "media_type": out_entry.get("media_type"),
                    "reason": "copy-on-write clone unavailable on this filesystem",
                })
                manifest.setdefault("capture_gaps", []).append({
                    "kind": "missing_output_hash",
                    "detail": (
                        f"Output {basename!r} could not be co-located into the "
                        f"imported run tree (copy-on-write clone unavailable)"
                    ),
                })
                co_location_failures += 1
            manifest["outputs"] = materialized
            # Final portable redaction: manual mappings may have injected URLs,
            # secrets, or the absolute source root into prompt/label/metadata.
            # Apply after materialization so gap/diagnostic text is covered too.
            sanitized = sanitize_portable(manifest, forbidden_paths=forbidden_paths)
            case["_manifest"] = sanitized
            (run_dir / "manifest.json").write_text(
                json.dumps(sanitized, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest_hash = "sha256:" + sha256_file(run_dir / "manifest.json")
            for case_record in experiment["cases"]:
                if case_record.get("run_id") == run_id:
                    case_record["source_manifest"] = {
                        "path": "manifest.json",
                        "content_hash": manifest_hash,
                    }
                    break

        # Manifest pins were added only after their final bytes existed.
        experiment = validate_experiment(experiment)

        # Build report.  Source root is recorded as the portable directory name
        # only — the imported run tree already holds independent COW clones, so the
        # absolute import root is neither needed nor safe to persist.
        report = _build_report(
            experiment_id=experiment_id,
            source_root=root.name,
            subdirs=subdirs,
            cases=cases,
            dedup_count=dedup_count,
            manual_count=sum(1 for c in cases if c.get("manual_mapping")),
            co_location_failures=co_location_failures,
        )

        try:
            report = validate_import_report(report)
        except ExperimentValidationError as exc:
            raise AstridError(
                f"internal error: built invalid import report: {exc}",
                recovery_command="report this bug — importer produced an invalid report",
            ) from exc

        # Write artifacts (deterministic ordering for byte-stability).  Every
        # final portable JSON document is redacted just before writing so the
        # no-URL / no-secret / no-absolute-source-root guarantee holds even
        # when a manual mapping or CLI field carried such material.
        out.mkdir(parents=True, exist_ok=True)
        _write_json(
            out / "experiment.json",
            sanitize_portable(_strip_internal(experiment), forbidden_paths=forbidden_paths),
        )
        _write_json(
            out / "import.report.json",
            sanitize_portable(report, forbidden_paths=forbidden_paths),
        )
        if manual:
            _write_json(
                out / "manual-mappings.json",
                sanitize_portable(
                    {
                        "schema_version": 1,
                        "experiment_id": experiment_id,
                        "provenance": "human_supplied_override",
                        "mappings": manual,
                    },
                    forbidden_paths=forbidden_paths,
                ),
            )

        print(json.dumps({
            "experiment": str(out / "experiment.json"),
            "import_report": str(out / "import.report.json"),
            "runs_dir": str(runs_dir),
            "imported_cases": len(cases),
        }, sort_keys=True))

        top_outputs = [
            {"path": "experiment.json", "type": "file"},
            {"path": "import.report.json", "type": "file"},
            {"path": "runs/", "type": "directory"},
        ]
        if manual:
            top_outputs.append({"path": "manual-mappings.json", "type": "file"})
        write_manifest(out / "manifest.json", sanitize_portable({
            "schema_version": 1,
            "kind": "experiment_import",
            "inputs": {
                # Portable name only — never persist the absolute import root.
                "root": root.name,
                "mapping": str(Path(args.mapping).name) if args.mapping else "",
            },
            # The synthesized runs/ tree is a durable directory output.
            "outputs": top_outputs,
            "created": experiment.get("created", "1970-01-01T00:00:00Z"),
            "warnings": [],
        }, forbidden_paths=forbidden_paths))

        return 0

    return run_pack_main("iteration.experiment_import", _run, argv=argv)


def _load_rubric(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return _default_rubric()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AstridError(
            f"cannot read rubric file: {exc}",
            recovery_command="provide a JSON file with a rubric array",
        ) from exc
    if isinstance(data, Mapping) and isinstance(data.get("rubric"), list):
        data = data["rubric"]
    if not isinstance(data, list):
        raise AstridError("rubric file must contain a list of rubric dimensions")
    return [dict(d) for d in data if isinstance(d, Mapping)]


def _synthesize_entry(
    sub: Path,
    root: Path,
    manual: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Synthesize one manifest + metadata for a single subdirectory."""
    resolved_sub = _resolve_submission_dir(sub, root)
    if resolved_sub is None:
        raise AstridError(
            f"submission directory is outside the import root: {sub.name}",
            recovery_command="remove symlinked or escaping submission directories",
        )
    sub = resolved_sub
    result_path = _resolve_regular_source(sub / "result.json", root)
    result = read_result_json(result_path) if result_path is not None else None
    safe_files = [
        resolved
        for candidate in sub.iterdir()
        for resolved in [_resolve_regular_source(candidate, root)]
        if resolved is not None
    ]
    has_screenshots = any(
        p.is_file() and re.match(r"^(before|after)[-_]?submit\.", p.name, re.IGNORECASE)
        for p in safe_files
    )
    media_files = [p for p in safe_files if not p.name.startswith(".")]

    if result is not None:
        manifest = synthesize_discord_manifest(
            result=result, run_dir=sub, subdir_name=sub.name
        )
        source_kind = "result_json"
    else:
        # No result.json — synthesize a truthful unknown record.
        manifest = {
            "schema_version": 1,
            "kind": "discord_browser.generate",
            "inputs": {"source_root": sub.name},
            "outputs": [],
            "created": "1970-01-01T00:00:00Z",
            "warnings": [],
            "status": "draft",
            "capture_gaps": [
                {
                    "kind": "missing_manifest",
                    "detail": "No result.json recovered from submission directory",
                }
            ],
            "provider_extension": {
                "provider": "discord_browser",
                "screenshot_only": has_screenshots,
                "empty": not has_screenshots and not media_files,
            },
        }
        source_kind = "screenshot_only" if has_screenshots else "empty"

    # Apply manual mapping (takes precedence over derived values).
    manual_entry = manual.get(sub.name)
    manual_applied = False
    if manual_entry:
        inputs = manifest.setdefault("inputs", {})
        if isinstance(manual_entry.get("prompt"), str):
            inputs["prompt"] = manual_entry["prompt"]
            inputs["prompt_capture"] = "manual"
            # A manually supplied prompt resolves the ambiguous-prompt gap.
            manifest["capture_gaps"] = [
                g for g in manifest.get("capture_gaps", [])
                if not (
                    isinstance(g, Mapping)
                    and (
                        g.get("kind") == "missing_prompt"
                        or str(g.get("detail", "")).startswith(
                            "Prompt was recovered from responsePreview"
                        )
                    )
                )
            ]
            manifest["capture_gaps"].append({
                "kind": "ambiguous_provenance",
                "detail": (
                    "Prompt was supplied by a manual mapping; its historical "
                    "association is human-asserted rather than mechanically verified"
                ),
            })
        if isinstance(manual_entry.get("seed"), int):
            inputs["seed"] = manual_entry["seed"]
        declared_inputs = manual_entry.get("inputs")
        if isinstance(declared_inputs, list):
            ordered: list[dict[str, Any]] = []
            output_by_path = {
                out.get("path"): out
                for out in manifest.get("outputs", [])
                if isinstance(out, Mapping)
            }
            moved_paths: set[str] = set()
            for ordinal, declared in enumerate(declared_inputs, start=1):
                if not isinstance(declared, Mapping):
                    continue
                path = declared.get("path")
                role = declared.get("role")
                if not isinstance(path, str) or not isinstance(role, str):
                    continue
                source = output_by_path.get(Path(path).name, {})
                entry = {
                    "ordinal": ordinal,
                    "role": role,
                    "path": Path(path).name,
                    "provenance": "manual_mapping",
                }
                for key in ("content_hash", "media_type", "bytes", "metadata"):
                    if key in source:
                        entry[key] = source[key]
                ordered.append(entry)
                moved_paths.add(Path(path).name)
            if ordered:
                inputs["ordered_artifacts"] = ordered
                reclassified = [
                    dict(out)
                    for out in manifest.get("outputs", [])
                    if (
                        isinstance(out, Mapping)
                        and out.get("path") in moved_paths
                    )
                ]
                manifest["outputs"] = [
                    out for out in manifest.get("outputs", [])
                    if not (
                        isinstance(out, Mapping)
                        and out.get("path") in moved_paths
                    )
                ]
                if reclassified:
                    extension = manifest.setdefault("provider_extension", {})
                    extension["reclassified_input_echoes"] = [
                        {
                            "path": item.get("path"),
                            "content_hash": item.get("content_hash"),
                        }
                        for item in reclassified
                    ]
                    manifest.setdefault("warnings", []).append(
                        "Captured download was manually classified as an input, "
                        "not a generated output"
                    )
                    if (
                        manifest.get("status") == "completed"
                        and not manifest["outputs"]
                    ):
                        manifest["status"] = "partial"
                        manifest.setdefault("capture_gaps", []).append({
                            "kind": "ambiguous_provenance",
                            "detail": (
                                "No distinct generated output was recovered after "
                                "the captured input echo was reclassified"
                            ),
                        })
        manifest.setdefault("provider_extension", {})["manual_fields"] = sorted(
            key for key in ("prompt", "seed", "label", "inputs") if key in manual_entry
        )
        manual_applied = True

    prompt = manifest.get("inputs", {}).get("prompt") if isinstance(manifest.get("inputs"), Mapping) else None
    ambiguous_prompt = not isinstance(prompt, str) or not prompt.strip()
    status = manifest.get("status") or "draft"
    seed = manifest.get("inputs", {}).get("seed") if isinstance(manifest.get("inputs"), Mapping) else None
    label = manual_entry.get("label") if manual_entry and isinstance(manual_entry.get("label"), str) else None
    if not label:
        label = (prompt[:60] + "…") if isinstance(prompt, str) and len(prompt) > 60 else (prompt or sub.name)

    run_id = derive_ulid(f"{root.name}/{sub.name}")
    return {
        "subdir": sub.name,
        "source_root": root.name,
        "run_id": run_id,
        "manifest": manifest,
        "status": status,
        "seed": seed if isinstance(seed, int) else None,
        "prompt": prompt if isinstance(prompt, str) else None,
        "ambiguous_prompt": ambiguous_prompt,
        "label": label,
        "source_kind": source_kind,
        "manual_mapping": manual_applied,
        "response_message_id": (
            manifest.get("inputs", {}).get("response_message_id")
            if isinstance(manifest.get("inputs"), Mapping) else None
        ),
        "_source_dir": sub,
        "_manifest": manifest,
        "_outcome": status,
        "_has_screenshots": has_screenshots,
        "_media_count": len([p for p in media_files if not re.match(r"^(before|after)[-_]?submit\.", p.name, re.IGNORECASE)]),
    }


def _dedupe_to_cases(
    synthesized: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Deduplicate recovery fetches by response message id; return cases."""
    by_response: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}
    no_response: list[dict[str, Any]] = []
    for entry in synthesized:
        rid = entry.get("response_message_id")
        if isinstance(rid, str) and rid:
            hashes = tuple(sorted(
                str(out["content_hash"])
                for out in entry.get("_manifest", {}).get("outputs", [])
                if isinstance(out, Mapping) and isinstance(out.get("content_hash"), str)
            ))
            # A response id alone is insufficient: recovery attempts with
            # different content hashes are distinct evidence and must survive.
            by_response.setdefault((rid, hashes), []).append(entry)
        else:
            no_response.append(entry)

    cases: list[dict[str, Any]] = []
    dedup_count = 0
    for (_rid, _hashes), group in by_response.items():
        # Primary = most media, then earliest subdir name.
        group.sort(key=lambda e: (-(e.get("_media_count") or 0), e["subdir"]))
        primary = group[0]
        duplicates = group[1:]
        primary["_duplicates"] = [d["subdir"] for d in duplicates]
        cases.append(primary)
        dedup_count += len(duplicates)
    cases.extend(no_response)
    cases.sort(key=lambda c: c["subdir"])
    return cases, dedup_count


def _assign_case_ids(cases: list[dict[str, Any]]) -> dict[str, str]:
    """Map each case's subdir to a stable, unique ``case_id``.

    The base slug is ``_slugify(subdir)``.  When two *distinct* subdirs collapse
    to the same slug, every colliding subdir is disambiguated with a short
    deterministic suffix derived from its ORIGINAL name (a content hash), so the
    mapping is stable across reruns and independent of input order.  No cases
    are merged or dropped — each subdir keeps its own case.
    """
    base_to_subs: dict[str, list[str]] = {}
    for c in cases:
        base_to_subs.setdefault(_slugify(c["subdir"]), []).append(c["subdir"])
    mapping: dict[str, str] = {}
    for c in cases:
        base = _slugify(c["subdir"])
        if len(base_to_subs[base]) > 1:
            # Collision: append a short stable suffix from the original name.
            suffix = derive_ulid(c["subdir"])[:10].lower()
            mapping[c["subdir"]] = f"{base}-{suffix}"
        else:
            mapping[c["subdir"]] = base
    return mapping


def _build_experiment(
    *,
    experiment_id: str,
    project_slug: str,
    title: str,
    question: str,
    rubric: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    outcome_values: list[str],
) -> dict[str, Any]:
    case_id_by_subdir = _assign_case_ids(cases)
    case_records: list[dict[str, Any]] = []
    for idx, c in enumerate(cases):
        case_id = case_id_by_subdir[c["subdir"]]
        case_record: dict[str, Any] = {
            "case_id": case_id,
            "label": c["label"],
            "run_id": c["run_id"],
            "attempt": 1,
            "factors": {"outcome": c["_outcome"]},
            "relationship": {"type": "baseline", "case_id": None},
            "expected_input_roles": [],
            "included": True,
            # Additive import provenance (not lifecycle fields):
            "imported": True,
            "source_subdir": c["subdir"],
            "ambiguous_prompt": c["ambiguous_prompt"],
            "manual_mapping": c.get("manual_mapping", False),
        }
        ordered = c.get("_manifest", {}).get("inputs", {}).get("ordered_artifacts", [])
        if isinstance(ordered, list):
            case_record["expected_input_roles"] = [
                str(item["role"])
                for item in ordered
                if isinstance(item, Mapping) and isinstance(item.get("role"), str)
            ]
        if c.get("_duplicates"):
            case_record["duplicate_of_subdirs"] = c["_duplicates"]
        case_records.append(case_record)

    created = "1970-01-01T00:00:00Z"
    return {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "project_slug": project_slug,
        "title": title,
        "question": question,
        "hypotheses": [],
        "factors": [
            {"id": "outcome", "values": outcome_values},
        ],
        "rubric": rubric,
        "cases": case_records,
        "created": created,
        "updated": created,
    }


def _build_report(
    *,
    experiment_id: str,
    source_root: str,
    subdirs: list[Path],
    cases: list[dict[str, Any]],
    dedup_count: int,
    manual_count: int,
    co_location_failures: int = 0,
) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    ambiguous = 0
    screenshot_only = 0
    empty = 0
    gap_counts: dict[str, int] = {}

    # Cross-case duplicate output groups by content hash.
    hash_to_cases: dict[str, list[str]] = {}
    for c in cases:
        status_counts[c["status"]] = status_counts.get(c["status"], 0) + 1
        if c["ambiguous_prompt"]:
            ambiguous += 1
        if c["source_kind"] == "screenshot_only":
            screenshot_only += 1
        elif c["source_kind"] == "empty":
            empty += 1
        for gap in c["_manifest"].get("capture_gaps", []):
            if isinstance(gap, Mapping):
                kind = str(gap.get("kind", "unknown"))
                gap_counts[kind] = gap_counts.get(kind, 0) + 1
        for out_entry in c["_manifest"].get("outputs", []):
            if isinstance(out_entry, Mapping) and isinstance(out_entry.get("content_hash"), str):
                hash_to_cases.setdefault(out_entry["content_hash"], []).append(c["subdir"])

    duplicate_groups = [
        {"content_hash": h, "subdirs": sorted(set(subs))}
        for h, subs in hash_to_cases.items()
        if len(set(subs)) > 1
    ]

    warnings: list[str] = []
    if ambiguous:
        warnings.append(f"{ambiguous} case(s) have unrecoverable prompts (marked ambiguous)")
    if screenshot_only:
        warnings.append(f"{screenshot_only} screenshot-only submission(s) remain unknown")
    if duplicate_groups:
        warnings.append(f"{len(duplicate_groups)} duplicate output group(s) across submissions")
    if co_location_failures:
        warnings.append(
            f"{co_location_failures} output(s) could not be co-located into the imported "
            f"run tree (copy-on-write clone unavailable); manifests record a capture gap and no "
            f"materialized local output is claimed for them"
        )

    return {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "source_root": source_root,
        "total_subdirs": len(subdirs),
        "imported_cases": len(cases),
        "skipped_subdirs": 0,
        "deduplicated_subdirs": dedup_count,
        "ambiguous_prompt_cases": ambiguous,
        "screenshot_only_cases": screenshot_only,
        "empty_subdirs": empty,
        "status_counts": status_counts,
        "duplicate_output_groups": duplicate_groups,
        "manual_mappings_applied": manual_count,
        "capture_gap_counts": gap_counts,
        "warnings": warnings,
        "notes": (
            "Retrospective import of unmanaged runs. Statuses reflect recovered "
            "evidence only; ambiguous and screenshot-only cases are intentionally "
            "left unknown rather than guessed."
        ),
    }


def _strip_internal(experiment: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep copy of experiment with no internal underscore keys."""
    return json.loads(json.dumps(experiment))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
