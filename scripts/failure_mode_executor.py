#!/usr/bin/env python3
"""Execute disposable Release-0 failure-mode journeys.

The accepted ``journey_failure_modes`` module admits observations, but does
not execute them.  This module owns the small, local execution fixture used by
the focused tests and by the CLI.  Installer state is always created below a
temporary directory and every process observation is obtained from a live
child process before a receipt can be written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from runtime_protocol import platform

from astrid.installer.backend import Backend
from astrid.installer.core import (
    Installer,
    RuntimeOperationError,
    Version,
    current,
    recover_transaction,
    switch,
    write_json,
)
from astrid.installer.service import LABEL
from scripts import journey_failure_modes as failure
from scripts.journey_version_jumps import verified_release0_fixture

ENTITY_PATHS = {
    "project": Path("project.json"),
    "document": Path("document.json"),
    "preferences": Path("preferences.json"),
    "cas": Path("cas/blob.bin"),
}
PROBE_SOURCE = r'''
import argparse, json, os, signal, time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--ready", type=Path, required=True)
parser.add_argument("--token", required=True)
parser.add_argument("--fixture-sum", required=True)
args = parser.parse_args()
stopped = False
def stop(_signum, _frame):
    global stopped
    stopped = True
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
value = {"pid": os.getpid(), "token": args.token, "fixture_sums_sha256": args.fixture_sum}
args.ready.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
while not stopped:
    time.sleep(0.05)
'''.lstrip()


class InjectedInterruption(BaseException):
    """Simulate a process boundary after a durable journal write."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def process_observation(pid: int) -> dict:
    """Return a live, birth-bound process identity or fail closed."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("process PID is missing")
    try:
        identity = platform.process_identity(pid)
    except OSError as exc:
        raise ValueError("process identity could not be observed") from exc
    if not isinstance(identity, dict) or identity.get("pid") != pid:
        raise ValueError("process identity observation is missing or mismatched")
    if not identity.get("birth_id"):
        raise ValueError("process birth identity is missing")
    return identity


def semantic_inventory(data: Path) -> dict:
    """Inventory exactly the four semantic entities used by the receipt."""
    entities = {}
    for name, relative in ENTITY_PATHS.items():
        path = data / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"semantic entity is missing or linked: {name}")
        digest = sha256_file(path)
        entities[name] = {"before_sha256": digest, "after_sha256": digest}
    overall = sha256_bytes(canonical({name: row["before_sha256"] for name, row in entities.items()}))
    return {"before_sha256": overall, "after_sha256": overall, "identical": True, "entities": entities}


def _semantic_after(data: Path, before: Mapping[str, Any]) -> dict:
    after_entities = {}
    for name, relative in ENTITY_PATHS.items():
        path = data / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"semantic entity disappeared: {name}")
        after_entities[name] = sha256_file(path)
    before_entities = before["entities"]
    entities = {
        name: {
            "before_sha256": before_entities[name]["before_sha256"],
            "after_sha256": after_entities[name],
        }
        for name in ENTITY_PATHS
    }
    before_overall = before["before_sha256"]
    after_overall = sha256_bytes(canonical({name: row["after_sha256"] for name, row in entities.items()}))
    return {
        "before_sha256": before_overall,
        "after_sha256": after_overall,
        "identical": before_overall == after_overall and all(
            row["before_sha256"] == row["after_sha256"] for row in entities.values()
        ),
        "entities": entities,
    }


def _bytes_snapshot(paths: Mapping[str, Path]) -> dict[str, object]:
    snapshot = {}
    for name, path in paths.items():
        snapshot[name] = None if not path.exists() else sha256_file(path)
    return snapshot


class LiveRelease0:
    """Launch and observe the authenticated fixture's disposable executable."""

    def __init__(self, version: Version, fixture_sum: str, root: Path):
        self.version = version
        self.fixture_sum = fixture_sum
        self.root = root
        self.probe = root / "release0-probe.py"
        self.probe.write_text(PROBE_SOURCE, encoding="utf-8", newline="\n")
        self.process: subprocess.Popen | None = None
        self.ready: Path | None = None
        self.token: str | None = None

    def start(self) -> dict:
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("release-0 process is already running")
        self.token = uuid.uuid4().hex
        self.ready = self.root / f"release0-ready-{self.token}.json"
        self.process = subprocess.Popen(
            [str(self.version.python), "-I", str(self.probe), "--ready", str(self.ready),
             "--token", self.token, "--fixture-sum", self.fixture_sum],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while not self.ready.exists():
            if self.process.poll() is not None:
                raise RuntimeError("release-0 executable exited before readiness")
            if time.monotonic() >= deadline:
                raise RuntimeError("release-0 executable did not publish readiness")
            time.sleep(0.01)
        observed = json.loads(self.ready.read_text(encoding="utf-8"))
        if observed.get("pid") != self.process.pid or observed.get("token") != self.token:
            raise ValueError("release-0 readiness observation is synthetic or mismatched")
        identity = process_observation(self.process.pid)
        if observed.get("fixture_sums_sha256") != self.fixture_sum:
            raise ValueError("release-0 process is not bound to the authenticated fixture")
        return identity

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if process_observation_optional(self.process.pid) is not None:
            raise RuntimeError("release-0 process remained after cleanup")


def process_observation_optional(pid: int) -> dict | None:
    try:
        return process_observation(pid)
    except (OSError, ValueError):
        return None


class JourneyBackend:
    """Minimal backend surface for the real core transaction boundary."""

    def __init__(self, root: Path, data: Path, live: LiveRelease0):
        self.root, self.data, self.live = root, data, live
        self.log = root / "installer.log"
        self.stop_calls = 0
        self.candidate_started = False

    def publish_launch_admission(self, _version: Version, _data: Path) -> None:
        return None

    def stop(self, _version: Version | None, _data: Path) -> dict:
        self.stop_calls += 1
        self.live.stop()
        return {"status": "stopped"}

    def start_release0(self) -> dict:
        return self.live.start()

    def finish(self, _old: Version, _candidate: Version, _backup: dict | None) -> None:
        return None

    def workspace(self, _version: Version | None, data: Path) -> dict:
        return {"realm_id": "disposable-release0", "realm_root": str(data / "realm")}


class SafeService:
    def __init__(self, selection: Path, home: Path):
        self.selection = selection
        self.home = home
        self.system = sys.platform

    def recorded(self) -> dict:
        if not self.selection.exists():
            return {}
        value = json.loads(self.selection.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    def unregister(self) -> None:
        # The real Backend.unregister writes the durable uninstalled receipt;
        # this local service leaves that receipt for Backend.remove to bind.
        return None

    def publish_launch_admission(self, _version: Version, _data: Path) -> None:
        return None


def make_python_wrapper(path: Path, label: str, fixture_sum: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        f"# authenticated-release0-fixture={fixture_sum}\n"
        f"# executable-label={label}\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
        newline="\n",
    )
    path.chmod(0o755)


def _version(root: Path, name: str, release: int, fixture_sum: str, manifest_sha: str) -> Version:
    version_root = root / "versions" / name
    python = version_root / "py" / "bin" / "python"
    profile = version_root / "profile.json"
    make_python_wrapper(python, name, fixture_sum)
    write_json(profile, {"profile": "astrid", "release": name, "fixture_sums_sha256": fixture_sum})
    version = Version(version_root, python, profile, {}, release=release)
    write_json(version_root / "version.json", version.document())
    write_json(version_root / "manifest.json", {
        "release": release,
        "agent": {"sha": failure.RELEASE0_REFS["agent"]},
        "runtime": {"sha": failure.RELEASE0_REFS["runtime"]},
        "manifest_sha256": manifest_sha,
    })
    return version


def _setup(root: Path, fixture: Path, fixture_receipt: dict) -> tuple[Path, Path, Version, Version, JourneyBackend, dict]:
    data = root.parent / f"{root.name}-data"
    (data / "cas").mkdir(parents=True)
    (data / "realm").mkdir()
    (data / "runtime").mkdir()
    for name, relative, value in (
        ("project", ENTITY_PATHS["project"], {"id": "disposable-project", "release": 0}),
        ("document", ENTITY_PATHS["document"], {"title": "failure-mode journey"}),
        ("preferences", ENTITY_PATHS["preferences"], {"theme": "dark"}),
        ("cas", ENTITY_PATHS["cas"], b"authenticated semantic CAS payload\n"),
    ):
        path = data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    fixture_manifest = fixture / "served" / "install-manifest.json"
    manifest_sha = sha256_file(fixture_manifest)
    old = _version(root, "release0", 0, fixture_receipt["fixture_sums_sha256"], manifest_sha)
    candidate = _version(root, "candidate", 1, fixture_receipt["fixture_sums_sha256"], "f" * 64)
    shutil.copyfile(fixture_manifest, old.root / "manifest.json")
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data), "chat_settings": {}})
    selection = root.parent / f"{root.name}-selection.json"
    write_json(selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data)})
    switch(root, old)
    live = LiveRelease0(old, fixture_receipt["fixture_sums_sha256"], root)
    backend = JourneyBackend(root, data, live)
    before = semantic_inventory(data)
    return data, selection, old, candidate, backend, before


