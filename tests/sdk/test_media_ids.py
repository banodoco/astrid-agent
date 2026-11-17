"""One media-id contract: canonical spelling, accepted spellings, resolution in a project.

Every command that takes a media id resolves it through ``astrid.sdk.media_ids``; every printed
media id is the canonical ``sha256:`` form.
"""

from __future__ import annotations

import base64
import hashlib
import io
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from PIL import Image

from astrid.sdk.media_ids import (
    MediaIdError,
    canonical_digest,
    media_id_text,
    resolve_project_media,
)

D1 = "sha256:74b9c3d7" + "a" * 56          # shares an 8-char prefix with D2
D2 = "sha256:74b9c3d7" + "b" * 56
D3 = "sha256:1234abcd" + "0" * 56
MISSING = "sha256:" + "f" * 64


def _row(digest: str, *, filename: str = "still.png", object_id: str | None = None) -> dict[str, Any]:
    return {"digest": digest, "object_id": object_id or digest, "filename": filename,
            "media_type": "image/png", "size": 10}


ROWS = [_row(D1, filename="a.png"), _row(D2, filename="b.png"), _row(D3, filename="c.png")]


class _Transport:
    """The generated Runtime client surface that RemoteMedia calls."""

    def __init__(self, rows=None) -> None:
        self.rows = list(ROWS if rows is None else rows)
        self.relations: list[dict[str, Any]] = []
        self.heads: list[str] = []
        self.objects: dict[str, bytes] = {}

    def list_project_objects(self, project, *, cursor=None, limit=50, **filters):
        return (list(self.rows), None)

    def create_media_relation(self, project, from_object_id, to_object_id, kind, *,
                              idempotency_key, metadata=None, ordinal=0):
        self.relations.append({"project": project, "from": from_object_id, "to": to_object_id,
                               "kind": kind, "ordinal": ordinal, "metadata": metadata})
        return {"ok": True}

    def head_object(self, object_id):
        self.heads.append(object_id)
        return {"object_id": object_id}

    def get_object(self, object_id):
        return {"data": self.objects[object_id]}


def _media(transport: _Transport):
    from astrid.sdk.remote import RemoteMedia

    return RemoteMedia(transport)


# -- the one contract -----------------------------------------------------------


def test_canonical_digest_accepts_the_labelled_and_bare_spellings() -> None:
    assert canonical_digest(D1) == D1
    assert canonical_digest(D1[7:]) == D1
    assert canonical_digest(D1[7:].upper()) == D1
    assert canonical_digest("sha256:" + "a" * 63) is None
    assert canonical_digest("74b9c3d7") is None  # a prefix is not a full digest
    assert canonical_digest("O-src") is None


def test_printed_media_id_is_always_the_canonical_form() -> None:
    assert media_id_text(D1[7:]) == D1
    assert media_id_text(D1) == D1
    assert media_id_text("unknown") == "unknown"


def test_resolver_accepts_full_bare_and_unique_prefix_forms() -> None:
    assert resolve_project_media(ROWS, "demo", D3)["digest"] == D3
    assert resolve_project_media(ROWS, "demo", D3[7:])["digest"] == D3
    assert resolve_project_media(ROWS, "demo", "1234abcd")["digest"] == D3
    assert resolve_project_media(ROWS, "demo", "sha256:1234ABCD")["digest"] == D3


def test_ambiguous_prefix_is_refused_and_lists_the_matches() -> None:
    with pytest.raises(MediaIdError) as caught:
        resolve_project_media(ROWS, "demo", "74b9c3d7")
    assert caught.value.code == "validation_error"
    message = str(caught.value)
    assert "ambiguous" in message and "matches 2 media" in message
    assert D1 in message and D2 in message and "a.png" in message


def test_a_longer_prefix_resolves_the_ambiguity() -> None:
    assert resolve_project_media(ROWS, "demo", "74b9c3d7" + "a" * 4)["digest"] == D1


def test_prefix_shorter_than_eight_characters_is_refused() -> None:
    with pytest.raises(MediaIdError) as caught:
        resolve_project_media(ROWS, "demo", "1234abc")
    assert caught.value.code == "validation_error"
    assert "too short" in str(caught.value)


def test_no_match_says_no_media_in_the_project_matches_the_id() -> None:
    with pytest.raises(MediaIdError) as caught:
        resolve_project_media(ROWS, "demo", "ffffffff")
    assert caught.value.code == "not_found"
    assert str(caught.value) == "no media in project demo matches ffffffff"
    with pytest.raises(MediaIdError) as caught:
        resolve_project_media(ROWS, "demo", MISSING)
    assert str(caught.value) == f"no media in project demo matches {MISSING}"
    assert "not in the selected project" not in str(caught.value)


