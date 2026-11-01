"""B7.2 negative proof for the generated-client cutover boundary."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


class _NoTransport:
    def __getattr__(self, name: str):
        raise AssertionError(f"retired transport operation reached: {name}")


class _NoReadPath:
    def read_bytes(self):
        raise AssertionError("path was read before rejecting unsupported realm")


def test_sdk_rejects_path_backed_media_before_reading_or_transport() -> None:
    from astrid.sdk.remote import RemoteMedia

    media = RemoteMedia(_NoTransport())
    for realm in ("external_local", "remote", None):
        result = media.import_file(
            project="demo", path=_NoReadPath(), realm=realm, idempotency_key="unsupported-media"
        )
        assert not result.ok
        assert result.error is not None and result.error.code == "validation_error"

    with pytest.raises(TypeError):
        media.import_file(
            project="demo", path=_NoReadPath(), reference_in_place=True, idempotency_key="reference-in-place"
        )

    result = media.verify("demo", "object-1", realm="external_local", idempotency_key="unsupported-verify")
    assert not result.ok
    assert result.error is not None and result.error.code == "validation_error"


def test_sdk_rejects_retired_legacy_children_without_transport() -> None:
    from astrid.sdk.remote import RemoteReferences, RemoteShots, RemoteTimelines

    transport = _NoTransport()
    with pytest.raises(TypeError):
        RemoteTimelines(transport).save("demo", "main", shots=[])
    with pytest.raises(TypeError):
        RemoteReferences(transport).create(timeline_id="legacy", object_id="m")
    with pytest.raises(TypeError):
        RemoteShots(transport).create(timeline_id="legacy", shot={})
    for operation in ("update", "archive", "recover"):
        assert getattr(RemoteReferences(transport), operation)(None, "ref", expected_version=1).error.code == "unsupported_operation"
        assert getattr(RemoteShots(transport), operation)(None, "shot", expected_version=1).error.code == "unsupported_operation"


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_product_parser_graph_does_not_load_local_authority() -> None:
    """Nested product parsers must be importable without SQLite/repositories."""
    probe = (
        "import sys; "
        "from astrid.core.cli.domain_media import build_parser as media; "
        "from astrid.core.cli.domain_references import build_parser as references; "
        "from astrid.core.cli.domain_shots import build_parser as shots; "
        "print('sqlite3' in sys.modules); "
        "print(any(name.startswith('astrid.core.store') or "
        "name.startswith('astrid.core.repositories') or "
        "name.endswith('.repository') for name in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == ["False", "False"]


def test_removed_media_realm_and_render_execution_mode_are_absent() -> None:
    media = _source("astrid/core/cli/domain_media.py")
    render = _source("astrid/packs/rendering/actions/render/task_adapter.py")
    assert "external_local" not in media
    assert 'execution_mode="in_process"' not in render


def test_product_parsers_have_no_repository_imports() -> None:
    for relative in (
        "astrid/core/cli/domain_media.py",
        "astrid/core/cli/domain_references.py",
        "astrid/core/cli/domain_shots.py",
    ):
        tree = ast.parse(_source(relative), filename=relative)
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not any(module.endswith(".repository") for module in imported), relative


def test_removed_repository_modules_are_absent() -> None:
    root = Path(__file__).resolve().parents[2]
    assert not (root / "astrid/packs/shots/repository.py").exists()
    assert not (root / "astrid/core/repositories").exists()
