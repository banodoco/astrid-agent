"""experiment_review_session — interactive rubric review over a prepared review.

Composes:

  experiment_prepare → experiment_review (interactive shell) → editorial.human_review

Reuses ``editorial.human_review`` as the review server (authentication, static
media mounts with HTTP Range support, schema-validated ``/submit``).  No second
web server is forked.

The action contributes:

- A safe, deterministic mounted-media mapping so browser playback resolves each
  run artifact without copying large media, exposing absolute paths, or
  permitting traversal/symlink escape.  Mounts are configured at server-launch
  time only; persisted artifacts record ``run_id → prefix`` (relative), never
  absolute paths.
- A generated JSON Schema that constrains the final rubric payload.
- A self-contained interactive HTML page that renders cases, plays mounted
  media, persists rubric drafts to the server (``review.state.json`` via
  ``/state.json`` + versioned ``/save``), survives a browser reload, and
  submits a schema-validated final payload. ``localStorage`` is only a
  resilience fallback.
- Final validation of ``review.final.json`` against the experiment rubric.

Static rendering stays provider-agnostic: the review shell never branches on
provider; only the safe mount prefix is added around run-relative paths.
"""

from __future__ import annotations

from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("iteration.experiment_review_session")
import argparse  # noqa: E402
import hashlib  # noqa: E402
import html as html_mod  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Mapping  # noqa: E402

from astrid.pack_api import write_manifest  # noqa: E402
from astrid.pack_api import build_rubric_response_schema, validate_conclusions, validate_review_final
from astrid.pack_api import ExperimentValidationError, validate_experiment
from astrid.pack_api import init_experiment_review_state
from astrid.pack_api import run_subprocess  # noqa: E402
from astrid import sdk  # noqa: E402

_REVIEW_CHILD_KEY = "experiment-human-review"
_REVIEW_WAIT_SECONDS = 30.0


