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

# Documented allowed-diff keys. Paths are dotted; every list index is written as
# "[]". The spec (docs/packs/M2-11a-SESSION-CHANNEL.md, "Parity contract")
# names only the categories: run ids, timing, scratch paths. These are the
# concrete evidence keys in those categories, taken from
# generic_host._ManagedWanChildAdapter.invoke, driver.materialize_host_result
# and driver.snapshot_native_outputs.
RUN_ID_KEYS = frozenset({
    "wan_native.token_id",
    "wan_native.invocation_id",
    "wan_native.birth",
    "wan_native.process_id",
})
TIMING_KEYS = frozenset({
    "wan_mapping.manifest.created",
})
SCRATCH_PATH_KEYS = frozenset({
    "wan_native.source_root",
    "wan_native.output_snapshots[].path",
    # stat is (st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns) of a scratch
    # file. Size stays exact through manifest.outputs[].bytes.
    "wan_native.output_snapshots[].stat",
    "wan_mapping.output_root",
    "wan_mapping.manifest.spool",
    "wan_mapping.staged_outputs[].path",
    "wan_mapping.staged_outputs[].stat",
})
ALLOWED_DIFF_KEYS = RUN_ID_KEYS | TIMING_KEYS | SCRATCH_PATH_KEYS

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


def _same_scalar_list(a: list[Any], b: list[Any]) -> bool:
    return [(type(item), item) for item in a] == [(type(item), item) for item in b]


def _diff(a: Any, b: Any, path: str, allowed: frozenset[str], out: list[str]) -> None:
    if path and _normalize(path) in allowed:
        return
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        for key in sorted(set(a) | set(b), key=str):
            child = _join(path, key)
            if key not in a or key not in b:
                if _normalize(child) not in allowed:
                    out.append(child)
                continue
            _diff(a[key], b[key], child, allowed, out)
        return
    if isinstance(a, list) and isinstance(b, list) and not (_is_scalar_list(a) and _is_scalar_list(b)):
        if len(a) != len(b):
            out.append(path)
            return
        for index, (left, right) in enumerate(zip(a, b)):
            _diff(left, right, _join(path, index), allowed, out)
        return
    if isinstance(a, list) and isinstance(b, list):
        if not _same_scalar_list(a, b):
            out.append(path)
        return
    if type(a) is not type(b) or a != b:
        out.append(path)


def compare_evidence(a: Any, b: Any, allowed_diff_keys: frozenset[str] | set[str]) -> list[str]:
    """Return the sorted dotted paths where ``a`` and ``b`` differ.

    A path is skipped when its list-index-normalized form is in
    ``allowed_diff_keys``. Scalar lists are compared as one value, so a
    file observation such as ``stat`` is matched by its key. Types are
    compared too, so ``1`` and ``True`` differ.
    """
    out: list[str] = []
    _diff(a, b, "", frozenset(allowed_diff_keys), out)
    return sorted(out)


# ------------------------------------------------------------- fake engine edge


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
        "wan_native.source_root",
        "wan_native.output_snapshots[].path",
        "wan_native.output_snapshots[].stat",
        "wan_mapping.output_root",
        "wan_mapping.manifest.spool",
        "wan_mapping.staged_outputs[].path",
        "wan_mapping.staged_outputs[].stat",
    })

    payload_a, _, _, _ = _run_both(tmp_path)

    # Perturb every leaf of the real evidence, one at a time. A leaf is
    # ignored only when its normalized path is an allowed key. Each allowed key
    # must occur in the evidence, so no entry is dead.
    leaves = list(_walk_leaves(payload_a))
    normalized = {_normalize(_format_location(location)) for location in leaves}
    assert ALLOWED_DIFF_KEYS <= normalized, sorted(ALLOWED_DIFF_KEYS - normalized)

    for location in leaves:
        path = _format_location(location)
        mutated = copy.deepcopy(payload_a)
        leaf = mutated
        for key in location[:-1]:
            leaf = leaf[key]
        _set_location(mutated, location, _perturb(leaf[location[-1]]))
        diffs = compare_evidence(payload_a, mutated, ALLOWED_DIFF_KEYS)
        if _normalize(path) in ALLOWED_DIFF_KEYS:
            assert diffs == [], (path, diffs)
        else:
            # A field can be stored twice (e.g. disclosed_engine also sits in
            # the manifest), so the mutated path must be reported, and every
            # reported path must be a non-allowed one.
            assert path in diffs, (path, diffs)
            assert all(_normalize(item) not in ALLOWED_DIFF_KEYS for item in diffs), (path, diffs)