def test_runtime_object_id_stored_verbatim_still_matches_exactly() -> None:
    rows = [_row(D1, object_id="O-src")]
    assert resolve_project_media(rows, "demo", "O-src")["digest"] == D1


# -- RemoteMedia: show, verify, relate ----------------------------------------


def test_media_show_accepts_a_bare_digest_and_returns_canonical_fields() -> None:
    result = _media(_Transport()).show("demo", D1[7:])
    assert result.ok, result.error
    assert result.data["digest"] == D1
    assert result.data["object_id"] == D1


def test_media_show_no_match_is_the_shared_message() -> None:
    result = _media(_Transport()).show("demo", "deadbeef00")
    assert not result.ok
    assert result.error.code == "not_found"
    assert result.error.message == "no media in project demo matches deadbeef00"


def test_media_show_ambiguous_prefix_fails_with_matches() -> None:
    result = _media(_Transport()).show("demo", "74b9c3d7")
    assert not result.ok and result.error.code == "validation_error"
    assert D1 in result.error.message and D2 in result.error.message


def test_media_verify_checks_and_reports_the_canonical_object() -> None:
    transport = _Transport()
    result = _media(transport).verify("demo", D3[7:], realm="managed_local")
    assert result.ok, result.error
    assert transport.heads == [D3]
    assert result.data["object_id"] == D3


def test_media_relate_takes_the_cli_contract_and_resolves_both_ends() -> None:
    transport = _Transport()
    result = _media(transport).relate("demo", relations=[{
        "from_media_id": D1[7:], "to_media_id": "1234abcd", "kind": "derived_from", "ordinal": 2,
    }], idempotency_key="k1")
    assert result.ok, result.error
    assert transport.relations == [{"project": "demo", "from": D1, "to": D3, "kind": "derived_from",
                                    "ordinal": 2, "metadata": None}]


def test_media_relate_keyword_form_resolves_prefixes_too() -> None:
    transport = _Transport()
    result = _media(transport).relate("demo", from_object_id="1234abcd", to_object_id=D1[7:],
                                      kind="variant_of", idempotency_key="k2")
    assert result.ok, result.error
    assert (transport.relations[0]["from"], transport.relations[0]["to"]) == (D3, D1)


def test_media_relate_refuses_one_relation_per_call_and_self_relations() -> None:
    media = _media(_Transport())
    many = media.relate("demo", relations=[{"from_media_id": D1, "to_media_id": D3, "kind": "derived_from"}] * 2)
    assert not many.ok and many.error.code == "validation_error"
    same = media.relate("demo", from_object_id=D1[7:], to_object_id=D1, kind="variant_of")
    assert not same.ok and same.error.code == "validation_error"


# -- media handles, open, transcripts, shot plans ----------------------------


def test_media_handle_accepts_a_bare_digest_and_a_prefix() -> None:
    from astrid.sdk.media_handles import resolve_media_handle

    client = SimpleNamespace(media=_media(_Transport()))
    for value in (D3[7:], "1234abcd", D3):
        descriptor, record = resolve_media_handle(client, "demo", value)
        assert descriptor["digest"] == D3
        assert record["digest"] == D3


def test_media_handle_ambiguous_prefix_is_refused() -> None:
    from astrid.sdk.media_handles import MediaHandleError, resolve_media_handle

    client = SimpleNamespace(media=_media(_Transport()))
    with pytest.raises(MediaHandleError, match="ambiguous"):
        resolve_media_handle(client, "demo", "74b9c3d7")


def test_media_open_finds_a_bare_id_and_returns_its_bytes() -> None:
    from astrid.sdk.media_open import open_reference

    payload = b"hello"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    transport = _Transport([_row(digest)])
    transport.objects[digest] = payload
    result = open_reference(transport, "demo", digest[7:], preview_bytes=16)
    assert result.ok, result.error
    assert base64.b64decode(result.data["preview"]["data"]) == payload


def test_transcript_identity_is_the_same_for_bare_and_canonical_ids() -> None:
    from astrid.sdk.media_transcript import transcript_document_id

    assert transcript_document_id("proj", D1[7:], "src") == transcript_document_id("proj", D1, "src")


def test_shot_plan_media_is_written_in_canonical_form() -> None:
    from astrid.sdk.shot_plan import ShotPlanError, canonical_plan_media

    client = SimpleNamespace(media=_media(_Transport()))
    plan = {
        "inputs": {"references": [D3[7:]], "masks": [D1], "voice": {"media": "1234abcd", "range": [1, 2]}},
        "takes": [{"media": D3[7:], "note": "x"}],
    }
    out = canonical_plan_media(client, "demo", plan)
    assert out["inputs"]["references"] == [D3]
    assert out["inputs"]["masks"] == [D1]
    assert out["inputs"]["voice"] == {"media": D3, "range": [1, 2]}
    assert out["takes"] == [{"media": D3, "note": "x"}]
    with pytest.raises(ShotPlanError, match="ambiguous"):
        canonical_plan_media(client, "demo", {"inputs": {"references": ["74b9c3d7"]}})