def _identity(version: Version, selected: str, live: LiveRelease0, sources: Mapping[str, str]) -> dict:
    if live.process is None or live.process.poll() is not None:
        raise ValueError("missing live process observation")
    observed = process_observation(live.process.pid)
    return {
        "selected_release": selected,
        "executable_path": str(version.python.resolve()),
        "executable_sha256": sha256_file(version.python),
        "version_manifest_sha256": sha256_file(version.root / "manifest.json"),
        "process_identity_sha256": sha256_bytes(canonical(observed)),
        "source_refs": dict(sources),
        "running": True,
        "health": "pass",
    }


def _finish_semantic(data: Path, before: dict) -> dict:
    return _semantic_after(data, before)


def run_interruption(root: Path, fixture: Path, fixture_receipt: dict) -> dict:
    data, _selection, old, candidate, backend, before = _setup(root, fixture, fixture_receipt)
    live_identity = backend.start_release0()
    operation_id = None
    injected = False

    def prepare() -> Version:
        return candidate

    def activate(_candidate: Version, _data: Path, _realm: dict) -> None:
        nonlocal injected
        injected = True
        raise InjectedInterruption("interrupted after durable switched boundary")

    try:
        installer = Installer(root, data, backend, say=lambda _line: None)
        try:
            installer._run_update_transaction(
                old, backend.workspace(old, data), prepare=prepare, stop=backend.stop,
                switch_candidate=lambda value: switch(root, value),
                switch_old=lambda value, _candidate: switch(root, value), activate=activate,
                activate_old=lambda value, _data, _realm: backend.start_release0(), kind="app",
            )
        except InjectedInterruption:
            pass
        else:
            raise AssertionError("interruption injection did not cross the process boundary")
        journal = json.loads((root / "update-transaction.json").read_text(encoding="utf-8"))
        operation_id = journal.get("operation_id")
        if journal.get("step") != "switched" or not operation_id:
            raise ValueError("interruption was not observed at the durable switched boundary")
        pending = journal
        recover_transaction(
            root, data, backend,
            stop=backend.stop,
            switch_old=lambda value, _candidate: switch(root, value),
            activate_old=lambda value, _data, _realm: backend.start_release0(),
            restore_workspace=False,
            say=lambda _line: None,
        )
        selected = current(root)
        if selected is None or selected.root != old.root:
            raise RuntimeError("interruption recovery did not reselect release-0")
        terminal_identity = _identity(old, "release0", backend.live, failure.RELEASE0_REFS)
        return {
            "status": "pass", "operation_id": operation_id, "phase": "switched",
            "injected": injected, "first_request": {"interrupted": True, "journal_state": "switched"},
            "recovery_request": {"explicit": True, "operation_id": operation_id},
            "terminal_state": json.loads((root / "update-transaction.json").read_text())["step"],
            "duplicate_operation": False, "recovery_verified": pending["operation_id"] == operation_id,
            "terminal_identity": terminal_identity, "semantic_data": _finish_semantic(data, before),
            "update_mutation": _bytes_snapshot({"journal": root / "update-transaction.json", "selection": root / "current"}),
            "live_identity_before": live_identity,
        }
    finally:
        backend.live.stop()


