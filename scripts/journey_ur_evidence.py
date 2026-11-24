"""Update/rollback leg: the pure evidence logic (candidate/manifest checks, comparisons, receipt evaluation).

No I/O beyond what the caller hands in, so every function here is unit tested without an install.
"""
from __future__ import annotations

if __package__:
    from .check_journey_evidence import DIGEST, SHA
else:
    from check_journey_evidence import DIGEST, SHA

R14 = {"release": 14, "app": "e0da794ac9d26a808ab4082b29f60408b66b3a4c",
       "agent": "c0ad6b54aadd21630d98bb5039613e37efe92a8f",
       "runtime": "5120462d67479dc4719406d0d34440edb616ace1",
       "vibecomfy": "48b5fa9e45630e1bc502651975ec0ef3a5e74c4c"}
GUARD_TRIGGERS = ("timeline_history_no_delete", "timeline_history_no_replace", "timeline_history_no_update")
EPHEMERAL_SUFFIXES = ("-wal", "-shm", "-journal")
HOOK_MARKER = "SKIP_AS_FAIL:rollback-hook-absent"
STEPS = {
    "step1-r14-install.json": ("r14_tuple_exact", "project_timeline_edit_saved", "digest_recorded",
                               "realm_ids_recorded", "fingerprint_recorded", "stamps_recorded"),
    "step2-update-r15.json": ("candidate_live_in_window", "r15_edit_saved", "digest_recorded",
                              "guard_layer_present_while_candidate_live", "installer_failed_health"),
    "step3-rollback.json": ("a_stamps_restored", "b_r14_runtime_starts", "c_guard_layer_removed",
                            "d_timeline_readback", "journal_rolled_back", "fingerprint_recorded", "no_data_lost"),
    "step4-resave-reupdate.json": ("e_r14_edit_saved", "e_reupdate_succeeded", "e_history_preserved"),
}
RESULT_NAME = "journey-result.json"


def sha_field(value, name):
    # check_journey_evidence.validate_tuple also needs the pinokio pin, bootstrap artifacts and a git checkout, so
    # only its SHA/DIGEST patterns are shared; this leg's tuple has no pinokio and no artifact hashes.
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError(f"candidate.{name}: full commit SHA required")
    return value


def parse_candidate(candidate: dict) -> dict:
    """The Release 15 tuple (the workflow's ``candidate`` input); it must not be the Release 14 tuple."""
    if not isinstance(candidate, dict):
        raise ValueError("candidate must be a JSON object")
    parsed = {name: sha_field(candidate.get(name), name) for name in ("app", "agent", "runtime")}
    digest = candidate.get("manifest_sha256")
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ValueError("candidate.manifest_sha256: SHA-256 required")
    if parsed["agent"] == R14["agent"] or parsed["runtime"] == R14["runtime"] or parsed["app"] == R14["app"]:
        raise ValueError("candidate shares a source with the Release 14 tuple; this leg needs a real update")
    return {**parsed, "manifest_sha256": digest}


def check_manifest(manifest: dict, expected: dict, *, release=None) -> dict:
    """The manifest must pin exactly ``expected`` agent/runtime (and vibecomfy when given)."""
    if not isinstance(manifest, dict) or type(manifest.get("release")) is not int:
        raise ValueError("manifest has no integer release")
    for name in ("agent", "runtime", "vibecomfy"):
        if name in expected and (manifest.get(name) or {}).get("sha") != expected[name]:
            raise ValueError(f"manifest {name} pin differs from the expected tuple")
    if release is not None and manifest["release"] != release:
        raise ValueError(f"manifest release {manifest['release']} is not {release}")
    if release is None and manifest["release"] <= R14["release"]:
        raise ValueError("candidate manifest is not newer than Release 14")
    return manifest


def hook_present(core_source: str, backend_source: str) -> bool:
    """Whether the Agent source carries the rollback hook (astrid-agent #609)."""
    return "remove_history_guards" in core_source and "def remove_history_guards" in backend_source


def guard_state(names: list[str]) -> str:
    return ("installed" if sorted(names) == list(GUARD_TRIGGERS) else "absent" if not names else "partial")