def _esc(text: str) -> str:
    return html_mod.escape(text, quote=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run an interactive, schema-validated experiment review session."
    )
    p.add_argument("--experiment", type=Path, required=True, help="Path to experiment.json.")
    p.add_argument("--runs-dir", type=Path, required=True, help="Directory containing project runs.")
    p.add_argument("--out", type=Path, required=True, help="Session output directory.")
    p.add_argument("--reviewer-id", default="reviewer", help="Reviewer id for the final payload.")
    p.add_argument("--reviewer-type", default="human", help="Reviewer type (human/agent).")
    p.add_argument("--conclusions", type=Path, default=None, help="Optional conclusions.json to display.")
    p.add_argument("--port", type=int, default=0, help="Server port (0 = auto).")
    p.add_argument("--timeout", type=int, default=0, help="Exit after N seconds without submit (0=unlimited).")
    p.add_argument("--no-open", action="store_true", help="Do not auto-open the browser.")
    p.add_argument(
        "--skip-server",
        action="store_true",
        help="Build all session artifacts and exit without launching the review server.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        out_dir = args.out.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        # 1. Prepare the normalized review (subprocess, like every composed action).
        prepare_out = out_dir / "prepare"
        _run_prepare(args.experiment.resolve(), args.runs_dir.resolve(), prepare_out)
        review_path = prepare_out / "review.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        # Publish a bounded root filename, retaining prepare's local artifact.
        _atomic_bytes(out_dir / "review.json", review_path.read_bytes())

        # 2. Build session artifacts (mounts, schema, data, html, media map).
        experiment = _load_experiment(args.experiment.resolve())
        case_ids = [
            str(c.get("case_id"))
            for c in review.get("cases", [])
            if isinstance(c, Mapping) and c.get("included", True)
        ]
        if not case_ids:
            # Nothing to review.  The rubric schema/runtime library permits an
            # empty review (exactly zero decisions), but an interactive session
            # over zero cases has no useful action — fail early and actionably.
            raise AstridError(
                f"experiment {experiment.get('experiment_id')!r} has no included cases to review",
                recovery_command=(
                    "mark at least one case included:true in the experiment, or import "
                    "submissions that produce included cases before starting a review session"
                ),
            )
        mounts, media_map = _resolve_media_mounts(review, args.runs_dir.resolve())
        schema = build_rubric_response_schema(experiment, case_ids=case_ids)
        conclusions = _read_optional_json(args.conclusions)
        if conclusions is not None:
            # Identity gate: a conclusions artifact bound to another experiment
            # is never embedded in this session's data payload.
            try:
                conclusions = validate_conclusions(
                    conclusions, experiment=experiment, case_ids=case_ids
                )
            except ExperimentValidationError as exc:
                raise AstridError(
                    f"--conclusions artifact is invalid or bound to another experiment: {exc}",
                    recovery_command=(
                        "supply a conclusions.json whose experiment_id matches the experiment"
                    ),
                ) from exc

        _write_json(out_dir / "response_schema.json", schema)
        _write_json(out_dir / "media_map.json", {
            "schema_version": 1,
            "experiment_id": review.get("experiment_id"),
            "media_mounts": media_map,
            "note": "run_id -> URL prefix; absolute run dirs are configured at server launch only",
        })
        data_payload = _build_data_payload(
            review=review,
            experiment=experiment,
            case_ids=case_ids,
            media_map=media_map,
            schema=schema,
            reviewer_id=args.reviewer_id,
            reviewer_type=args.reviewer_type,
            conclusions=conclusions,
        )
        _write_json(out_dir / "data.json", data_payload)
        (out_dir / "review_session.html").write_text(
            _build_session_html(data_payload), encoding="utf-8"
        )

        # Initialize the durable, versioned draft state.  Idempotent: a re-run
        # over the same out dir preserves an in-flight review's draft.
        state_path = out_dir / "review.state.json"
        init_experiment_review_state(state_path, str(review.get("experiment_id", "")))

        # 3. Optionally stop after artifact generation (useful for tests/CI).
        if args.skip_server:
            _write_orchestrator_manifest(out_dir, experiment, finalized=False)
            print(json.dumps({
                "review": str(out_dir / "review.json"),
                "session_html": str(out_dir / "review_session.html"),
                "data": str(out_dir / "data.json"),
                "response_schema": str(out_dir / "response_schema.json"),
                "media_map": str(out_dir / "media_map.json"),
                "state": str(state_path),
                "skipped_server": True,
            }, sort_keys=True))
            return 0

        # 4. Launch editorial.human_review (blocks until /submit or timeout).
        final_path = out_dir / "review.final.json"
        _run_human_review(
            html_path=out_dir / "review_session.html",
            data_path=out_dir / "data.json",
            schema_path=out_dir / "response_schema.json",
            state_path=state_path,
            out_path=final_path,
            mounts=mounts,
            port=args.port,
            timeout=args.timeout,
            no_open=args.no_open,
        )

        # 5. Validate the final payload against the experiment rubric.
        if final_path.is_file():
            payload = json.loads(final_path.read_text(encoding="utf-8"))
            try:
                validated = validate_review_final(
                    payload, experiment=experiment, case_ids=case_ids
                )
            except ExperimentValidationError as exc:
                raise AstridError(
                    f"submitted review.final.json failed rubric validation: {exc}",
                    recovery_command="correct the submission and resubmit, or widen the rubric",
                ) from exc
            _write_json(out_dir / "review.final.validated.json", validated)
        _write_orchestrator_manifest(out_dir, experiment, finalized=final_path.is_file())
        return 0

    return run_pack_main("iteration.experiment_review_session", _run, argv=argv)


# ── step helpers ───────────────────────────────────────────────────────────


def _run_prepare(experiment: Path, runs_dir: Path, out: Path) -> None:
    cmd = [
        sys.executable, "-X", "utf8", "-m",
        "astrid.packs.iteration.actions.experiment_prepare.run",
        "--experiment", str(experiment),
        "--runs-dir", str(runs_dir),
        "--out", str(out),
    ]
    run_subprocess(cmd, label="experiment_prepare", orchestrator="experiment_review_session")


def _run_human_review(
    *,
    html_path: Path,
    data_path: Path,
    schema_path: Path,
    state_path: Path,
    out_path: Path,
    mounts: dict[str, Path],
    port: int,
    timeout: int,
    no_open: bool,
) -> None:
    root = out_path.parent.resolve()
    # Snapshot the small generated files through the public producer boundary.
    # Direct media mounts stay on the admitted machine: Runtime child target
    # inheritance verifies placement before execution. Media is never copied.
    inputs = {
        "html": _producer_file(html_path, root, "session_html", "text/html"),
        "data": _producer_file(data_path, root, "data", "application/json"),
        "response_schema": _producer_file(schema_path, root, "response_schema", "application/json"),
        "state": _producer_file(state_path, root, "state", "application/json"),
        "port": port,
        "timeout": timeout,
        "no_open": no_open,
    }
    if mounts:
        inputs["serve"] = [f"{prefix}={directory}" for prefix, directory in mounts.items()]
    receipt_path = root / "human_review.child.json"
    previous_identity = None
    waits = 0
    while True:
        waits += 1
        try:
            child = sdk.invoke(
                "editorial.human_review", kind="action", inputs=inputs,
                child_key=_REVIEW_CHILD_KEY, wait=True,
                timeout_seconds=_REVIEW_WAIT_SECONDS if timeout == 0 else max(60.0, timeout + 60.0),
                poll_seconds=0.1,
            )
        except Exception as exc:
            _write_json(receipt_path, {
                "capability_id": "editorial.human_review", "child_key": _REVIEW_CHILD_KEY,
                "identity": previous_identity, "waits": waits,
                "ok": False, "error": str(exc),
            })
            raise
        identity = {
            "run_id": child.kernel_run_id, "task_id": child.kernel_task_id,
            "attempt_id": child.kernel_attempt_id,
        }
        _write_json(receipt_path, {
            "capability_id": "editorial.human_review", "child_key": _REVIEW_CHILD_KEY,
            **identity, "waits": waits, "ok": child.ok,
            "previous_identity": previous_identity,
            "state": child.raw_result.get("state", "unknown"),
            "outputs": child.outputs.get("managed_outputs", []), "error": child.error,
        })
        # A wait window never restarts the child or resets its host deadline.
        # The host owns lease-bound interactive lifetime and recovery snapshots.
        if previous_identity is not None and any(
            previous_identity[name] and identity[name] and previous_identity[name] != identity[name]
            for name in ("run_id", "task_id", "attempt_id")
        ):
            raise AstridError("Human Review wait changed the admitted child identity")
        previous_identity = {
            name: identity[name] or (previous_identity or {}).get(name)
            for name in identity
        }
        if not child.ok:
            if timeout == 0 and (child.error or {}).get("code") == "task_wait_timeout":
                if not identity["run_id"] or not identity["task_id"]:
                    raise AstridError("Human Review wait timeout has no admitted child identity")
                continue
            raise AstridError(
                f"Human Review child did not complete: task={child.kernel_task_id}, "
                f"state={child.raw_result.get('state', 'unknown')}, error={child.error}",
            )
        if child.raw_result.get("state") != "completed":
            raise AstridError("Human Review child has no completed settlement")
        break

    rows = child.outputs.get("managed_outputs", [])
    selected = {}
    for name in ("decisions", "state_result"):
        matches = [row for row in rows if row.get("output_port") == name]
        if len(matches) != 1:
            raise AstridError(f"Human Review child must return exactly one {name} output")
        selected[name] = matches[0]
    # Materialize only final, settled outputs through the originating SDK
    # binding; intermediate snapshots are recovered by the accepted driver.
    payloads = {}
    for name, row in selected.items():
        local = child.materialize_output(row["association_id"])
        if dict(local.output) != row:
            raise AstridError("Human Review materialized output identity changed")
        source = _contained_file(root, root / local.filename)
        payload = source.read_bytes()
        if len(payload) != row["size"] or "sha256:" + hashlib.sha256(payload).hexdigest() != row["digest"]:
            raise AstridError("Human Review materialized bytes disagree with the settled receipt")
        payloads[name] = payload
    _atomic_bytes(out_path, payloads["decisions"])
    _atomic_bytes(state_path, payloads["state_result"])


def _contained_file(root: Path, path: Path) -> Path:
    source = path.resolve(strict=True)
    if not source.is_file() or not source.is_relative_to(root.resolve()):
        raise AstridError("Human Review file escapes the parent output root")
    return source


def _producer_file(path: Path, root: Path, port: str, media_type: str) -> dict[str, str]:
    source = _contained_file(root, path)
    return {"filename": source.relative_to(root).as_posix(), "output_port": port, "media_type": media_type}


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".review-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _load_experiment(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AstridError(
            f"cannot read experiment file: {path}: {exc}",
            recovery_command="verify the --experiment path points to a valid experiment.json",
        ) from exc
    try:
        return validate_experiment(data)
    except ExperimentValidationError as exc:
        raise AstridError(
            f"invalid experiment definition: {exc}",
            recovery_command="check the experiment.json against the experiment contract",
        ) from exc


