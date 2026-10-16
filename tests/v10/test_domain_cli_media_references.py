"""Product CLI family tests: media and nested references (m4 plan step 27).

Task T30 proves the ``media`` product family
(``astrid/core/cli/domain_media.py``): the planned verbs
(``import|list|show|verify|relate|thumbnails``) are reachable through one-call
SDK adapters, import accepts **only files/folders**, relate accepts only the
frozen five relation kinds, and the manifest-declared nested ``references``
mount (``astrid/packs/references/cli.py``) exposes exactly
``create|update|archive|associate|link|set-primary|list|show`` beneath ``media`` — with
**no top-level references family**. Every mutation preserves exact media
IDs, mutation receipts/keys, validation, and typed failure envelopes; all
help is executable; and the gateway dispatch routes the media family (and
its nested references mount) through the product boundary.
"""

from __future__ import annotations

import argparse
import json
import types
from typing import Any, cast

import pytest

from astrid.core.cli.domain_product import (
    PRODUCT_FAMILY_SET,
    ProductRegistryError,
    is_product_family,
    run_product_family,
)
from astrid.core.receipts.contract import CommandReceipt
from astrid.sdk.contracts import DomainResult, ErrorObject

ENVELOPE_KEYS = {"ok", "data", "error", "receipt", "idempotency_key"}

MEDIA_RELATION_KINDS = (
    "derived_from",
    "variant_of",
    "uses_as_input",
    "mask_for",
    "audio_for",
)
REFERENCE_KINDS = ("character", "place", "object", "clothing", "other")
MEDIA_REFERENCE_ROLES = ("canonical", "used_as_input", "depicts", "inspired_by")
REFERENCE_LINK_KINDS = (
    "belongs_to",
    "wears",
    "located_in",
    "associated_with",
    "related_to",
)


