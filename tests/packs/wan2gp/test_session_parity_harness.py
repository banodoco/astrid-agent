"""M2-11a parity harness: host path versus in-process pack path, same inputs.

Path A runs the real ``GenericPackHost._run_wan_session`` on a bare host object.
Only the engine edge is faked: ``invoke_wan_session`` returns native evidence
and the managed-session lookup is a no-op. ``compile_host_settings``,
``materialize_host_result`` and output custody run for real.

Path B calls the same pack driver functions in-process against a fake session
whose ``submit`` returns the same native evidence. This is the shape the pack
action will take once it takes a session client.
"""

from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from astrid.core.execution.generic_host import GenericPackHost, _ManagedWanChildAdapter
from astrid.core.execution.managed_tool_session import AdmissionToken
from astrid.packs.wan2gp.src.driver import (
    compile_host_settings,
    materialize_host_result,
    snapshot_native_outputs,
)

FIXTURE = Path(__file__).parent / "fixtures" / "one_frame_engine_result.json"

# Ignored-diff keys (run identity and wall-clock timing only). Everything else
# is compared exactly, except the canonicalization below.
RUN_ID_KEYS = frozenset({
    "wan_native.token_id",
    "wan_native.invocation_id",
    "wan_native.birth",
    "wan_native.process_id",
})
TIMING_KEYS = frozenset({
    "wan_mapping.manifest.created",
})
ALLOWED_DIFF_KEYS = RUN_ID_KEYS | TIMING_KEYS

# Canonicalization (applied to both sides before comparing, not an allow-list):
# - run roots (source_root, output_root, manifest.spool) become "<root>";
# - each file path is taken relative to its run root, so the RELATIVE structure
#   (same files, same names, same nesting) must match exactly;
# - a file stat keeps only st_size (index 2): dev, inode, mtime_ns and ctime_ns
#   are per-run scratch metadata. size and sha256 stay exact.
STAT_SIZE_INDEX = 2

TOKEN = AdmissionToken(
    token_id="tok-parity-1",
    session_id="sess-parity-1",
    capability_id="wan2gp",
    invocation_id="inv-parity-1",
    generation=1,
    binding_identity=("binding", 1),
)


# ---------------------------------------------------------------- comparator


def _normalize(path: str) -> str:
    return re.sub(r"\[\d+\]", "[]", path)


def _join(path: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else str(key)


def _is_scalar_list(value: Any) -> bool:
    return isinstance(value, list) and not any(isinstance(item, (Mapping, list)) for item in value)


def _relative(value: Any, root: Any) -> Any:
    """Path under its run root, as a root-relative string; anything else is kept verbatim."""
    if not isinstance(value, str) or not isinstance(root, str) or not root:
        return value
    if value == root:
        return "."
    prefix = root.rstrip("/") + "/"
    return value[len(prefix):] if value.startswith(prefix) else value


def _canonicalize(evidence: Any) -> Any:
    """Return a copy with run roots replaced, paths made relative, and stat reduced to size."""
    out = copy.deepcopy(evidence)
    native = out.get("wan_native") if isinstance(out, dict) else None
    mapping = out.get("wan_mapping") if isinstance(out, dict) else None
    source_root = native.get("source_root") if isinstance(native, dict) else None
    output_root = mapping.get("output_root") if isinstance(mapping, dict) else None
    if isinstance(native, dict):
        if "source_root" in native:
            native["source_root"] = "<root>"
        for item in native.get("output_snapshots", []) or []:
            if isinstance(item, dict):
                if "path" in item:
                    item["path"] = _relative(item["path"], source_root)
                if isinstance(item.get("stat"), list):
                    item["stat"] = [item["stat"][STAT_SIZE_INDEX]]
    if isinstance(mapping, dict):
        if "output_root" in mapping:
            mapping["output_root"] = "<root>"
        manifest = mapping.get("manifest")
        if isinstance(manifest, dict) and "spool" in manifest:
            manifest["spool"] = _relative(manifest["spool"], output_root) if manifest["spool"] != output_root else "<root>"
        for item in mapping.get("staged_outputs", []) or []:
            if isinstance(item, dict):
                if "path" in item:
                    item["path"] = _relative(item["path"], output_root)
                if isinstance(item.get("stat"), list):
                    item["stat"] = [item["stat"][STAT_SIZE_INDEX]]
    return out


def _drop_ignored(evidence: Any, ignored: frozenset[str]) -> Any:
    out = copy.deepcopy(evidence)

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key in list(node):
                child = _join(path, key)
                if _normalize(child) in ignored:
                    del node[key]
                else:
                    walk(node[key], child)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, _join(path, index))

    walk(out, "")
    return out


