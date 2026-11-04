"""Authenticated legacy restoration through the production provider and store."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
from pathlib import Path

import pytest

from astrid.sdk.timeline import validation
from astrid.sdk.timeline._command_state import ABSENT, present
from astrid.sdk.timeline._commit_timing_validation import _reference_basis
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commit import bundle_from_document
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step

pytest_plugins = ("tests.editing_conformance.test_current_pair_reader",)

pytestmark = pytest.mark.integration

RUNTIME_COMMIT = "d9e2a71e87c827290a3ff58a94f6fb8b4bc11e0b"
ARCHIVE_HASH = "2915bd4ac0d46954d19c30f87a517b82074218699978e650fab03fcba81b72d3"
PROJECT = "7b6f2bae8ffe42bf925216c38249ed79"
REVISION = "a3e671256f1c42b8837d3e118c97b21b"
PUBLICATION = "txn-11c90075be6c4c8bb9e2627366f0fe4f"
DIGEST = "sha256:b6069d1ddccc0279e871b142773b63b73b107ebf9d0adc11ab73832b69097afb"


def command(op, args):
    return {"schema_version": 1, "op": op, "version": "1", "select": None, "args": args}


def snapshot(service):
    return tuple(service.store.conn.iterdump())


def document(service):
    head = service.get_project_timeline(PROJECT, "film")
    return service._timeline_commits()[0].revision_closure(PROJECT, "film", head["head_revision_id"])


@pytest.fixture
def case(reader, tmp_path, monkeypatch, request):
    service, _ = reader
    from runtime_protocol.timeline_commit_baseline import verify_baseline

    evidence = Path(os.environ.get("ASTRID_LEGACY_RESTORE_EVIDENCE", str(tmp_path / "evidence"))) / request.node.name
    evidence.mkdir(parents=True, exist_ok=True)
    captures = []
    original_validate = validation.validate_final_candidate

    def save(name, value):
        (evidence / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")

    facts = verify_baseline(service.store.conn, PROJECT, "film",
                            read_object=lambda value: service.cas.read(value.removeprefix("sha256:")))
    assert facts["head"] == {"revision_id": REVISION, "publication_id": PUBLICATION}
    source = document(service)
    assert source["parent_revision"]["content_digest"] == DIGEST
    origins = {}
    for name in ("astrid.sdk.timeline.commit", "astrid.sdk.timeline.validation",
                 "astrid.sdk.timeline._commit_timing_validation", "runtime_protocol.service",
                 "runtime_protocol.timeline_commit_auth", "runtime_protocol.timeline_commit_store"):
        path = Path(importlib.import_module(name).__file__).resolve()
        origins[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    save("source.json", {"canonical": source, "facts": facts, "origins": origins,
                         "runtime_commit": RUNTIME_COMMIT, "archive_sha256": ARCHIVE_HASH})

    def observe(candidate, **kwargs):
        packet = copy.deepcopy({"candidate": candidate, **kwargs})
        packet["reference_basis"] = _reference_basis(
            candidate, ("parent", "", "music-bed"), candidate["parent"]["clips"][0], "word:word-1")
        save(f"validation-{len(captures)}.json", packet)
        captures.append(packet)
        return original_validate(candidate, **kwargs)

    monkeypatch.setattr(validation, "validate_final_candidate", observe)

    def submit(calls, key, **controls):
        from runtime_protocol.timeline_commit_port import route_path

        head = service.get_project_timeline(PROJECT, "film")
        body = {"api_version": 1, "project_id": PROJECT, "timeline_id": "film",
                "base_revision_id": head["head_revision_id"],
                "base_publication_id": head["head_publication_id"],
                "commands": calls, "label": key, "merge": "fields", "reads": [],
                "steps": [step(str(index), index, call, reads=[]) for index, call in enumerate(calls)],
                "source": {"via": "python"}, "rationale": "Authenticated legacy restoration",
                "group_id": key, "dry_run": False, **controls}
        save(key + "-before.json", {"request": body, "canonical": document(service),
                                    "store": snapshot(service), "idempotency_key": key,
                                    "history": service._timeline_commits()[0].history_entry(
                                        PROJECT, "film", head["head_publication_id"])})
        try:
            result = service.commit_timeline(PROJECT, "film", body, idempotency_key=key,
                                            publisher="legacy-restore-test",
                                            invocation_path=route_path(PROJECT, "film"), authorize=lambda: None)
        except Exception as error:
            save(key + "-refusal.json", {"type": type(error).__name__, "message": str(error),
                                        "details": getattr(error, "details", None),
                                        "code": getattr(error, "code", None)})
            raise
        save(key + "-reply.json", result)
        if not body["dry_run"]:
            assert result["data"]["canonical_document"] == document(service)
            assert result["receipt"]["result"] == result["data"]
        return result

    yield service, submit, source, captures, save


def moved(case):
    service, submit, source, _, _ = case
    call = capture_move(bundle_from_document(source), "parent/clip:music-bed", 2)
    accepted = submit([call], "move")
    return accepted, command("timeline.inverse", {"publications": [accepted["receipt"]["receipt_id"]]})


@pytest.mark.parametrize("operation", ["inverse", "step-inverse", "restore"])
def test_authenticated_source_baseline_restores_exact_legacy_bytes(case, operation):
    service, submit, source, captures, _ = case
    accepted, inverse = moved(case)
    post_move = document(service)
    if operation == "step-inverse":
        inverse["args"]["steps"] = {accepted["receipt"]["receipt_id"]: ["0"]}
    call = inverse if operation != "restore" else command(
        "timeline.restore", {"revision_id": REVISION, "content_digest": DIGEST})
    before = snapshot(service)
    preview = submit([call], "preview", dry_run=True)
    assert preview["data"]["status"] == "preview"
    assert preview["data"]["would_status"] == "accepted"
    assert preview["receipt"] is None
    assert snapshot(service) == before
    restored = submit([call], "restore")
    assert restored["data"]["canonical_document"] == source
    assert restored["receipt"]["receipt_id"] not in (PUBLICATION, accepted["receipt"]["receipt_id"])
    records = service._timeline_commits()[0].history_entry(
        PROJECT, "film", restored["receipt"]["receipt_id"])["records"]
    assert records["label"] == "restore"
    assert records["steps"][0]["id"] == "0"
    assert records["steps"][0]["patch"]
    assert records["undo_of"] == ([accepted["receipt"]["receipt_id"]] if operation != "restore" else [])
    assert records["undo_steps"] == (inverse["args"]["steps"] if operation == "step-inverse" else None)
    assert records["restored_from"] == (call["args"] if operation == "restore" else None)
    # The old current-head-only path is red even for this exact authenticated restoration.
    with pytest.raises(ValueError, match="legacy word anchor is missing"):
        validation.validate_final_candidate(captures[-1]["candidate"], baseline=bundle_from_document(post_move))


@pytest.mark.parametrize("stale", [False, True])
def test_inverse_preserves_unrelated_parent_move(case, stale):
    service, submit, source, _, _ = case
    accepted, inverse = moved(case)
    other = capture_move(bundle_from_document(document(service)), "parent/clip:music-other", 3)
    disjoint = submit([other], "disjoint")
    controls = ({"base_revision_id": accepted["data"]["accepted_revision_id"],
                 "base_publication_id": accepted["receipt"]["receipt_id"]} if stale else {})
    restored = submit([inverse], "inverse", **controls)
    assert restored["data"]["status"] == ("merged" if stale else "accepted")
    expected = copy.deepcopy(disjoint["data"]["canonical_document"]["parent_revision"]["payload"])
    expected["clips"][0] = copy.deepcopy(source["parent_revision"]["payload"]["clips"][0])
    expected["config"]["clips"] = copy.deepcopy(expected["clips"])
    actual = restored["data"]["canonical_document"]
    assert actual["parent_revision"]["payload"] == expected
    for field in ("shot_revisions", "internal_timeline_revisions", "dependency_manifest"):
        assert actual[field] == source[field]


@pytest.mark.parametrize("damage", ["new", "modified", "equal-authored", "forged-source", "forged-publication",
                                     "forged-step", "digest", "foreign", "aba", "membership", "order"])
def test_invalid_or_unauthenticated_restoration_refuses_without_writes(case, damage):
    service, submit, source, _, _ = case
    _, inverse = moved(case)
    controls = {}
    if damage in ("new", "modified", "equal-authored", "forged-source"):
        value = "word:word-1" if damage != "modified" else "word:forged"
        previous = present("word:word-1") if damage in ("modified", "equal-authored") else ABSENT
        calls = [patch(change(["clips", "id:music-bed", "app", "astrid", "on"], previous, value))]
        if damage in ("modified", "equal-authored"):
            calls.insert(0, inverse)
        if damage == "forged-source":
            controls["source"] = {"via": "python", "baseline": source, "history": {"records": []}}
    elif damage == "forged-publication":
        calls = [command("timeline.inverse", {"publications": ["txn-forged"]})]
    elif damage == "forged-step":
        inverse["args"]["steps"] = {inverse["args"]["publications"][0]: ["forged"]}
        calls = [inverse]
    elif damage in ("digest", "foreign"):
        calls = [command("timeline.restore", {"revision_id": REVISION if damage == "digest" else "foreign",
                                              "content_digest": "sha256:" + "0" * 64 if damage == "digest" else DIGEST})]
    else:
        if damage == "aba":
            for index, at in enumerate((3, 2)):
                submit([capture_move(bundle_from_document(document(service)), "parent/clip:music-bed", at)],
                       "aba-" + str(index))
            calls = [inverse]
        else:
            rows = copy.deepcopy(document(service)["parent_revision"]["payload"]["clips"])
            changed = rows[:1] if damage == "membership" else list(reversed(rows))
            # A legitimate intervening collection write must not acquire history authority.
            submit([patch(change(["clips"], present(rows), changed))], "collection")
            calls = [inverse]
    before = snapshot(service)
    with pytest.raises(Exception) as caught:
        submit(calls, "refused", **controls)
    details = getattr(caught.value, "details", None) or {}
    if damage == "forged-source":
        assert caught.value.code == "validation_error"
        assert "Additional properties are not allowed" in str(caught.value)
    else:
        assert details.get("reason") or details.get("code"), str(caught.value)
    assert snapshot(service) == before


@pytest.mark.parametrize("damage", ["changed", "alias", "shot-owner", "internal-owner", "target", "target-remove",
                                     "documents", "outside", "no-writes", "ordinary-write", "until-flag"])
def test_restoration_source_unit_keeps_exact_reference_and_write_guards(case, damage):
    _, submit, _, captures, save = case
    _, inverse = moved(case)
    submit([inverse], "inverse")
    packet = captures[-1]
    candidate = copy.deepcopy(packet["candidate"])
    baseline = packet["baseline"]
    sources = packet["source_bundles"]
    records = copy.deepcopy(packet["restoration_records"])
    if damage == "changed":
        candidate["parent"]["clips"][0]["app"]["astrid"]["on"] = "word:forged"
    elif damage == "alias":
        candidate["parent"]["clips"][0]["app"]["on"] = "word:forged"
    elif damage == "until-flag":
        candidate["parent"]["clips"][0]["app"]["astrid"]["until_film_end"] = False
    elif damage in ("shot-owner", "internal-owner"):
        field = "revision_id" if damage == "shot-owner" else "internal_timeline_revision_id"
        next(iter(candidate["source_mapping"]["shots"].values()))[field] = "unproven-successor"
    elif damage == "target":
        next(iter(candidate["shots"].values()))["internal_timeline"]["clips"][0]["at"] = 1
    elif damage == "target-remove":
        next(iter(candidate["shots"].values()))["internal_timeline"]["clips"].pop(0)
    elif damage == "documents":
        candidate["source_mapping"]["pinned_documents"] = [{"forged": True}]
    elif damage == "outside":
        candidate["project_id"] = "outside"
    elif damage == "no-writes":
        records = []
    else:
        from astrid.sdk.timeline.commands import execute

        ordinary = patch(change(["clips", "id:music-bed", "app", "astrid", "on"],
                                present("word:word-1"), "word:word-1"))
        records.extend(execute(candidate, [ordinary]).records)
    candidate["parent"]["config"]["clips"] = copy.deepcopy(candidate["parent"]["clips"])
    save("unit-negative.json", {"candidate": candidate, "baseline": baseline,
                                "source_bundles": sources, "restoration_records": records})
    with pytest.raises((ValueError, KeyError)):
        validation.validate_final_candidate(candidate, baseline=baseline, source_bundles=sources,
                                            restoration_records=records)
