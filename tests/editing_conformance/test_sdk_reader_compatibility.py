"""Public SDK creation and replay at the locked Runtime boundary."""

from io import BytesIO

import pytest
from PIL import Image

from astrid.core.receipts.canonical import canonical_bytes
from tests.editing_conformance.test_commit_runtime_m1 import open_client

pytestmark = pytest.mark.integration

_RATIO = {"numerator": 30000, "denominator": 1001}
_EXTENSION = {"keep": [None, {}, False, 0, 0.0]}


def _payload(result):
    return result.data["canonical_document"]["parent_revision"]["payload"]


def _history(client, project):
    result = client.timelines.history(project, "film", limit=100)
    assert result.ok, result.as_dict()
    return result.data


def _commit(client, project, command, key):
    head = _history(client, project)["head"]
    result = client.timelines.commit(project, "film", {
        "api_version": 1, "project_id": project, "timeline_id": "film",
        "base_revision_id": head["revision_id"], "base_publication_id": head["publication_id"],
        "commands": [{"schema_version": 1, "version": "1", "select": None, **command}],
    }, idempotency_key=key)
    assert result.ok, result.as_dict()
    return result


def _assert_exact_reload(client, project, expected):
    opened = client.timelines.open_bundle(project, "film")
    assert opened.ok, opened.as_dict()
    revision_id = _history(client, project)["head"]["revision_id"]
    revision = client._remote._transport.get_project_parent_composition_revision(project, "film", revision_id)
    assert canonical_bytes(revision["payload"]) == canonical_bytes(expected)


def _picture(daemon, project):
    image = BytesIO()
    Image.new("RGB", (16, 16), "red").save(image, "PNG")
    media = daemon.service.ingest(
        project, image.getvalue(), media_type="image/png", idempotency_key="probe-image",
    )["data"]["object_id"]
    return {
        "tracks": [{"id": "picture", "kind": "visual", "label": "Picture"}],
        "clips": [{"id": "picture-clip", "track": "picture", "clipType": "image",
                   "at": 0, "hold": 2, "asset": "picture"}],
        "registry": {"assets": {"picture": {"media_id": media, "type": "image/png"}}},
    }


@pytest.mark.parametrize("shape", [
    "numeric", "rational-canvas", "rational-output", "app-extension", "clip-extension",
    "track-config-theme-extensions",
])
def test_public_create_reaches_runtime_and_preserves_supported_values(real_runtime, monkeypatch, tmp_path, shape):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "client-data"))
    daemon = real_runtime(authoring_profile="astrid")
    project = daemon.service.create_project({"slug": shape, "name": shape})["id"]
    client = open_client(daemon)
    canvas = {"width": 640, "height": 360, "fps": _RATIO if shape == "rational-canvas" else 24}
    config = {"tracks": [], "theme_overrides": {"visual": {"canvas": canvas}}}
    if shape == "rational-output":
        config["output"] = {"fps": _RATIO, "resolution": "640x360", "file": "film.mp4"}
    if shape in ("app-extension", "clip-extension"):
        config.update(_picture(daemon, project))
        clip = config["clips"][0]
        clip.update({"app": {"opaque": _EXTENSION}} if shape == "app-extension" else {"opaque": _EXTENSION})
    if shape == "track-config-theme-extensions":
        config.update(opaque=_EXTENSION, tracks=[{"id": "picture", "kind": "visual", "opaque": _EXTENSION}])
        config["theme_overrides"]["opaque_theme"] = _EXTENSION

    create = {"project": project, "timeline_id": "film", "name": "Main",
              "config": config, "idempotency_key": "create"}
    result = client.timelines.create_empty(**create)
    history = _history(client, project)
    if not result.ok:
        assert [item["intent"] for item in history["items"]] == ["bootstrap"]
    assert result.ok, result.as_dict()
    assert len(history["items"]) == 2
    original = _payload(result)
    stored = original["config"]
    rate = _RATIO if shape in ("rational-canvas", "rational-output") else 24
    assert canonical_bytes(stored["output"]["fps"]) == canonical_bytes(rate)
    assert canonical_bytes(stored["theme_overrides"]) == canonical_bytes(config["theme_overrides"])
    if "clips" in config:
        assert canonical_bytes(original["clips"]) == canonical_bytes(config["clips"])
    if shape == "track-config-theme-extensions":
        assert canonical_bytes(stored["opaque"]) == canonical_bytes(_EXTENSION)
        assert canonical_bytes(stored["tracks"][0]["opaque"]) == canonical_bytes(_EXTENSION)

    reopened = open_client(daemon)
    _assert_exact_reload(reopened, project, original)
    renamed = _commit(reopened, project, {
        "op": "doc.patch", "args": {"changes": [{
            "op": "set", "ref": "parent", "path": ["name"],
            "before": {"state": "present", "value": "Main"}, "value": "Reopened Main",
        }]},
    }, "rename")
    expected = {**original, "name": "Reopened Main"}
    assert canonical_bytes(_payload(renamed)) == canonical_bytes(expected)
    _assert_exact_reload(reopened, project, expected)

    assert renamed.receipt is not None
    undone = _commit(reopened, project, {
        "op": "timeline.inverse", "args": {"publications": [renamed.receipt.receipt_id]},
    }, "undo-rename")
    assert canonical_bytes(_payload(undone)) == canonical_bytes(original)
    _assert_exact_reload(open_client(daemon), project, original)

    replay = client.timelines.create_empty(**create)
    assert replay.ok, replay.as_dict()
    assert replay.receipt == result.receipt
    assert canonical_bytes(replay.data["canonical_document"]) == canonical_bytes(result.data["canonical_document"])
    assert len(_history(client, project)["items"]) == 4


@pytest.mark.parametrize(("control", "reason"), [
    ("unknown-structural-field", "future_structural"),
    ("unsafe-extension-value", "finite"),
    ("malformed-timing", "fps rational must be positive"),
    ("unbound-media", "media"),
    ("unknown-json-clip-kind", "json-future-kind"),
])
def test_public_create_refuses_invalid_reader_inputs_without_publication(
    real_runtime, monkeypatch, tmp_path, control, reason,
):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "client-data"))
    daemon = real_runtime(authoring_profile="astrid")
    project = daemon.service.create_project({"slug": control, "name": control})["id"]
    client = open_client(daemon)
    if control == "unknown-structural-field":
        config = {"tracks": [{"id": "picture", "kind": "visual", "future_structural": True}]}
    elif control == "unsafe-extension-value":
        config = {"tracks": [], "opaque": {"values": [float("nan")]}}
    elif control == "malformed-timing":
        config = {"tracks": [], "output": {"fps": {"numerator": 24, "denominator": 0},
                                          "resolution": "640x360", "file": "film.mp4"}}
    else:
        config = _picture(daemon, project)
        if control == "unbound-media":
            del config["registry"]["assets"]["picture"]["media_id"]
        else:
            config["clips"][0].update(clipType="json-future-kind", params={"future": True})

    result = client.timelines.create_empty(
        project=project, timeline_id="film", name="Main", config=config,
        idempotency_key="invalid-create",
    )
    assert not result.ok and result.error is not None, result.as_dict()
    assert reason in result.error.message, result.as_dict()
    assert [item["intent"] for item in _history(client, project)["items"]] == ["bootstrap"]