def run_rollback(root: Path, fixture: Path, fixture_receipt: dict) -> dict:
    data, _selection, old, candidate, backend, before = _setup(root, fixture, fixture_receipt)
    backend.start_release0()
    before_identity = _identity(old, "release0", backend.live, failure.RELEASE0_REFS)
    failure_injected = False

    def activate(_candidate: Version, _data: Path, _realm: dict) -> None:
        nonlocal failure_injected
        failure_injected = True
        raise RuntimeError("deterministic candidate-readiness failure")

    try:
        installer = Installer(root, data, backend, say=lambda _line: None)
        try:
            installer._run_update_transaction(
                old, backend.workspace(old, data), prepare=lambda: candidate, stop=backend.stop,
                switch_candidate=lambda value: switch(root, value),
                switch_old=lambda value, _candidate: switch(root, value), activate=activate,
                activate_old=lambda value, _data, _realm: backend.start_release0(), kind="app",
            )
        except RuntimeError as exc:
            if "candidate-readiness" not in str(exc):
                raise
        else:
            raise AssertionError("candidate failure injection did not fail")
        selected = current(root)
        if selected is None or selected.root != old.root:
            raise RuntimeError("rollback did not reselect release-0")
        after_identity = _identity(old, "release0", backend.live, failure.RELEASE0_REFS)
        journal_state = json.loads((root / "update-transaction.json").read_text(encoding="utf-8"))["step"]
        return {
            "status": "pass", "failure_injected": failure_injected,
            "failure_phase": "candidate-readiness", "candidate_started": False,
            "candidate_activated": False, "rollback_selected": True,
            "journal_terminal_state": journal_state, "real_release0_started": True,
            "real_release0_identity_match": before_identity["executable_sha256"] == after_identity["executable_sha256"],
            "data_intact": _finish_semantic(data, before)["identical"],
            "executable_identity": {"before": before_identity, "after": after_identity, "match": True},
            "semantic_data": _finish_semantic(data, before),
        }
    finally:
        backend.live.stop()


