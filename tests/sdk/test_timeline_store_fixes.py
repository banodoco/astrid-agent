"""Store fixes: working copies never land under ``~/.astrid``; publish keeps the candidate's parent-config changes."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import astrid.sdk.storage_root as storage_root
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline import store
from astrid.sdk.timeline.store import drafts_root
from astrid.sdk.timeline_checkout import Checkout, TimelineEditError

PUBLISHABLE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "timeline_editing" / "tiny_publishable.json"


# ---------------------------------------------------------------- (A) drafts root

def test_drafts_root_follows_the_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    assert drafts_root() == tmp_path / "data" / "drafts"


def test_drafts_root_without_env_is_the_sdk_data_root_never_home_astrid(monkeypatch, tmp_path):
    # The suite fixture already creates tmp_path / "home" for HOME isolation.
    home = tmp_path / "synthetic-home"
    home.mkdir()
    monkeypatch.delenv("ASTRID_RUNTIME_DATA_ROOT", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(storage_root, "CONFIG_PATH", tmp_path / "missing-astrid-runtime.json")

    root = drafts_root()

    assert root == storage_root.resolve_runtime_data_root() / "drafts"
    assert root == home / ".astrid-data" / "drafts"
    assert home / ".astrid" not in root.parents
    assert not (home / ".astrid").exists()


def test_media_seconds_closes_probe_tempfile_before_reopening_and_unlinks(monkeypatch):
    import astrid.core.media as media

    payload = b"not a wav file; ffprobe must open this path"
    probed_paths = []

    class Media:
        def read_bytes(self, _digest):
            return payload

    class Client:
        media = Media()

    def probe(path):
        path = Path(path)
        probed_paths.append(path)
        assert path.read_bytes() == payload
        return 2.5

    monkeypatch.setattr(media, "ffprobe_duration_seconds", probe)

    assert store.media_seconds("project", {"media_id": "sha256:" + "a" * 64, "type": "video/mp4"}, client=Client()) == 2.5
    assert len(probed_paths) == 1
    assert not probed_paths[0].exists()


# ---------------------------------------------------------------- (B) publish keeps parent-config changes

def _base() -> dict:
    return json.loads(PUBLISHABLE.read_text(encoding="utf-8"))


def _unrelated_clip_change(bundle: dict) -> dict:
    out = copy.deepcopy(bundle)
    shot = next(iter(out["shots"].values()))
    clip = shot["internal_timeline"]["clips"][0]
    clip.setdefault("params", {})["x"] = 12345
    return out


def test_publish_merge_keeps_candidate_parent_config_changes_next_to_head_changes(tmp_path):
    path = tmp_path / "main.json"
    Checkout(_base()).save(path, quiet=True)
    head, candidate = Checkout.load(path), Checkout.load(path)
    head.bundle = _unrelated_clip_change(head.bundle)
    candidate.bundle["parent"]["config"]["chapters"] = [{"name": "01 OPEN", "from": "c1"}]

    head.save(quiet=True)
    candidate.save(quiet=True)

    merged = Checkout.load(path).bundle
    assert intent.chapters(merged) == [{"name": "01 OPEN", "from": "c1"}]  # read through intent: written under app.astrid
    assert merged["shots"] == head.bundle["shots"]  # the head's clip change is kept too


def test_publish_merge_flags_a_parent_config_key_changed_differently_and_force_wins(tmp_path):
    path = tmp_path / "main.json"
    Checkout(_base()).save(path, quiet=True)
    head, candidate = Checkout.load(path), Checkout.load(path)
    head.bundle["parent"]["config"]["theme_overrides"] = {"visual": {"canvas": {"fps": 24}}}
    candidate.bundle["parent"]["config"]["theme_overrides"] = {"visual": {"canvas": {"fps": 60}}}

    head.save(quiet=True)
    with pytest.raises(TimelineEditError, match=r"both changed 1 of the same thing\(s\): parent\."):
        candidate.save(quiet=True)
    assert Checkout.load(path).bundle["parent"]["config"]["theme_overrides"] == {
        "visual": {"canvas": {"fps": 24}}
    }

    candidate.save(force=True, quiet=True)
    assert Checkout.load(path).bundle["parent"]["config"]["theme_overrides"] == {
        "visual": {"canvas": {"fps": 60}}
    }
