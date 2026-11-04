"""experiment_review — Render deterministic HTML review page."""

from __future__ import annotations

from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("iteration.experiment_review")
import argparse  # noqa: E402
import csv  # noqa: E402
import html  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Mapping  # noqa: E402
from urllib.parse import quote  # noqa: E402

from astrid.pack_api import write_manifest  # noqa: E402
from astrid.pack_api import validate_conclusions, validate_review_final
from astrid.pack_api import ExperimentValidationError, is_valid_content_hash, validate_review
from astrid.pack_api import sha256_file  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render a deterministic provider-independent HTML review page."
    )
    parser.add_argument(
        "--review",
        required=True,
        help="Path to normalized review.json from experiment_prepare.",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Directory for review.html output.",
    )
    parser.add_argument(
        "--conclusions",
        default=None,
        help="Optional path to a conclusions.json (observations/inferences/decisions).",
    )
    parser.add_argument(
        "--review-final",
        default=None,
        help="Optional path to a review.final.json to render recorded rubric decisions.",
    )
    parser.add_argument(
        "--runs-dir",
        default=None,
        help=(
            "Optional owning runs/ directory. When supplied, media hrefs are "
            "verified again and made relative to review.html for offline playback."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)

        review_path = Path(args.review).resolve()
        out_dir = Path(args.out).resolve()

        # Read and validate review
        try:
            review_raw = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AstridError(
                f"Cannot read review file: {review_path}: {exc}",
                recovery_command="verify the --review path points to a valid review.json",
            ) from exc

        try:
            review = validate_review(review_raw)
        except ExperimentValidationError as exc:
            raise AstridError(
                f"Invalid review: {exc}",
                recovery_command="re-run experiment_prepare to generate a valid review.json",
            ) from exc

        # Work on a deep JSON copy because the optional static-media verification
        # annotates individual entries without rewriting review.json.
        review = json.loads(json.dumps(review))
        conclusions = _read_optional_json(args.conclusions, label="conclusions")
        review_final = _read_optional_json(args.review_final, label="review-final")

        # Identity gate: optional review-final / conclusions artifacts must be
        # for THIS experiment before they are rendered.  A cross-experiment
        # artifact is rejected rather than embedded alongside another review.
        review_experiment = {
            "experiment_id": review["experiment_id"],
            "rubric": review.get("rubric", []),
        }
        included_case_ids = [
            str(c["case_id"])
            for c in review.get("cases", [])
            if isinstance(c, Mapping) and c.get("included", True)
        ]
        if review_final is not None:
            try:
                review_final = validate_review_final(
                    review_final,
                    experiment=review_experiment,
                    case_ids=included_case_ids,
                )
            except ExperimentValidationError as exc:
                raise AstridError(
                    f"--review-final artifact is invalid or bound to another experiment: {exc}",
                    recovery_command=(
                        "supply a review.final.json whose experiment_id matches the review "
                        "and whose decisions cover exactly the included case set"
                    ),
                ) from exc
        if conclusions is not None:
            try:
                conclusions = validate_conclusions(
                    conclusions,
                    experiment=review_experiment,
                    case_ids=included_case_ids,
                )
            except ExperimentValidationError as exc:
                raise AstridError(
                    f"--conclusions artifact is invalid or bound to another experiment: {exc}",
                    recovery_command=(
                        "supply a conclusions.json whose experiment_id matches the review"
                    ),
                ) from exc

        # Build HTML (static renderer stays provider-agnostic; media mounts
        # and conclusions are optional additive inputs).
        media_mounts = None
        if args.runs_dir:
            media_mounts = _prepare_static_media(
                review, Path(args.runs_dir).resolve(), out_dir
            )
        html_content = _build_html(
            review,
            conclusions=conclusions,
            review_final=review_final,
            media_mounts=media_mounts,
        )

        # Write output
        out_dir.mkdir(parents=True, exist_ok=True)
        html_path = out_dir / "review.html"
        html_path.write_text(html_content, encoding="utf-8")
        summary_path = out_dir / "review.summary.csv"
        summary_path.write_text(_build_summary_csv(review), encoding="utf-8")

        print(json.dumps({"review_html": str(html_path)}, sort_keys=True))

        # Write universal result manifest with stable timestamp from review.
        # Each durable output is declared exactly once: the output list is
        # de-duplicated by path (first occurrence wins) so the manifest never
        # emits two identical declarations for the same artifact.
        manifest_created = review.get("created")
        if not isinstance(manifest_created, str):
            manifest_created = "1970-01-01T00:00:00Z"

        manifest = {
            "schema_version": 1,
            "kind": "experiment_review",
            "inputs": {
                "review": review_path.name,
                "review_sha256": f"sha256:{sha256_file(review_path)}",
            },
            "outputs": _dedupe_outputs([
                {"path": "review.html", "type": "file"},
                {"path": "review.summary.csv", "type": "file"},
            ]),
            "created": manifest_created,
            "warnings": [],
        }
        if args.conclusions:
            manifest["inputs"]["conclusions"] = Path(args.conclusions).name
            manifest["inputs"]["conclusions_sha256"] = (
                f"sha256:{sha256_file(Path(args.conclusions).resolve())}"
            )
        if args.review_final:
            manifest["inputs"]["review_final"] = Path(args.review_final).name
            manifest["inputs"]["review_final_sha256"] = (
                f"sha256:{sha256_file(Path(args.review_final).resolve())}"
            )
        write_manifest(out_dir / "manifest.json", manifest)

        return 0

    return run_pack_main("iteration.experiment_review", _run, argv=argv)


# ── Media type helpers ──────────────────────────────────────────────────────


def _dedupe_outputs(outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return *outputs* with duplicate ``path`` declarations removed.

    Each durable output is declared exactly once: when two entries share a
    path, the first occurrence wins and later duplicates are dropped.  This
    keeps the universal manifest honest — a single artifact (e.g.
    ``review.html``) is never declared twice.
    """
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for entry in outputs:
        path = entry.get("path") if isinstance(entry, Mapping) else None
        if isinstance(path, str) and path in seen:
            continue
        if isinstance(path, str):
            seen.add(path)
        unique.append(entry)
    return unique

def _is_image(media_type: str | None) -> bool:
    """Return True if *media_type* represents a browser-renderable image."""
    if not media_type:
        return False
    return media_type.startswith("image/")


def _is_video(media_type: str | None) -> bool:
    """Return True if *media_type* represents a browser-playable video."""
    if not media_type:
        return False
    return media_type.startswith("video/")


def _is_audio(media_type: str | None) -> bool:
    """Return True if *media_type* represents a browser-playable audio."""
    if not media_type:
        return False
    return media_type.startswith("audio/")


# ── HTML rendering ─────────────────────────────────────────────────────────

def _read_optional_json(
    path: str | None, *, label: str
) -> dict[str, Any] | None:
    """Read an optional JSON file; a supplied invalid path fails closed."""
    if not path:
        return None
    p = Path(path).resolve()
    if not p.is_file():
        raise AstridError(f"--{label} file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AstridError(f"cannot read --{label} JSON: {exc}") from exc
    if not isinstance(data, Mapping):
        raise AstridError(f"--{label} JSON must be an object")
    return dict(data)


def _prepare_static_media(
    review: dict[str, Any],
    runs_dir: Path,
    out_dir: Path,
) -> dict[str, str]:
    """Reverify media and return run-id URL prefixes relative to review.html."""
    mounts: dict[str, str] = {}
    real_runs = runs_dir.resolve()
    for case in review.get("cases", []):
        if not isinstance(case, dict):
            continue
        run_id = str(case.get("run_id", ""))
        run_dir = (real_runs / run_id).resolve()
        try:
            run_dir.relative_to(real_runs)
        except ValueError:
            continue
        if not run_dir.is_dir():
            continue
        rel_prefix = Path(os.path.relpath(run_dir, out_dir)).as_posix()
        mounts[run_id] = quote(rel_prefix, safe="/.")
        for collection in ("inputs", "outputs"):
            for entry in case.get(collection, []):
                if not isinstance(entry, dict) or not entry.get("verified"):
                    continue
                rel = entry.get("path")
                expected = entry.get("content_hash")
                if not isinstance(rel, str) or not isinstance(expected, str):
                    entry["verified"] = False
                    continue
                artifact = (run_dir / rel).resolve()
                try:
                    artifact.relative_to(run_dir)
                    actual = f"sha256:{sha256_file(artifact)}"
                except (ValueError, OSError):
                    actual = None
                if actual != expected:
                    entry["verified"] = False
                    case.setdefault("capture_gaps", []).append({
                        "kind": "ambiguous_provenance",
                        "detail": (
                            f"{collection[:-1].title()} {rel!r} changed after "
                            "preparation and was disabled"
                        ),
                    })
    return mounts


def _build_summary_csv(review: Mapping[str, Any]) -> str:
    """Return a deterministic portable case summary."""
    buf = io.StringIO(newline="")
    fields = [
        "case_id", "run_id", "included", "status", "provider", "backend",
        "model", "model_actual", "mode", "seed", "input_count",
        "output_count", "capture_gap_count",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for case in review.get("cases", []):
        if not isinstance(case, Mapping):
            continue
        params = case.get("parameters", {})
        writer.writerow({
            "case_id": case.get("case_id", ""),
            "run_id": case.get("run_id", ""),
            "included": str(bool(case.get("included", True))).lower(),
            "status": case.get("status", ""),
            "provider": case.get("provider", ""),
            "backend": case.get("backend") or "",
            "model": case.get("model") or "",
            "model_actual": case.get("model_actual") or "",
            "mode": case.get("mode") or "",
            "seed": params.get("seed", "") if isinstance(params, Mapping) else "",
            "input_count": len(case.get("inputs", [])),
            "output_count": len(case.get("outputs", [])),
            "capture_gap_count": len(case.get("capture_gaps", [])),
        })
    return buf.getvalue()


def _build_html(
    review: Mapping[str, Any],
    *,
    conclusions: Mapping[str, Any] | None = None,
    review_final: Mapping[str, Any] | None = None,
    media_mounts: Mapping[str, str] | None = None,
) -> str:
    """Build a deterministic, self-contained HTML review page.

    ``media_mounts`` (``run_id → URL prefix``) is optional and only supplied
    by the review-session orchestrator so browser playback resolves run
    artifacts via a safe mounted route.  When absent (the static renderer),
    media ``src`` values stay run-relative — preserving provider-agnostic,
    offline-renderable output.
    """
    experiment_id = _esc(str(review.get("experiment_id", "Unknown")))
    title = _esc(str(review.get("title", experiment_id)))
    question = _esc(str(review.get("question", "")))
    cases = review.get("cases", [])
    created = _esc(str(review.get("created", "")))

    # Hypotheses
    hypotheses_html = ""
    hypotheses = review.get("hypotheses", [])
    if hypotheses:
        hypotheses_html = '<div class="section hypotheses"><h2>Hypotheses</h2><ul>'
        for h in hypotheses:
            if isinstance(h, Mapping):
                hid = _esc(str(h.get("id", "")))
                claim = _esc(str(h.get("claim", "")))
                status = _esc(str(h.get("status", "provisional")))
                hypotheses_html += (
                    f'<li><strong>{hid}</strong>: {claim} '
                    f'<span class="badge badge-sm badge-{status}">{status}</span></li>'
                )
        hypotheses_html += "</ul></div>"

    # Rubric
    rubric_html = ""
    rubric = review.get("rubric", [])
    if rubric:
        rubric_html = '<div class="section rubric"><h2>Rubric</h2><ul>'
        for r in rubric:
            if isinstance(r, Mapping):
                rid = _esc(str(r.get("id", "")))
                label = _esc(str(r.get("label", "")))
                rubric_html += f"<li><strong>{rid}</strong>: {label}</li>"
        rubric_html += "</ul></div>"
    diagnostics_html = _render_diagnostics(review.get("diagnostics"))

    # Compute summary stats
    total = len(cases)
    status_order = [
        "completed", "partial", "failed", "provider_rejected",
        "timed_out", "interrupted", "draft",
    ]
    status_counts: dict[str, int] = {}
    for c in cases:
        s = str(c.get("status", "draft"))
        status_counts[s] = status_counts.get(s, 0) + 1

    # Status badges
    status_badges = "".join(
        f'<span class="badge badge-{s}">{s.replace("_", " ")}: {status_counts.get(s, 0)}</span>'
        for s in status_order
        if s in status_counts
    )

    # Build case cards
    case_cards = ""
    # Index recorded rubric decisions by case_id (if a review.final was supplied).
    final_by_case: dict[str, Mapping[str, Any]] = {}
    if isinstance(review_final, Mapping):
        final_reviewer = review_final.get("reviewer")
        for dec in review_final.get("decisions") or []:
            if isinstance(dec, Mapping) and isinstance(dec.get("case_id"), str):
                enriched = dict(dec)
                if isinstance(final_reviewer, Mapping):
                    enriched.setdefault("reviewer", dict(final_reviewer))
                final_by_case[dec["case_id"]] = enriched
    for case in cases:
        case_cards += _render_case_card(
            case,
            media_mounts=media_mounts,
            final_decision=final_by_case.get(
                str(case.get("case_id", "")) if isinstance(case, Mapping) else ""
            ),
        )

    # Conclusions (observations / inferences / decisions) — rendered as
    # distinct sections so an inference is never presented as an observed fact.
    conclusions_html = _render_conclusions(conclusions)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Experiment Review — {experiment_id}</title>
<style>
:root {{
  --bg: #0d1117;
  --fg: #c9d1d9;
  --border: #30363d;
  --card-bg: #161b22;
  --accent: #58a6ff;
  --success: #3fb950;
  --warning: #d2991d;
  --error: #f85149;
  --muted: #8b949e;
}}
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg);
  color: var(--fg);
  line-height: 1.6;
  padding: 2rem;
  max-width: 1400px;
  margin: 0 auto;
}}
h1 {{ font-size: 1.75rem; margin-bottom: 0.5rem; }}
h2 {{ font-size: 1.25rem; margin: 1.5rem 0 0.75rem; }}
h3 {{ font-size: 1rem; margin: 0 0 0.5rem; }}
.header {{ border-bottom: 1px solid var(--border); padding-bottom: 1rem; margin-bottom: 1.5rem; }}
.meta {{ color: var(--muted); font-size: 0.875rem; }}
.badges {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 1rem 0; }}
.badge {{
  display: inline-block;
  padding: 0.25rem 0.75rem;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}
.badge-sm {{ font-size: 0.65rem; padding: 0.15rem 0.5rem; text-transform: none; }}
.badge-completed, .badge-confirmed {{ background: var(--success); color: #000; }}
.badge-partial {{ background: var(--warning); color: #000; }}
.badge-failed, .badge-provider_rejected, .badge-refuted {{ background: var(--error); color: #fff; }}
.badge-timed_out, .badge-interrupted {{ background: #6e3b1a; color: #fff; }}
.badge-draft {{ background: var(--muted); color: #000; }}
.badge-provisional {{ background: var(--accent); color: #000; }}
.card {{
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1.25rem;
  margin-bottom: 1rem;
}}
.card-header {{
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-bottom: 1rem;
}}
.card-id {{ font-family: monospace; font-size: 0.75rem; color: var(--muted); }}
.card-label {{ font-weight: 600; }}
.card-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 1rem;
}}
@media (max-width: 900px) {{
  .card-grid {{ grid-template-columns: 1fr; }}
}}
.section-title {{
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--muted);
  margin-bottom: 0.5rem;
  border-bottom: 1px solid var(--border);
  padding-bottom: 0.25rem;
}}
.input-entry, .output-entry {{
  background: rgba(255,255,255,0.03);
  border-radius: 4px;
  padding: 0.5rem;
  margin-bottom: 0.5rem;
  font-size: 0.8rem;
}}
.hash {{ font-family: monospace; font-size: 0.65rem; color: var(--accent); word-break: break-all; }}
.path {{ font-family: monospace; font-size: 0.7rem; color: var(--muted); }}
.metadata {{ font-size: 0.7rem; color: var(--muted); margin-top: 0.25rem; }}
.media {{ max-width: 100%; border-radius: 4px; margin-top: 0.25rem; display: block; }}
.media audio {{ width: 100%; margin-top: 0.25rem; }}
.media video {{ width: 100%; max-height: 300px; background: #000; }}
.media img {{ max-width: 100%; max-height: 240px; object-fit: contain; background: rgba(0,0,0,0.5); }}
.prompt-box {{
  background: rgba(255,255,255,0.05);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0.75rem;
  font-size: 0.85rem;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
}}
.params-grid {{
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 0.25rem 0.75rem;
  font-size: 0.75rem;
}}
.param-key {{ color: var(--muted); font-weight: 600; }}
.param-val {{ font-family: monospace; word-break: break-all; }}
.warning-item {{ color: var(--warning); font-size: 0.75rem; margin: 0.25rem 0; }}
.error-item {{ color: var(--error); font-size: 0.8rem; font-weight: 600; margin: 0.5rem 0; padding: 0.5rem; background: rgba(248,81,73,0.1); border-radius: 4px; }}
.capture-gaps {{ margin-top: 0.75rem; padding-top: 0.5rem; border-top: 1px solid var(--border); }}
.gap-item {{ color: var(--warning); font-size: 0.7rem; margin: 0.15rem 0; }}
.media-placeholder {{
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(255,255,255,0.03);
  border: 1px dashed var(--border);
  border-radius: 4px;
  padding: 1rem;
  font-size: 0.75rem;
  color: var(--muted);
  min-height: 60px;
}}
.provenance {{ font-size: 0.65rem; color: var(--muted); margin-top: 0.25rem; }}
.section {{ margin-bottom: 1rem; }}
.section ul {{ list-style: none; padding-left: 0; font-size: 0.85rem; }}
.section li {{ padding: 0.25rem 0; border-bottom: 1px solid var(--border); }}
.footer {{ border-top: 1px solid var(--border); margin-top: 2rem; padding-top: 1rem; font-size: 0.75rem; color: var(--muted); }}
.features {{ display: flex; gap: 0.75rem; flex-wrap: wrap; margin-top: 0.5rem; }}
.feature-col {{ display: flex; flex-direction: column; gap: 0.2rem; }}
.feature-title {{ font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }}
.feature-chip {{ display: inline-block; font-size: 0.7rem; padding: 0.1rem 0.4rem; border-radius: 3px; font-family: monospace; background: rgba(88,166,255,0.12); }}
.feature-chip.requested {{ background: rgba(88,166,255,0.16); color: var(--accent); }}
.feature-chip.applied {{ background: rgba(63,185,80,0.16); color: var(--success); }}
.feature-chip.dropped {{ background: rgba(248,81,73,0.16); color: var(--error); }}
.recorded-decision {{ margin-top: 0.75rem; padding: 0.5rem; background: rgba(88,166,255,0.06); border-radius: 4px; font-size: 0.78rem; }}
.conclusions ul {{ list-style: none; padding-left: 0; }}
.conclusions li {{ padding: 0.35rem 0; border-bottom: 1px solid var(--border); font-size: 0.85rem; }}
</style>
</head>
<body>
<div class="header">
  <h1>{title}</h1>
  <div class="meta">ID: {experiment_id} &middot; Generated: {created} &middot; Cases: {total}</div>
  {f'<p style="margin-top:0.5rem;font-style:italic">{question}</p>' if question else ''}
  <div class="badges">{status_badges}</div>
</div>

{hypotheses_html}
{rubric_html}
{diagnostics_html}

<h2>Cases</h2>
{case_cards}

{conclusions_html}
<div class="footer">
  <p>Provider-independent experiment review &middot; Per-card provenance shows resolved run, manifest, and digest where available &middot; Unresolved cards display capture gaps honestly &middot; Generated by iteration.experiment_review</p>
</div>
</body>
</html>"""


def _render_conclusions(conclusions: Mapping[str, Any] | None) -> str:
    """Render observations, inferences, and decisions as distinct sections.

    The three record kinds are visually and structurally separated so that an
    inference (a claim with confidence/status) is never presented as an
    observed fact.  Returns an empty string when no conclusions are supplied.
    """
    if not isinstance(conclusions, Mapping):
        return ""

    def _section(
        heading: str,
        cls: str,
        items: Any,
        *,
        render,
    ) -> str:
        if not isinstance(items, list) or not items:
            return ""
        body = "".join(render(it) for it in items if isinstance(it, Mapping))
        if not body:
            return ""
        return (
            f'<div class="section conclusions"><h2>{heading}</h2>'
            f'<ul class="{cls}">{body}</ul></div>'
        )

    def _render_observation(obs: Mapping[str, Any]) -> str:
        oid = _esc(str(obs.get("id", "")))
        claim = _esc(str(obs.get("claim", "")))
        evidence = obs.get("evidence", [])
        ev_items: list[str] = []
        if isinstance(evidence, list):
            for ev in evidence:
                if isinstance(ev, Mapping):
                    ev_items.append(
                        "case="
                        + str(ev.get("case_id", "?"))
                        + " kind="
                        + str(ev.get("kind", "?"))
                        + (
                            " ref=" + str(ev.get("ref"))
                            if ev.get("ref") is not None else ""
                        )
                    )
        ev_text = _esc("; ".join(ev_items))
        return (
            f'<li><strong>{oid}</strong> '
            f'<span class="badge badge-sm badge-confirmed">observation</span>: {claim} '
            f'<span class="meta">({len(ev_items)} evidence'
            + (f": {ev_text}" if ev_text else "")
            + ")</span></li>"
        )

    def _render_inference(inf: Mapping[str, Any]) -> str:
        iid = _esc(str(inf.get("id", "")))
        claim = _esc(str(inf.get("claim", "")))
        confidence = _esc(str(inf.get("confidence", "medium")))
        status = _esc(str(inf.get("status", "provisional")))
        ev_count = len(inf.get("evidence_ids", [])) if isinstance(inf.get("evidence_ids"), list) else 0
        return (
            f'<li><strong>{iid}</strong> '
            f'<span class="badge badge-sm badge-provisional">inference</span>: {claim} '
            f'<span class="meta">(confidence {confidence} · status {status} · {ev_count} evidence)</span></li>'
        )

    def _render_decision(dec: Mapping[str, Any]) -> str:
        did = _esc(str(dec.get("id", "")))
        claim = _esc(str(dec.get("claim", "")))
        based = dec.get("based_on", [])
        based_str = _esc(", ".join(str(b) for b in based)) if isinstance(based, list) else ""
        return (
            f'<li><strong>{did}</strong> '
            f'<span class="badge badge-sm badge-partial">decision</span>: {claim}'
            + (f' <span class="meta">(based on {based_str})</span>' if based_str else "")
            + "</li>"
        )

    parts = [
        _section("Observations", "observations", conclusions.get("observations"), render=_render_observation),
        _section("Inferences", "inferences", conclusions.get("inferences"), render=_render_inference),
        _section("Decisions", "decisions", conclusions.get("decisions"), render=_render_decision),
    ]
    rendered = "".join(p for p in parts if p)
    if not rendered:
        return ""
    return '<h2>Conclusions</h2>' + rendered


def _render_diagnostics(diagnostics: Any) -> str:
    """Render duplicate/input-echo and provenance warnings prominently."""
    if not isinstance(diagnostics, Mapping):
        return ""
    rows: list[str] = []
    for warning in diagnostics.get("warnings", []):
        rows.append(f'<div class="warning-item">{_esc(str(warning))}</div>')
    for group in diagnostics.get("duplicate_output_groups", []):
        if not isinstance(group, Mapping):
            continue
        rows.append(
            '<div class="warning-item">Duplicate output '
            + _esc(str(group.get("content_hash", "")))
            + " across cases "
            + _esc(", ".join(str(v) for v in group.get("case_ids", [])))
            + "</div>"
        )
    for mismatch in diagnostics.get("source_manifest_mismatches", []):
        if isinstance(mismatch, Mapping):
            rows.append(
                '<div class="error-item">Source manifest mismatch for case '
                + _esc(str(mismatch.get("case_id", "?")))
                + "</div>"
            )
    if not rows:
        return ""
    return (
        '<div class="section diagnostics"><h2>Integrity diagnostics</h2>'
        + "".join(rows)
        + "</div>"
    )


def _render_media_tag(
    path: str,
    media_type: str | None,
    content_hash: str | None,
    *,
    is_input: bool = False,
    verified: bool = False,
    src: str | None = None,
) -> str:
    """Render an inline media element or an unresolved placeholder.

    Only produces an actual <img>, <audio>, or <video> tag when ALL of:
    - verified is True (local filesystem evidence confirmed)
    - content_hash is present (a real local SHA-256 digest)
    - media_type is a supported image/video/audio type

    ``src`` overrides the media URL used in the tag attribute while *path*
    remains the displayed run-relative location.  The session orchestrator uses
    this to point at a safe mounted route without exposing absolute paths.

    All values are HTML-escaped so media URLs cannot escape the mounted
    experiment root.
    """
    safe_path = _esc(path)
    safe_src = _esc(src) if src is not None else safe_path
    hash_str = _esc(content_hash) if content_hash else ""

    if not verified:
        # Unverified — cannot render playable media
        gap_reasons = []
        if not verified:
            gap_reasons.append("Not verified on local filesystem")
        if not content_hash:
            gap_reasons.append("No verified content hash")
        gap_text = " · ".join(gap_reasons)
        hash_line = f'<div class="hash">{hash_str}</div>' if hash_str else ""
        return (
            '<div class="media-placeholder">\n'
            f'  <span class="path">{safe_path}</span>\n'
            f'  {hash_line}\n'
            f'  <div class="gap-item">{_esc(gap_text)}</div>\n'
            '</div>'
        )

    if not content_hash:
        # Has verified flag but no actual hash — inconsistent, show placeholder
        hash_line = ""
        return (
            '<div class="media-placeholder">\n'
            f'  <span class="path">{safe_path}</span>\n'
            f'  {hash_line}\n'
            '  <div class="gap-item">No content hash — cannot render inline</div>\n'
            '</div>'
        )

    if not media_type:
        # Verified + hashed but no media type → placeholder
        hash_line = f'<div class="hash">{hash_str}</div>' if hash_str else ""
        return (
            '<div class="media-placeholder">\n'
            f'  <span class="path">{safe_path}</span>\n'
            f'  {hash_line}\n'
            '  <div class="gap-item">No media type — cannot render inline</div>\n'
            '</div>'
        )

    safe_type = _esc(media_type)

    if _is_image(media_type):
        return (
            f'<div class="media">\n'
            f'  <img src="{safe_src}" alt="{safe_path}" loading="lazy">\n'
            f'</div>'
        )
    elif _is_video(media_type):
        return (
            f'<div class="media">\n'
            f'  <video src="{safe_src}" controls preload="metadata">\n'
            f'    Your browser does not support the video tag.\n'
            f'  </video>\n'
            f'</div>'
        )
    elif _is_audio(media_type):
        return (
            f'<div class="media">\n'
            f'  <audio src="{safe_src}" controls preload="metadata">\n'
            f'    Your browser does not support the audio tag.\n'
            f'  </audio>\n'
            f'</div>'
        )
    else:
        # Known media type but not inline-renderable (e.g. application/json)
        hash_line = f'<div class="hash">{hash_str}</div>' if hash_str else ""
        return (
            '<div class="media-placeholder">\n'
            f'  <span class="path">{safe_path}</span>\n'
            f'  {hash_line}\n'
            f'  <div>{safe_type}</div>\n'
            '</div>'
        )


def _render_case_card(
    case: Mapping[str, Any],
    *,
    media_mounts: Mapping[str, str] | None = None,
    final_decision: Mapping[str, Any] | None = None,
) -> str:
    """Render a single case card in HTML."""
    case_id = _esc(str(case.get("case_id", "?")))
    run_id_raw = str(case.get("run_id", "?"))
    run_id = _esc(run_id_raw)
    label = _esc(str(case.get("label", case_id)))
    mount_prefix = ""
    if media_mounts and run_id_raw in media_mounts:
        mount_prefix = media_mounts[run_id_raw].rstrip("/")

    def _src_for(rel_path: str) -> str | None:
        if not mount_prefix:
            return None
        return f"{mount_prefix}/{quote(rel_path.lstrip('/'), safe='/')}"

    status = _esc(str(case.get("status", "draft")))
    provider = _esc(str(case.get("provider", "unknown")))
    model = _esc(str(case.get("model") or "?"))
    model_actual = _esc(str(case.get("model_actual") or ""))
    mode = _esc(str(case.get("mode") or ""))
    prompt = _esc(str(case.get("prompt") or "(no prompt recorded)"))
    prompt_capture = _esc(str(case.get("prompt_capture") or "exact-or-declared"))
    request = case.get("request")
    request_html = ""
    if isinstance(request, Mapping):
        request_html = (
            '<div class="section-title" style="margin-top:.5rem">'
            "Exact non-secret request</div><div class=\"prompt-box\">"
            + _esc(json.dumps(request, indent=2, sort_keys=True))
            + "</div>"
        )
    cost = case.get("cost_usd")
    timing = case.get("timing", {})
    duration_ms = timing.get("duration_ms") if isinstance(timing, Mapping) else None
    error_msg = _esc(str(case.get("error") or ""))
    included = case.get("included", True)

    # source_manifest provenance
    src_manifest = case.get("source_manifest", {})
    src_manifest_path = ""
    src_manifest_hash = ""
    src_manifest_verified = False
    if isinstance(src_manifest, Mapping):
        src_manifest_path = _esc(str(src_manifest.get("path", "")))
        src_manifest_hash = _esc(str(src_manifest.get("content_hash", "")))
        src_manifest_verified = src_manifest.get("verified") is True
    run_record = case.get("run_record", {})
    run_verified = (
        isinstance(run_record, Mapping) and run_record.get("verified") is True
    )

    # Status class
    status_class = f"badge-{status}"

    included_marker = "" if included else ' <span style="color:var(--muted)">[EXCLUDED]</span>'

    # Render inputs with inline media
    inputs_html = ""
    for inp in case.get("inputs", []):
        role = _esc(str(inp.get("role", "other")))
        path = str(inp.get("path", "?"))
        content_hash = inp.get("content_hash")
        media_type = inp.get("media_type")
        metadata = inp.get("metadata", {})
        meta_str = ""
        if isinstance(metadata, Mapping) and metadata:
            parts = []
            for k, v in metadata.items():
                parts.append(f"{_esc(str(k))}: {_esc(str(v))}")
            meta_str = " · ".join(parts)
        hash_str = _esc(str(content_hash)) if content_hash else ""
        verified = bool(inp.get("verified", False))
        media_html = _render_media_tag(
            path, media_type, content_hash,
            is_input=True, verified=verified, src=_src_for(path),
        )
        prov = f"run:{run_id}" + (f" · manifest:{src_manifest_path}" if src_manifest_path else "")
        if content_hash:
            prov += f" · sha256:{content_hash[7:17]}…"
        inputs_html += f"""<div class="input-entry">
  <strong>{role}</strong> <span class="path">{_esc(path)}</span>
  {f'<div class="hash">{hash_str}</div>' if hash_str else ''}
  {f'<div>{_esc(str(media_type))}</div>' if media_type else ''}
  {f'<div class="metadata">{meta_str}</div>' if meta_str else ''}
  {media_html}
  <div class="provenance">{_esc(prov)}</div>
</div>"""

    # Render outputs with inline media
    outputs_html = ""
    outputs = case.get("outputs", [])
    if outputs:
        for out in outputs:
            path = str(out.get("path", "?"))
            content_hash = out.get("content_hash")
            media_type = out.get("media_type")
            metadata = out.get("metadata", {})
            meta_str = ""
            if isinstance(metadata, Mapping) and metadata:
                parts = []
                for k, v in metadata.items():
                    parts.append(f"{_esc(str(k))}: {_esc(str(v))}")
                meta_str = " · ".join(parts)
            hash_str = _esc(str(content_hash)) if content_hash else ""
            verified = bool(out.get("verified", False))
            media_html = _render_media_tag(
                path, media_type, content_hash,
                is_input=False, verified=verified, src=_src_for(path),
            )
            prov = f"run:{run_id}" + (f" · manifest:{src_manifest_path}" if src_manifest_path else "")
            if content_hash:
                prov += f" · sha256:{content_hash[7:17]}…"
            outputs_html += f"""<div class="output-entry">
  <span class="path">{_esc(path)}</span>
  {f'<div class="hash">{hash_str}</div>' if hash_str else ''}
  {f'<div>{_esc(str(media_type))}</div>' if media_type else ''}
  {f'<div class="metadata">{meta_str}</div>' if meta_str else ''}
  {media_html}
  <div class="provenance">{_esc(prov)}</div>
</div>"""
    elif status in ("failed", "provider_rejected", "timed_out", "interrupted"):
        outputs_html = '<div class="media-placeholder">No outputs — execution did not complete</div>'
    else:
        outputs_html = '<div class="media-placeholder">No outputs recorded</div>'

    # Parameters
    params = case.get("parameters", {})
    params_html = ""
    if isinstance(params, Mapping) and params:
        params_html = '<div class="params-grid">'
        for k, v in params.items():
            params_html += f'<span class="param-key">{_esc(str(k))}</span><span class="param-val">{_esc(str(v))}</span>'
        params_html += '</div>'

    # Requested vs applied vs dropped features — three distinct columns so two
    # cases are not treated as equivalent merely because prompt text matches.
    features_html = _render_features(case)

    # Recorded rubric decision (when a review.final was supplied).
    decision_html = _render_recorded_decision(final_decision)

    # Warnings
    warnings = case.get("warnings", [])
    warnings_html = ""
    if warnings:
        for w in warnings:
            warnings_html += f'<div class="warning-item">{_esc(str(w))}</div>'

    # Error
    error_html = ""
    if error_msg:
        error_html = f'<div class="error-item">Error: {error_msg}</div>'

    # Capture gaps
    gaps = case.get("capture_gaps", [])
    gaps_html = ""
    if gaps:
        gaps_html = '<div class="capture-gaps"><div class="section-title">Capture Gaps</div>'
        for g in gaps:
            kind = _esc(str(g.get("kind", "unknown")))
            detail = _esc(str(g.get("detail", "")))
            gaps_html += f'<div class="gap-item">[{kind}] {detail}</div>'
        gaps_html += '</div>'

    # Timing line
    timing_str = ""
    if duration_ms is not None:
        timing_str = f" · {duration_ms}ms"
    if cost is not None:
        timing_str += " · $" + f"{cost:.4f}"

    # Provider line
    provider_line = f"{provider}"
    if model:
        provider_line += f" / {model}"
    if model_actual and model_actual != model:
        provider_line += f" ({model_actual})"
    if mode:
        provider_line += f" · {mode}"

    # Per-card provenance indicator
    has_verified_output_hash = any(
        out.get("verified") is True
        and isinstance(out.get("content_hash"), str)
        and is_valid_content_hash(out["content_hash"])
        for out in outputs
        if isinstance(out, Mapping)
    ) if outputs else False
    prov_note = ""
    if src_manifest_path:
        if run_verified and src_manifest_verified and has_verified_output_hash:
            prov_note = '<div class="provenance">✓ run · manifest · SHA-256</div>'
        elif src_manifest_hash and src_manifest_verified:
            prov_note = (
                '<div class="provenance">manifest digest verified; '
                'run record or output hash incomplete</div>'
            )
        else:
            prov_note = '<div class="provenance">run / manifest provenance unresolved</div>'
    else:
        prov_note = f'<div class="provenance">run: {run_id} · unresolved</div>'

    return f"""<div class="card">
  <div class="card-header">
    <div>
      <span class="badge {status_class}">{status.replace('_', ' ')}</span>
      <span class="card-label">{label}</span>{included_marker}
    </div>
    <div class="card-id">{case_id}<br>run: {run_id}</div>
  </div>
  <div class="meta" style="margin-bottom:0.75rem">{provider_line}{timing_str}</div>

  {error_html}

  <div class="card-grid">
    <div>
      <div class="section-title">Inputs</div>
      {inputs_html if inputs_html else '<div class="media-placeholder">No inputs recorded</div>'}
    </div>
    <div>
      <div class="section-title">Prompt &amp; Parameters</div>
      <div class="prompt-box">{prompt}</div>
      <div class="metadata">Prompt capture: {prompt_capture}</div>
      {request_html}
      {params_html}
      {features_html}
      {warnings_html}
    </div>
    <div>
      <div class="section-title">Outputs</div>
      {outputs_html}
    </div>
  </div>

  {decision_html}
  {prov_note}
  {gaps_html}
</div>"""


def _render_features(case: Mapping[str, Any]) -> str:
    """Render requested / applied / dropped feature columns distinctly."""
    def _col(title: str, cls: str, items: Any) -> str:
        if not isinstance(items, list) or not items:
            return ""
        chips = "".join(
            f'<span class="feature-chip {cls}">{_esc(str(it))}</span>'
            for it in items
        )
        return f'<div class="feature-col"><span class="feature-title">{title}</span>{chips}</div>'

    cols = [
        _col("Requested", "requested", case.get("requested_features")),
        _col("Applied", "applied", case.get("applied_features")),
        _col("Dropped", "dropped", case.get("dropped_features")),
    ]
    body = "".join(c for c in cols if c)
    if not body:
        return ""
    return f'<div class="features">{body}</div>'


def _render_recorded_decision(decision: Mapping[str, Any] | None) -> str:
    """Render a previously recorded rubric decision under the case card."""
    if not isinstance(decision, Mapping):
        return ""
    scores = decision.get("scores")
    verdict = decision.get("verdict")
    notes = decision.get("notes")
    reviewer = decision.get("reviewer")
    parts: list[str] = []
    if isinstance(scores, Mapping) and scores:
        rendered = ", ".join(
            f"{_esc(str(k))}={_esc(str(v))}" for k, v in scores.items()
        )
        parts.append(f"scores: {rendered}")
    if isinstance(verdict, str) and verdict:
        parts.append(f"verdict: {_esc(verdict)}")
    if isinstance(notes, str) and notes.strip():
        parts.append(f"notes: {_esc(notes)}")
    if isinstance(reviewer, Mapping):
        rid = reviewer.get("id")
        rtype = reviewer.get("type")
        if isinstance(rid, str):
            parts.append(f"by {_esc(str(rtype))}:{_esc(str(rid))}")
    if not parts:
        return ""
    return (
        '<div class="recorded-decision"><span class="section-title">'
        "Recorded decision</span>"
        + " · ".join(parts)
        + "</div>"
    )


def _esc(text: str) -> str:
    """HTML-escape a string, preventing XSS from untrusted provider text."""
    return html.escape(text, quote=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
