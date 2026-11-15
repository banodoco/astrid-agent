"""The pod validates just its selected declaration inside the generation process."""
import os

import pytest

from astrid.core.generation import cli, entry_check, listing, local_job
from astrid.packs.runpod import machine


def test_selected_entry_process_start_budget():
    command = machine.generation_command("qwen-image-2.1-turbo", "/root/astrid/runs/job", mode="t2i")
    assert command.count(f"{machine.POD_VENV}/bin/astrid generate ") == 1
    assert "generate list" not in command
    assert "listing.json" not in command
    assert "--check-entry" in command


@pytest.mark.parametrize("rows,mode,error", [
    ([], "t2i", "entry 'entry' is not listed by the pod's runtime"),
    ([{"id": "entry", "backend": "local", "mode": "edit", "availability": "available"}], "t2i",
     "entry 'entry' has no local declaration for the requested mode"),
    ([{"id": "entry", "backend": "cloud", "mode": "t2i", "availability": "available"}], "t2i",
     "entry 'entry' has no local declaration for the requested mode"),
    ([{"id": "entry", "backend": "local", "mode": "t2i", "availability": "unavailable"}], "t2i",
     "entry 'entry' is unavailable on the pod's runtime"),
])
def test_same_process_guard_refuses_with_original_message_and_exit(monkeypatch, capsys, rows, mode, error):
    selected = []
    monkeypatch.setattr(listing, "catalog_rows", lambda *, entry_id: selected.append(entry_id) or rows)
    monkeypatch.setattr(local_job, "run_job", lambda *a, **k: pytest.fail("refused entry ran"))
    assert cli.main(["entry", "--placement", "local", "--job", "unused.json", "--out", "unused",
                     "--mode", mode, "--check-entry", "--json"]) == 3
    assert capsys.readouterr().err.strip() == error
    assert selected == ["entry"]


def test_selected_available_entry_runs_in_guard_process_before_readiness(monkeypatch):
    pids = []

    def selected(*, entry_id):
        assert entry_id == "entry"
        pids.append(os.getpid())
        return [{"id": "entry", "backend": "local", "mode": "t2i", "availability": "available"}]

    def run(*args, **kwargs):
        pids.append(os.getpid())
        return {"outputs": []}

    monkeypatch.setattr(listing, "catalog_rows", selected)
    monkeypatch.setattr(local_job, "run_job", run)
    assert cli.main(["entry", "--placement", "local", "--job", "unused.json", "--out", "unused",
                     "--mode", "t2i", "--check-entry", "--json"]) == 0
    assert pids == [os.getpid(), os.getpid()]


def test_selected_projection_has_no_catalogue_walk_or_target_probe(monkeypatch):
    from astrid.core.generation import entries
    from astrid.core.model_catalog.registry import ModelRegistry
    from scripts.gen_capability_index import _in_repo_packs_only

    def forbidden(*args, **kwargs):
        pytest.fail("selected check walked the full catalogue or probed readiness")

    monkeypatch.setattr(ModelRegistry, "list_all", forbidden)
    monkeypatch.setattr(entries, "ready_entries", forbidden)
    monkeypatch.setattr(listing, "_targets", forbidden)
    checked = []
    original = listing.model_availability

    def available(model, *args, **kwargs):
        checked.append(model)
        return original(model, *args, **kwargs)

    monkeypatch.setattr(listing, "model_availability", available)
    with _in_repo_packs_only():
        assert entry_check.selected_entry_error("qwen-image-2.1-turbo", "t2i") is None
    assert checked and set(checked) == {"qwen-image-2.1-turbo"}


def test_selected_projection_preserves_catalogue_declarations():
    from scripts.gen_capability_index import _in_repo_packs_only

    with _in_repo_packs_only():
        rows = listing.catalog_rows()
        for entry in ("qwen-image-2.1-turbo", "z-image-turbo", "hunyuan-video-1.5", "missing-entry"):
            assert listing.catalog_rows(entry_id=entry) == [row for row in rows if row["id"] == entry]
