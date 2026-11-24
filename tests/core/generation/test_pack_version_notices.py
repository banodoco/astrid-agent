"""LE-10 renderer witnesses; target_schema integration follows WS-0g."""
from astrid.core.generation.entries import pack_version_notices
from astrid.core.generation.preflight import readiness_envelope
from vibecomfy.porting.provenance import extract_provenance


REPO = "https://github.com/example/fixture-pack"
FIRST = "a" * 40
SECOND = "b" * 40


def _receipt(version):
    return extract_provenance({"nodes": [
        {"id": 1, "type": "FixtureNode",
         "properties": {"cnr_id": "fixture-pack", "ver": version}},
    ]}).to_json()


def _selected(**fields):
    return [{"slug": "fixture-pack", "source": "git", "url": REPO,
             "commit": SECOND, **fields}]


def test_ui_version_and_selected_commit_are_both_visible():
    notice, = pack_version_notices(_receipt("1.0.0"), _selected(version="2.0.0"), installing=True)
    assert notice["message"] == f"fixture-pack: recorded 1.0.0, installing 2.0.0 ({SECOND})"
    assert notice["details"]["recorded_versions"] == ["1.0.0"]
    assert notice["details"]["selected_commit"] == SECOND
    envelope = readiness_envelope(issues=[notice["issue"]])
    assert envelope["status"] == "ready_with_warnings"
    assert envelope["issues"][0]["code"] == "pack_version_differs"
    assert envelope["fixes"] == []


def test_exact_different_commits_warn_but_selection_does_not_claim_install():
    notice, = pack_version_notices(_receipt(FIRST), _selected(), installing=False)
    assert notice["message"] == f"fixture-pack: recorded {FIRST}, selected {SECOND}"
    assert notice["issue"]["code"] == "pack_version_differs"


def test_explicit_api_pin_is_not_a_claim_of_recorded_version():
    receipt = extract_provenance({"1": {"class_type": "FixtureNode", "inputs": {}}}).to_json()
    notice, = pack_version_notices(receipt, _selected(entry_add=True), installing=True)
    assert notice["message"] == f"fixture-pack: version not recorded; pinned at {SECOND} by you"
    assert notice["issue"] is None


def test_incomparable_label_does_not_manufacture_a_mismatch():
    notice, = pack_version_notices(_receipt("1.0.0"), _selected(), installing=True)
    assert notice["message"] == (
        f"fixture-pack: recorded 1.0.0, installing {SECOND}; version correspondence is unverified"
    )
    assert notice["issue"] is None