def _resolve_media_mounts(
    review: Mapping[str, Any],
    runs_dir: Path,
) -> tuple[dict[str, Path], dict[str, str]]:
    """Resolve safe per-run media mounts.

    Returns ``(server_mounts, persisted_map)``:

    - ``server_mounts`` maps URL prefix → absolute run dir, used only at
      server-launch time.
    - ``persisted_map`` maps ``run_id → prefix`` (no absolute paths) and is
      safe to commit.

    Only run ids whose resolved directories actually exist inside the resolved
    runs root are mounted.  The mount-root check here complements the
    human_review static handler, which containment-checks every request below
    an accepted mount.
    """
    server_mounts: dict[str, Path] = {}
    persisted: dict[str, str] = {}
    resolved_runs_dir = runs_dir.resolve()
    for case in review.get("cases", []):
        if not isinstance(case, Mapping):
            continue
        run_id = str(case.get("run_id", ""))
        if not run_id or run_id in persisted:
            continue
        run_dir = (resolved_runs_dir / run_id).resolve()
        try:
            run_dir.relative_to(resolved_runs_dir)
        except ValueError:
            continue
        if not run_dir.is_dir():
            continue
        prefix = f"/media/{run_id}"
        server_mounts[prefix] = run_dir
        persisted[run_id] = prefix
    return server_mounts, persisted


