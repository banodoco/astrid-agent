"""Focused checks for the pre-dispatch Pinokio bundle compatibility tool."""
from __future__ import annotations

import json

import pytest

from scripts import ci_journey_provider as provider
from scripts import ci_journey_r24 as r24
from scripts import ci_journey_r24_push_admission as push_admission
from scripts import pinokio_bundle_compat as compat
from scripts import pinokio_linux_mode_actions as mode_actions

CURRENT = {
    "app": "79f7e5397c044c4c97dc7e97b20ad96f787e5247",
    "agent": "ab43bad818ef65ca1e929b973a2f61cd9fc4bf61",
    "runtime": "2776ba973b6c8f3555b5629b0840d9691a98d3c2",
    "pinokio": "3a6a98ae251ca126505245286e2d3e760f4348e9",
    "manifest_sha256": "b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f",
    "artifacts": {
        "public/install": "1" * 64,
        "public/install.sh": "2" * 64,
        "public/install.ps1": "3" * 64,
    },
    "published_at": "2026-11-02T17:55:27Z",
}
CANDIDATE = {
    "app": "bfb15570d72c39fbdc32f6209ecec7c929b71d5c",
    "agent": "69dda026212ca16c37cc8239d06ccd159603aa24",
    "runtime": "2776ba973b6c8f3555b5629b0840d9691a98d3c2",
    "pinokio": "7955800419fc92b11e553d6f7acb7613090617e6",
    "manifest_sha256": "09707100ad238cf206085aa5b267ca4108e482ac50e06068a3475932180b65bd",
    "artifacts": dict(CURRENT["artifacts"]),
}
PRODUCER = "13cb31104c8aae655d0ee04efdb9537aa111e340"


def test_current_tuple_parser_accepts_exact_shape(tmp_path) -> None:
    path = tmp_path / "CURRENT-TUPLE.json"
    path.write_text(json.dumps(CURRENT), encoding="utf-8")
    raw, full, candidate = compat.load_current_tuple(path)
    assert json.loads(raw) == CURRENT
    assert full == CURRENT
    assert candidate == {key: CURRENT[key] for key in compat._CANDIDATE_FIELDS}


def test_provisional_candidate_parser_accepts_exact_shape_without_publication(tmp_path) -> None:
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(CANDIDATE), encoding="utf-8")
    raw, full, candidate = compat.load_candidate(path)
    assert json.loads(raw) == CANDIDATE
    assert full == CANDIDATE
    assert candidate == {key: CANDIDATE[key] for key in compat._CANDIDATE_FIELDS}
    assert "published_at" not in full


@pytest.mark.parametrize("mutation", ["extra", "artifact", "sha", "time"])
def test_current_tuple_parser_rejects_noncanonical_binding(tmp_path, mutation) -> None:
    value = json.loads(json.dumps(CURRENT))
    if mutation == "extra":
        value["other"] = True
    elif mutation == "artifact":
        del value["artifacts"]["public/install.ps1"]
    elif mutation == "sha":
        value["agent"] = "not-a-commit"
    else:
        value["published_at"] = "today"
    path = tmp_path / "CURRENT-TUPLE.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(compat.CompatibilityError):
        compat.load_current_tuple(path)


def test_provisional_runner_and_split_producers_remain_nonqualifying() -> None:
    candidate = {key: CURRENT[key] for key in compat._CANDIDATE_FIELDS}
    assert r24.R24_CANDIDATE == provider.LIVE_CANDIDATE
    assert r24.R24_CANDIDATE != candidate
    assert provider.LIVE_PRODUCER_REF == push_admission.PRODUCER_REF
    assert provider.LIVE_PRODUCER_REF != PRODUCER
    assert mode_actions.MODE_PRODUCER_REF == PRODUCER
    assert mode_actions.MODES["fully-local"] == "pinokio-fully-local-linux"
    assert r24.R24_CANDIDATE == {
        key: CANDIDATE[key] for key in compat._CANDIDATE_FIELDS
    }
    assert compat.boundary() == {
        "authenticated_custody": False,
        "qualification": "not-evaluated",
        "m4_6_receipt": False,
        "live_action_executed": False,
    }
