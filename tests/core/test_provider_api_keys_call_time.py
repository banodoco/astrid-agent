"""Call-time credential resolution regressions for provider integrations."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.generation.backends.fal import FalBackend
from astrid.core.model_catalog.schema import BackendSpec, ModelEntry, ModeSpec
from astrid.core.util.http import HttpClient


def _fal_entry() -> ModelEntry:
    return ModelEntry(
        id="test-fal-model",
        modality="image",
        modes={
            "t2i": ModeSpec(
                supports=("prompt",),
                requires=("prompt",),
                backends={"cloud": BackendSpec(endpoint="fal-ai/test")},
            )
        },
    )


def _assert_missing_fal_key(error: AstridError) -> None:
    assert "FAL_KEY not found" in str(error.cause)
    assert "credential set FAL_KEY" in error.recovery_command
    assert "shared astrid.env" in error.recovery_command


def test_fal_backend_resolves_missing_key_at_generate_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shared_env = tmp_path / "astrid.env"
    shared_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("ASTRID_ENV_FILE", str(shared_env))
    monkeypatch.delenv("FAL_KEY", raising=False)

    client = Mock(spec=HttpClient)
    submit = Mock()
    from astrid.core.generation.backends import fal as fal_module

    monkeypatch.setattr(fal_module, "fal_submit_and_poll", submit)

    backend = FalBackend(client=client)
    with pytest.raises(AstridError) as exc_info:
        backend.generate(_fal_entry(), "t2i", {"prompt": "test"}, tmp_path / "out")

    _assert_missing_fal_key(exc_info.value)
    submit.assert_not_called()
    client.post_json.assert_not_called()