def _diff(a: Any, b: Any, path: str, out: list[str]) -> None:
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        for key in sorted(set(a) | set(b), key=str):
            child = _join(path, key)
            if key not in a or key not in b:
                out.append(child)
                continue
            _diff(a[key], b[key], child, out)
        return
    if isinstance(a, list) and isinstance(b, list) and not (_is_scalar_list(a) and _is_scalar_list(b)):
        if len(a) != len(b):
            out.append(path)
            return
        for index, (left, right) in enumerate(zip(a, b)):
            _diff(left, right, _join(path, index), out)
        return
    if isinstance(a, list) and isinstance(b, list):
        if [(type(item), item) for item in a] != [(type(item), item) for item in b]:
            out.append(path)
        return
    if type(a) is not type(b) or a != b:
        out.append(path)


def compare_evidence(a: Any, b: Any, allowed_diff_keys: frozenset[str] | set[str]) -> list[str]:
    """Return the sorted dotted paths where canonicalized ``a`` and ``b`` differ.

    ``allowed_diff_keys`` are dropped from both sides. Canonicalization (roots,
    relative paths, stat size) is always applied. Types are compared, so 1 and True differ.
    """
    ignored = frozenset(allowed_diff_keys)
    left = _drop_ignored(_canonicalize(a), ignored)
    right = _drop_ignored(_canonicalize(b), ignored)
    out: list[str] = []
    _diff(left, right, "", out)
    return sorted(out)