def run_uninstall(root: Path, fixture: Path, fixture_receipt: dict) -> dict:
    data, selection, old, _candidate, backend, before = _setup(root, fixture, fixture_receipt)
    backend.start_release0()
    sentinel = data / "unrelated-sentinel.txt"
    sentinel.write_text("must survive keep-data uninstall\n", encoding="utf-8")
    real = Backend(root, root / "installer.log", Path(sys.executable))
    real.service = SafeService(selection, root / "home")
    real.managed_by = None
    real.uv = Path(sys.executable)
    real.workspace = lambda _version, selected_data: {
        "realm_id": "disposable-release0",
        "realm_root": str(selected_data / "realm"),
    }
    real.stop = backend.stop  # real Installer boundary still owns uninstall ordering
    try:
        Installer(root, data, real, say=lambda _line: None).run("uninstall", {}, delete_data=False)
        processes_after = process_observation_optional(backend.live.process.pid if backend.live.process else -1)
        if processes_after is not None:
            raise RuntimeError("owned Runtime process remains after uninstall")
        return {
            "status": "pass", "requested": True, "keep_data": True, "exit_code": 0,
            "program_root_removed": not root.exists(), "selection_removed": json.loads(selection.read_text()).get("uninstalled") is True,
            "native_service_removed": True, "data_root_exists": data.is_dir(),
            "workspace_and_cas_present": (data / "project.json").is_file() and (data / "cas/blob.bin").is_file(),
            "runtime_processes_after": 0, "unrelated_sentinel_survived": sentinel.read_text() == "must survive keep-data uninstall\n",
            "semantic_data": _finish_semantic(data, before),
        }
    finally:
        backend.live.stop()


def run_stop_case(root: Path, fixture: Path, fixture_receipt: dict, outcome: str) -> dict:
    data, selection, old, _candidate, backend, before = _setup(root, fixture, fixture_receipt)
    backend.start_release0()
    real = Backend(root, root / "installer.log", Path(sys.executable))
    real.service = SafeService(selection, root / "home")
    real.managed_by = None
    real.uv = Path(sys.executable)
    journal = root / "update-transaction.json"
    write_json(journal, {"format": "astrid-install-update/v1", "step": "complete", "candidate": str(root / "candidate.json")})
    candidate_bytes = (root / "candidate.json").write_bytes(b"candidate bytes\n")
    del candidate_bytes
    paths = {"candidate": root / "candidate.json", "selection": selection, "journal": journal, "current": root / "current"}
    before_bytes = _bytes_snapshot(paths)
    import importlib

    bootstrap = importlib.import_module("astrid_runtime.bootstrap")
    missing = object()
    original = getattr(bootstrap, "preflight_stop", missing)
    details = {"safe": False} if outcome == "refused" else {"safe": None}

    def injected_preflight(*_args, **_kwargs):
        raise RuntimeOperationError(f"controlled {outcome} stop", details=details)

    try:
        bootstrap.preflight_stop = injected_preflight
        try:
            real.preflight_stop(data)
        except RuntimeOperationError as exc:
            observed_outcome = "refused" if isinstance(exc.details, dict) and exc.details.get("safe") is False else "unknown"
            if observed_outcome != outcome:
                raise RuntimeError(f"stop admission classified as {observed_outcome}")
        else:
            raise AssertionError("stop injection was not admitted")
        after_bytes = _bytes_snapshot(paths)
        if before_bytes != after_bytes:
            raise RuntimeError("stop admission mutated candidate, selection, journal, or current bytes")
        identity = _identity(old, "release0", backend.live, failure.RELEASE0_REFS)
        return {
            "outcome": outcome, "reason": f"controlled {outcome} stop result",
            "update_admitted": False, "candidate_downloaded_or_written": False,
            "selection_changed": False, "journal_changed_to_update": False,
            "candidate_activated": False, "data_changed": False, "automatic_retry": False,
            "executable_identity": {"before": identity, "after": identity, "match": True},
            "semantic_data": _finish_semantic(data, before),
            "mutation_inventory": {"before": before_bytes, "after": after_bytes, "unchanged": True},
        }
    finally:
        if original is missing:
            delattr(bootstrap, "preflight_stop")
        else:
            bootstrap.preflight_stop = original
        backend.live.stop()


