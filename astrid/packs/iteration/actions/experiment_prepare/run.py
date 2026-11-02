"""experiment_prepare — Normalize experiment manifests into review model."""

from __future__ import annotations

from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("iteration.experiment_prepare")
import argparse  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from astrid.pack_api import write_manifest  # noqa: E402
from astrid.pack_api import build_diagnostics, build_normalized_review, resolve_manifest_path
from astrid.pack_api import ExperimentValidationError, validate_diagnostics, validate_experiment, validate_review
from astrid.pack_api import sha256_file  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize experiment case manifests into a provider-independent review model."
    )
    parser.add_argument(
        "--experiment",
        required=True,
        help="Path to experiment.json definition file.",
    )
    parser.add_argument(
        "--runs-dir",
        required=True,
        help="Directory containing project runs (for resolving run/manifest paths).",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Directory for experiment_prepare outputs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)

        experiment_path = Path(args.experiment).resolve()
        runs_dir = Path(args.runs_dir).resolve()
        out_dir = Path(args.out).resolve()

        # Read and validate experiment
        try:
            experiment_raw = json.loads(experiment_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AstridError(
                f"Cannot read experiment file: {experiment_path}: {exc}",
                recovery_command="verify the --experiment path points to a valid experiment.json",
            ) from exc

        try:
            experiment = validate_experiment(experiment_raw)
        except ExperimentValidationError as exc:
            raise AstridError(
                f"Invalid experiment definition: {exc}",
                recovery_command="check the experiment.json against docs/contracts/experiment-contract.md",
            ) from exc

        # Resolve case manifests.  Missing/invalid source evidence creates
        # first-class failed/unknown records with diagnostics — it does NOT
        # erase the rest of the experiment.  We only abort (raise) on an
        # experiment definition that cannot produce any output at all.
        cases_with_manifests: list[tuple[dict[str, Any], str, Path]] = []
        for original_case in experiment["cases"]:
            case = dict(original_case)
            run_id = case["run_id"]
            run_dir = (runs_dir / run_id).resolve()
            try:
                run_dir.relative_to(runs_dir)
            except ValueError:
                cases_with_manifests.append(
                    (case, "manifest.json", runs_dir / f".unsafe-{run_id}")
                )
                continue
            if not run_dir.is_dir():
                cases_with_manifests.append(
                    (case, "manifest.json", runs_dir / run_id)
                )
                continue

            expected_manifest = case.get("source_manifest")
            manifest_rel = (
                expected_manifest.get("path", "manifest.json")
                if isinstance(expected_manifest, dict)
                else "manifest.json"
            )
            resolved_manifest, _manifest_error = resolve_manifest_path(
                run_dir, manifest_rel
            )
            if resolved_manifest is None:
                # Missing manifest: pass through so the build function
                # records a missing_manifest capture gap instead of aborting.
                # The shared resolver also rejects symlink escapes here before
                # normalization gets a chance to read or hash source bytes.
                cases_with_manifests.append(
                    (case, manifest_rel, run_dir)
                )
                continue

            cases_with_manifests.append((case, manifest_rel, run_dir))

        # Build normalized review
        review = build_normalized_review(
            experiment=experiment,
            cases_with_manifests=cases_with_manifests,
        )

        # Validate review
        try:
            review = validate_review(review)
        except ExperimentValidationError as exc:
            raise AstridError(
                f"Internal error: generated invalid review: {exc}",
                recovery_command="report this bug — normalized review failed validation",
            ) from exc

        # Build diagnostics
        diagnostics = build_diagnostics(review)
        review["diagnostics"] = diagnostics

        # Validate diagnostics
        try:
            diagnostics = validate_diagnostics(diagnostics)
        except ExperimentValidationError as exc:
            raise AstridError(
                f"Internal error: generated invalid diagnostics: {exc}",
                recovery_command="report this bug — diagnostics failed validation",
            ) from exc

        # Write outputs
        out_dir.mkdir(parents=True, exist_ok=True)

        review_path = out_dir / "review.json"
        diag_path = out_dir / "diagnostics.json"

        _write_json(review_path, review)
        _write_json(diag_path, diagnostics)

        print(json.dumps({
            "review": str(review_path),
            "diagnostics": str(diag_path),
        }, sort_keys=True))

        # Write universal result manifest using a stable timestamp
        # derived from the experiment definition.
        manifest_created = experiment.get("created") or experiment.get("updated")
        if not isinstance(manifest_created, str):
            manifest_created = "1970-01-01T00:00:00Z"

        manifest = {
            "schema_version": 1,
            "kind": "experiment_prepare",
            "inputs": {
                "experiment": experiment_path.name,
                "experiment_sha256": f"sha256:{sha256_file(experiment_path)}",
                "runs_dir": runs_dir.name,
            },
            "outputs": [
                {"path": "review.json", "type": "file"},
                {"path": "diagnostics.json", "type": "file"},
            ],
            "created": manifest_created,
            "warnings": [],
        }
        write_manifest(out_dir / "manifest.json", manifest)

        return 0

    return run_pack_main("iteration.experiment_prepare", _run, argv=argv)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