def compare_stamps(before: dict, after: dict) -> list[str]:
    """Release 14 must be back exactly: same version, tuple, manifest bytes and health identity; new instance."""
    problems = []
    for key in ("version_id", "identity", "manifest_sha256", "tuple"):
        if before.get(key) != after.get(key) or before.get(key) in (None, {}, ""):
            problems.append(f"stamp {key} differs or is missing: {before.get(key)!r} -> {after.get(key)!r}")
    for key in ("identity", "schema_digest", "protocol"):
        if (before.get("health") or {}).get(key) != (after.get("health") or {}).get(key):
            problems.append(f"health {key} differs after rollback")
    if not (before.get("health") or {}).get("identity"):
        problems.append("health reported no identity stamp")
    if before.get("runtime_instance_id") == after.get("runtime_instance_id"):
        problems.append("Runtime instance did not change across the update and rollback")
    return problems


def compare_reads(candidate_read: dict, old_read: dict) -> dict:
    """Release 14's read-back of the Release 15 save: identical, or only by fields it cannot represent."""
    mismatched, unrepresentable = [], []
    old_clips = {clip.get("clip_id"): clip for clip in old_read.get("parent_clips", [])}
    for clip in candidate_read.get("parent_clips", []):
        other = old_clips.get(clip.get("clip_id"))
        if other is None:
            mismatched.append(f"clip {clip.get('clip_id')} missing")
            continue
        for field, value in clip.items():
            if field not in other:
                unrepresentable.append(f"{clip.get('clip_id')}.{field}")
            elif other[field] != value:
                mismatched.append(f"{clip.get('clip_id')}.{field}")
    if len(old_clips) != len(candidate_read.get("parent_clips", [])):
        mismatched.append("clip count differs")
    same = bool(candidate_read.get("head_content_digest")) and (
        candidate_read.get("head_content_digest") == old_read.get("head_content_digest"))
    if not same and not unrepresentable and not mismatched:
        mismatched.append("head content digest differs with every clip field identical")
    return {"identical": same, "unrepresentable": sorted(unrepresentable), "mismatched": sorted(mismatched),
            "ok": not mismatched and bool(candidate_read.get("head_content_digest"))}


def fingerprint_summary(before: dict, after: dict) -> dict:
    """Data survival between two stopped-root snapshots (see data_fingerprint.py); ephemeral SQLite files ignored."""
    lost = [path for path in before["files"] if path not in after["files"] and not path.endswith(EPHEMERAL_SUFFIXES)]
    changed = [path for path, meta in before["files"].items() if path in after["files"] and not meta.get("live")
               and after["files"][path]["sha256"] != meta["sha256"]]
    return {"realms_before": before["realms"], "realms_after": after["realms"],
            "realms_lost": sorted(set(before["realms"]) - set(after["realms"])),
            "files_lost": sorted(lost), "files_changed": sorted(changed),
            "files_new": sorted(set(after["files"]) - set(before["files"])),
            "ok": bool(before["realms"]) and not (set(before["realms"]) - set(after["realms"])) and not lost}


def evaluate(receipts: dict) -> list[str]:
    """Every problem in a set of receipts keyed by file name; empty only when every required check passed."""
    problems = []
    for name, required in STEPS.items():
        receipt = receipts.get(name)
        if not isinstance(receipt, dict):
            problems.append(f"{name}: receipt missing")
            continue
        if receipt.get("status") != "pass":
            problems.append(f"{name}: status {receipt.get('status')!r}")
        checks = receipt.get("checks") if isinstance(receipt.get("checks"), dict) else {}
        for check in required:
            if checks.get(check) != "pass":
                problems.append(f"{name}: check {check} is {checks.get(check, 'missing')!r}")
    result = receipts.get(RESULT_NAME)
    if not isinstance(result, dict):
        problems.append(f"{RESULT_NAME}: receipt missing")
        return problems
    if result.get("status") != "pass":
        problems.append(f"{RESULT_NAME}: status {result.get('status')!r}: {result.get('error')}")
    if result.get("hook_present") is not True:
        problems.append(f"{HOOK_MARKER}: hook_present={result.get('hook_present')!r}; the candidate Agent must carry "
                        "the rollback hook (astrid-agent #609) for step 3(c) to pass")
    if result.get("final_cleanup") != "pass":
        problems.append(f"{RESULT_NAME}: final_cleanup is {result.get('final_cleanup')!r}")
    return problems
