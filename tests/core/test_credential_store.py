from __future__ import annotations

import os
from pathlib import Path

import pytest

from astrid.core.util.credential_store import main, store_credential
from astrid.core.util.secrets import load_local_api_key_with_source


def test_store_updates_one_key_atomically_and_preserves_other_lines(tmp_path: Path) -> None:
    target = tmp_path / ".astrid" / "astrid.env"
    target.parent.mkdir()
    target.write_text(
        "# keep this comment\nOPENAI_API_KEY=openai-value\nRUNPOD_API_KEY=old\nRUNPOD_API_KEY=duplicate\n",
        encoding="utf-8",
    )

    result = store_credential("runpod", "new-runpod-value", env_file=target)

    assert result == target
    assert target.read_text(encoding="utf-8") == (
        "# keep this comment\nOPENAI_API_KEY=openai-value\nRUNPOD_API_KEY='new-runpod-value'\n"
    )
    assert load_local_api_key_with_source(
        "RUNPOD_API_KEY", environ={"ASTRID_ENV_FILE": str(target)}
    ) == ("new-runpod-value", "astrid_env_file")
    assert load_local_api_key_with_source(
        "OPENAI_API_KEY", environ={"ASTRID_ENV_FILE": str(target)}
    ) == ("openai-value", "astrid_env_file")
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_store_rejects_multiline_values_and_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "astrid.env"
    with pytest.raises(ValueError, match="single-line"):
        store_credential("runpod", "first\nsecond", env_file=target)
    assert not target.exists()

    actual = tmp_path / "actual.env"
    actual.write_text("OPENAI_API_KEY=untouched\n", encoding="utf-8")
    link = tmp_path / "link.env"
    link.symlink_to(actual)
    with pytest.raises(ValueError, match="symbolic link"):
        store_credential("runpod", "new-value", env_file=link)
    assert actual.read_text(encoding="utf-8") == "OPENAI_API_KEY=untouched\n"


def test_store_accepts_custom_environment_reference(tmp_path: Path) -> None:
    target = tmp_path / "astrid.env"

    store_credential("HUGGING_FACE_HUB_TOKEN", "custom-hf-token", env_file=target)

    assert target.read_text(encoding="utf-8") == "HUGGING_FACE_HUB_TOKEN='custom-hf-token'\n"


@pytest.mark.parametrize(
    "secret",
    [
        "token # with comment-like text",
        'token with "double quotes"',
        "token with 'single quotes'",
        "literal-${HOME}-reference",
        r"back\\slash",
    ],
)
def test_store_round_trips_dotenv_special_characters(
    tmp_path: Path, secret: str
) -> None:
    target = tmp_path / "astrid.env"

    store_credential("runpod", secret, env_file=target)

    assert load_local_api_key_with_source(
        "RUNPOD_API_KEY", environ={"ASTRID_ENV_FILE": str(target)}
    ) == (secret, "astrid_env_file")


def test_invalid_provider_fails_before_prompting(monkeypatch, capsys) -> None:
    def unexpected_prompt(prompt: str) -> str:
        raise AssertionError("invalid provider must be rejected before reading a credential")

    monkeypatch.setattr("astrid.core.util.credential_store.getpass.getpass", unexpected_prompt)

    with pytest.raises(SystemExit, match="2"):
        main(["set", "not-a-provider"])

    assert "use a supported provider" in capsys.readouterr().err


def test_command_refuses_getpass_echo_fallback(monkeypatch, capsys) -> None:
    def visible_prompt(prompt: str) -> str:
        import getpass

        raise getpass.GetPassWarning("terminal echo is enabled")

    monkeypatch.setattr("astrid.core.util.credential_store.getpass.getpass", visible_prompt)

    assert main(["set", "runpod"]) == 1

    output = capsys.readouterr()
    assert "hidden input requires an interactive terminal" in output.err
    assert "terminal echo is enabled" not in output.err


def test_store_command_reads_secret_without_echoing_it(monkeypatch, tmp_path: Path, capsys) -> None:
    secret = "never-echo-this-token"
    target = tmp_path / "astrid.env"
    monkeypatch.setenv("ASTRID_ENV_FILE", str(target))
    monkeypatch.setattr("astrid.core.util.credential_store.getpass.getpass", lambda prompt: secret)

    assert main(["set", "runpod"]) == 0

    output = capsys.readouterr()
    assert "Stored runpod credential" in output.out
    assert secret not in output.out
    assert secret not in output.err
    assert target.read_text(encoding="utf-8") == f"RUNPOD_API_KEY='{secret}'\n"