def test_timelines_assets_prints_the_canonical_digest(capsys, monkeypatch) -> None:
    import astrid.sdk.timeline_checkout as checkout_module
    from astrid.packs.timeline.cli import _cmd_assets

    class _Checkout:
        @classmethod
        def open(cls, project, ref, client=None):
            return cls()

        def assets(self, key=None):
            return [{"key": "still", "media": {"content_sha256": D1[7:], "type": "image/png", "size_bytes": 3},
                     "clips": ["c1"]}]

    monkeypatch.setattr(checkout_module, "find_draft", lambda *a, **k: None)
    monkeypatch.setattr(checkout_module, "Checkout", _Checkout)
    parsed = SimpleNamespace(project="demo", ref="main", draft=None, key=None, client=None)
    assert _cmd_assets(parsed) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"still  {D1}  image/png  3 bytes")


# -- media crop accepts every spelling and dry-runs the pillarbox case ----------


def _pillarbox_png() -> bytes:
    rng = np.random.default_rng(3)
    arr = rng.integers(60, 256, size=(200, 320, 3), dtype=np.uint8)
    arr[:, :60] = 0
    arr[:, 260:] = 0
    out = io.BytesIO()
    Image.fromarray(arr, "RGB").save(out, format="PNG")
    return out.getvalue()


def test_media_crop_accepts_a_bare_digest_and_reports_canonical_source() -> None:
    from astrid.core.media_crop import crop_media

    data = _pillarbox_png()
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    transport = _Transport([{"digest": digest, "object_id": digest, "filename": "frame.png", "media_type": "image/png"}])

    from astrid.sdk.remote import RemoteMedia

    class _Media(RemoteMedia):
        def read_bytes(self, object_id):
            return data

    client = SimpleNamespace(media=_Media(transport))
    result = crop_media(client, project="demo", ref=digest[7:19], remove_bars=True, dry_run=True)
    assert result.ok, result.error
    assert result.data["source"]["digest"] == digest
    assert result.data["box"] == {"x": 60, "y": 0, "w": 200, "h": 200}


# -- an exact reference name beats a hex media prefix --------------------------

D4 = "sha256:cafe1234" + "c" * 56        # a media row whose prefix equals the reference name below
REF_NAME = "cafe1234"


def _reference_client(*, exists: bool):
    """A client with one reference named cafe1234 (primary media D1) and a media row D4 starting cafe1234."""
    rows = [_row(D4, filename="prefix.png"), _row(D1, filename="a.png")]
    transport = _Transport(rows)

    def show(project, ref):
        if exists and ref == REF_NAME:
            return SimpleNamespace(ok=True, error=None, data={
                "reference_id": "ref-1", "name": REF_NAME,
                "media_references": [{"association_id": "as-1", "media_id": D1, "ordinal": 0, "is_primary": True}],
            })
        return SimpleNamespace(ok=False, data=None, error=SimpleNamespace(code="not_found", message="no reference"))

    return SimpleNamespace(media=_media(transport), references=SimpleNamespace(show=show))


def test_bare_hex_name_that_is_a_reference_resolves_to_the_reference_not_the_prefix() -> None:
    from astrid.sdk.media_handles import resolve_media_handle

    descriptor, record = resolve_media_handle(_reference_client(exists=True), "demo", REF_NAME)
    assert descriptor["digest"] == D1
    assert record["handle"] == "ref:cafe1234"


def test_bare_hex_name_without_a_reference_is_a_media_prefix() -> None:
    from astrid.sdk.media_handles import resolve_media_handle

    descriptor, _ = resolve_media_handle(_reference_client(exists=False), "demo", REF_NAME)
    assert descriptor["digest"] == D4


def test_references_entry_with_a_hex_reference_name_resolves_to_the_reference() -> None:
    from astrid.sdk.media_handles import apply_media_handles

    capability = SimpleNamespace(id="gen", inputs=(SimpleNamespace(name="image_ref", type="file"),))
    values, lineage, links = apply_media_handles(
        _reference_client(exists=True), "demo", capability,
        {"references": [{"ref": REF_NAME, "role": "source"}]},
    )
    assert values["image_ref"]["digest"] == D1
    assert links and links[0]["reference_id"] == "ref-1"
