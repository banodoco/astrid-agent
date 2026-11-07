"""Hivemind's real v3 manifest exercises strict action admission."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from astrid.core.pack.canonical import CanonicalPackValidationError, validate_canonical_pack


ROOT = Path(__file__).resolve().parents[2]
HIVEMIND = ROOT / "astrid" / "core" / "hivemind"
WRITER_ACTIONS = ("contribute", "ingest_article", "ingest_workflow", "ingest_youtube")
CONTRIBUTOR_SECRET = "HIVEMIND_CONTRIBUTOR_KEY"
CONTRIBUTE_CHOICES = (
    "submit-resource",
    "propose-revision",
    "decide-revision",
    "mark-canonical",
    "capture-message",
    "evidence",
)


def _admit_action(tmp_path: Path, action: dict) -> None:
    root = tmp_path / "hivemind"
    root.mkdir()
    (root / "actions").mkdir()
    (root / "actions" / "probe.py").write_text("def main(): pass\n", encoding="utf-8")
    (root / "pack.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 3,
            "id": "hivemind",
            "name": "Hivemind",
            "version": "2.0.0",
            "actions": {"probe": action},
        }),
        encoding="utf-8",
    )
    validate_canonical_pack(root)


def _probe_action(*, inputs: list | None = None, secrets: object = None, extra: bool = False) -> dict:
    action = {
        "description": "Schema probe.",
        "invocation": {"kind": "python", "path": "actions/probe.py", "function": "main"},
        "inputs": inputs if inputs is not None else [],
        "outputs": [],
    }
    if secrets is not None:
        action["secrets"] = secrets
    if extra:
        action["unexpected"] = True
    return action


def test_bundled_hivemind_strict_admission_preserves_declarations() -> None:
    admitted = validate_canonical_pack(HIVEMIND).definition.to_dict()
    actions = admitted["actions"]

    assert set(actions) == {
        "search", "get_item", "refresh_media", "contribute",
        "ingest_article", "ingest_workflow", "ingest_youtube",
    }
    assert {name for name, action in actions.items() if action.get("secrets")} == set(WRITER_ACTIONS)
    assert all(actions[name]["secrets"] == [CONTRIBUTOR_SECRET] for name in WRITER_ACTIONS)

    contribute_input = next(item for item in actions["contribute"]["inputs"] if item["name"] == "type")
    assert tuple(contribute_input["enum"]) == CONTRIBUTE_CHOICES


@pytest.mark.parametrize(
    ("secrets", "extra"),
    [
        ("TOKEN", False),
        ({"TOKEN": True}, False),
        (["TOKEN", 3], False),
        ([""], False),
        (["lowercase"], False),
        (["BAD-NAME"], False),
        (["TOKEN", "TOKEN"], False),
        (None, True),
    ],
    ids=("scalar-container", "mapping-container", "non-string-item", "blank-name", "lowercase", "invalid-name", "duplicate", "unknown-action-field"),
)
def test_action_secrets_reject_invalid_declarations(
    tmp_path: Path, secrets: object, extra: bool
) -> None:
    with pytest.raises(CanonicalPackValidationError):
        _admit_action(tmp_path, _probe_action(secrets=secrets, extra=extra))


@pytest.mark.parametrize(
    ("enum", "type_value", "extra", "valid"),
    [
        (["submit-resource", "propose-revision", "decide-revision", "mark-canonical", "capture-message", "evidence"], "string", False, True),
        (None, "string", False, False),
        ("one", "string", False, False),
        ({"one": True}, "string", False, False),
        ([], "string", False, False),
        (["first", "first"], "string", False, False),
        (["  "], "string", False, False),
        (["ok", 2], "string", False, False),
        (["one", "two"], None, False, False),
        (["one", "two"], 3, False, False),
        (["one", "two"], True, False, False),
        (["one", "two"], "number", False, False),
        (["one"], "string", True, False),
    ],
    ids=("real-contribute-choices", "null-container", "scalar-container", "mapping-container", "empty", "duplicate", "blank-choice", "non-string-choice", "missing-type", "integer-type", "boolean-type", "non-string-type", "unknown-input-field"),
)
def test_input_enum_is_strict_and_preserved(
    tmp_path: Path, enum: object, type_value: object, extra: bool, valid: bool
) -> None:
    item = {"name": "mode", "type": type_value, "enum": enum}
    if extra:
        item["extra"] = "field"
    if valid:
        _admit_action(tmp_path, _probe_action(inputs=[item]))
        return
    with pytest.raises(CanonicalPackValidationError):
        _admit_action(tmp_path, _probe_action(inputs=[item]))
