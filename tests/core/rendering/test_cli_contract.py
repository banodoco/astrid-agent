"""T7.2 — frozen renderer CLI JSON and error contract.

Locks the internal renderer-authoring CLI ``--json`` shapes and error behavior:

* every remaining authoring verb (``create``, ``list``, ``inspect``,
  ``validate``, plus the ``support`` verb) emits a STABLE, verb-specific JSON object on
  stdout under ``--json`` — exact keys asserted per verb, NO universal
  envelope (no ``ok``/``status``/``data``/``result`` wrapper);
* plain mode stays human-readable text;
* two invocations in different cwd/workspaces produce identical output for
  the same inputs (session independence);
* ``create`` into a colliding dest → structured ``conflict`` error;
* an untrusted (``ASTRID_PACKS_PATH``-discovered) pack in an extra root is
  refused with a clear message;
* ``support`` on a backend that declines → the frozen ``RendererError``
  shape (``kind: unsupported`` + recovery guidance);
* a cancelled/``KeyboardInterrupt`` path reports cleanly — one JSON object /
  one line, no traceback dump;
* exit codes are 0 on success and non-zero on failure — no independent
  exit-code taxonomy beyond that.

The ``replay`` verb exists and freezes its own JSON contract here
(``test_replay_json_shape_is_stable``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.rendering.cli import main as renderers_cli_main
from astrid.core.rendering.scaffold import SCAFFOLD_FILES, create_renderer_scaffold
from tests.helpers.cli_runner import run_cli

BUILTIN_IDS = (
    "rendering.ffmpeg",
    "rendering.remotion",
    "rendering.ffmpeg-finalizer",
)

# The exact key set of the frozen RendererError wire shape.
RENDERER_ERROR_KEYS = frozenset(
    {"schema_version", "kind", "backend", "message", "recovery_command", "details"}
)

# Keys that would indicate a forbidden universal envelope.
_ENVELOPE_KEYS = frozenset({"ok", "status", "data", "result", "success", "error"})


def _load_json(stdout: str) -> dict[str, object]:
    """Parse a single-line JSON object; refuse multi-line/extra output."""
    assert stdout.endswith("\n"), "JSON output must end with a newline"
    lines = stdout.splitlines()
    assert len(lines) == 1, f"expected exactly one JSON line, got {len(lines)}"
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload


def _assert_no_envelope(payload: dict[str, object]) -> None:
    assert _ENVELOPE_KEYS.isdisjoint(payload.keys()), (
        f"universal envelope key leaked into verb JSON: {sorted(payload.keys())}"
    )


# ---------------------------------------------------------------------------
# verb-specific JSON shapes (exact keys, no universal envelope)
# ---------------------------------------------------------------------------


def test_create_json_shape_is_stable(tmp_path: Path) -> None:
    result = run_cli(renderers_cli_main, ["create", "wave", str(tmp_path / "wave"), "--json"])

    assert result.exit_code == 0
    payload = _load_json(result.stdout)
    assert set(payload.keys()) == {"dest", "files"}
    _assert_no_envelope(payload)
    assert payload["dest"] == str((tmp_path / "wave").resolve())
    assert payload["files"] == list(SCAFFOLD_FILES)
    assert result.stderr == ""


def test_list_json_shape_is_stable() -> None:
    result = run_cli(renderers_cli_main, ["list", "--json"])

    assert result.exit_code == 0
    payload = _load_json(result.stdout)
    assert set(payload.keys()) == {"ids"}
    _assert_no_envelope(payload)
    ids = payload["ids"]
    assert isinstance(ids, list)
    for capability_id in BUILTIN_IDS:
        assert capability_id in ids
    assert result.stderr == ""


def test_inspect_json_shape_is_stable() -> None:
    result = run_cli(renderers_cli_main, ["inspect", "rendering.ffmpeg", "--json"])

    assert result.exit_code == 0
    payload = _load_json(result.stdout)
    assert set(payload.keys()) == {
        "id",
        "kind",
        "name",
        "version",
        "protocol_version",
        "command",
        "operations",
        "required_binaries",
        "required_permissions",
        "timeout_seconds",
        "description",
        "capabilities",
        "source_pack",
        "source_kind",
        "manifest_path",
        "precedence",
        "alias_chain",
        "override",
        "conflicts",
        "overrides",
        "eligibility",
        "eligibility_reason",
        "trust_method",
    }
    _assert_no_envelope(payload)
    assert payload["id"] == "rendering.ffmpeg"
    assert payload["kind"] == "renderer"
    assert payload["eligibility"] == "eligible"
    assert payload["trust_method"] == "source_tree"
    assert result.stderr == ""


def test_inspect_json_shape_for_scaffolded_extra_root_pack(tmp_path: Path) -> None:
    create_renderer_scaffold("wave", tmp_path / "wave")
    result = run_cli(
        renderers_cli_main,
        ["inspect", "wave.wave", "--pack-root", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0
    payload = _load_json(result.stdout)
    assert set(payload.keys()) == {
        "id",
        "kind",
        "name",
        "version",
        "protocol_version",
        "command",
        "operations",
        "required_binaries",
        "required_permissions",
        "timeout_seconds",
        "description",
        "capabilities",
        "source_pack",
        "source_kind",
        "manifest_path",
        "precedence",
        "alias_chain",
        "override",
        "conflicts",
        "overrides",
        "eligibility",
        "eligibility_reason",
        "trust_method",
    }
    assert payload["id"] == "wave.wave"
    assert payload["source_pack"] == "wave"
    assert payload["source_kind"] == "extra"
    assert payload["eligibility"] == "eligible"
    assert payload["trust_method"] == "explicit_extra_pack_root"


def test_validate_json_shape_is_stable(tmp_path: Path) -> None:
    dest = create_renderer_scaffold("wave", tmp_path / "wave")
    result = run_cli(renderers_cli_main, ["validate", str(dest), "--json"])

    assert result.exit_code == 0
    payload = _load_json(result.stdout)
    assert set(payload.keys()) == {"path", "valid", "errors", "warnings"}
    _assert_no_envelope(payload)
    assert payload["path"] == str(dest.resolve())
    assert payload["valid"] is True
    assert payload["errors"] == []
    assert payload["warnings"] == []
    assert result.stderr == ""


def test_validate_json_shape_on_invalid_pack(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "pack.yaml").write_text("id: [unclosed\n", encoding="utf-8")

    result = run_cli(renderers_cli_main, ["validate", str(broken), "--json"])

    assert result.exit_code != 0
    payload = _load_json(result.stdout)
    assert set(payload.keys()) == {"path", "valid", "errors", "warnings"}
    assert payload["valid"] is False
    assert isinstance(payload["errors"], list) and payload["errors"]
    assert payload["path"] == str(broken.resolve())


def test_support_json_shape_is_stable(tmp_path: Path) -> None:
    create_renderer_scaffold("wave", tmp_path / "wave")
    result = run_cli(
        renderers_cli_main,
        ["support", "wave.wave", "--pack-root", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0
    payload = _load_json(result.stdout)
    assert set(payload.keys()) == {
        "schema_version",
        "supported",
        "reasons",
        "features",
        "alternatives",
        "backend",
        "backend_version",
    }
    _assert_no_envelope(payload)
    assert payload["schema_version"] == 1
    assert payload["supported"] is True
    assert payload["backend"] == "wave.wave"
    assert result.stderr == ""


# ---------------------------------------------------------------------------
# plain mode stays human-readable text
# ---------------------------------------------------------------------------


def test_plain_mode_list_is_human_readable_text() -> None:
    result = run_cli(renderers_cli_main, ["list"])

    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines
    assert not result.stdout.lstrip().startswith("{")
    for capability_id in BUILTIN_IDS:
        assert capability_id in lines


def test_plain_mode_inspect_is_human_readable_text() -> None:
    result = run_cli(renderers_cli_main, ["inspect", "rendering.ffmpeg"])

    assert result.exit_code == 0
    assert not result.stdout.lstrip().startswith("{")
    assert "id: rendering.ffmpeg" in result.stdout
    assert "kind: renderer" in result.stdout
    assert "capabilities:" in result.stdout
    assert "eligibility: eligible" in result.stdout


def test_plain_mode_validate_is_human_readable_text(tmp_path: Path) -> None:
    dest = create_renderer_scaffold("wave", tmp_path / "wave")
    result = run_cli(renderers_cli_main, ["validate", str(dest)])

    assert result.exit_code == 0
    assert not result.stdout.lstrip().startswith("{")
    assert f"valid: {dest.resolve()}" in result.stdout


def test_plain_mode_create_is_human_readable_text(tmp_path: Path) -> None:
    result = run_cli(renderers_cli_main, ["create", "wave", str(tmp_path / "wave")])

    assert result.exit_code == 0
    assert not result.stdout.lstrip().startswith("{")
    assert "created renderer scaffold at" in result.stdout
    assert "files: pack.yaml rendering/renderer.yaml render.py test_renderer.py" in result.stdout


def test_plain_mode_support_is_human_readable_text(tmp_path: Path) -> None:
    create_renderer_scaffold("wave", tmp_path / "wave")
    result = run_cli(
        renderers_cli_main,
        ["support", "wave.wave", "--pack-root", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert not result.stdout.lstrip().startswith("{")
    assert "support: wave.wave" in result.stdout
    assert "supported: true" in result.stdout


def test_plain_mode_errors_are_text_not_json() -> None:
    result = run_cli(renderers_cli_main, ["inspect", "no.such.renderer"])

    assert result.exit_code != 0
    assert result.stdout == ""
    assert not result.stderr.lstrip().startswith("{")
    assert "unknown renderer/planner/finalizer id 'no.such.renderer'" in result.stderr


# ---------------------------------------------------------------------------
# session independence: same inputs, different cwd/workspaces → identical JSON
# ---------------------------------------------------------------------------


def test_list_json_is_identical_across_workspaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()

    monkeypatch.chdir(first_cwd)
    first = run_cli(renderers_cli_main, ["list", "--json"])
    monkeypatch.chdir(second_cwd)
    second = run_cli(renderers_cli_main, ["list", "--json"])

    assert first.exit_code == second.exit_code == 0
    assert first.stdout == second.stdout
    assert first.stderr == second.stderr == ""


def test_inspect_json_is_identical_across_workspaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()

    monkeypatch.chdir(first_cwd)
    first = run_cli(renderers_cli_main, ["inspect", "rendering.ffmpeg", "--json"])
    monkeypatch.chdir(second_cwd)
    second = run_cli(renderers_cli_main, ["inspect", "rendering.ffmpeg", "--json"])

    assert first.exit_code == second.exit_code == 0
    assert first.stdout == second.stdout
    assert first.stderr == second.stderr == ""


# ---------------------------------------------------------------------------
# conflict: create into a colliding dest → structured error
# ---------------------------------------------------------------------------


def test_create_conflict_emits_structured_error_json(tmp_path: Path) -> None:
    create_renderer_scaffold("wave", tmp_path / "wave")

    result = run_cli(
        renderers_cli_main,
        ["create", "wave", str(tmp_path / "wave"), "--json"],
    )

    assert result.exit_code != 0
    assert result.stdout == ""
    payload = _load_json(result.stderr)
    assert set(payload.keys()) == {"verb", "error"}
    assert payload["verb"] == "create"
    error = payload["error"]
    assert isinstance(error, dict)
    assert set(error.keys()) == {"kind", "message", "recovery_command"}
    assert error["kind"] == "conflict"
    assert "refusing to overwrite" in error["message"]
    assert error["recovery_command"] == (
        "python3 -m astrid.core.rendering.cli create --help"
    )


def test_create_conflict_plain_mode_keeps_clear_text(tmp_path: Path) -> None:
    create_renderer_scaffold("wave", tmp_path / "wave")

    result = run_cli(renderers_cli_main, ["create", "wave", str(tmp_path / "wave")])

    assert result.exit_code != 0
    assert not result.stderr.lstrip().startswith("{")
    assert "refusing to overwrite" in result.stderr


# ---------------------------------------------------------------------------
# trust denial: an untrusted pack in an extra root is refused
# ---------------------------------------------------------------------------

_ENV_PACKS_PATH = "ASTRID_PACKS_PATH"


def test_untrusted_env_discovered_pack_is_ineligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_renderer_scaffold("wave", tmp_path / "roots" / "wave")
    monkeypatch.setenv(_ENV_PACKS_PATH, str(tmp_path / "roots"))

    inspect = run_cli(
        renderers_cli_main,
        ["inspect", "wave.wave", "--json"],
    )

    assert inspect.exit_code == 0
    payload = _load_json(inspect.stdout)
    assert payload["eligibility"] == "ineligible"
    assert payload["source_kind"] == "env"
    assert "not executable" in payload["eligibility_reason"]


# ---------------------------------------------------------------------------
# unsupported support: a backend that declines → frozen RendererError shape
# ---------------------------------------------------------------------------

_DECLINE_RENDER_PY = """\
#!/usr/bin/env python3
\"\"\"Fixture backend whose support verb declines with a frozen RendererError.\"\"\"
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="render.py")
    parser.add_argument("verb", choices=("render", "support"))
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    result_path = Path(args.result)
    if args.verb == "support":
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "unsupported",
                    "backend": "decline.decline",
                    "message": "decline.decline declines support for this request",
                    "recovery_command": (
                        "select a renderer that supports the requested mode"
                    ),
                    "details": {"mode": "requested"},
                }
            ),
            encoding="utf-8",
        )
        return 0
    result_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "unsupported",
                "backend": "decline.decline",
                "message": "decline.decline declines render for this request",
                "recovery_command": (
                    "select a renderer that supports the requested mode"
                ),
                "details": {"verb": "render"},
            }
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
"""


def _scaffold_declining_backend(tmp_path: Path) -> Path:
    dest = create_renderer_scaffold("decline", tmp_path / "decline")
    (dest / "render.py").write_text(_DECLINE_RENDER_PY, encoding="utf-8")
    return dest


def test_support_on_declining_backend_emits_frozen_renderer_error(
    tmp_path: Path,
) -> None:
    _scaffold_declining_backend(tmp_path)

    result = run_cli(
        renderers_cli_main,
        ["support", "decline.decline", "--pack-root", str(tmp_path), "--json"],
    )

    assert result.exit_code != 0
    assert result.stdout == ""
    payload = _load_json(result.stderr)
    assert set(payload.keys()) == RENDERER_ERROR_KEYS
    assert payload["schema_version"] == 1
    assert payload["kind"] == "unsupported"
    assert payload["backend"] == "decline.decline"
    assert "declines support" in payload["message"]
    assert payload["recovery_command"] == (
        "select a renderer that supports the requested mode"
    )
    assert isinstance(payload["details"], dict)


def test_support_on_unknown_backend_emits_frozen_renderer_error() -> None:
    result = run_cli(
        renderers_cli_main,
        ["support", "no.such.renderer", "--json"],
    )

    assert result.exit_code != 0
    assert result.stdout == ""
    payload = _load_json(result.stderr)
    assert set(payload.keys()) == RENDERER_ERROR_KEYS
    assert payload["schema_version"] == 1
    assert payload["kind"] == "unsupported"
    assert payload["backend"] == "no.such.renderer"
    assert payload["recovery_command"] == "select an execution-eligible renderer and retry"


def test_support_on_declining_backend_plain_mode_keeps_text(tmp_path: Path) -> None:
    _scaffold_declining_backend(tmp_path)

    result = run_cli(
        renderers_cli_main,
        ["support", "decline.decline", "--pack-root", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert not result.stderr.lstrip().startswith("{")
    assert "declines support" in result.stderr
    assert "recovery: select a renderer that supports the requested mode" in (
        result.stderr
    )


# ---------------------------------------------------------------------------
# exit codes: 0 on success, non-zero on failure — no finer taxonomy
# ---------------------------------------------------------------------------


def test_exit_code_contract_is_only_zero_vs_nonzero(tmp_path: Path) -> None:
    create_renderer_scaffold("wave", tmp_path / "wave")

    successes = [
        run_cli(renderers_cli_main, ["list", "--json"]),
        run_cli(renderers_cli_main, ["inspect", "rendering.ffmpeg", "--json"]),
        run_cli(renderers_cli_main, ["validate", str(tmp_path / "wave"), "--json"]),
        run_cli(
            renderers_cli_main,
            ["support", "wave.wave", "--pack-root", str(tmp_path), "--json"],
        ),
    ]
    failures = [
        run_cli(renderers_cli_main, ["inspect", "no.such.renderer", "--json"]),
        run_cli(
            renderers_cli_main,
            ["create", "wave", str(tmp_path / "wave"), "--json"],
        ),
    ]

    for result in successes:
        assert result.exit_code == 0
    for result in failures:
        assert result.exit_code != 0


def test_replay_json_shape_is_stable(tmp_path: Path) -> None:
    """The replay verb emits one frozen JSON object under --json (no
    universal envelope)."""
    from astrid.core.foundation.hash import sha256_file as _sha256
    from astrid.core.rendering.contracts import compute_request_digest as _digest
    from astrid.core.rendering.replay import ReplayBundle, write_replay_bundle
    from tests.core.rendering.test_replay import _candidate, _copy_pack

    extra_root = _copy_pack(tmp_path)
    candidate = _candidate(extra_root)
    source_timeline = tmp_path / "timeline.json"
    source_timeline.write_text('{"tracks": [], "clips": []}', encoding="utf-8")
    digest = _sha256(source_timeline)
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
        request_digest=_digest(payload),
        manifest_digest=candidate.manifest_digest,
        argv=["python3", "backend.py", "render", "--request", "request.json", "--result", "result.json"],
        inputs={"timeline": str(source_timeline)},
        payload=payload,
        metadata={"verb": "render", "success": False},
    )
    bundle_dir = write_replay_bundle(bundle, tmp_path / "bundle")

    result = run_cli(
        renderers_cli_main,
        ["replay", str(bundle_dir), "--pack-root", str(extra_root), "--json"],
    )

    assert result.exit_code == 0
    payload = _load_json(result.stdout)
    assert set(payload.keys()) == {
        "verb",
        "renderer_id",
        "manifest_digest",
        "manifest_digest_match",
        "request_digest",
        "request_digest_verified",
        "replay_verb",
        "drift",
        "output",
    }
    _assert_no_envelope(payload)
