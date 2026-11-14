"""Plan 32 T6d: default entries are data; the user's pins and placement live in the Runtime document."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.generation import defaults, placement as placements, settings
from astrid.core.generation.defaults import Candidate


def test_no_hard_coded_table_remains():
    from astrid.sdk import generation
    assert not any(name.endswith("_DEFAULT_ENTRIES") for name in vars(generation))


def test_builtin_slots_resolve_as_before_and_video_is_h3():
    listing = defaults.listing()
    by_slot = {row["slot"]: row for row in listing["slots"]}
    assert by_slot["image.t2i"]["entry"] == "qwen-image-2.1-turbo"
    assert [o["entry"] for o in by_slot["image.t2i"]["others"]] == ["z-image", "qwen-image-2512"]
    assert by_slot["image.edit"]["entry"] == "qwen-image-2.1-turbo-edit"
    assert by_slot["audio.tts"]["entry"] == "audio/qwen3_tts_custom_voice"
    assert by_slot["audio.music"]["entry"] == "audio/minimax_music_3"
    assert by_slot["audio.sfx"]["entry"] == "audio/hunyuan_video_foley"
    assert by_slot["video.i2v"]["entry"] == "h3" and by_slot["video.i2v"]["pack"] == "h3_av"
    assert by_slot["video.i2v"]["source"] == "pack default"


def test_third_party_contribution_becomes_default_and_a_pin_overrides_it():
    third = Candidate("acme", "acme-t2i", "image", "t2i", 99)
    offered = [Candidate("generation", "qwen-image-2.1-turbo", "image", "t2i", 30), third]
    entries = [{"pack": c.pack, "entry": c.entry, "kind": c.kind, "modes": {c.mode}} for c in offered]
    assert defaults.resolve("image", "t2i", pins={}, candidates=offered, entries=entries).entry == "acme-t2i"
    pinned = defaults.resolve("image", "t2i", pins={"image.t2i": {"pack": "generation", "entry": "qwen-image-2.1-turbo"}},
                              candidates=offered, entries=entries)
    assert (pinned.entry, pinned.source) == ("qwen-image-2.1-turbo", "user pin")


@pytest.mark.parametrize("pin, key", [
    ({"pack": "generation", "entry": "nope"}, "defaults.image.t2i"),
    ({"pack": "generation", "entry": "audio/minimax_music_3"}, "defaults.image.t2i"),
    ({"pack": "generation"}, "defaults.image.t2i"),
])
def test_bad_pin_is_refused_naming_the_key(pin, key):
    with pytest.raises(AstridError) as caught:
        defaults.check_pin("image.t2i", pin, defaults.known_entries())
    assert caught.value.field == key and caught.value.code == "generation_settings_invalid"


def test_set_then_unset_round_trips(generation_settings_store):
    snapshot = defaults.set_default("image", "t2i", "z-image")
    assert snapshot.content["defaults"] == {"image.t2i": {"pack": "generation", "entry": "z-image"}}
    row = {r["slot"]: r for r in defaults.listing()["slots"]}["image.t2i"]
    assert (row["entry"], row["source"], row["origin"]) == ("z-image", "user pin", "Runtime user settings, version 1")
    defaults.unset_default("image", "t2i")
    assert generation_settings_store.documents["generation"][0]["defaults"] == {}
    assert {r["slot"]: r for r in defaults.listing()["slots"]}["image.t2i"]["entry"] == "qwen-image-2.1-turbo"


def test_set_default_refuses_unknown_entry_and_wrong_slot():
    with pytest.raises(AstridError) as caught:
        defaults.set_default("image", "t2i", "no-such-entry")
    assert caught.value.field == "defaults.image.t2i"
    with pytest.raises(AstridError):
        defaults.set_default("image", "t2i", "audio/minimax_music_3")


def test_pinned_entry_is_first_in_order():
    defaults.set_default("image", "t2i", "qwen-image-2512")
    assert defaults.ordered_ids("image", "t2i")[0] == "qwen-image-2512"


def test_cli_defaults_json_names_entry_and_source(monkeypatch, capsys, generation_settings_store):
    from astrid.core.generation import cli

    class Client:
        def __enter__(self): return self
        def __exit__(self, *exc): return False
    monkeypatch.setattr(cli, "_open_client", lambda: Client())
    monkeypatch.setattr(settings, "transport_of", lambda client: generation_settings_store)
    assert cli.main(["defaults", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = {row["slot"]: row for row in payload["slots"]}
    assert all(row["entry"] and row["source"] and row["origin"] for row in rows.values())
    assert cli.main(["defaults", "set", "image", "t2i", "z-image", "--json"]) == 0
    assert {r["slot"]: r for r in json.loads(capsys.readouterr().out)["slots"]}["image.t2i"]["source"] == "user pin"
    assert cli.main(["defaults", "unset", "image", "t2i", "--json"]) == 0
    assert {r["slot"]: r for r in json.loads(capsys.readouterr().out)["slots"]}["image.t2i"]["source"] == "pack default"


def test_document_round_trips_through_checkout_and_checkin(tmp_path, generation_settings_store):
    defaults.set_default("image", "t2i", "z-image")
    path = tmp_path / "settings.json"
    assert settings.checkout(path)["version"] == 1
    document = json.loads(path.read_text())
    document["placement"] = {"kind": "local", "device": 1}
    path.write_text(json.dumps(document))
    assert settings.checkin(path)["version"] == 2
    assert generation_settings_store.documents["generation"][0]["placement"] == {"kind": "local", "device": 1}


def test_stale_checkin_is_refused_with_both_versions(tmp_path):
    path = tmp_path / "settings.json"
    settings.checkout(path)
    defaults.set_default("image", "t2i", "z-image")  # someone else writes first
    with pytest.raises(AstridError) as caught:
        settings.checkin(path)
    assert caught.value.code == "generation_settings_version_conflict"
    assert set(caught.value.state_snapshot) >= {"yours", "current"} or "yours" in json.dumps(caught.value.state_snapshot)


def test_checkin_refuses_unknown_key_and_unknown_pin(tmp_path):
    path = tmp_path / "settings.json"
    settings.checkout(path)
    path.write_text(json.dumps({"bogus": 1}))
    with pytest.raises(AstridError) as caught:
        settings.checkin(path)
    assert caught.value.field == "bogus"
    path.write_text(json.dumps({"defaults": {"image.t2i": {"pack": "generation", "entry": "ghost"}}}))
    with pytest.raises(AstridError) as caught:
        settings.checkin(path)
    assert caught.value.field == "defaults.image.t2i"


def test_secrets_are_never_stored():
    with pytest.raises(AstridError) as caught:
        settings.validate_document({"machines": {"runpod": {"api_token": "abc"}}})
    assert caught.value.field == "machines.runpod.api_token"


def test_old_toml_default_is_imported_once(tmp_path, generation_settings_store):
    legacy = tmp_path / "config.toml"
    legacy.write_text('[placement]\nkind = "attached"\nendpoint = "http://127.0.0.1:8199"\n'
                      '[runpod]\nprovider_account_ref = "acct"\ngpu_types = ["A40"]\n')
    first = settings.read(legacy_path=legacy)
    assert first.imported and first.content["placement"]["endpoint"] == "http://127.0.0.1:8199"
    assert first.content["machines"]["runpod"]["gpu_types"] == ["A40"]
    legacy.write_text('[placement]\nkind = "local"\n')  # no longer read for Astrid
    second = settings.read(legacy_path=legacy)
    assert not second.imported and second.content["placement"]["kind"] == "attached"
    assert generation_settings_store.documents["generation"][1] == 1


def test_placement_default_in_the_document_is_what_generate_uses():
    settings.update(lambda d: d["placement"].update({"kind": "attached", "endpoint": "http://127.0.0.1:9000"}))
    resolved = placements.resolve_placement(None)
    assert (resolved.kind, resolved.endpoint) == ("attached", "http://127.0.0.1:9000")
    assert placements.resolve_placement({"kind": "local"}).endpoint is None


def test_runpod_preferences_live_under_machines_runpod(generation_settings_store):
    from astrid.packs.runpod import target_selection
    raw = {"provider_account_ref": "acct", "gpu_types": ["A40"], "image": "w@sha256:" + "a" * 64,
           "volume": None, "budget_cap_usd": 10.0}
    target_selection.save_runpod_preferences(raw)
    stored = generation_settings_store.documents["generation"][0]["machines"]["runpod"]
    assert stored["provider_account_ref"] == "acct" and "credential" not in json.dumps(stored).replace("credential_ref", "")
    assert target_selection.read_runpod_preferences().gpu_types == ("A40",)


def test_unavailable_store_uses_builtin_settings_and_logs_debug(monkeypatch, caplog, capsys):
    import logging

    failure = RuntimeError("launcher transport unavailable")

    def fail_to_open():
        raise failure

    monkeypatch.setattr(settings, "_store_factory", None)
    monkeypatch.setattr(settings, "_launcher_store", fail_to_open)
    with caplog.at_level(logging.DEBUG, logger=settings.__name__):
        snapshot = settings.read()

    assert snapshot.source == "unavailable"
    assert snapshot.version == 0
    assert snapshot.content == settings.empty_document()
    records = [record for record in caplog.records if record.name == settings.__name__]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert records[0].getMessage() == "Could not reach the settings store; using built-in generation settings"
    assert records[0].exc_info[1] is failure
    assert "RuntimeError: launcher transport unavailable" in caplog.text
    assert "Traceback" not in capsys.readouterr().err
