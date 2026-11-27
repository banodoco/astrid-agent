"""CPU-only proof of the attached download peak budget and write ordering."""
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from astrid.core.generation import asset_gaps
from astrid.core.generation.engine_contracts import ModelSpec
from tests.core.generation.environment_states import snapshot


def test_download_peak_bytes():
    first = {"folder": "a", "file": "one", "bytes": 100}
    second = {"folder": "b", "file": "two", "bytes": 150}
    assert asset_gaps.download_peak_bytes([]) == 0
    assert asset_gaps.download_peak_bytes([first]) == 200
    assert asset_gaps.download_peak_bytes([first, second]) == 400
    key = ("a", "one")
    assert asset_gaps.download_peak_bytes([first], {key: 0}) == 200
    assert asset_gaps.download_peak_bytes([first], {key: 50}) == 150
    assert asset_gaps.download_peak_bytes([first], {key: 100}) == 100
    assert asset_gaps.download_peak_bytes([first], {key: 200}) == 100
    assert asset_gaps.download_peak_bytes([first, second], {("b", "two"): 75}) == 325
    assert asset_gaps.download_peak_bytes([second, first]) == 350


@pytest.fixture
def machine(tmp_path, monkeypatch):
    gaps, sources = [], {}
    for key, name, size in (("a", "one", 100), ("b", "two", 150)):
        folder = tmp_path / key
        folder.mkdir()
        gaps.append({"role": key, "key": key, "names": [name], "folder": str(folder),
                     "served": set(), "local": set()})
        sources[f"{key}/{name}"] = ModelSpec(
            name, None, None, None, f"https://huggingface.co/x/resolve/main/{name}",
            f"{key}/{name}", size, "a" * 64)
    client = SimpleNamespace(target=SimpleNamespace(url="http://127.0.0.1:8188"))
    disk = SimpleNamespace(free=400 + asset_gaps.DOWNLOAD_MARGIN_BYTES)
    monkeypatch.setattr(asset_gaps, "_download_gaps", lambda *args: (gaps, [], None, None))
    monkeypatch.setattr(asset_gaps.shutil, "disk_usage", lambda path: disk)

    def plan():
        return asset_gaps.plan_attached_model_download(client, "entry", "mode", sources=sources)

    def apply(digest):
        return asset_gaps.apply_attached_model_download(
            client, "entry", "mode", sources=sources, plan_sha256=digest)

    return SimpleNamespace(root=tmp_path, disk=disk, plan=plan, apply=apply)


def test_plan_is_read_only_and_checks_peak_plus_margin(machine, monkeypatch):
    monkeypatch.setattr(asset_gaps, "_hard_links_supported", lambda folder: pytest.fail("plan probed"))
    before = snapshot(machine.root)
    machine.disk.free -= 1
    plan = machine.plan()
    assert plan["total_bytes"] == 250
    assert plan["peak_bytes"] == 400
    assert plan["partial_bytes"] == []
    assert plan["space_ok"] is False
    rendered = asset_gaps.render_attached_download_plan(plan)
    assert "Total:" in rendered and "At its peak the download needs" in rendered
    assert "Not enough free space" in rendered
    assert all("hard_links" not in f for f in plan["files"])
    machine.disk.free += 1
    enough = machine.plan()
    assert enough["space_ok"] is True
    assert enough["plan_sha256"] == plan["plan_sha256"]
    assert snapshot(machine.root) == before


def test_partial_reduces_peak_without_changing_digest(machine, monkeypatch):
    monkeypatch.setattr(asset_gaps, "_hard_links_supported", lambda folder: pytest.fail("plan probed"))
    original = machine.plan()
    (machine.root / "b" / ".two.astrid-resume").write_bytes(b"x" * 75)
    before = snapshot(machine.root)
    machine.disk.free = 325 + asset_gaps.DOWNLOAD_MARGIN_BYTES
    plan = machine.plan()
    assert plan["partial_bytes"] == [{"file": "two", "bytes": 75}]
    assert plan["peak_bytes"] == 325
    assert plan["space_ok"] is True
    assert plan["plan_sha256"] == original["plan_sha256"]
    assert snapshot(machine.root) == before


def test_non_regular_partial_is_not_credited(machine):
    (machine.root / "b" / ".two.astrid-resume").mkdir()
    assert machine.plan()["partial_bytes"] == []
    assert machine.plan()["peak_bytes"] == 400