def _walk_leaves(obj: Any, prefix: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    if isinstance(obj, Mapping):
        for key in sorted(obj, key=str):
            yield from _walk_leaves(obj[key], prefix + (key,))
    elif isinstance(obj, list) and not _is_scalar_list(obj):
        for index, item in enumerate(obj):
            yield from _walk_leaves(item, prefix + (index,))
    else:
        yield prefix


def _format_location(location: tuple[Any, ...]) -> str:
    path = ""
    for key in location:
        path = _join(path, key)
    return path


def _perturb(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    if isinstance(value, str):
        return value + "~"
    if value is None:
        return "__perturbed__"
    if isinstance(value, list):
        if not value:
            return ["__perturbed__"]
        return [_perturb(value[0]), *value[1:]]
    raise TypeError(f"unexpected leaf {type(value)!r}")


def _set_location(root: Any, location: tuple[Any, ...], value: Any) -> None:
    target = root
    for key in location[:-1]:
        target = target[key]
    target[location[-1]] = value


# ------------------------------------------------------------- fake engine edge










def _native_evidence(token: AdmissionToken, source_root: Path, fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Build the native evidence the Wan child would return for one frame."""
    source_root.mkdir(parents=True, exist_ok=True)
    generated = list(fixture["generated_files"])
    for name in generated:
        (source_root / name).write_bytes(str(fixture["output_bytes_text"]).encode("utf-8"))
    return {
        **_ManagedWanChildAdapter.identity(token),
        "native_job_id": fixture["native_job_id"],
        "events": [],
        "result": {"success": True, "errors": [], "generated_files": generated},
        "birth": fixture["birth"],
        "process_id": fixture["process_id"],
        "returncode": fixture["returncode"],
        "terminal": True,
        "source_root": str(source_root.absolute()),
        "output_snapshots": snapshot_native_outputs(generated, source_root),
        "native_error": None,
    }


class FakeEngineSession:
    """Engine edge for both paths. ``submit`` is once per run, like the channel."""

    def __init__(self, evidence: Mapping[str, Any]) -> None:
        self._evidence = copy.deepcopy(dict(evidence))
        self.submitted: list[dict[str, Any]] = []

    def submit(self, settings: Mapping[str, Any], timeout_seconds: float) -> dict[str, Any]:
        if self.submitted:
            raise AssertionError("submit is allowed once per run")
        self.submitted.append({"settings": dict(settings), "timeout_seconds": timeout_seconds})
        return copy.deepcopy(self._evidence)


class _FakeManagedToolSession:
    def adapter_for(self, token: AdmissionToken, begin: bool = False) -> None:
        return None


# ----------------------------------------------------------------- the two paths


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def run_host_path(
    inputs: Mapping[str, Any],
    *,
    token: AdmissionToken,
    attempt: Path,
    output_root: Path,
    session: FakeEngineSession,
) -> dict[str, Any]:
    """Path A: the real host method, with only the engine edge replaced."""
    host = GenericPackHost.__new__(GenericPackHost)
    host.managed_tool_session = _FakeManagedToolSession()

    def invoke_wan_session(managed_token, settings, *, timeout, cancelled, progress_path=None):
        assert managed_token is token
        assert timeout > 0
        return session.submit(settings, timeout)

    host.invoke_wan_session = invoke_wan_session
    outcome = host._run_wan_session(
        inputs=dict(inputs),
        output_root=output_root,
        attempt=attempt,
        managed_token=token,
        execution_deadline=time.monotonic() + 30.0,
        cancelled=lambda: False,
    )
    assert isinstance(outcome, SimpleNamespace)
    return outcome.payload


def run_pack_in_process(
    inputs: Mapping[str, Any],
    *,
    token: AdmissionToken,
    output_root: Path,
    session: FakeEngineSession,
) -> dict[str, Any]:
    """Path B: the pack driver functions in-process, as the pack action will call them."""
    settings = compile_host_settings(dict(inputs))
    evidence = session.submit(settings, 30.0)
    result = evidence["result"]
    if result.get("success") is not True:
        raise RuntimeError("native Wan2GP job failed")
    mapped = materialize_host_result(
        evidence,
        attempt_root=output_root,
        inputs=dict(inputs),
        expected_identity=_ManagedWanChildAdapter.identity(token),
    )
    return {"wan_native": dict(evidence), "wan_mapping": mapped}


def _run_both(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], FakeEngineSession, FakeEngineSession]:
    fixture = _load_fixture()
    inputs = dict(fixture["inputs"])
    # One native source: both paths observe the same engine output bytes.
    evidence = _native_evidence(TOKEN, tmp_path / "native-spool", fixture)

    session_a = FakeEngineSession(evidence)
    payload_a = run_host_path(
        inputs,
        token=TOKEN,
        attempt=tmp_path / "a" / "attempt",
        output_root=tmp_path / "a" / "out",
        session=session_a,
    )
    session_b = FakeEngineSession(evidence)
    payload_b = run_pack_in_process(
        inputs,
        token=TOKEN,
        output_root=tmp_path / "b" / "out",
        session=session_b,
    )
    return payload_a, payload_b, session_a, session_b


# ------------------------------------------------------------------------ tests


def test_identical_fake_evidence_yields_no_differences_between_paths(tmp_path: Path) -> None:
    payload_a, payload_b, session_a, session_b = _run_both(tmp_path)

    # Non-vacuous: both paths really materialized the one frame.
    fixture = _load_fixture()
    expected_sha = snapshot_native_outputs(
        fixture["generated_files"], tmp_path / "native-spool"
    )[0]["sha256"]
    assert payload_a["wan_mapping"]["generated_files"] == fixture["generated_files"]
    assert payload_a["wan_mapping"]["staged_outputs"][0]["sha256"] == expected_sha
    assert payload_b["wan_mapping"]["staged_outputs"][0]["sha256"] == expected_sha

    # Both paths sent the same compiled settings exactly once. timeout_seconds
    # is a timing value (the host sends its remaining deadline), not evidence.
    assert len(session_a.submitted) == len(session_b.submitted) == 1
    assert session_a.submitted[0]["settings"] == session_b.submitted[0]["settings"]
    assert session_a.submitted[0]["settings"] == compile_host_settings(fixture["inputs"])

    assert compare_evidence(payload_a, payload_b, ALLOWED_DIFF_KEYS) == []


def test_changed_result_field_is_reported(tmp_path: Path) -> None:
    payload_a, payload_b, _, _ = _run_both(tmp_path)
    assert compare_evidence(payload_a, payload_b, ALLOWED_DIFF_KEYS) == []

    changed = copy.deepcopy(payload_b)
    changed["wan_native"]["native_job_id"] = "wan-native-job-other"
    changed["wan_mapping"]["native_job_id"] = "wan-native-job-other"

    assert compare_evidence(payload_a, changed, ALLOWED_DIFF_KEYS) == [
        "wan_mapping.native_job_id",
        "wan_native.native_job_id",
    ]


def test_allowed_diff_keys_are_exactly_the_documented_set(tmp_path: Path) -> None:
    assert ALLOWED_DIFF_KEYS == frozenset({
        "wan_native.token_id",
        "wan_native.invocation_id",
        "wan_native.birth",
        "wan_native.process_id",
        "wan_mapping.manifest.created",
    })

    payload_a, _, _, _ = _run_both(tmp_path)

    # Perturb every leaf of the real evidence, one at a time. The leaves that may
    # differ without a parity failure are exactly: the allowed run-identity and
    # timing keys; the per-run root strings (replaced by "<root>"); and the stat
    # entries other than size (dev, inode, mtime_ns, ctime_ns). Every other leaf
    # must be reported. Each allowed key must occur in the evidence.
    leaves = list(_walk_leaves(payload_a))
    normalized = {_normalize(_format_location(location)) for location in leaves}
    assert ALLOWED_DIFF_KEYS <= normalized, sorted(ALLOWED_DIFF_KEYS - normalized)

    tolerated_roots = {"wan_native.source_root", "wan_mapping.output_root"}
    for location in leaves:
        path = _format_location(location)
        normal = _normalize(path)
        mutated = copy.deepcopy(payload_a)
        leaf = mutated
        for key in location[:-1]:
            leaf = leaf[key]
        _set_location(mutated, location, _perturb(leaf[location[-1]]))
        diffs = compare_evidence(payload_a, mutated, ALLOWED_DIFF_KEYS)
        if normal in ALLOWED_DIFF_KEYS:
            assert diffs == [], (path, diffs)
        elif normal in tolerated_roots:
            # Moving a root string makes its descendants fall outside it, so the
            # relative structure changes for those descendants only.
            assert all(item == "wan_mapping.manifest.spool" or item.endswith(".path") for item in diffs), (path, diffs)
        elif normal.endswith(".stat") or normal.endswith(".stat[]"):
            # Perturbing the whole stat list changes st_dev (index 0) only: ignored.
            assert diffs == [], (path, diffs)
        else:
            assert path in diffs or any(item.startswith(path) for item in diffs), (path, diffs)


def test_size_of_a_scratch_file_is_compared_exactly(tmp_path: Path) -> None:
    payload_a, _, _, _ = _run_both(tmp_path)
    changed = copy.deepcopy(payload_a)
    stat = changed["wan_mapping"]["staged_outputs"][0]["stat"]
    stat[2] = stat[2] + 1
    diffs = compare_evidence(payload_a, changed, ALLOWED_DIFF_KEYS)
    assert diffs == ["wan_mapping.staged_outputs[0].stat"]


def test_inode_and_mtime_of_a_scratch_file_are_ignored(tmp_path: Path) -> None:
    payload_a, _, _, _ = _run_both(tmp_path)
    changed = copy.deepcopy(payload_a)
    stat = changed["wan_mapping"]["staged_outputs"][0]["stat"]
    stat[0], stat[1], stat[3], stat[4] = stat[0] + 9, stat[1] + 9, stat[3] + 9, stat[4] + 9
    assert compare_evidence(payload_a, changed, ALLOWED_DIFF_KEYS) == []


def test_run_root_move_with_same_relative_structure_is_not_a_diff(tmp_path: Path) -> None:
    payload_a, _, _, _ = _run_both(tmp_path)
    moved = copy.deepcopy(payload_a)
    moved["wan_native"]["source_root"] = moved["wan_native"]["source_root"] + "-moved"
    for item in moved["wan_native"]["output_snapshots"]:
        item["path"] = item["path"].replace(payload_a["wan_native"]["source_root"], moved["wan_native"]["source_root"], 1)
    assert compare_evidence(payload_a, moved, ALLOWED_DIFF_KEYS) == []


def test_extra_missing_or_renamed_output_file_fails_parity(tmp_path: Path) -> None:
    payload_a, _, _, _ = _run_both(tmp_path)
    assert payload_a["wan_mapping"]["staged_outputs"], "fixture must stage at least one output"

    extra = copy.deepcopy(payload_a)
    extra["wan_mapping"]["staged_outputs"].append(copy.deepcopy(extra["wan_mapping"]["staged_outputs"][0]))
    assert compare_evidence(payload_a, extra, ALLOWED_DIFF_KEYS) != []

    missing = copy.deepcopy(payload_a)
    missing["wan_mapping"]["staged_outputs"] = []
    assert compare_evidence(payload_a, missing, ALLOWED_DIFF_KEYS) != []

    renamed = copy.deepcopy(payload_a)
    item = renamed["wan_mapping"]["staged_outputs"][0]
    item["path"] = item["path"] + ".renamed"
    diffs = compare_evidence(payload_a, renamed, ALLOWED_DIFF_KEYS)
    assert diffs == ["wan_mapping.staged_outputs[0].path"]
