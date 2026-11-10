"""T7.4 — pinned replay and drift acknowledgement for captured render bundles.

The ``replay`` verb re-runs a captured bundle's pinned backend command with
its localized inputs, pinning the qualified renderer id, the manifest digest,
and the request digest:

* a fresh bundle replays and reproduces the deterministic output;
* manifest-digest drift (silent backend substitution) is refused without
  ``--acknowledge-drift``;
* acknowledged drift proceeds and produces the expected output (PROOF);
* a tampered request is refused by its digest mismatch;
* a corrected bundle input is drift until acknowledged;
* the route reports the pinned ids/digests in its output.

The replay route reuses the frozen protocol: the same transport command with
the same request/result JSON shapes — never bypassing the service contract.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from astrid.core.foundation.hash import sha256_file
from astrid.core.gateway.dispatch import _TOP_LEVEL_HANDLERS
from astrid.core.rendering.cli import main as renderers_cli_main
from astrid.core.rendering.contracts import compute_request_digest
from astrid.core.rendering.registry import load_default_registries
from astrid.core.rendering.replay import ReplayBundle, write_replay_bundle
from astrid.core.rendering.transport import CommandTransport

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "renderer_packs"
    / "raw_command"
)
BACKEND_ID = "raw_command.renderer"


def _copy_pack(tmp_path: Path) -> Path:
    """Copy the committed raw_command fixture pack under an extra pack root."""
    extra_root = tmp_path / "extra"
    shutil.copytree(FIXTURE_ROOT, extra_root / "raw_command")
    return extra_root


def _candidate(extra_root: Path):
    renderers, _, _ = load_default_registries(
        None,
        extra_pack_roots=(str(extra_root),),
    )
    return renderers.get(BACKEND_ID)


def _make_bundle(
    tmp_path: Path,
    candidate,
    *,
    timeline_text: str = '{"tracks": [], "clips": []}',
    output_name: str = "raw_command.mp4",
    window: dict | None = None,
) -> Path:
    """Write a replay bundle whose request digest pins the stored request.json."""
    source_timeline = tmp_path / "timeline.json"
    source_timeline.write_text(timeline_text, encoding="utf-8")
    digest = sha256_file(source_timeline)
    payload = {
        "schema_version": 1,
        "timeline_path": f"inputs/{digest}",
        "assets_registry_path": None,
        "output_name": output_name,
        "window": window,
        "audio": "rendered",
        "profile": None,
        "backend_config": {},
        "metadata": {},
    }
    bundle = ReplayBundle(
        renderer_id=candidate.id,
        request_digest=compute_request_digest(payload),
        manifest_digest=candidate.manifest_digest,
        argv=[
            "python3",
            "backend.py",
            "render",
            "--request",
            "/host/workspace/request.json",
            "--result",
            "/host/workspace/result.json",
        ],
        inputs={"timeline": str(source_timeline)},
        payload=payload,
        metadata={"verb": "render", "success": True},
    )
    return write_replay_bundle(bundle, tmp_path / "bundle")


def _reference_output_bytes(pack_root: Path, request: dict) -> bytes:
    """Render the same wire request directly through the frozen transport."""
    import tempfile

    workspace = Path(tempfile.mkdtemp(prefix="astrid-renderers-ref-"))
    request_path = workspace / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    transport = CommandTransport(BACKEND_ID, termination_grace=0.15)
    result = transport.run(
        "render",
        [sys.executable, "backend.py"],
        request_path=request_path,
        result_path=workspace / "result.json",
        cwd=pack_root,
        timeout=30,
    )
    return (workspace / result.video.path).read_bytes()


def _bundle_json(bundle_dir: Path) -> dict:
    return json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))


def _stdout(capsys) -> str:
    return capsys.readouterr().out


def _stderr(capsys) -> str:
    return capsys.readouterr().err


# ---------------------------------------------------------------------------
# Fresh bundle: replay succeeds and reproduces the output
# ---------------------------------------------------------------------------


def test_replay_fresh_bundle_succeeds_and_reproduces_output(
    tmp_path: Path,
    capsys,
) -> None:
    extra_root = _copy_pack(tmp_path)
    candidate = _candidate(extra_root)
    bundle_dir = _make_bundle(tmp_path, candidate)

    assert (
        renderers_cli_main(
            ["replay", str(bundle_dir), "--pack-root", str(extra_root)]
        )
        == 0
    )
    text = _stdout(capsys)
    assert f"replay: {BACKEND_ID}" in text
    assert f"manifest_digest: {candidate.manifest_digest}" in text
    assert "manifest_digest_match: true" in text
    assert "request_digest_verified: true" in text
    assert "drift: none" in text

    output = Path(text.partition("output: ")[2].strip())
    assert output.is_file()
    assert output.stat().st_size > 0

    # Reproduces the exact deterministic output of the frozen protocol.
    payload = json.loads((bundle_dir / "request.json").read_text(encoding="utf-8"))
    assert output.read_bytes() == _reference_output_bytes(
        extra_root / "raw_command", payload
    )


def test_replay_reports_pinned_ids_and_digests(tmp_path: Path, capsys) -> None:
    extra_root = _copy_pack(tmp_path)
    candidate = _candidate(extra_root)
    bundle_dir = _make_bundle(tmp_path, candidate)

    assert (
        renderers_cli_main(
            ["replay", str(bundle_dir), "--pack-root", str(extra_root)]
        )
        == 0
    )
    pinned = _bundle_json(bundle_dir)
    text = _stdout(capsys)
    assert f"replay: {pinned['renderer_id']}" in text
    assert f"manifest_digest: {pinned['manifest_digest']}" in text
    assert f"request_digest: {pinned['request_digest']}" in text
    assert "verb: render" in text
    assert "drift: none" in text
    assert candidate.manifest_digest == pinned["manifest_digest"]


# ---------------------------------------------------------------------------
# Manifest-digest drift: refused without acknowledgement
# ---------------------------------------------------------------------------


def test_manifest_digest_drift_refused_without_acknowledgement(
    tmp_path: Path,
    capsys,
) -> None:
    extra_root = _copy_pack(tmp_path)
    candidate = _candidate(extra_root)
    bundle_dir = _make_bundle(tmp_path, candidate)

    # Fixture correction after capture: the backend manifest changes, so the
    # current manifest digest no longer matches the pinned one.
    manifest = extra_root / "raw_command" / "rendering" / "renderer.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "\n# corrected fixture\n",
        encoding="utf-8",
    )
    assert sha256_file(manifest) != candidate.manifest_digest

    assert (
        renderers_cli_main(
            ["replay", str(bundle_dir), "--pack-root", str(extra_root)]
        )
        == 2
    )
    message = _stderr(capsys)
    assert "manifest digest drift" in message
    assert "silent backend substitution" in message
    assert "--acknowledge-drift" in message
    assert _stdout(capsys) == ""


# ---------------------------------------------------------------------------
# Acknowledged drift proceeds and produces the expected output (PROOF)
# ---------------------------------------------------------------------------


def test_acknowledged_manifest_drift_proceeds_and_produces_expected_output(
    tmp_path: Path,
    capsys,
) -> None:
    extra_root = _copy_pack(tmp_path)
    candidate = _candidate(extra_root)
    bundle_dir = _make_bundle(tmp_path, candidate)

    manifest = extra_root / "raw_command" / "rendering" / "renderer.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "\n# corrected fixture\n",
        encoding="utf-8",
    )

    assert (
        renderers_cli_main(
            [
                "replay",
                str(bundle_dir),
                "--pack-root",
                str(extra_root),
                "--acknowledge-drift",
            ]
        )
        == 0
    )
    text = _stdout(capsys)
    assert "manifest_digest_match: false" in text
    assert "drift: acknowledged" in text
    output = Path(text.partition("output: ")[2].strip())
    assert output.is_file()
    payload = json.loads((bundle_dir / "request.json").read_text(encoding="utf-8"))
    assert output.read_bytes() == _reference_output_bytes(
        extra_root / "raw_command", payload
    )


def test_corrected_bundle_input_requires_acknowledgement_then_succeeds(
    tmp_path: Path,
    capsys,
) -> None:
    """PROOF: fixing the bundle's input is drift until acknowledged."""
    extra_root = _copy_pack(tmp_path)
    candidate = _candidate(extra_root)
    bundle_dir = _make_bundle(tmp_path, candidate)

    descriptor = next(iter(_bundle_json(bundle_dir)["inputs"].values()))
    input_path = bundle_dir / descriptor["path"]
    input_path.write_text(
        '{"tracks": [], "clips": [{"fixed": true}]}', encoding="utf-8"
    )

    assert (
        renderers_cli_main(
            ["replay", str(bundle_dir), "--pack-root", str(extra_root)]
        )
        == 2
    )
    message = _stderr(capsys)
    assert "localized input drift" in message
    assert "--acknowledge-drift" in message

    assert (
        renderers_cli_main(
            [
                "replay",
                str(bundle_dir),
                "--pack-root",
                str(extra_root),
                "--acknowledge-drift",
            ]
        )
        == 0
    )
    text = _stdout(capsys)
    assert "drift: acknowledged" in text
    output = Path(text.partition("output: ")[2].strip())
    assert output.is_file()
    assert output.stat().st_size > 0