def test_apply_rechecks_space_and_refuses_without_writing(machine, monkeypatch):
    plan = machine.plan()
    before = snapshot(machine.root)
    calls = []
    monkeypatch.setattr(asset_gaps, "_hard_links_supported", lambda folder: calls.append("probe"))
    monkeypatch.setattr(asset_gaps, "_fetch_into_folder", lambda *a, **k: calls.append("fetch"))
    machine.disk.free -= 1
    with pytest.raises(asset_gaps.AttachedDownloadRefused) as caught:
        machine.apply(plan["plan_sha256"])
    assert caught.value.code == "insufficient_space"
    assert "peak" in str(caught.value)
    assert str(caught.value).endswith("Nothing was written.")
    assert calls == []
    assert snapshot(machine.root) == before


def test_apply_probes_each_folder_before_fetch(machine, monkeypatch):
    plan = machine.plan()
    calls = []

    def probe(folder):
        calls.append(("probe", folder.name))
        return folder.name == "a"

    def fetch(item, client, *, progress):
        calls.append(("fetch", item["file"], item["hard_links"]))
        return 0

    monkeypatch.setattr(asset_gaps, "_hard_links_supported", probe)
    monkeypatch.setattr(asset_gaps, "_fetch_into_folder", fetch)
    result = machine.apply(plan["plan_sha256"])
    assert calls == [("probe", "a"), ("probe", "b"), ("fetch", "one", True), ("fetch", "two", False)]
    assert len(result["downloaded"]) == 2


def test_per_volume_budget_is_independent(machine, monkeypatch):
    """A surplus on one volume cannot satisfy a shortage on the other."""
    a, b = machine.root / "a", machine.root / "b"
    devices = {str(a): 101, str(b): 202}
    # Replace only the module's stat lookup, not pathlib's real filesystem calls.
    monkeypatch.setattr(asset_gaps, "os", SimpleNamespace(
        stat=lambda path: SimpleNamespace(st_dev=devices[str(path)]),
        path=os.path, access=os.access, W_OK=os.W_OK,
    ))
    margin = asset_gaps.DOWNLOAD_MARGIN_BYTES
    free = {str(a): 200 + margin, str(b): 300 + margin}
    monkeypatch.setattr(asset_gaps.shutil, "disk_usage",
                        lambda path: SimpleNamespace(free=free[str(path)]))
    before = snapshot(machine.root)
    monkeypatch.setattr(asset_gaps, "_hard_links_supported",
                        lambda *a: pytest.fail("preview/refusal probed"))
    monkeypatch.setattr(asset_gaps, "_fetch_into_folder",
                        lambda *a, **k: pytest.fail("refusal fetched"))
    plan = machine.plan()
    assert plan["peak_bytes"] == 500
    assert plan["space_ok"] is True
    free[str(a)] -= 1
    free[str(b)] += 1000
    assert machine.plan()["space_ok"] is False
    with pytest.raises(asset_gaps.AttachedDownloadRefused) as caught:
        machine.apply(plan["plan_sha256"])
    assert caught.value.code == "insufficient_space"
    free[str(a)] += 1
    free[str(b)] = 300 + margin - 1
    assert machine.plan()["space_ok"] is False
    assert snapshot(machine.root) == before


def test_digest_matches_original_confirmation_body(machine):
    plan = machine.plan()
    wanted = sorted(
        ({key: f[key] for key in ("file", "folder", "url", "bytes", "sha256")}
         for f in plan["files"] + plan["present"]),
        key=lambda f: (f["folder"], f["file"]),
    )
    body = {"entry": "entry", "mode": "mode", "target": "http://127.0.0.1:8188",
            "wanted": wanted, "manual": []}
    expected = hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert plan["plan_sha256"] == expected


@pytest.mark.parametrize(("digest", "code"), [
    (None, "confirmation_required"), ("0" * 64, "plan_changed"),
])
def test_refused_confirmation_never_probes_or_fetches(machine, monkeypatch, digest, code):
    before = snapshot(machine.root)
    monkeypatch.setattr(asset_gaps, "_hard_links_supported",
                        lambda *a: pytest.fail("refusal probed"))
    monkeypatch.setattr(asset_gaps, "_fetch_into_folder",
                        lambda *a, **k: pytest.fail("refusal fetched"))
    with pytest.raises(asset_gaps.AttachedDownloadRefused) as caught:
        machine.apply(digest)
    assert caught.value.code == code
    assert snapshot(machine.root) == before
