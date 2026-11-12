"""The VibeComfy pin utility reads the digest-verified recipe authority and bumps it consistently."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.core.generation import engine_recipe
from astrid.core.generation.engine_contracts import IntegrityError
from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION
from scripts import vibecomfy_pin

ROOT = Path(__file__).resolve().parents[1]


def test_pin_script_reads_the_constant() -> None:
    assert vibecomfy_pin.read() == VIBECOMFY_ENGINE_REVISION


@pytest.fixture
def recipe_workspace(tmp_path, monkeypatch) -> tuple[Path, ...]:
    """Stage the authority chain independently of the checkout's real pins."""
    root = tmp_path / "astrid"
    entry = next(item for item in engine_recipe._index_rows()
                 if (item["engine"], item["platform"]) == ("comfyui", "windows-x86_64-cu130"))
    recipe = json.loads((engine_recipe._ROOT / entry["recipe"]["path"]).read_text(encoding="utf-8"))
    pins = json.loads((engine_recipe._ROOT / recipe["pins"]["path"]).read_text(encoding="utf-8"))
    pins["sources"]["vibecomfy"]["revision"] = "a" * 40
    pins_bytes = (json.dumps(pins, indent=2) + "\n").encode("utf-8")
    recipe["pins"]["sha256"] = hashlib.sha256(pins_bytes).hexdigest()
    recipe_bytes = (json.dumps(recipe, indent=2) + "\n").encode("utf-8")
    declaration_path = "packs/vibecomfy/extensions/generation.engine/comfyui.yaml"
    declaration_bytes = (engine_recipe._ROOT / declaration_path).read_text(encoding="utf-8").replace(
        entry["recipe"]["sha256"], hashlib.sha256(recipe_bytes).hexdigest()).encode("utf-8")
    payloads = {
        root / recipe["pins"]["path"]: pins_bytes,
        root / entry["recipe"]["path"]: recipe_bytes,
        root / declaration_path: declaration_bytes,
    }
    for path, payload in payloads.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    monkeypatch.setattr(engine_recipe, "_ROOT", root)
    monkeypatch.setattr(vibecomfy_pin, "ROOT", root)
    return tuple(payloads)


def test_pin_script_main_reads_current_recipe_authority(recipe_workspace, capsys) -> None:
    assert vibecomfy_pin.read() == "a" * 40
    assert vibecomfy_pin.main([]) == 0
    assert capsys.readouterr().out == "a" * 40 + "\n"


def test_pin_script_rejects_unverified_pin_bytes(recipe_workspace) -> None:
    pins_path = recipe_workspace[0]
    pins = json.loads(pins_path.read_text(encoding="utf-8"))
    pins["sources"]["vibecomfy"]["revision"] = "b" * 40
    pins_path.write_text(json.dumps(pins), encoding="utf-8")
    with pytest.raises(IntegrityError, match="packaged data digest mismatch"):
        vibecomfy_pin.read()


@pytest.mark.parametrize("revision", ["", "abc", "A" * 40, "b" * 39])
def test_pin_script_rejects_malformed_bumps_without_writes(recipe_workspace, revision) -> None:
    before = {path: path.read_bytes() for path in recipe_workspace}
    with pytest.raises(SystemExit, match="expected a full 40-character lowercase commit sha"):
        vibecomfy_pin.main([revision])
    assert {path: path.read_bytes() for path in recipe_workspace} == before
    assert vibecomfy_pin.read() == "a" * 40


@pytest.fixture
def packs_copy(tmp_path, monkeypatch) -> Path:
    """Copy the checkout's real package-owned pins, recipes and index for a bump."""
    root = tmp_path / "astrid"
    shutil.copytree(engine_recipe._ROOT / "packs", root / "packs")
    shutil.copytree(engine_recipe._ROOT / "core" / "model_catalog", root / "core" / "model_catalog")
    monkeypatch.setattr(engine_recipe, "_ROOT", root)
    monkeypatch.setattr(vibecomfy_pin, "ROOT", root)
    return root


@pytest.fixture
def vibecomfy_checkout(tmp_path) -> tuple[Path, str, dict[str, bytes]]:
    """Create an offline VibeComfy-shaped git repo containing catalog templates."""
    checkout = tmp_path / "vibecomfy"
    checkout.mkdir()
    catalog = (ROOT / "astrid" / "core" / "model_catalog" / "models.yaml").read_text(encoding="utf-8")
    template_ids = list(dict.fromkeys(row[0] for row in vibecomfy_pin._catalog_routes(catalog)))
    payloads: dict[str, bytes] = {}
    for index, template_id in enumerate(template_ids):
        payload = f"workflow for {template_id}, fixture {index}\n".encode()
        payloads[template_id] = payload
        path = checkout / "ready_templates" / (template_id + ".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Pin tests"], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "fixture templates"], check=True)
    revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    # Make the working tree disagree with HEAD so the bump must hash git-show
    # bytes from the requested commit, rather than whatever happens to be checked out.
    first_id = template_ids[0]
    (checkout / "ready_templates" / (first_id + ".py")).write_bytes(b"uncommitted different bytes\n")
    return checkout, revision, payloads