# ---------------------------------------------------------------------------
# Request-digest mismatch: refused as bundle tampering
# ---------------------------------------------------------------------------


def test_request_digest_mismatch_is_refused_as_tampering(
    tmp_path: Path,
    capsys,
) -> None:
    extra_root = _copy_pack(tmp_path)
    candidate = _candidate(extra_root)
    bundle_dir = _make_bundle(tmp_path, candidate)

    request_path = bundle_dir / "request.json"
    tampered = json.loads(request_path.read_text(encoding="utf-8"))
    tampered["output_name"] = "tampered.mp4"
    request_path.write_text(json.dumps(tampered), encoding="utf-8")

    assert (
        renderers_cli_main(
            ["replay", str(bundle_dir), "--pack-root", str(extra_root)]
        )
        == 2
    )
    message = _stderr(capsys)
    assert "request digest mismatch" in message
    assert "tamper" in message
    assert "refusing" in message

    # Acknowledging drift does not repair a modified request contract.
    assert (
        renderers_cli_main(
            [
                "replay",
                str(bundle_dir),
                "--pack-root",
                str(extra_root),
                "--acknowledge-drift",
            ]
        )
        == 2
    )
    assert "request digest mismatch" in _stderr(capsys)


# ---------------------------------------------------------------------------
# Pinned renderer identity
# ---------------------------------------------------------------------------