def _receipt(command_kind: str, key: str) -> CommandReceipt:
    return CommandReceipt(
        receipt_id=f"R-{command_kind}",
        command_kind=command_kind,
        idempotency_key=key,
        request_hash="hash",
        project_id="P-1",
        project_seq=(1, 1),
        event_ids=("E-1",),
        result={"ok": True},
        created_at="2026-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Recording fake client (one call per service method, canned envelopes)
# ---------------------------------------------------------------------------


class _RecordingMedia:
    def __init__(self, owner: "_FakeClient") -> None:
        self._owner = owner

    def import_file(self, *, project, path, realm="managed_local", idempotency_key=None):
        self._owner.calls.append(
            (
                "media.import_file",
                {
                    "project": project,
                    "path": str(path),
                    "realm": realm,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"id": "M-1", "project_id": "P-1", "content_hash": "abc", "digest": "sha256:" + "a" * 64},
            receipt=_receipt("core.media.import", key),
            idempotency_key=key,
        )

    def import_directory(self, *, project, directory, realm="managed_local", idempotency_key=None):
        self._owner.calls.append(
            (
                "media.import_directory",
                {
                    "project": project,
                    "directory": str(directory),
                    "realm": realm,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            [
                {
                    "path": "a.png",
                    "media": {"id": "M-1"},
                    "receipt": _receipt("core.media.import", f"{key}#0").as_dict(),
                    "idempotency_key": f"{key}#0",
                }
            ],
            idempotency_key=key,
        )

    def list(self, project):
        self._owner.calls.append(("media.list", {"project": project}))
        return DomainResult.success([{"id": "M-1", "project_id": "P-1"}])

    def show(self, project, ref):
        self._owner.calls.append(("media.show", {"project": project, "ref": ref}))
        return DomainResult.success({"id": ref, "project_id": "P-1"})

    def verify(self, project, ref, *, realm, idempotency_key=None):
        self._owner.calls.append(
            (
                "media.verify",
                {
                    "project": project,
                    "ref": ref,
                    "realm": realm,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"id": ref, "verified": True},
            receipt=_receipt("core.media.verified", key),
            idempotency_key=key,
        )

    def relate(self, project, *, relations, idempotency_key=None):
        self._owner.calls.append(
            (
                "media.relate",
                {
                    "project": project,
                    "relations": list(relations),
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"relations": [dict(r) for r in relations]},
            receipt=_receipt("core.media.relate", key),
            idempotency_key=key,
        )


class _RecordingReferences:
    def __init__(self, owner: "_FakeClient") -> None:
        self._owner = owner

    def create(
        self,
        *,
        project,
        kind,
        name,
        media_id,
        description="",
        metadata=None,
        idempotency_key=None,
    ):
        self._owner.calls.append(
            (
                "references.create",
                {
                    "project": project,
                    "kind": kind,
                    "name": name,
                    "media_id": media_id,
                    "description": description,
                    "metadata": metadata,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"reference_id": "R-1", "kind": kind, "name": name, "media_id": media_id},
            receipt=_receipt("reference.create", key),
            idempotency_key=key,
        )

    def update(
        self,
        project,
        ref,
        *,
        name=None,
        description=None,
        metadata=None,
        idempotency_key=None,
    ):
        self._owner.calls.append(
            (
                "references.update",
                {
                    "project": project,
                    "ref": ref,
                    "name": name,
                    "description": description,
                    "metadata": metadata,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"reference_id": ref, "name": name or "Aria"},
            receipt=_receipt("reference.update", key),
            idempotency_key=key,
        )

    def archive(self, project, ref, *, idempotency_key=None):
        self._owner.calls.append(
            (
                "references.archive",
                {
                    "project": project,
                    "ref": ref,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"reference_id": ref, "archived": True},
            receipt=_receipt("reference.archive", key),
            idempotency_key=key,
        )

    def recover(self, project, ref, *, idempotency_key=None):
        self._owner.calls.append(
            (
                "references.recover",
                {"project": project, "ref": ref, "idempotency_key": idempotency_key},
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"reference_id": ref, "status": "active", "changed": True},
            receipt=_receipt("reference.recover", key),
            idempotency_key=key,
        )

    def associate(
        self,
        project,
        ref,
        *,
        media_id,
        role,
        idempotency_key=None,
    ):
        self._owner.calls.append(
            (
                "references.associate",
                {
                    "project": project,
                    "ref": ref,
                    "media_id": media_id,
                    "role": role,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"reference_id": ref, "media_reference_id": "MR-1", "media_id": media_id},
            receipt=_receipt("reference.associate", key),
            idempotency_key=key,
        )

    def link(
        self,
        project,
        *,
        from_reference_id,
        to_reference_id,
        kind,
        metadata=None,
        idempotency_key=None,
    ):
        self._owner.calls.append(
            (
                "references.link",
                {
                    "project": project,
                    "from_reference_id": from_reference_id,
                    "to_reference_id": to_reference_id,
                    "kind": kind,
                    "metadata": metadata,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {
                "from_reference_id": from_reference_id,
                "to_reference_id": to_reference_id,
                "kind": kind,
            },
            receipt=_receipt("reference.link", key),
            idempotency_key=key,
        )

    def set_primary(
        self,
        project,
        ref,
        *,
        association_id,
        idempotency_key=None,
    ):
        self._owner.calls.append(
            (
                "references.set_primary",
                {
                    "project": project,
                    "ref": ref,
                    "association_id": association_id,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        key = idempotency_key or "generated-key"
        return DomainResult.success(
            {"reference_id": ref, "media_reference_id": association_id},
            receipt=_receipt("reference.set_primary", key),
            idempotency_key=key,
        )

    def list(self, project, *, include_archived=False):
        kwargs = {"project": project}
        if include_archived:
            kwargs["include_archived"] = True
        self._owner.calls.append(
            (
                "references.list",
                kwargs,
            )
        )
        return DomainResult.success([{"reference_id": "R-1", "name": "Aria"}])

    def show(self, project, ref):
        self._owner.calls.append(("references.show", {"project": project, "ref": ref}))
        return DomainResult.success({"reference_id": ref, "name": "Aria"})


class _FakeClient:
    """Minimal AstridClient stand-in: records every SDK call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.media = _RecordingMedia(self)
        self.references = _RecordingReferences(self)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _run(family: str, args: list[str], client: _FakeClient | None = None) -> int:
    return run_product_family(family, args, client=client or _FakeClient())


def _subparser_choices(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("parser has no subparsers")


# ---------------------------------------------------------------------------
# T30 — media family
# ---------------------------------------------------------------------------


def test_media_parser_has_thumbnail_backfill_and_references_mount() -> None:
    from astrid.core.cli.domain_media import COMMANDS, build_parser
    from astrid.packs.references.cli import COMMANDS as REFERENCE_COMMANDS

    assert tuple(spec.name for spec in COMMANDS) == (
        "import",
        "list",
        "show",
        "open",
        "verify",
        "relate",
        "thumbnails",
    )
    assert all(spec.aliases == () for spec in COMMANDS)
    parser = build_parser(
        _FakeClient(), reference_commands=REFERENCE_COMMANDS
    )
    assert parser.prog == "astrid media"
    assert _subparser_choices(parser) == {
        "import",
        "list",
        "show",
        "open",
        "verify",
        "relate",
        "thumbnails",
        "references",
    }


def test_product_dispatch_injects_references_commands_at_composition_boundary() -> None:
    import astrid.core.cli.domain_product as domain_product
    from astrid.packs.references import cli as references_cli

    seen: dict[str, object] = {}
    media_module = types.ModuleType("media_stub")

    def build_parser(client, *, reference_commands):  # noqa: ANN001
        seen["client"] = client
        seen["reference_commands"] = reference_commands
        parser = argparse.ArgumentParser()
        parser.set_defaults(handler=lambda parsed: 0)
        return parser

    media_module.build_parser = build_parser  # type: ignore[attr-defined]
    client = object()
    assert domain_product.run_product_family(
        "media",
        [],
        client=client,
        _parser_modules=cast(
            Any, {"media": media_module, "references": references_cli}
        ),
    ) == 0
    assert seen == {
        "client": client,
        "reference_commands": references_cli.COMMANDS,
    }


def test_product_dispatch_rejects_missing_or_malformed_reference_commands() -> None:
    import astrid.core.cli.domain_product as domain_product

    media_module = types.ModuleType("media_stub")
    media_module.build_parser = (  # type: ignore[attr-defined]
        lambda client: argparse.ArgumentParser()
    )
    missing = types.ModuleType("missing_references")
    malformed = types.ModuleType("malformed_references")
    malformed.COMMANDS = ()  # type: ignore[attr-defined]

    for references_module in (missing, malformed):
        with pytest.raises(ProductRegistryError, match="valid COMMANDS"):
            domain_product.run_product_family(
                "media",
                [],
                client=object(),
                _parser_modules=cast(
                    Any,
                    {
                        "media": media_module,
                        "references": references_module,
                    },
                ),
            )


def test_no_top_level_references_family() -> None:
    """references is not a product family: product dispatch rejects it."""
    from astrid.core.gateway import dispatch

    assert "references" not in PRODUCT_FAMILY_SET
    assert not is_product_family("references")
    assert "references" not in dispatch._top_level_commands()
    client = _FakeClient()
    with pytest.raises(ProductRegistryError, match="not a product family"):
        _run("references", ["list", "--project", "demo"], client=client)
    assert client.calls == []


def test_media_import_file_is_one_sdk_call_with_exact_envelope(tmp_path, capsys) -> None:
    path = tmp_path / "shot.png"
    path.write_bytes(b"png-bytes")
    client = _FakeClient()
    rc = _run(
        "media",
        ["import", str(path), "--project", "demo", "--json"],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "media.import_file",
            {
                "project": "demo",
                "path": str(path),
                "realm": "managed_local",
                "idempotency_key": None,
            },
        )
    ]
    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["ok"] is True
    assert envelope["data"]["id"] == "M-1"
    assert envelope["receipt"]["command_kind"] == "core.media.import"
    assert envelope["idempotency_key"] == "generated-key"


def test_media_import_human_output_includes_reusable_handle_and_swap_command(tmp_path, capsys) -> None:
    path = tmp_path / "shot.png"
    path.write_bytes(b"png-bytes")
    rc = _run("media", ["import", str(path), "--project", "demo"], client=_FakeClient())
    lines = capsys.readouterr().out.splitlines()

    assert rc == 0
    handle = "sha256:" + "a" * 64
    assert f"handle: {handle}" in lines
    assert f"--swap-asset {handle}" in lines[-1]


def test_media_import_directory_is_one_sdk_call(tmp_path, capsys) -> None:
    directory = tmp_path / "in"
    directory.mkdir()
    (directory / "a.png").write_bytes(b"a")
    client = _FakeClient()
    rc = _run(
        "media",
        [
            "import",
            str(directory),
            "--project",
            "demo",
            "--idempotency-key",
            "dir-key",
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    (verb, kwargs) = client.calls[0]
    assert verb == "media.import_directory"
    assert kwargs == {
        "project": "demo",
        "directory": str(directory),
        "realm": "managed_local",
        "idempotency_key": "dir-key",
    }
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["data"][0]["idempotency_key"] == "dir-key#0"
    assert envelope["idempotency_key"] == "dir-key"


def test_media_import_rejects_non_file_or_directory(tmp_path) -> None:
    missing = tmp_path / "missing.png"
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run("media", ["import", str(missing), "--project", "demo"], client=client)
    assert excinfo.value.code == 2
    assert client.calls == []


def test_media_import_rejects_external_realm_before_sdk_call() -> None:
    import os

    path = os.path.abspath(__file__)
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run("media", ["import", path, "--project", "demo", "--realm", "external_local"], client=client)
    assert excinfo.value.code == 2
    assert client.calls == []


def test_media_list_is_one_sdk_call(capsys) -> None:
    client = _FakeClient()
    rc = _run("media", ["list", "--project", "demo", "--json"], client=client)
    assert rc == 0
    assert client.calls == [("media.list", {"project": "demo"})]
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["data"] == [{"id": "M-1", "project_id": "P-1"}]
    assert envelope["receipt"] is None
    assert envelope["idempotency_key"] == ""


def test_media_show_is_one_sdk_call(capsys) -> None:
    client = _FakeClient()
    rc = _run("media", ["show", "--project", "demo", "M-1", "--json"], client=client)
    assert rc == 0
    assert client.calls == [("media.show", {"project": "demo", "ref": "M-1"})]
    assert json.loads(capsys.readouterr().out)["data"]["id"] == "M-1"


def test_media_verify_is_one_sdk_call_with_realm_and_key(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "media",
        [
            "verify",
            "--project",
            "demo",
            "M-1",
            "--realm",
            "managed_local",
            "--idempotency-key",
            "verify-key",
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "media.verify",
            {
                "project": "demo",
                "ref": "M-1",
                "realm": "managed_local",
                "idempotency_key": "verify-key",
            },
        )
    ]
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["data"]["verified"] is True
    assert envelope["receipt"]["command_kind"] == "core.media.verified"
    assert envelope["idempotency_key"] == "verify-key"


def test_media_verify_requires_realm() -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run("media", ["verify", "--project", "demo", "M-1"], client=client)
    assert excinfo.value.code == 2
    assert client.calls == []


def test_media_verify_rejects_unknown_realm() -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run(
            "media",
            [
                "verify",
                "--project",
                "demo",
                "M-1",
                "--realm",
                "cloud",
            ],
            client=client,
        )
    assert excinfo.value.code == 2
    assert client.calls == []


def test_media_relate_is_one_sdk_call_with_frozen_kind(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "media",
        [
            "relate",
            "--project",
            "demo",
            "--from",
            "M-1",
            "--to",
            "M-2",
            "--kind",
            "variant_of",
            "--ordinal",
            "0",
            "--metadata",
            '{"note": "v2"}',
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    (verb, kwargs) = client.calls[0]
    assert verb == "media.relate"
    assert kwargs == {
        "project": "demo",
        "relations": [
            {
                "from_media_id": "M-1",
                "to_media_id": "M-2",
                "kind": "variant_of",
                "ordinal": 0,
                "metadata": {"note": "v2"},
            }
        ],
        "idempotency_key": None,
    }
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["receipt"]["command_kind"] == "core.media.relate"


def test_media_relate_rejects_kind_outside_frozen_five() -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run(
            "media",
            [
                "relate",
                "--project",
                "demo",
                "--from",
                "M-1",
                "--to",
                "M-2",
                "--kind",
                "remix_of",
            ],
            client=client,
        )
    assert excinfo.value.code == 2
    assert client.calls == []


def test_media_failure_envelope_exits_one(capsys) -> None:
    class _FailingMedia(_RecordingMedia):
        def show(self, project, ref):
            self._owner.calls.append(("media.show", {"project": project, "ref": ref}))
            return DomainResult.failure(
                ErrorObject(
                    code="media_not_found",
                    message="no media row for M-9",
                    details={"media_id": ref},
                )
            )

    class _FailingClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.media = _FailingMedia(self)

    client = _FailingClient()
    rc = _run("media", ["show", "--project", "demo", "M-9", "--json"], client=client)
    assert rc == 1
    assert len(client.calls) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["error"]["code"] == "media_not_found"
    assert json.loads(captured.out)["error"]["message"] == "no media row for M-9"


def test_media_unknown_verb_is_a_usage_error() -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run("media", ["export", "--project", "demo"], client=client)
    assert excinfo.value.code == 2
    assert client.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["import", "--help"],
        ["list", "--help"],
        ["show", "--help"],
        ["verify", "--help"],
        ["relate", "--help"],
    ],
)
def test_media_help_is_executable(argv: list[str]) -> None:
    from astrid.core.cli.domain_media import build_parser
    from astrid.packs.references.cli import COMMANDS as REFERENCE_COMMANDS

    parser = build_parser(
        _FakeClient(), reference_commands=REFERENCE_COMMANDS
    )
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(argv)
    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# T30 — nested references family beneath media
# ---------------------------------------------------------------------------


def test_references_parser_has_exactly_nine_verbs_beneath_media() -> None:
    from astrid.packs.references.cli import COMMANDS, build_parser

    assert tuple(spec.name for spec in COMMANDS) == (
        "create",
        "update",
        "archive",
        "recover",
        "associate",
        "link",
        "set-primary",
        "list",
        "show",
    )
    assert all(spec.aliases == () for spec in COMMANDS)
    parser = build_parser(_FakeClient())
    assert parser.prog == "astrid media references"
    assert _subparser_choices(parser) == {
        "create",
        "update",
        "archive",
        "recover",
        "associate",
        "link",
        "set-primary",
        "list",
        "show",
    }
    parsed = parser.parse_args(["list", "--project", "demo"])
    assert parsed.json is False


def test_media_references_routes_beneath_media_parser(capsys) -> None:
    """``media references`` is reachable only through the media parser."""
    client = _FakeClient()
    rc = _run(
        "media",
        ["references", "list", "--project", "demo", "--json"],
        client=client,
    )
    assert rc == 0
    assert client.calls == [("references.list", {"project": "demo"})]
    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["ok"] is True


def test_references_create_is_one_sdk_call_with_exact_envelope(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "media",
        [
            "references",
            "create",
            "--project",
            "demo",
            "--kind",
            "character",
            "--name",
            "Aria",
            "--media",
            "M-1",
            "--description",
            "protagonist",
            "--metadata",
            '{"tone": "warm"}',
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "references.create",
            {
                "project": "demo",
                "kind": "character",
                "name": "Aria",
                "media_id": "M-1",
                "description": "protagonist",
                "metadata": {"tone": "warm"},
                "idempotency_key": None,
            },
        )
    ]
    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["ok"] is True
    assert envelope["data"]["media_id"] == "M-1"
    assert envelope["receipt"]["command_kind"] == "reference.create"
    assert envelope["idempotency_key"] == "generated-key"


def test_references_create_forwards_caller_key() -> None:
    client = _FakeClient()
    rc = _run(
        "media",
        [
            "references",
            "create",
            "--project",
            "demo",
            "--kind",
            "object",
            "--name",
            "Lamp",
            "--media",
            "M-2",
            "--idempotency-key",
            "ref-key",
        ],
        client=client,
    )
    assert rc == 0
    assert client.calls[0][1]["idempotency_key"] == "ref-key"


def test_references_create_rejects_kind_outside_frozen_vocabulary() -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run(
            "media",
            [
                "references",
                "create",
                "--project",
                "demo",
                "--kind",
                "prop",
                "--name",
                "Lamp",
                "--media",
                "M-2",
            ],
            client=client,
        )
    assert excinfo.value.code == 2
    assert client.calls == []


def test_references_update_is_one_sdk_call(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "media",
        [
            "references",
            "update",
            "--project",
            "demo",
            "R-1",
            "--name",
            "Aria v2",
            "--metadata",
            '{"tone": "cool"}',
            "--idempotency-key",
            "upd-key",
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "references.update",
            {
                "project": "demo",
                "ref": "R-1",
                "name": "Aria v2",
                "description": None,
                "metadata": {"tone": "cool"},
                "idempotency_key": "upd-key",
            },
        )
    ]
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["idempotency_key"] == "upd-key"


def test_references_archive_is_one_sdk_call(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "media",
        ["references", "archive", "--project", "demo", "R-1", "--json"],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "references.archive",
            {"project": "demo", "ref": "R-1", "idempotency_key": None},
        )
    ]
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["data"]["archived"] is True


def test_references_recover_accepts_recovery_name_in_one_sdk_call(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "media",
        ["references", "recover", "--project", "demo", "Seed", "--json"],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "references.recover",
            {"project": "demo", "ref": "Seed", "idempotency_key": None},
        )
    ]
    assert json.loads(capsys.readouterr().out)["data"]["changed"] is True


def test_references_associate_is_one_sdk_call_with_role(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "media",
        [
            "references",
            "associate",
            "--project",
            "demo",
            "R-1",
            "--media",
            "M-3",
            "--role",
            "used_as_input",
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "references.associate",
            {
                "project": "demo",
                "ref": "R-1",
                "media_id": "M-3",
                "role": "used_as_input",
                "idempotency_key": None,
            },
        )
    ]
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["receipt"]["command_kind"] == "reference.associate"


def test_references_associate_rejects_role_outside_frozen_vocabulary() -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run(
            "media",
            [
                "references",
                "associate",
                "--project",
                "demo",
                "R-1",
                "--media",
                "M-3",
                "--role",
                "owns",
            ],
            client=client,
        )
    assert excinfo.value.code == 2
    assert client.calls == []


def test_references_link_is_one_sdk_call_with_frozen_kind(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "media",
        [
            "references",
            "link",
            "--project",
            "demo",
            "--from",
            "R-1",
            "--to",
            "R-2",
            "--kind",
            "related_to",
            "--metadata",
            '{"strength": "strong"}',
            "--idempotency-key",
            "link-key",
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "references.link",
            {
                "project": "demo",
                "from_reference_id": "R-1",
                "to_reference_id": "R-2",
                "kind": "related_to",
                "metadata": {"strength": "strong"},
                "idempotency_key": "link-key",
            },
        )
    ]
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["receipt"]["command_kind"] == "reference.link"
    assert envelope["idempotency_key"] == "link-key"


def test_references_link_rejects_kind_outside_frozen_five() -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run(
            "media",
            [
                "references",
                "link",
                "--project",
                "demo",
                "--from",
                "R-1",
                "--to",
                "R-2",
                "--kind",
                "friends_with",
            ],
            client=client,
        )
    assert excinfo.value.code == 2
    assert client.calls == []


def test_references_list_is_one_sdk_call(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "media",
        [
            "references",
            "list",
            "--project",
            "demo",
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    assert client.calls == [("references.list", {"project": "demo"})]
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["data"] == [{"reference_id": "R-1", "name": "Aria"}]


def test_references_show_is_one_sdk_call(capsys) -> None:
    client = _FakeClient()
    rc = _run("media", ["references", "show", "--project", "demo", "R-1", "--json"], client=client)
    assert rc == 0
    assert client.calls == [("references.show", {"project": "demo", "ref": "R-1"})]
    assert json.loads(capsys.readouterr().out)["data"]["reference_id"] == "R-1"


def test_references_failure_envelope_exits_one(capsys) -> None:
    class _FailingReferences(_RecordingReferences):
        def show(self, project, ref):
            self._owner.calls.append(("references.show", {"project": project, "ref": ref}))
            return DomainResult.failure(
                ErrorObject(
                    code="reference_not_found",
                    message="missing",
                    details={"reference_id": ref},
                )
            )

    class _FailingClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.references = _FailingReferences(self)

    client = _FailingClient()
    rc = _run("media", ["references", "show", "--project", "demo", "R-9", "--json"], client=client)
    assert rc == 1
    assert len(client.calls) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["error"]["code"] == "reference_not_found"
    assert json.loads(captured.out)["error"]["message"] == "missing"


def test_references_unknown_verb_is_a_usage_error() -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run(
            "media",
            ["references", "frobnicate", "--project", "demo", "R-1"],
            client=client,
        )
    assert excinfo.value.code == 2
    assert client.calls == []


def test_references_set_primary_is_one_sdk_call(capsys) -> None:
    client = _FakeClient()
    rc = _run(
        "media",
        [
            "references",
            "set-primary",
            "--project",
            "demo",
            "R-1",
            "--media-reference",
            "MR-2",
            "--idempotency-key",
            "sp-key",
            "--json",
        ],
        client=client,
    )
    assert rc == 0
    assert client.calls == [
        (
            "references.set_primary",
            {
                "project": "demo",
                "ref": "R-1",
                "association_id": "MR-2",
                "idempotency_key": "sp-key",
            },
        )
    ]
    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) == ENVELOPE_KEYS
    assert envelope["ok"] is True
    assert envelope["receipt"]["command_kind"] == "reference.set_primary"
    assert envelope["idempotency_key"] == "sp-key"


def test_references_set_primary_rejects_missing_media_reference() -> None:
    client = _FakeClient()
    with pytest.raises(SystemExit) as excinfo:
        _run(
            "media",
            ["references", "set-primary", "--project", "demo", "R-1"],
            client=client,
        )
    assert excinfo.value.code == 2
    assert client.calls == []


@pytest.mark.parametrize(
    "argv",
    [
        ["references", "--help"],
        ["references", "create", "--help"],
        ["references", "update", "--help"],
        ["references", "archive", "--help"],
        ["references", "associate", "--help"],
        ["references", "link", "--help"],
        ["references", "set-primary", "--help"],
        ["references", "list", "--help"],
        ["references", "show", "--help"],
    ],
)
def test_media_references_help_is_executable(argv: list[str]) -> None:
    from astrid.core.cli.domain_media import build_parser
    from astrid.packs.references.cli import COMMANDS as REFERENCE_COMMANDS

    parser = build_parser(
        _FakeClient(), reference_commands=REFERENCE_COMMANDS
    )
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(argv)
    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# T30 — gateway dispatch cutover for the media family
# ---------------------------------------------------------------------------


def test_dispatch_media_routes_through_product_dispatch(monkeypatch) -> None:
    from astrid.core.gateway import dispatch

    seen: dict[str, object] = {}

    def _fake_product(args):  # noqa: ANN001
        seen["args"] = list(args)
        return 5

    monkeypatch.setattr(dispatch, "_dispatch_product", _fake_product)
    assert dispatch._dispatch_media(["import", "in.png", "--project", "demo"]) == 5
    assert seen["args"] == ["media", "import", "in.png", "--project", "demo"]


def test_dispatch_media_is_a_registered_top_level_command() -> None:
    from astrid.core.gateway import dispatch

    assert "media" in dispatch._top_level_commands()
    # Current gateway surface: eight families (five product + three operational).
    assert dispatch._top_level_commands() == frozenset(
        {
            "projects",
            "preferences",
            "documents",
            "timelines",
            "media",
            "tasks",
            "runs",
            "doctor",
            "backup",
            "dev",
        }
    )