def test_pin_script_bump_updates_catalog_routes_from_target_commit_bytes(packs_copy, vibecomfy_checkout, capsys) -> None:
    checkout, revision, payloads = vibecomfy_checkout
    original_catalog = (ROOT / "astrid" / "core" / "model_catalog" / "models.yaml").read_text(encoding="utf-8")
    routes = list(dict.fromkeys(row[0] for row in vibecomfy_pin._catalog_routes(original_catalog)))
    assert set(routes) == set(payloads)
    assert vibecomfy_pin.main([revision, "--checkout", str(checkout)]) == 0
    # main() prints the revision read back through the digest-verified authority.
    assert capsys.readouterr().out == revision + "\n"
    assert vibecomfy_pin.read() == revision
    catalog_path = packs_copy / "core" / "model_catalog" / "models.yaml"
    catalog_after = catalog_path.read_text(encoding="utf-8")
    expected_catalog = original_catalog
    for template_id, start, end in reversed(vibecomfy_pin._catalog_routes(original_catalog)):
        expected_hash = "sha256:" + hashlib.sha256(payloads[template_id]).hexdigest()
        expected_catalog = expected_catalog[:start] + expected_hash + expected_catalog[end:]
    assert catalog_after == expected_catalog
    bumped = 0
    for path in (packs_copy / "packs").glob("*/pins.json"):
        for name, source in json.loads(path.read_text(encoding="utf-8")).get("sources", {}).items():
            if name == "vibecomfy":
                assert source["revision"] == revision, (path, name)
                bumped += 1
    assert bumped >= 2
    # Every indexed recipe still loads: the pin and recipe digest chain was rewritten together.
    for entry in engine_recipe._index_rows():
        engine_recipe.load_recipe(entry["engine"], entry["platform"])
    # Reapplying the same resolved pin and target commit is byte stable.
    files = sorted(path for path in packs_copy.rglob("*") if path.is_file())
    before = {path: path.read_bytes() for path in files}
    assert vibecomfy_pin.main([revision, "--checkout", str(checkout)]) == 0
    assert {path: path.read_bytes() for path in files} == before


def test_pin_script_bump_to_the_current_revision_changes_no_bytes(packs_copy) -> None:
    checkout = Path(os.environ.get("ASTRID_VIBECOMFY_CHECKOUT", ""))
    assert checkout.is_dir(), "pin stability needs the VibeComfy checkout (ASTRID_VIBECOMFY_CHECKOUT)"
    files = sorted(path for path in packs_copy.rglob("*") if path.is_file())
    before = {path: path.read_bytes() for path in files}
    vibecomfy_pin.bump(VIBECOMFY_ENGINE_REVISION, checkout=checkout)
    assert {path: path.read_bytes() for path in files} == before


def test_pin_script_missing_template_fails_before_any_writes(packs_copy, vibecomfy_checkout) -> None:
    checkout, revision, _ = vibecomfy_checkout
    missing = next(row[0] for row in vibecomfy_pin._catalog_routes(
        (packs_copy / "core" / "model_catalog" / "models.yaml").read_text(encoding="utf-8")))
    path = checkout / "ready_templates" / (missing + ".py")
    path.unlink()
    subprocess.run(["git", "-C", str(checkout), "rm", "-q", str(path.relative_to(checkout))], check=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "remove catalog template"], check=True)
    revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    files = sorted(path for path in packs_copy.rglob("*") if path.is_file())
    before = {path: path.read_bytes() for path in files}
    with pytest.raises(SystemExit, match="cannot resolve target ready template"):
        vibecomfy_pin.main([revision, "--checkout", str(checkout)])
    assert {path: path.read_bytes() for path in files} == before


def test_pin_script_missing_revision_fails_before_any_writes(packs_copy, vibecomfy_checkout) -> None:
    checkout, _, _ = vibecomfy_checkout
    files = sorted(path for path in packs_copy.rglob("*") if path.is_file())
    before = {path: path.read_bytes() for path in files}
    with pytest.raises(SystemExit, match="VibeComfy git operation failed"):
        vibecomfy_pin.main(["b" * 40, "--checkout", str(checkout)])
    assert {path: path.read_bytes() for path in files} == before


def test_pin_script_runs_standalone_outside_checkout(tmp_path) -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / "vibecomfy_pin.py")],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == VIBECOMFY_ENGINE_REVISION + "\n"


def test_no_other_tracked_file_copies_the_pin() -> None:
    result = subprocess.run(
        ["git", "grep", "-l", "-I", VIBECOMFY_ENGINE_REVISION],
        cwd=ROOT, capture_output=True, text=True,
    )
    # git grep exits 1 for "no matches"; anything else (e.g. 128 outside a repo) is a failure.
    assert result.returncode in (0, 1), result.stderr
    tracked = result.stdout.split()
    # Package-owned pin manifests select the current engine revision.
    # Historical release metadata retains its reviewed installer default.
    # All other tracked copies remain forbidden.
    allowed = {
        "astrid/core/generation/vibecomfy_dependency.py",
        "astrid/packs/runpod/pins.json",
        "astrid/packs/vibecomfy/pins.json",
        "tests/upgrade/releases.json",
    }
    assert set(tracked) <= allowed, f"derive these from vibecomfy_dependency.py instead: {sorted(set(tracked) - allowed)}"


def test_pin_script_bumps_comfyui_without_changing_vibecomfy(packs_copy, capsys) -> None:
    revision = "c" * 40
    vibecomfy_revision = vibecomfy_pin.read()
    assert vibecomfy_pin.main(["--source", "comfyui", revision]) == 0
    assert capsys.readouterr().out == revision + "\n"
    assert vibecomfy_pin.read("comfyui") == revision
    assert vibecomfy_pin.read() == vibecomfy_revision
    for entry in engine_recipe._index_rows():
        engine_recipe.load_recipe(entry["engine"], entry["platform"])
    for path in (packs_copy / "packs").glob("*/pins.json"):
        source = json.loads(path.read_text(encoding="utf-8"))["sources"].get("comfyui")
        if source is not None:
            assert source["revision"] == revision