def _build_data_payload(
    *,
    review: Mapping[str, Any],
    experiment: Mapping[str, Any],
    case_ids: list[str],
    media_map: Mapping[str, str],
    schema: Mapping[str, Any],
    reviewer_id: str,
    reviewer_type: str,
    conclusions: Mapping[str, Any] | None,
) -> dict[str, Any]:
    # Slim each case to what the client needs (no secret material).
    slim_cases: list[dict[str, Any]] = []
    for case in review.get("cases", []):
        if not isinstance(case, Mapping):
            continue
        run_id = str(case.get("run_id", ""))
        slim_cases.append({
            "case_id": case.get("case_id"),
            "run_id": run_id,
            "label": case.get("label", case.get("case_id")),
            "status": case.get("status", "draft"),
            "provider": case.get("provider", "unknown"),
            "model": case.get("model"),
            "prompt": case.get("prompt"),
            "prompt_capture": case.get("prompt_capture"),
            "request": case.get("request"),
            "parameters": case.get("parameters", {}),
            "warnings": case.get("warnings", []),
            "error": case.get("error"),
            "capture_gaps": case.get("capture_gaps", []),
            "source_manifest": case.get("source_manifest", {}),
            "run_record": case.get("run_record", {}),
            "included": case.get("included", True),
            "inputs": [
                {
                    "ordinal": inp.get("ordinal"),
                    "role": inp.get("role"),
                    "path": inp.get("path"),
                    "media_type": inp.get("media_type"),
                    "content_hash": inp.get("content_hash"),
                    "verified": bool(inp.get("verified", False)),
                }
                for inp in case.get("inputs", []) if isinstance(inp, Mapping)
            ],
            "outputs": [
                {
                    "path": out.get("path"),
                    "media_type": out.get("media_type"),
                    "content_hash": out.get("content_hash"),
                    "verified": bool(out.get("verified", False)),
                }
                for out in case.get("outputs", []) if isinstance(out, Mapping)
            ],
            "media_prefix": media_map.get(run_id, ""),
        })
    return {
        "schema_version": 1,
        "experiment_id": review.get("experiment_id"),
        "title": review.get("title", review.get("experiment_id")),
        "question": review.get("question", ""),
        "rubric": list(experiment.get("rubric", [])),
        # Initial draft state version; the live version is tracked from /state.json.
        "state_version": 0,
        "reviewer": {"type": reviewer_type, "id": reviewer_id},
        "case_ids": case_ids,
        "media_mounts": dict(media_map),
        "cases": slim_cases,
        "conclusions": dict(conclusions) if isinstance(conclusions, Mapping) else {},
    }