def test_unknown_pinned_renderer_is_refused(tmp_path: Path, capsys) -> None:
    extra_root = _copy_pack(tmp_path)
    candidate = _candidate(extra_root)
    bundle_dir = _make_bundle(tmp_path, candidate)

    pinned = _bundle_json(bundle_dir)
    pinned["renderer_id"] = "no.such.renderer"
    (bundle_dir / "bundle.json").write_text(json.dumps(pinned), encoding="utf-8")

    assert (
        renderers_cli_main(
            ["replay", str(bundle_dir), "--pack-root", str(extra_root)]
        )
        == 2
    )
    message = _stderr(capsys)
    assert "not resolvable" in message
    assert "no.such.renderer" in message


def test_replay_missing_bundle_dir_fails(capsys) -> None:
    assert renderers_cli_main(["replay", "/no/such/bundle"]) == 2
    assert "no replay bundle found" in _stderr(capsys)


# ---------------------------------------------------------------------------
# Gateway routing
# ---------------------------------------------------------------------------


def test_dispatch_routes_replay_verb(tmp_path: Path, capsys) -> None:
    assert "replay" not in _TOP_LEVEL_HANDLERS

    extra_root = _copy_pack(tmp_path)
    candidate = _candidate(extra_root)
    bundle_dir = _make_bundle(tmp_path, candidate)

    assert (
        renderers_cli_main(["replay", str(bundle_dir), "--pack-root", str(extra_root)]) == 0
    )
    text = _stdout(capsys)
    assert f"replay: {BACKEND_ID}" in text
    assert "request_digest_verified: true" in text


def test_replay_support_bundle_reports_result_file(tmp_path: Path, capsys) -> None:
    """A support-verb bundle replays to a SupportReport result file, not a
    video path (the bundle's argv carries the support verb)."""
    extra_root = _copy_pack(tmp_path)
    candidate = _candidate(extra_root)
    source_timeline = tmp_path / "timeline.json"
    source_timeline.write_text('{"tracks": [], "clips": []}', encoding="utf-8")
    digest = sha256_file(source_timeline)
    payload = {
        "schema_version": 1,
        "timeline_path": f"inputs/{digest}",
        "assets_registry_path": None,
        "output_name": "raw_command.mp4",
        "window": None,
        "audio": "rendered",
        "profile": None,
        "backend_config": {},
        "metadata": {},
    }
    bundle = ReplayBundle(
        renderer_id=candidate.id,
        request_digest=compute_request_digest(payload),
        manifest_digest=candidate.manifest_digest,
        argv=[
            "python3",
            "backend.py",
            "support",
            "--request",
            "request.json",
            "--result",
            "result.json",
        ],
        inputs={"timeline": str(source_timeline)},
        payload=payload,
        metadata={"verb": "support", "success": False},
    )
    bundle_dir = write_replay_bundle(bundle, tmp_path / "support-bundle")

    assert (
        renderers_cli_main(
            ["replay", str(bundle_dir), "--pack-root", str(extra_root)]
        )
        == 0
    )
    text = _stdout(capsys)
    assert "verb: support" in text
    output = Path(text.partition("output: ")[2].strip())
    assert output.is_file()
    assert output.stat().st_size > 0
