"""Enforce the reviewed user-state side-store ratchet."""
from pathlib import Path

from scripts import check_user_state_side_stores as guard

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "reshape" / "baselines" / "user_state_side_stores.json"


def test_scan_source_counts_occurrences_and_ignores_comments():
    assert guard.scan_source('Path.home()') == 1
    for name in ("STATE", "DATA", "CONFIG"):
        assert guard.scan_source(f'XDG_{name}_HOME') == 1
    assert guard.scan_source('os.path.expanduser("~/state")') == 1
    assert guard.scan_source("os.path.expanduser( '~/.config')") == 1
    assert guard.scan_source('Path.home() / os.environ["XDG_DATA_HOME"]') == 2
    assert guard.scan_source('Path.home() / Path.home()') == 2
    assert guard.scan_source('  # Path.home() XDG_STATE_HOME') == 0
    assert guard.scan_source('XDG_CACHE_HOME\nos.path.expanduser(user_path)') == 0


def _baseline(files):
    return {"categories": {"user-files": "user-owned files"}, "files": files}


def test_findings_reports_growth_and_unknown_categories():
    baseline = _baseline({
        "astrid/risen.py": {"count": 1, "category": "user-files"},
        "astrid/fallen.py": {"count": 3, "category": "user-files"},
        "astrid/gone.py": {"count": 1, "category": "user-files"},
        "astrid/unknown.py": {"count": 1, "category": "not-reviewed"},
    })
    assert guard.findings({"astrid/new.py": 2, "astrid/risen.py": 2,
                           "astrid/fallen.py": 1}, baseline) == [
        "new side store: astrid/new.py uses 2 home/XDG paths; classify it in the "
        "baseline with a category and a reason, or use Runtime app-state",
        "astrid/risen.py rose from 1 to 2",
        "astrid/unknown.py: unknown category 'not-reviewed'",
    ]
    assert guard.findings({"astrid/fallen.py": 1}, _baseline({
        "astrid/fallen.py": {"count": 3, "category": "user-files"},
        "astrid/gone.py": {"count": 1, "category": "user-files"},
    })) == []


def test_reviewed_baseline_is_well_formed():
    baseline = guard.load_baseline(BASELINE)
    assert baseline["schema"] == "astrid.user-state-side-stores.v1"
    assert baseline["rule"]
    assert all(isinstance(reason, str) and reason for reason in baseline["categories"].values())
    for path, entry in baseline["files"].items():
        assert path.startswith("astrid/") and path.endswith(".py")
        assert type(entry["count"]) is int and entry["count"] > 0
        assert entry["category"] in baseline["categories"]
    assert {path for path, entry in baseline["files"].items()
            if entry["category"] == "move-plan-35"} == {
        "astrid/skills/state.py",
        "astrid/core/compute_profile.py",
        "astrid/core/generation/targets.py",
    }


def test_tree_ratchet_holds():
    assert guard.findings(guard.scan_tree(ROOT), guard.load_baseline(BASELINE)) == []


def test_planted_new_side_store_fails(tmp_path, monkeypatch, capsys):
    source = tmp_path / "astrid"
    source.mkdir()
    (source / "known.py").write_text('Path.home()\n', encoding="utf-8")
    baseline = _baseline({"astrid/known.py": {"count": 1, "category": "user-files"}})
    assert guard.findings(guard.scan_tree(tmp_path), baseline) == []
    baseline_path = tmp_path / "baseline.json"
    import json
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    monkeypatch.setattr(guard, "ROOT", tmp_path)
    monkeypatch.setattr(guard, "BASELINE", baseline_path)
    assert guard.main(["--check"]) == 0
    (source / "new.py").write_text('Path.home()\n', encoding="utf-8")
    assert guard.main(["--check"]) == 1
    assert "new side store: astrid/new.py" in capsys.readouterr().err
    original = baseline_path.read_bytes()
    assert guard.main(["--write"]) == 1
    assert baseline_path.read_bytes() == original


def test_write_tightens_counts_without_adding_classifications(tmp_path, monkeypatch):
    import json
    source = tmp_path / "astrid"
    source.mkdir()
    (source / "known.py").write_text('Path.home()\n', encoding="utf-8")
    (source / "gone.py").write_text('# no longer uses a home path\n', encoding="utf-8")
    baseline = _baseline({
        "astrid/known.py": {"count": 2, "category": "user-files"},
        "astrid/gone.py": {"count": 1, "category": "user-files"},
        "astrid/absent.py": {"count": 1, "category": "user-files"},
    })
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")
    monkeypatch.setattr(guard, "ROOT", tmp_path)
    monkeypatch.setattr(guard, "BASELINE", path)
    assert guard.main(["--write"]) == 0
    updated = guard.load_baseline(path)
    assert updated["categories"] == baseline["categories"]
    assert updated["files"] == {
        "astrid/known.py": {"count": 1, "category": "user-files"},
        "astrid/absent.py": {"count": 1, "category": "user-files"},
    }
