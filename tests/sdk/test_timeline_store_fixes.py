"""Store fixes: working copies never land under ``~/.astrid``; publish keeps the candidate's parent-config changes."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import astrid.sdk.storage_root as storage_root
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline import store
from astrid.sdk.timeline.store import drafts_root, three_way

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


def test_publish_merge_keeps_candidate_parent_config_changes_next_to_head_changes():
    base = _base()
    head = _unrelated_clip_change(base)
    candidate = copy.deepcopy(base)
    candidate["parent"]["config"]["chapters"] = [{"name": "01 OPEN", "from": "c1"}]

    merged, conflicts = three_way(base, head, candidate)

    assert conflicts == []
    assert intent.chapters(merged) == [{"name": "01 OPEN", "from": "c1"}]  # read through intent: written under app.astrid
    assert merged["shots"] == head["shots"]  # the head's clip change is kept too


def test_publish_merge_flags_a_parent_config_key_changed_differently_and_force_wins():
    base = _base()
    head = copy.deepcopy(base)
    head["parent"]["config"]["theme_overrides"] = {"visual": {"canvas": {"fps": 24}}}
    candidate = copy.deepcopy(base)
    candidate["parent"]["config"]["theme_overrides"] = {"visual": {"canvas": {"fps": 60}}}

    _merged, conflicts = three_way(base, head, candidate)
    # the existing message format is "<key> (<unit kind>) changed ...", so the parent-config key reads "theme_overrides (parent-config)"
    assert any("theme_overrides (parent-config)" in line for line in conflicts), conflicts

    forced, forced_conflicts = three_way(base, head, candidate, force=True)
    assert forced_conflicts
    assert forced["parent"]["config"]["theme_overrides"] == {"visual": {"canvas": {"fps": 60}}}