# ── interactive HTML ───────────────────────────────────────────────────────


def _build_session_html(data: Mapping[str, Any]) -> str:
    """Return a deterministic, self-contained interactive review page.

    The page reads ``/data.json`` at runtime, renders case cards with media
    served from the safe ``/media/<run_id>/`` mounts, and persists rubric
    drafts to the **server** via ``/state.json`` + ``/save`` (the canonical
    store), surviving a browser reload.  ``localStorage`` is kept only as a
    resilience fallback for when the server state is unavailable.  Autosaves
    carry ``base_state_version``; a stale-version ``409`` response triggers a
    server-state reload and a single retry against the new version (local
    unsaved edits are merged on top).  The final payload is posted to
    ``/submit`` for schema validation.
    """
    title = _esc(str(data.get("title", "Experiment Review")))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Review Session</title>
<style>
:root {{--bg:#0d1117;--fg:#c9d1d9;--border:#30363d;--card:#161b22;--accent:#58a6ff;--success:#3fb950;--warning:#d2991d;--error:#f85149;--muted:#8b949e;}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--fg);line-height:1.5;padding:1.5rem;max-width:1200px;margin:0 auto;}}
h1{{font-size:1.5rem;margin-bottom:.25rem;}}
.meta{{color:var(--muted);font-size:.85rem;margin-bottom:1rem;}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:1rem;margin-bottom:1rem;}}
.card-head{{display:flex;justify-content:space-between;gap:.5rem;flex-wrap:wrap;margin-bottom:.5rem;}}
.badge{{display:inline-block;padding:.15rem .5rem;border-radius:999px;font-size:.7rem;font-weight:600;text-transform:uppercase;}}
.badge-completed{{background:var(--success);color:#000;}} .badge-failed,.badge-provider_rejected{{background:var(--error);color:#fff;}}
.badge-partial{{background:var(--warning);color:#000;}} .badge-timed_out,.badge-interrupted{{background:#6e3b1a;color:#fff;}} .badge-draft{{background:var(--muted);color:#000;}}
.prompt{{background:rgba(255,255,255,.05);border:1px solid var(--border);border-radius:4px;padding:.5rem;font-size:.8rem;white-space:pre-wrap;margin:.5rem 0;}}
.media{{margin:.5rem 0;}} .media video{{max-width:100%;max-height:300px;background:#000;}} .media img{{max-width:100%;max-height:240px;}} .media audio{{width:100%;}}
.placeholder{{font-size:.75rem;color:var(--muted);border:1px dashed var(--border);border-radius:4px;padding:.5rem;margin:.5rem 0;}}
.rubric-row{{display:flex;align-items:center;gap:.5rem;margin:.25rem 0;flex-wrap:wrap;}}
.rubric-row label{{min-width:160px;font-size:.85rem;}}
.rubric-row input[type=number]{{width:5rem;}}
.rubric-row input[type=range]{{flex:1;min-width:120px;}}
.verdict{{margin:.5rem 0;}} .verdict input{{width:100%;padding:.3rem;background:var(--card);border:1px solid var(--border);color:var(--fg);border-radius:4px;}}
.notes textarea{{width:100%;height:3rem;padding:.3rem;background:var(--card);border:1px solid var(--border);color:var(--fg);border-radius:4px;}}
.draft-status{{font-size:.7rem;color:var(--muted);min-height:1em;}}
.submit-bar{{position:sticky;bottom:0;background:var(--bg);padding:.75rem 0;border-top:1px solid var(--border);margin-top:1rem;display:flex;gap:.5rem;align-items:center;}}
button{{background:var(--accent);color:#000;border:none;border-radius:4px;padding:.5rem 1rem;font-weight:600;cursor:pointer;}}
button:disabled{{opacity:.5;cursor:not-allowed;}}
.saved{{color:var(--success);}}
</style>
</head>
<body>
<h1 id="title">Loading…</h1>
<div class="meta" id="meta"></div>
<div id="cards"></div>
<div class="submit-bar">
  <span class="draft-status" id="draft-status"></span>
  <button id="submit" disabled>Submit final review</button>
</div>
<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
let DATA = null;
let draft = {{}};
let serverStateVersion = 0;   // tracked from /state.json and /save responses
let saveInFlight = false;
let saveQueued = false;
let saveTimer = null;

function storageKey() {{
  return 'astrid-review:' + DATA.experiment_id;
}}
function loadLocalDraft() {{
  try {{ return JSON.parse(localStorage.getItem(storageKey()) || '{{}}') || {{}}; }}
  catch(e) {{ return {{}}; }}
}}
function persistLocalDraft() {{
  try {{ localStorage.setItem(storageKey(), JSON.stringify(draft)); }} catch(e) {{}}
}}
function setStatus(text, ok) {{
  const el = document.getElementById('draft-status');
  el.textContent = text;
  el.className = ok ? 'draft-status saved' : 'draft-status';
}}
// Canonical store: server-persisted /state.json. Returns null on any failure
// so the caller falls back to localStorage rather than blocking the review.
async function fetchState() {{
  try {{
    const res = await fetch('/state.json?token=' + encodeURIComponent(TOKEN),
      {{headers: {{'X-Session-Token': TOKEN}}}});
    if (!res.ok) return null;
    const st = await res.json();
    if (st && typeof st.state_version === 'number') return st;
    return null;
  }} catch(e) {{ return null; }}
}}
async function postSave(baseVersion) {{
  const res = await fetch('/save?token=' + encodeURIComponent(TOKEN), {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json', 'X-Session-Token': TOKEN}},
    body: JSON.stringify({{base_state_version: baseVersion, experiment_id: DATA.experiment_id, draft: draft}}),
  }});
  return res;
}}
// Debounced autosave: one save in flight at a time, follow-ups coalesced.
function scheduleSave() {{
  if (saveTimer) clearTimeout(saveTimer);
  setStatus('saving…', false);
  saveTimer = setTimeout(() => {{
    saveTimer = null;
    runSave();
  }}, 400);
}}
async function runSave() {{
  if (saveInFlight) {{ saveQueued = true; return; }}
  saveInFlight = true;
  try {{
    let base = serverStateVersion;
    let res = await postSave(base);
    if (res.status === 409) {{
      // Stale base: adopt the canonical server state, overlay local unsaved
      // edits, then retry once against the new version.
      const st = await fetchState();
      if (st) {{
        serverStateVersion = st.state_version;
        draft = Object.assign({{}}, st.draft || {{}}, draft);
        persistLocalDraft();
        syncInputsFromDraft();
        setStatus('merged stale draft — retrying', false);
        res = await postSave(serverStateVersion);
      }}
    }}
    if (res.status === 200) {{
      const data = await res.json();
      serverStateVersion = data.state_version;
      setStatus('saved', true);
      return;
    }}
    if (res.status === 409) {{
      setStatus('stale draft — could not save (reload to retry)', false);
      return;
    }}
    const txt = await res.text().catch(() => '');
    setStatus('save failed: ' + res.status + ' ' + txt, false);
  }} catch(e) {{
    setStatus('save failed: ' + e, false);
  }} finally {{
    saveInFlight = false;
    if (saveQueued) {{ saveQueued = false; runSave(); }}
  }}
}}
function syncInputsFromDraft() {{
  document.querySelectorAll('[data-key]').forEach(el => {{
    const v = draft[el.dataset.key];
    if (v != null) el.value = v;
  }});
}}
function mediaTag(c, entry, kind) {{
  const esc = (s) => String(s).replace(/[&<>"']/g, (ch) => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
  if (!entry.verified) return '<div class="placeholder">' + esc(entry.path||'') + ' — not verified locally</div>';
  const mt = entry.media_type || '';
  const prefix = c.media_prefix || '';
  const encodedPath = String(entry.path || '').split('/').map(encodeURIComponent).join('/');
  const src = prefix && entry.path ? (prefix + '/' + encodedPath) : encodedPath;
  const safe = esc(src);
  if (mt.startsWith('image/')) return '<div class="media"><img src="'+safe+'" loading="lazy"></div>';
  if (mt.startsWith('video/')) return '<div class="media"><video src="'+safe+'" controls preload="metadata"></video></div>';
  if (mt.startsWith('audio/')) return '<div class="media"><audio src="'+safe+'" controls preload="metadata"></audio></div>';
  return '<div class="placeholder">'+esc(entry.path||'')+' · '+esc(mt)+'</div>';
}}
function renderCase(c) {{
  const esc = (s) => String(s).replace(/[&<>"']/g, (ch) => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
  const inputs = (c.inputs||[]).map(i => mediaTag(c, i, 'input')).join('');
  const outputs = (c.outputs||[]).map(o => mediaTag(c, o, 'output')).join('');
  const rubric = (DATA.rubric||[]).map(r => {{
    const min = (r.scale&&r.scale.min!=null)?r.scale.min:0;
    const max = (r.scale&&r.scale.max!=null)?r.scale.max:5;
    const key = c.case_id+'.'+r.id;
    const val = (draft[key]!=null)?draft[key]:'';
    return '<div class="rubric-row"><label>'+esc(r.label||r.id)+' ('+min+'–'+max+')</label>'
      + '<input type="range" min="'+min+'" max="'+max+'" step="1" data-key="'+esc(key)+'" value="'+(val!==''?val:min)+'">'
      + '<input type="number" min="'+min+'" max="'+max+'" step="1" data-key="'+esc(key)+'" value="'+(val!==''?val:'')+'"></div>';
  }}).join('');
  const verdictKey = c.case_id+'.verdict';
  const notesKey = c.case_id+'.notes';
  const prov = c.source_manifest || {{}};
  const hashes = (c.outputs||[]).map(o => o.content_hash).filter(Boolean).join(', ');
  const gaps = (c.capture_gaps||[]).map(g => '['+esc(g.kind||'unknown')+'] '+esc(g.detail||'')).join('<br>');
  return '<div class="card">'
    + '<div class="card-head"><div><span class="badge badge-'+esc(c.status)+'">'+esc(c.status)+'</span> '
    + '<strong>'+esc(c.label||c.case_id)+'</strong></div>'
    + '<div style="font-family:monospace;font-size:.7rem;color:var(--muted)">'+esc(c.case_id)+'</div></div>'
    + '<div class="meta">'+esc(c.provider||'')+(c.model?(' / '+esc(c.model)):'')+'</div>'
    + (c.prompt?('<div class="prompt">'+esc(c.prompt)+'</div>'):'')
    + '<div class="meta">prompt capture: '+esc(c.prompt_capture||'exact-or-declared')+'</div>'
    + (c.error?('<div class="placeholder" style="color:var(--error)">Error: '+esc(c.error)+'</div>'):'')
    + (inputs?('<div><div style="font-size:.7rem;color:var(--muted);text-transform:uppercase">Inputs</div>'+inputs+'</div>'):'')
    + (outputs?('<div><div style="font-size:.7rem;color:var(--muted);text-transform:uppercase;margin-top:.5rem">Outputs</div>'+outputs+'</div>'):'')
    + '<div class="meta">run: '+esc(c.run_id)+' · manifest: '+esc(prov.path||'unresolved')
    + (prov.content_hash?(' · '+esc(prov.content_hash)):'')+(hashes?(' · outputs: '+esc(hashes)):'')+'</div>'
    + (gaps?('<div class="placeholder">'+gaps+'</div>'):'')
    + '<div style="margin-top:.5rem"><div style="font-size:.7rem;color:var(--muted);text-transform:uppercase">Rubric</div>'+rubric+'</div>'
    + '<div class="verdict"><input type="text" data-key="'+esc(verdictKey)+'" placeholder="verdict (e.g. iterate)" value="'+esc(draft[verdictKey]||'')+'"></div>'
    + '<div class="notes"><textarea data-key="'+esc(notesKey)+'" placeholder="notes">'+esc(draft[notesKey]||'')+'</textarea></div>'
    + '</div>';
}}
function bindInputs() {{
  document.querySelectorAll('[data-key]').forEach(el => {{
    el.addEventListener('input', () => {{
      draft[el.dataset.key] = el.value;
      persistLocalDraft();   // resilience fallback (not canonical)
      scheduleSave();
    }});
  }});
}}
function buildPayload() {{
  const decisions = DATA.cases.filter(c => c.included !== false).map(c => {{
    const scores = {{}};
    (DATA.rubric||[]).forEach(r => {{
      const raw = draft[c.case_id+'.'+r.id];
      const n = raw!==''&&raw!=null?parseInt(raw,10):NaN;
      scores[r.id] = isNaN(n)?((r.scale&&r.scale.min)||0):n;
    }});
    return {{
      case_id: c.case_id,
      scores: scores,
      verdict: draft[c.case_id+'.verdict'] || 'unspecified',
      notes: draft[c.case_id+'.notes'] || '',
      created: new Date().toISOString(),
    }};
  }});
  return {{
    schema_version: 1,
    experiment_id: DATA.experiment_id,
    reviewer: DATA.reviewer,
    decisions: decisions,
  }};
}}
async function fetchData() {{
  const res = await fetch('/data.json');
  DATA = await res.json();
  // Load the canonical server draft first; fall back to localStorage only if
  // the server state is unavailable.
  const st = await fetchState();
  if (st) {{
    serverStateVersion = st.state_version;
    draft = st.draft || {{}};
  }} else {{
    draft = loadLocalDraft();
    setStatus('server state unavailable — using local draft', false);
  }}
  persistLocalDraft();
  document.getElementById('title').textContent = DATA.title || DATA.experiment_id;
  document.getElementById('meta').textContent = DATA.question ? (DATA.question + ' — ') : '';
  document.getElementById('cards').innerHTML = DATA.cases.map(renderCase).join('');
  bindInputs();
  document.getElementById('submit').disabled = false;
  if (st) setStatus('saved', true);
}}
document.getElementById('submit').addEventListener('click', async () => {{
  const payload = buildPayload();
  const res = await fetch('/submit?token=' + encodeURIComponent(TOKEN), {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json', 'X-Session-Token': TOKEN}},
    body: JSON.stringify(payload),
  }});
  if (res.status === 204) {{
    document.getElementById('submit').textContent = 'Submitted ✓';
    document.getElementById('submit').disabled = true;
  }} else {{
    const txt = await res.text();
    document.getElementById('draft-status').textContent = 'submit failed: ' + res.status + ' ' + txt;
    document.getElementById('draft-status').className = 'draft-status';
  }}
}});
fetchData().catch(e => {{
  document.getElementById('cards').textContent = 'Failed to load /data.json: ' + e;
}});
</script>
</body>
</html>"""


# ── small utils ────────────────────────────────────────────────────────────


def _read_optional_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    p = path.resolve()
    if not p.is_file():
        raise AstridError(f"optional JSON file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AstridError(f"cannot read optional JSON file {p}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise AstridError(f"optional JSON file must contain an object: {p}")
    return dict(data)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_orchestrator_manifest(out_dir: Path, experiment: Mapping[str, Any], *, finalized: bool) -> None:
    outputs = [
        {"name": "review", "path": "review.json", "type": "file"},
        {"name": "data", "path": "data.json", "type": "file"},
        {"name": "session_html", "path": "review_session.html", "type": "file"},
        {"name": "response_schema", "path": "response_schema.json", "type": "file"},
        {"name": "media_map", "path": "media_map.json", "type": "file"},
        {"name": "state", "path": "review.state.json", "type": "file"},
    ]
    if finalized:
        outputs.append({"name": "validated_final", "path": "review.final.validated.json", "type": "file"})
    write_manifest(out_dir / "manifest.json", {
        "schema_version": 1,
        "kind": "experiment_review_session",
        "inputs": {"experiment": str(experiment.get("experiment_id", ""))},
        "outputs": outputs,
        "created": experiment.get("created", "1970-01-01T00:00:00Z"),
        "warnings": [],
    })


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
