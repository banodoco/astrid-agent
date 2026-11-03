"""Synthetic shape checks only; no fixture represents production evidence."""
import copy
import hashlib
import io
import json
import stat
import zipfile

import pytest

from scripts import check_journey_evidence as checker


SLOTS = [row for row in checker.M4_6_MATRIX if row["mode"].startswith("pinokio-")]
CANDIDATE = dict(app="1" * 40, agent="2" * 40, runtime="3" * 40,
                 pinokio="4" * 40, manifest_sha256="5" * 64)


def bundle(slot):
    payloads = {p: b'{"synthetic":true}' for p in checker._M4_6_NATIVE_FILES
                if p != "journey-failure-result.json"}
    receipt = {"slot_id": slot["job_id"], "mode": slot["mode"], "os": slot["os"],
               "runner_image": slot["runner"], "shell": slot["shell"],
               "manifest_sha256": CANDIDATE["manifest_sha256"],
               "candidate_checkouts": {k: {"sha": CANDIDATE[k]} for k in ("app", "agent", "runtime", "pinokio")},
               "evidence_files": [{"path": p, "sha256": hashlib.sha256(data).hexdigest()}
                                  for p, data in sorted(payloads.items())],
               "scenarios": {"keep_data_uninstall": {"exit_code": 0}}}
    payloads["journey-failure-result.json"] = json.dumps(receipt).encode()
    return payloads


def zipped(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        for path, content in files.items():
            z.writestr(path, content)
    return stream.getvalue()


def case(slot):
    archive = zipped(bundle(slot))
    artifact = {"id": 41, "name": f"m4-6-native-21-2-{slot['job_id']}", "expired": False,
                "workflow_run": {"id": 21}, "size_in_bytes": len(archive),
                "digest": "sha256:" + hashlib.sha256(archive).hexdigest()}
    job = {"id": 31, "run_id": 21, "run_attempt": 2, "status": "completed", "conclusion": "success",
           "name": f"m4-6-native-{slot['job_id']}", "runner_name": "synthetic-runner",
           "labels": [slot["runner"]], "check_run_url": "https://api.github.com/repos/banodoco/astrid-agent/check-runs/32"}
    return dict(candidate=copy.deepcopy(CANDIDATE), slot_id=slot["job_id"], run_id=21, attempt=2,
                job_id=31, check_run_id=32, artifact_id=41, harness_sha="a" * 40,
                head_sha="b" * 40, workflow_sha="c" * 40, workflow_ref="c" * 40, workflow=b"synthetic\n",
                run={"id": 21, "run_attempt": 2, "head_sha": "b" * 40,
                     "repository": {"full_name": "banodoco/astrid-agent"},
                     "path": ".github/workflows/journey.yml", "workflow_id": 51},
                jobs=[job], check_run={"id": 32, "head_sha": "b" * 40, "name": job["name"],
                                       "status": "completed", "conclusion": "success"},
                artifact=artifact, artifacts=[copy.deepcopy(artifact)], archive=archive)


def rearchive(c, files):
    c["archive"] = zipped(files)
    c["artifact"]["size_in_bytes"] = len(c["archive"])
    c["artifact"]["digest"] = "sha256:" + hashlib.sha256(c["archive"]).hexdigest()
    c["artifacts"] = [copy.deepcopy(c["artifact"])]


@pytest.mark.parametrize("slot", SLOTS, ids=lambda s: s["job_id"])
def test_synthetic_shapes_never_admit(slot):
    result = checker.m4_6_actions_index(**case(slot))
    assert result["admitted"] is False
    assert "unauthenticated diagnostic" in result["reason"]
    assert result["slot_id"] == slot["job_id"]


@pytest.mark.parametrize("change", [
    lambda c: c["run"].update(id=22),
    lambda c: c["run"].update(run_attempt=1),
    lambda c: c["run"].update(head_sha="d" * 40),
    lambda c: c.update(workflow_ref="a" * 40),
    lambda c: c["jobs"].clear(),
    lambda c: c["jobs"].append(copy.deepcopy(c["jobs"][0])),
    lambda c: c["jobs"][0].update(name="renamed"),
    lambda c: c["jobs"][0].update(conclusion="skipped"),
    lambda c: c["jobs"][0].update(conclusion="cancelled"),
    lambda c: c["jobs"][0].update(run_attempt=1),
    lambda c: c["jobs"][0].update(labels=["windows-2022"]),
    lambda c: c["jobs"][0].update(check_run_url="wrong"),
    lambda c: c["check_run"].update(id=31),
    lambda c: c["check_run"].update(head_sha="d" * 40),
    lambda c: c["artifact"].update(name="m4-6-native-21-1-pinokio-backend-linux"),
    lambda c: c["artifact"].update(size_in_bytes=1),
    lambda c: c["artifact"].update(digest="sha256:" + "f" * 64),
    lambda c: c["artifacts"].append(copy.deepcopy(c["artifact"])),
    lambda c: c["artifacts"][0].update(id=42),
    lambda c: c.update(archive=b"corrupt"),
    lambda c: c["candidate"].update(agent="d" * 40),
])
def test_metadata_mismatches_fail(change):
    c = case(SLOTS[1])
    change(c)
    with pytest.raises(ValueError):
        checker.m4_6_actions_index(**c)


@pytest.mark.parametrize("field,value", [("mode", "pinokio-fully-local"), ("os", "windows"),
                                         ("shell", "powershell.exe"), ("runner_image", "windows-2022")])
def test_crossed_slot_fields_fail(field, value):
    slot = SLOTS[1]
    c = case(slot)
    files = bundle(slot)
    row = json.loads(files["journey-failure-result.json"])
    row[field] = value
    files["journey-failure-result.json"] = json.dumps(row).encode()
    rearchive(c, files)
    with pytest.raises(ValueError):
        checker.m4_6_actions_index(**c)


@pytest.mark.parametrize("change", ["missing", "renamed", "digest", "exit_missing", "exit_bool", "exit_nonzero"])
def test_bundle_mismatches_fail(change):
    slot = SLOTS[1]
    c = case(slot)
    files = bundle(slot)
    row = json.loads(files["journey-failure-result.json"])
    path = row["evidence_files"][0]["path"]
    if change == "missing":
        del files[path]
    elif change == "renamed":
        files["../escape.json"] = files.pop(path)
    elif change == "digest":
        files[path] = b'{}'
    elif change == "exit_missing":
        del row["scenarios"]["keep_data_uninstall"]["exit_code"]
    elif change == "exit_bool":
        row["scenarios"]["keep_data_uninstall"]["exit_code"] = False
    else:
        row["scenarios"]["keep_data_uninstall"]["exit_code"] = 1
    files["journey-failure-result.json"] = json.dumps(row).encode()
    rearchive(c, files)
    with pytest.raises(ValueError):
        checker.m4_6_actions_index(**c)


def test_symlink_and_duplicate_entries_fail():
    slot = SLOTS[1]
    for duplicate in (False, True):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as z:
            for path, content in bundle(slot).items():
                if not duplicate and path.startswith("evidence/"):
                    info = zipfile.ZipInfo(path)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    z.writestr(info, content)
                else:
                    z.writestr(path, content)
            if duplicate:
                z.writestr("journey-failure-result.json", b'{}')
        with pytest.raises(ValueError):
            checker._m4_6_native_bundle(stream.getvalue(), CANDIDATE, slot)