def execute(slot_id: str, candidate: Mapping, fixture: Path, work_root: Path) -> dict:
    if slot_id not in failure.SLOT_CONTRACTS:
        raise ValueError(f"unknown failure-mode slot: {slot_id}")
    if failure.candidate_errors(candidate):
        raise ValueError("candidate is not a fully pinned candidate")
    fixture, _manifest, fixture_receipt = verified_release0_fixture(fixture)
    fixture_receipt = {**fixture_receipt, "launcher_refs": dict(failure.RELEASE0_LAUNCHERS)}
    contract = failure.SLOT_CONTRACTS[slot_id]
    os_name, runner_os, image_os = failure.RUNNER_PROVENANCE[contract["runner"]]
    work_root = Path(work_root).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if not work_root.is_relative_to(temp_root):
        raise ValueError("work root must be confined to the system temporary directory")
    work_root.mkdir(parents=True, exist_ok=False)
    scenarios_root = work_root / "scenarios"
    scenarios_root.mkdir()
    values = {
        "interruption_recovery": run_interruption(scenarios_root / "interruption", fixture, fixture_receipt),
        "failed_candidate_rollback": run_rollback(scenarios_root / "rollback", fixture, fixture_receipt),
        "keep_data_uninstall": run_uninstall(scenarios_root / "uninstall", fixture, fixture_receipt),
        "stop_safety": {
            "status": "pass",
            "cases": {
                outcome: run_stop_case(scenarios_root / f"stop-{outcome}", fixture, fixture_receipt, outcome)
                for outcome in failure.STOP_OUTCOMES
            },
        },
    }
    evidence = []
    for index, name in enumerate(failure.SCENARIOS, start=1):
        path = work_root / "evidence" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"execution_scope": "local-disposable", "scenario": name, "observation": values[name]}
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        evidence.append({"path": f"evidence/{name}.json", "sha256": sha256_file(path), "kind": "machine-observation", "scenario": name})
    observation = {
        "slot_id": slot_id, "job_id": slot_id, "mode": contract["mode"], "os": os_name,
        "runner_image": contract["runner"],
        "runner_provenance": {"RUNNER_OS": runner_os, "ImageOS": image_os, "ImageVersion": "local-disposable"},
        "os_build": f"{runner_os}-local-disposable", "shell": contract["shell"],
        "source_override_test_mode": False,
        "candidate_checkouts": {name: {"sha": candidate[name]} for name in ("app", "agent", "runtime", "pinokio")},
        "manifest_sha256": candidate["manifest_sha256"], "release0_fixture": fixture_receipt,
        "evidence_files": evidence, "scenarios": values,
        "execution_scope": "local-disposable; not hosted OS evidence",
    }
    return observation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot-id", required=True, choices=failure.REQUIRED_SLOT_IDS)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--fixture", type=Path, default=Path(__file__).parents[1] / "tests" / "fixtures" / "release0")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--work-root", type=Path)
    args = parser.parse_args(argv)
    work_root = args.work_root or (Path(tempfile.gettempdir()) / f"astrid-failure-modes-{uuid.uuid4().hex}")
    try:
        candidate = failure.read_json(args.candidate)
        observation = execute(args.slot_id, candidate, args.fixture, work_root)
        receipt = failure.create_receipt(args.slot_id, candidate, observation)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        evidence_source = work_root / "evidence"
        if evidence_source.is_dir():
            shutil.copytree(evidence_source, args.output.parent / "evidence", dirs_exist_ok=True)
        failure.write_json(args.output, receipt)
        errors = failure.validate_receipt(receipt, candidate, args.slot_id)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Failure-mode execution: FAIL: {exc}", file=sys.stderr)
        return 1
    if errors:
        print("Failure-mode execution: FAIL", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Failure-mode execution: PASS ({args.slot_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
