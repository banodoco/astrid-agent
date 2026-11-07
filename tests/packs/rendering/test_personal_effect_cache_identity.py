"""PK-10 (new): a personal effect's frame-cache identity comes from its pack root.

The canonical per-user ``personal`` pack (``$HOME/Astrid/packs/personal``)
contributes render elements. When a timeline selects one of its effects, the
cache identity is computed from that effect's admitted files inside the
personal pack root, labelled by owner/kind/id: editing the selected effect
changes the key, editing an unselected personal file does not, and the same
bytes under a different home give the same pack-root identity.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from astrid.core.element.registry import clear_default_registry_cache
from astrid.packs.rendering.actions.timeline_visualize import composed_frame as capture
from tests.packs.rendering.test_selected_frame_inputs import config_for


@pytest.fixture(autouse=True)
def registry_cache():
    clear_default_registry_cache()
    capture._DIGEST_MEMO.clear()
    yield
    clear_default_registry_cache()
    capture._DIGEST_MEMO.clear()


def _personal(home: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Create the effect with the production personal-pack writer (the scaffold)."""
    from astrid.packs.rendering.actions.html_canvas_effect.run import scaffold

    _use_home(monkeypatch, home)
    (home / "Astrid" / "packs").mkdir(parents=True)
    project = home.parent / (home.name + "-project")
    scaffold(effect_id="my-glow", label=None, description=None, project_root=project,
             out_path=project / "report.json")
    pack = home / "Astrid" / "packs" / "personal"
    element = pack / "rendering" / "elements" / "effects" / "my-glow"
    assert (element / "element.yaml").is_file() and (element / "component.tsx").is_file()
    (pack / "notes.txt").write_text("unselected personal bytes\n")
    clear_default_registry_cache()
    return pack, element


def _snapshot() -> dict:
    return {"project_slug": "demo", "timeline_id": "t", "render_run_id": "r",
            "video_digest": "sha256:" + "a" * 64, "fps_rational": [24, 1], "duration_frames": 24,
            "config": config_for("my-glow")}


def _use_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    monkeypatch.delenv("ASTRID_RENDER_PACK_ROOTS", raising=False)
    clear_default_registry_cache()
    capture._DIGEST_MEMO.clear()


def test_selected_personal_effect_identity_comes_from_its_pack_root(tmp_path, monkeypatch):
    home = tmp_path / "home-a"
    pack, element = _personal(home, monkeypatch)
    checkout = capture._checkout_root()
    roots = capture._pack_root_digests(checkout, config_for("my-glow"))
    assert [owner for owner, _ in roots] == ["personal"]
    first = capture.frame_cache_key(_snapshot(), 1, [32, 18])

    # An unselected personal file is not a cache input.
    (pack / "notes.txt").write_text("edited, still unselected\n")
    capture._DIGEST_MEMO.clear()
    assert capture.frame_cache_key(_snapshot(), 1, [32, 18]) == first

    # The selected effect's implementation is.
    (element / "component.tsx").write_text((element / "component.tsx").read_text() + "\n// edited\n")
    capture._DIGEST_MEMO.clear()
    assert capture.frame_cache_key(_snapshot(), 1, [32, 18]) != first


def test_same_personal_bytes_under_another_home_give_the_same_pack_root_identity(tmp_path, monkeypatch):
    home_a, home_b = tmp_path / "home-a", tmp_path / "home-b"
    _personal(home_a, monkeypatch)
    shutil.copytree(home_a, home_b)
    checkout = capture._checkout_root()

    _use_home(monkeypatch, home_a)
    first = capture._pack_root_digests(checkout, config_for("my-glow"))
    _use_home(monkeypatch, home_b)
    second = capture._pack_root_digests(checkout, config_for("my-glow"))
    assert first == second and first[0][0] == "personal"
    assert str(home_a) not in json.dumps(first) and str(home_b) not in json.dumps(second)


def test_timeline_without_personal_selection_has_no_personal_identity(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _personal(home, monkeypatch)
    checkout = capture._checkout_root()
    assert capture._pack_root_digests(checkout, {"clips": []}) == []
    assert all(owner != "personal" for owner, _ in
               capture._pack_root_digests(checkout, config_for("text-card")))
