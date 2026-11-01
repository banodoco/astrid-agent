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
import io
import json
import os
import platform as host_platform
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import venv
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from runtime_protocol import platform as runtime_platform

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
PROBE_SOURCE = r"""
import argparse, hashlib, json, os, signal, time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--ready", type=Path, required=True)
parser.add_argument("--token", required=True)
parser.add_argument("--fixture-sum", required=True)
parser.add_argument("--agent-root", type=Path, required=True)
parser.add_argument("--runtime-root", type=Path)
parser.add_argument("--agent-sha256", required=True)
parser.add_argument("--runtime-sha256")
parser.add_argument("--fail-when", type=Path)
args = parser.parse_args()
stopped = False
def stop(_signum, _frame):
    global stopped
    stopped = True
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

import astrid
agent_path = Path(astrid.__file__).resolve(strict=True)
if not agent_path.is_relative_to(args.agent_root.resolve(strict=True)):
    raise SystemExit("Agent imported outside the exact archived source")
agent_sha256 = hashlib.sha256(agent_path.read_bytes()).hexdigest()
if agent_sha256 != args.agent_sha256:
    raise SystemExit("Agent source digest differs from the archived Git object")
value = {
    "pid": os.getpid(), "token": args.token,
    "fixture_sums_sha256": args.fixture_sum,
    "agent_module": str(agent_path), "agent_module_sha256": agent_sha256,
}
if args.runtime_root is not None:
    import astrid_runtime
    runtime_path = Path(astrid_runtime.__file__).resolve(strict=True)
    if not runtime_path.is_relative_to(args.runtime_root.resolve(strict=True)):
        raise SystemExit("Runtime imported outside the exact archived source")
    runtime_sha256 = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
    if runtime_sha256 != args.runtime_sha256:
        raise SystemExit("Runtime source digest differs from the archived Git object")
    value.update(runtime_module=str(runtime_path), runtime_module_sha256=runtime_sha256)
args.ready.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
while not stopped:
    if args.fail_when is not None and args.fail_when.exists():
        raise SystemExit(73)
    time.sleep(0.05)
""".lstrip()


@dataclass(frozen=True)
class ArchivedSources:
    agent: Path
    runtime: Path | None
    refs: dict[str, str]
    agent_module_sha256: str
    runtime_module_sha256: str | None
    provenance: dict[str, dict[str, str]]


class InjectedInterruption(BaseException):
    """Simulate a process boundary after a durable journal write."""


class HistoricalSourcePrerequisiteError(ValueError):
    """The immutable release-0 source object was not provisioned."""

    def __init__(self, *, component: str, required_ref: str, searched: Path) -> None:
        self.prerequisite = {
            "component": component,
            "required_ref": required_ref,
            "object_type": "commit",
            "searched_repository": str(searched),
            "provisioning": "explicit immutable Git object or checked-in release-0 fixture",
        }
        super().__init__(
            f"Historical release-0 prerequisite missing: required {component} Git object "
            f"{required_ref}; provision that immutable commit or use the checked-in "
            "release-0 fixture. Candidate/live-URL/foreground fallbacks are forbidden."
        )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _git_output(repo: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"exact source object is unavailable in {repo}: {' '.join(args)}") from exc
    return completed.stdout.strip()


def _git_discovery_root(start: Path) -> Path:
    probe = start.expanduser().resolve()
    if probe.is_file():
        probe = probe.parent
    try:
        return Path(_git_output(probe, "rev-parse", "--show-toplevel")).resolve(strict=True)
    except ValueError:
        return probe


def _git_repo(start: Path, required_ref: str, *, component: str) -> Path:
    if failure.SHA.fullmatch(required_ref) is None:
        raise ValueError(f"historical {component} ref must be a full commit SHA")
    env_name = (
        "ASTRID_RELEASE0_AGENT_REPO"
        if component == "Agent"
        else "ASTRID_RELEASE0_RUNTIME_REPO"
    )
    configured = os.environ.get(env_name)
    searched = (
        Path(configured).expanduser().resolve()
        if configured
        else _git_discovery_root(start)
    )
    try:
        resolved = _git_output(searched, "rev-parse", f"{required_ref}^{{commit}}")
        object_type = _git_output(searched, "cat-file", "-t", required_ref)
    except ValueError:
        raise HistoricalSourcePrerequisiteError(
            component=component, required_ref=required_ref, searched=searched
        ) from None
    if resolved != required_ref or object_type != "commit":
        raise HistoricalSourcePrerequisiteError(
            component=component, required_ref=required_ref, searched=searched
        )
    return searched.resolve(strict=True)


def _extract_git_paths(repo: Path, ref: str, destination: Path, paths: tuple[str, ...]) -> None:
    if _git_output(repo, "rev-parse", f"{ref}^{{commit}}") != ref:
        raise ValueError(f"source repository does not resolve exact commit {ref}")
    try:
        archive = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", ref, *paths],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"could not archive exact source {ref}") from exc
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        members = stream.getmembers()
        if any(
            member.name.startswith("/") or ".." in Path(member.name).parts for member in members
        ):
            raise ValueError("Git archive contains an unsafe path")
        stream.extractall(destination, members=members)


def _archive_sources(
    destination: Path,
    agent_repo: Path,
    runtime_repo: Path,
    refs: Mapping[str, str],
    *,
    include_runtime: bool,
) -> ArchivedSources:
    agent = destination / "agent"
    _extract_git_paths(
        agent_repo,
        refs["agent"],
        agent,
        (
            "astrid/__init__.py",
            "astrid/version.py",
            "astrid_runtime_client",
            "pyproject.toml",
        ),
    )
    runtime = None
    runtime_digest = None
    if include_runtime:
        runtime = destination / "runtime"
        _extract_git_paths(
            runtime_repo,
            refs["runtime"],
            runtime,
            ("astrid_runtime", "runtime_protocol"),
        )
        runtime_digest = sha256_file(runtime / "astrid_runtime" / "__init__.py")
    return ArchivedSources(
        agent=agent,
        runtime=runtime,
        refs=dict(refs),
        agent_module_sha256=sha256_file(agent / "astrid" / "__init__.py"),
        runtime_module_sha256=runtime_digest,
        provenance={
            "agent": {"repo": str(agent_repo), "ref": refs["agent"], "object_type": "commit"},
            "runtime": {
                "repo": str(runtime_repo), "ref": refs["runtime"], "object_type": "commit"
            },
        },
    )


def _source_inputs(
    destination: Path,
    candidate: Mapping[str, str],
    *,
    agent_repo: Path | None = None,
    runtime_repo: Path | None = None,
) -> tuple[ArchivedSources, ArchivedSources]:
    agent_repo = _git_repo(
        agent_repo or Path(__file__).resolve(), failure.RELEASE0_REFS["agent"], component="Agent"
    )
    if runtime_repo is None:
        import runtime_protocol

        runtime_repo = _git_repo(
            Path(runtime_protocol.__file__).resolve(),
            failure.RELEASE0_REFS["runtime"],
            component="Runtime",
        )
    else:
        runtime_repo = _git_repo(
            runtime_repo, failure.RELEASE0_REFS["runtime"], component="Runtime"
        )
    old = _archive_sources(
        destination / "release0",
        agent_repo,
        runtime_repo,
        failure.RELEASE0_REFS,
        include_runtime=True,
    )
    candidate_sources = _archive_sources(
        destination / "candidate",
        agent_repo,
        runtime_repo,
        {"agent": candidate["agent"], "runtime": candidate["runtime"]},
        include_runtime=False,
    )
    return old, candidate_sources


def process_observation(pid: int) -> dict:
    """Return a live, birth-bound process identity or fail closed."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("process PID is missing")
    try:
        identity = runtime_platform.process_identity(pid)
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
    overall = sha256_bytes(
        canonical({name: row["before_sha256"] for name, row in entities.items()})
    )
    return {
        "before_sha256": overall,
        "after_sha256": overall,
        "identical": True,
        "entities": entities,
    }


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
    after_overall = sha256_bytes(
        canonical({name: row["after_sha256"] for name, row in entities.items()})
    )
    return {
        "before_sha256": before_overall,
        "after_sha256": after_overall,
        "identical": before_overall == after_overall
        and all(row["before_sha256"] == row["after_sha256"] for row in entities.values()),
        "entities": entities,
    }


def _bytes_snapshot(paths: Mapping[str, Path]) -> dict[str, object]:
    snapshot = {}
    for name, path in paths.items():
        snapshot[name] = None if not path.exists() else sha256_file(path)
    return snapshot


class LiveExecutable:
    """Launch exact archived Runtime or the candidate validation boundary."""

    def __init__(
        self,
        version: Version,
        fixture_sum: str,
        root: Path,
        data: Path,
        sources: ArchivedSources,
        *,
        fail_on_request: bool = False,
    ):
        self.version = version
        self.fixture_sum = fixture_sum
        self.root = root
        self.data = data
        self.sources = sources
        self.fail_on_request = fail_on_request
        self.probe = root / f"{version.root.name}-executable-probe.py"
        self.probe.write_text(PROBE_SOURCE, encoding="utf-8", newline="\n")
        self.process: subprocess.Popen | None = None
        self.ready: Path | None = None
        self.fail_gate: Path | None = None
        self.token: str | None = None
        self.observed: dict | None = None

    def start(self) -> dict:
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("product process is already running")
        if self.sources.runtime is not None:
            return self._start_runtime()
        return self._start_candidate_probe()

    def _environment(self) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONHOME", "PYTHONPATH", "ASTRID_RUNTIME_SOURCE_MANIFEST"}
        }
        home = self.root / "release0-home"
        home.mkdir(exist_ok=True)
        environment.update(
            HOME=str(home),
            USERPROFILE=str(home),
            LOCALAPPDATA=str(self.root / "release0-local-app-data"),
            XDG_STATE_HOME=str(self.root / "release0-state"),
            ASTRID_RUNTIME_HOME=str(home),
            ASTRID_RUNTIME_DATA_ROOT=str(self.data),
        )
        return environment

    def _module_observation(self) -> dict:
        script = (
            "import hashlib,json; from pathlib import Path; import astrid,astrid_runtime; "
            "a=Path(astrid.__file__).resolve(strict=True); "
            "r=Path(astrid_runtime.__file__).resolve(strict=True); "
            "print(json.dumps({'agent_module':str(a),'agent_module_sha256':hashlib.sha256(a.read_bytes()).hexdigest(),"
            "'runtime_module':str(r),'runtime_module_sha256':hashlib.sha256(r.read_bytes()).hexdigest()}))"
        )
        try:
            observed = json.loads(
                subprocess.check_output(
                    [str(self.version.python), "-I", "-c", script],
                    text=True,
                    env=self._environment(),
                    timeout=10,
                )
            )
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "release-0 executable could not import its exact product sources"
            ) from exc
        agent_path = Path(observed.get("agent_module", ""))
        runtime_path = Path(observed.get("runtime_module", ""))
        if (
            not agent_path.is_file()
            or not agent_path.is_relative_to(self.sources.agent)
            or not runtime_path.is_file()
            or self.sources.runtime is None
            or not runtime_path.is_relative_to(self.sources.runtime)
            or observed.get("agent_module_sha256") != self.sources.agent_module_sha256
            or observed.get("runtime_module_sha256") != self.sources.runtime_module_sha256
        ):
            raise ValueError("release-0 executable imported source outside the exact Git archives")
        return observed

    def _start_runtime(self) -> dict:
        self.observed = self._module_observation()
        source_manifest = self.root / "release0-source-profile.json"
        write_json(
            source_manifest,
            {
                "profile": "astrid",
                "runtime_checkout": str(self.sources.runtime),
                "source_checkout": str(self.sources.agent),
            },
        )
        common = [
            "--profile",
            "astrid",
            "--data-root",
            str(self.data),
            "--source-manifest",
            str(source_manifest),
            "--hosted-origin",
            "off",
            "--json",
        ]
        catalog = self.data / "runtime" / "catalog.json"
        if not catalog.exists():
            realm = self.data / "runtime" / "realms" / "release0-executable"
            configured = subprocess.run(
                [
                    str(self.version.python),
                    "-I",
                    "-m",
                    "astrid_runtime.cli",
                    "workspace",
                    "create",
                    "--realm-root",
                    str(realm),
                    "--realm-id",
                    "release0-executable",
                    *common,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._environment(),
                timeout=30,
            )
            if configured.returncode != 0:
                raise RuntimeError(
                    "release-0 workspace configuration failed: "
                    + (configured.stdout or configured.stderr).strip()
                )
        self.process = subprocess.Popen(
            [
                str(self.version.python),
                "-I",
                "-m",
                "astrid_runtime.cli",
                "run",
                *common,
            ],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self._environment(),
            start_new_session=True,
        )
        discovery_path = self.data / "runtime" / "discovery.json"
        deadline = time.monotonic() + 10
        discovery = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("release-0 Runtime executable exited before discovery")
            try:
                value = json.loads(discovery_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                value = None
            if isinstance(value, dict) and value.get("pid") == self.process.pid:
                discovery = value
                break
            time.sleep(0.02)
        if discovery is None:
            raise RuntimeError("release-0 Runtime executable did not publish discovery")
        identity = process_observation(self.process.pid)
        if (
            discovery.get("process_birth_id") != identity.get("birth_id")
            or discovery.get("active_realm") != "release0-executable"
            or not discovery.get("endpoint")
            or not discovery.get("runtime_instance_id")
        ):
            raise ValueError("release-0 Runtime discovery does not match its live process")
        self.observed.update(
            fixture_sums_sha256=self.fixture_sum,
            runtime_discovery=discovery,
        )
        return identity

    def _start_candidate_probe(self) -> dict:
        self.token = uuid.uuid4().hex
        self.ready = self.root / f"{self.version.root.name}-ready-{self.token}.json"
        self.fail_gate = self.root / f"{self.version.root.name}-fail-{self.token}"
        command = [
            str(self.version.python),
            "-I",
            str(self.probe),
            "--ready",
            str(self.ready),
            "--token",
            self.token,
            "--fixture-sum",
            self.fixture_sum,
            "--agent-root",
            str(self.sources.agent),
            "--agent-sha256",
            self.sources.agent_module_sha256,
        ]
        if self.sources.runtime is not None:
            command.extend(
                [
                    "--runtime-root",
                    str(self.sources.runtime),
                    "--runtime-sha256",
                    str(self.sources.runtime_module_sha256),
                ]
            )
        if self.fail_on_request:
            command.extend(["--fail-when", str(self.fail_gate)])
        self.process = subprocess.Popen(
            command,
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while not self.ready.exists():
            if self.process.poll() is not None:
                raise RuntimeError("product executable exited before readiness")
            if time.monotonic() >= deadline:
                raise RuntimeError("product executable did not publish readiness")
            time.sleep(0.01)
        observed = json.loads(self.ready.read_text(encoding="utf-8"))
        if observed.get("pid") != self.process.pid or observed.get("token") != self.token:
            raise ValueError("product readiness observation is synthetic or mismatched")
        identity = process_observation(self.process.pid)
        if observed.get("fixture_sums_sha256") != self.fixture_sum:
            raise ValueError("product process is not bound to the authenticated fixture")
        if (
            observed.get("agent_module_sha256") != self.sources.agent_module_sha256
            or observed.get("runtime_module_sha256") != self.sources.runtime_module_sha256
        ):
            raise ValueError("product process did not load the exact archived source modules")
        self.observed = observed
        return identity

    def fail(self) -> dict:
        if not self.fail_on_request or self.process is None or self.fail_gate is None:
            raise RuntimeError("candidate failure boundary is not armed")
        identity = process_observation(self.process.pid)
        self.fail_gate.write_text("fail candidate readiness\n", encoding="utf-8")
        returncode = self.process.wait(timeout=5)
        if returncode != 73:
            raise RuntimeError(f"candidate failure boundary returned {returncode}, expected 73")
        if process_observation_optional(self.process.pid) is not None:
            raise RuntimeError("failed candidate process remained after its nonzero exit")
        return {"process_identity": identity, "exit_code": returncode, "observation": self.observed}

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
            raise RuntimeError("product process remained after cleanup")


def process_observation_optional(pid: int) -> dict | None:
    try:
        return process_observation(pid)
    except (OSError, ValueError):
        return None


class JourneyBackend:
    """Minimal backend surface for the real core transaction boundary."""

    def __init__(self, root: Path, data: Path, live: LiveExecutable):
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


def _venv_python(venv_root: Path) -> Path:
    return venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _version(
    root: Path,
    name: str,
    release: int,
    fixture_sum: str,
    manifest_sha: str,
    sources: ArchivedSources,
) -> Version:
    version_root = root / "versions" / name
    python_root = version_root / "py"
    version_root.mkdir(parents=True, exist_ok=False)
    venv.EnvBuilder(with_pip=False, symlinks=False).create(python_root)
    python = _venv_python(python_root)
    if python.is_symlink() or not python.is_file():
        raise ValueError("selected product executable must be a copied regular file")
    site_packages = Path(
        subprocess.check_output(
            [str(python), "-I", "-c", "import site; print(site.getsitepackages()[0])"],
            text=True,
        ).strip()
    )
    source_paths = [sources.agent]
    if sources.runtime is not None:
        source_paths.append(sources.runtime)
    (site_packages / "astrid-exact-sources.pth").write_text(
        "".join(f"{path}\n" for path in source_paths), encoding="utf-8", newline="\n"
    )
    profile = version_root / "profile.json"
    write_json(
        profile,
        {
            "profile": "astrid",
            "release": name,
            "fixture_sums_sha256": fixture_sum,
            "source_refs": sources.refs,
        },
    )
    version = Version(version_root, python, profile, {}, release=release)
    write_json(version_root / "version.json", version.document())
    write_json(
        version_root / "manifest.json",
        {
            "release": release,
            "agent": {"sha": sources.refs["agent"]},
            "runtime": {"sha": sources.refs["runtime"]},
            "manifest_sha256": manifest_sha,
        },
    )
    return version


def _setup(
    root: Path,
    fixture: Path,
    fixture_receipt: dict,
    old_sources: ArchivedSources,
    candidate_sources: ArchivedSources,
) -> tuple[Path, Path, Version, Version, JourneyBackend, dict]:
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
    old = _version(
        root,
        "release0",
        0,
        fixture_receipt["fixture_sums_sha256"],
        manifest_sha,
        old_sources,
    )
    candidate = _version(
        root,
        "candidate",
        1,
        fixture_receipt["fixture_sums_sha256"],
        "f" * 64,
        candidate_sources,
    )
    shutil.copyfile(fixture_manifest, old.root / "manifest.json")
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data), "chat_settings": {}})
    selection = root.parent / f"{root.name}-selection.json"
    write_json(selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data)})
    switch(root, old)
    live = LiveExecutable(old, fixture_receipt["fixture_sums_sha256"], root, data, old_sources)
    backend = JourneyBackend(root, data, live)
    before = semantic_inventory(data)
    return data, selection, old, candidate, backend, before


def _identity(
    version: Version, selected: str, live: LiveExecutable, sources: Mapping[str, str]
) -> dict:
    if live.process is None or live.process.poll() is not None:
        raise ValueError("missing live process observation")
    observed = process_observation(live.process.pid)
    return {
        "selected_release": selected,
        "executable_path": str(version.python),
        "executable_sha256": sha256_file(version.python),
        "version_manifest_sha256": sha256_file(version.root / "manifest.json"),
        "process_identity_sha256": sha256_bytes(canonical(observed)),
        "source_refs": dict(sources),
        "loaded_sources": dict(live.observed or {}),
        "running": True,
        "health": "pass",
    }


def _finish_semantic(data: Path, before: dict) -> dict:
    return _semantic_after(data, before)


def run_interruption(
    root: Path,
    fixture: Path,
    fixture_receipt: dict,
    old_sources: ArchivedSources,
    candidate_sources: ArchivedSources,
) -> dict:
    data, _selection, old, candidate, backend, before = _setup(
        root, fixture, fixture_receipt, old_sources, candidate_sources
    )
    live_identity = backend.start_release0()
    operation_id = None
    injected = False
    result = None

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
                old,
                backend.workspace(old, data),
                prepare=prepare,
                stop=backend.stop,
                switch_candidate=lambda value: switch(root, value),
                switch_old=lambda value, _candidate: switch(root, value),
                activate=activate,
                activate_old=lambda value, _data, _realm: backend.start_release0(),
                kind="app",
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
            root,
            data,
            backend,
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
        result = {
            "status": "pass",
            "operation_id": operation_id,
            "phase": "switched",
            "injected": injected,
            "first_request": {"interrupted": True, "journal_state": "switched"},
            "recovery_request": {"explicit": True, "operation_id": operation_id},
            "terminal_state": json.loads((root / "update-transaction.json").read_text())["step"],
            "duplicate_operation": False,
            "recovery_verified": pending["operation_id"] == operation_id,
            "terminal_identity": terminal_identity,
            "semantic_data": _finish_semantic(data, before),
            "update_mutation": _bytes_snapshot(
                {"journal": root / "update-transaction.json", "selection": root / "current"}
            ),
            "live_identity_before": live_identity,
        }
        return result
    finally:
        owned_pid = backend.live.process.pid if backend.live.process else None
        backend.live.stop()
        if result is not None:
            result["cleanup"] = {
                "owned_processes_after": 0,
                "observed_pid": owned_pid,
                "process_absent": process_observation_optional(owned_pid or -1) is None,
            }


def run_rollback(
    root: Path,
    fixture: Path,
    fixture_receipt: dict,
    old_sources: ArchivedSources,
    candidate_sources: ArchivedSources,
) -> dict:
    data, _selection, old, candidate, backend, before = _setup(
        root, fixture, fixture_receipt, old_sources, candidate_sources
    )
    backend.start_release0()
    before_identity = _identity(old, "release0", backend.live, failure.RELEASE0_REFS)
    failure_injected = False
    candidate_boundary = LiveExecutable(
        candidate,
        fixture_receipt["fixture_sums_sha256"],
        root,
        data,
        candidate_sources,
        fail_on_request=True,
    )
    candidate_failure = None
    result = None

    def activate(_candidate: Version, _data: Path, _realm: dict) -> None:
        nonlocal candidate_failure, failure_injected
        failure_injected = True
        candidate_boundary.start()
        candidate_failure = candidate_boundary.fail()
        raise RuntimeError("deterministic candidate-readiness failure")

    try:
        installer = Installer(root, data, backend, say=lambda _line: None)
        try:
            installer._run_update_transaction(
                old,
                backend.workspace(old, data),
                prepare=lambda: candidate,
                stop=backend.stop,
                switch_candidate=lambda value: switch(root, value),
                switch_old=lambda value, _candidate: switch(root, value),
                activate=activate,
                activate_old=lambda value, _data, _realm: backend.start_release0(),
                kind="app",
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
        journal_state = json.loads((root / "update-transaction.json").read_text(encoding="utf-8"))[
            "step"
        ]
        result = {
            "status": "pass",
            "failure_injected": failure_injected,
            "failure_phase": "candidate-readiness-process",
            "candidate_started": False,
            "candidate_activated": False,
            "rollback_selected": True,
            "journal_terminal_state": journal_state,
            "real_release0_started": True,
            "real_release0_identity_match": before_identity["executable_sha256"]
            == after_identity["executable_sha256"],
            "data_intact": _finish_semantic(data, before)["identical"],
            "candidate_process_boundary": {
                "started": candidate_failure is not None,
                "exit_code": (candidate_failure or {}).get("exit_code"),
                "process_identity_sha256": sha256_bytes(
                    canonical((candidate_failure or {}).get("process_identity", {}))
                ),
                "loaded_sources": (candidate_failure or {}).get("observation"),
            },
            "executable_identity": {
                "before": before_identity,
                "after": after_identity,
                "match": True,
            },
            "semantic_data": _finish_semantic(data, before),
        }
        return result
    finally:
        candidate_pid = candidate_boundary.process.pid if candidate_boundary.process else None
        release0_pid = backend.live.process.pid if backend.live.process else None
        candidate_boundary.stop()
        backend.live.stop()
        if result is not None:
            result["cleanup"] = {
                "owned_processes_after": 0,
                "candidate_pid": candidate_pid,
                "release0_pid": release0_pid,
                "candidate_absent": process_observation_optional(candidate_pid or -1) is None,
                "release0_absent": process_observation_optional(release0_pid or -1) is None,
            }


def run_uninstall(
    root: Path,
    fixture: Path,
    fixture_receipt: dict,
    old_sources: ArchivedSources,
    candidate_sources: ArchivedSources,
) -> dict:
    data, selection, old, _candidate, backend, before = _setup(
        root, fixture, fixture_receipt, old_sources, candidate_sources
    )
    backend.start_release0()
    before_identity = _identity(old, "release0", backend.live, failure.RELEASE0_REFS)
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
    result = None
    try:
        Installer(root, data, real, say=lambda _line: None).run("uninstall", {}, delete_data=False)
        processes_after = process_observation_optional(
            backend.live.process.pid if backend.live.process else -1
        )
        if processes_after is not None:
            raise RuntimeError("owned Runtime process remains after uninstall")
        result = {
            "status": "pass",
            "requested": True,
            "keep_data": True,
            "exit_code": 0,
            "program_root_removed": not root.exists(),
            "selection_removed": json.loads(selection.read_text()).get("uninstalled") is True,
            "native_service_removed": False,
            "disposable_service_selection_removed": True,
            "executable_identity_before": before_identity,
            "data_root_exists": data.is_dir(),
            "workspace_and_cas_present": (data / "project.json").is_file()
            and (data / "cas/blob.bin").is_file(),
            "runtime_processes_after": 0,
            "unrelated_sentinel_survived": sentinel.read_text()
            == "must survive keep-data uninstall\n",
            "semantic_data": _finish_semantic(data, before),
        }
        return result
    finally:
        owned_pid = backend.live.process.pid if backend.live.process else None
        backend.live.stop()
        if result is not None:
            result["cleanup"] = {
                "owned_processes_after": 0,
                "observed_pid": owned_pid,
                "process_absent": process_observation_optional(owned_pid or -1) is None,
            }


def run_stop_case(
    root: Path,
    fixture: Path,
    fixture_receipt: dict,
    outcome: str,
    old_sources: ArchivedSources,
    candidate_sources: ArchivedSources,
) -> dict:
    data, selection, old, _candidate, backend, before = _setup(
        root, fixture, fixture_receipt, old_sources, candidate_sources
    )
    backend.start_release0()
    real = Backend(root, root / "installer.log", Path(sys.executable))
    real.service = SafeService(selection, root / "home")
    real.managed_by = None
    real.uv = Path(sys.executable)
    journal = root / "update-transaction.json"
    write_json(
        journal,
        {
            "format": "astrid-install-update/v1",
            "step": "complete",
            "candidate": str(root / "candidate.json"),
        },
    )
    candidate_bytes = (root / "candidate.json").write_bytes(b"candidate bytes\n")
    del candidate_bytes
    paths = {
        "candidate": root / "candidate.json",
        "selection": selection,
        "journal": journal,
        "current": root / "current",
    }
    before_bytes = _bytes_snapshot(paths)
    import importlib

    bootstrap = importlib.import_module("astrid_runtime.bootstrap")
    missing = object()
    original = getattr(bootstrap, "preflight_stop", missing)
    details = {"safe": False} if outcome == "refused" else {"safe": None}
    result = None

    def injected_preflight(*_args, **_kwargs):
        raise RuntimeOperationError(f"controlled {outcome} stop", details=details)

    try:
        bootstrap.preflight_stop = injected_preflight
        try:
            real.preflight_stop(data)
        except RuntimeOperationError as exc:
            observed_outcome = (
                "refused"
                if isinstance(exc.details, dict) and exc.details.get("safe") is False
                else "unknown"
            )
            if observed_outcome != outcome:
                raise RuntimeError(f"stop admission classified as {observed_outcome}")
        else:
            raise AssertionError("stop injection was not admitted")
        after_bytes = _bytes_snapshot(paths)
        if before_bytes != after_bytes:
            raise RuntimeError(
                "stop admission mutated candidate, selection, journal, or current bytes"
            )
        identity = _identity(old, "release0", backend.live, failure.RELEASE0_REFS)
        result = {
            "outcome": outcome,
            "reason": f"controlled {outcome} stop result",
            "update_admitted": False,
            "candidate_downloaded_or_written": False,
            "selection_changed": False,
            "journal_changed_to_update": False,
            "candidate_activated": False,
            "data_changed": False,
            "automatic_retry": False,
            "executable_identity": {"before": identity, "after": identity, "match": True},
            "semantic_data": _finish_semantic(data, before),
            "mutation_inventory": {"before": before_bytes, "after": after_bytes, "unchanged": True},
        }
        return result
    finally:
        if original is missing:
            delattr(bootstrap, "preflight_stop")
        else:
            bootstrap.preflight_stop = original
        owned_pid = backend.live.process.pid if backend.live.process else None
        backend.live.stop()
        if result is not None:
            result["cleanup"] = {
                "owned_processes_after": 0,
                "observed_pid": owned_pid,
                "process_absent": process_observation_optional(owned_pid or -1) is None,
            }


def _runner_observation(_contract: Mapping[str, str]) -> tuple[dict, str, str]:
    system = host_platform.system()
    provenance = {
        "RUNNER_OS": system,
        "ImageOS": f"local-{system.lower()}",
        "ImageVersion": "local-disposable",
    }
    # This executor deliberately owns only the disposable product boundary. A
    # shared platform adapter must add native service-manager observations and
    # its own hosted provenance before the existing receipt gate can pass.
    scope = "local-disposable; not hosted OS evidence"
    os_build = f"{provenance['RUNNER_OS']}-{host_platform.platform()}"
    return provenance, os_build, scope


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def execution_observation_errors(observation: Mapping) -> list[str]:
    """Check executor-only facts the shared receipt contract cannot infer."""
    errors = []
    scenarios = observation.get("scenarios")
    if not isinstance(scenarios, Mapping):
        return ["executor.scenarios: executed observations are missing"]

    identities = {
        "interruption": (scenarios.get("interruption_recovery") or {}).get("terminal_identity"),
        "rollback.before": (
            (scenarios.get("failed_candidate_rollback") or {}).get("executable_identity") or {}
        ).get("before"),
        "rollback.after": (
            (scenarios.get("failed_candidate_rollback") or {}).get("executable_identity") or {}
        ).get("after"),
        "uninstall": (scenarios.get("keep_data_uninstall") or {}).get("executable_identity_before"),
    }
    stop_cases = (scenarios.get("stop_safety") or {}).get("cases") or {}
    for outcome in failure.STOP_OUTCOMES:
        pair = (stop_cases.get(outcome) or {}).get("executable_identity") or {}
        identities[f"stop.{outcome}.before"] = pair.get("before")
        identities[f"stop.{outcome}.after"] = pair.get("after")
    for name, identity in identities.items():
        if not isinstance(identity, Mapping):
            errors.append(f"executor.{name}: executable identity is missing")
            continue
        loaded = identity.get("loaded_sources")
        if not isinstance(loaded, Mapping):
            errors.append(f"executor.{name}: live loaded-source observation is missing")
            continue
        for source in ("agent", "runtime"):
            if not isinstance(loaded.get(f"{source}_module"), str) or not _sha256(
                loaded.get(f"{source}_module_sha256")
            ):
                errors.append(f"executor.{name}: exact {source} module observation is missing")
        discovery = loaded.get("runtime_discovery")
        if (
            not isinstance(discovery, Mapping)
            or type(discovery.get("pid")) is not int
            or not discovery.get("process_birth_id")
            or not discovery.get("runtime_instance_id")
        ):
            errors.append(f"executor.{name}: real Runtime discovery observation is missing")

    rollback = scenarios.get("failed_candidate_rollback") or {}
    candidate_boundary = rollback.get("candidate_process_boundary") or {}
    candidate_loaded = candidate_boundary.get("loaded_sources") or {}
    if (
        candidate_boundary.get("started") is not True
        or candidate_boundary.get("exit_code") != 73
        or not _sha256(candidate_boundary.get("process_identity_sha256"))
        or not _sha256(candidate_loaded.get("agent_module_sha256"))
    ):
        errors.append("executor.rollback: observed nonzero candidate process boundary is missing")

    for name in failure.SCENARIOS:
        scenario = scenarios.get(name) or {}
        if name == "stop_safety":
            cleanup_rows = [
                (f"{name}.{outcome}", (stop_cases.get(outcome) or {}).get("cleanup") or {})
                for outcome in failure.STOP_OUTCOMES
            ]
        else:
            cleanup_rows = [(name, scenario.get("cleanup") or {})]
        for cleanup_name, cleanup in cleanup_rows:
            absent = [value for key, value in cleanup.items() if key.endswith("absent")]
            if (
                cleanup.get("owned_processes_after") != 0
                or not absent
                or not all(value is True for value in absent)
            ):
                errors.append(f"executor.{cleanup_name}: owned-process cleanup is missing")
    for outcome in failure.STOP_OUTCOMES:
        mutation = (stop_cases.get(outcome) or {}).get("mutation_inventory") or {}
        if mutation.get("unchanged") is not True or mutation.get("before") != mutation.get("after"):
            errors.append(f"executor.stop.{outcome}: no-update byte inventory changed")
    return errors


def execute_installed_fault(scenario: str, adapter: Any, evidence_root: Path) -> dict:
    """Run one installed fault via ``adapter.run_step(step, previous)``.

    The adapter must operate on the same native installation for every step and
    return the observations documented in the R-47 report. This helper never
    creates, deletes, restores, or cleans an installation. It fails immediately
    on an absent observation; its return is input to the existing journey
    evidence pipeline, not a hosted verdict. The disposable executor below is
    not an installed adapter.
    """
    sequences = {
        "interrupted-update": ("baseline", "interrupt", "recover", "verify"),
        "rollback": ("baseline", "fail_candidate", "verify"),
    }
    if scenario not in sequences or not callable(getattr(adapter, "run_step", None)):
        raise ValueError("unknown installed fault scenario or missing adapter")
    root = Path(evidence_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("evidence root is not a directory")
    observations: dict[str, dict] = {}
    evidence_seen: set[str] = set()

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    def check_evidence(row: Mapping, step: str, *, fresh: bool) -> None:
        files = row.get("evidence")
        require(isinstance(files, list) and bool(files), f"{step}: evidence missing")
        for item in files:
            require(isinstance(item, Mapping), f"{step}: invalid evidence")
            name, digest = item.get("path"), item.get("sha256")
            require(isinstance(name, str) and bool(name) and _sha256(digest),
                    f"{step}: evidence path or digest missing")
            path = Path(name)
            require(not path.is_absolute() and ".." not in path.parts,
                    f"{step}: evidence path escapes root")
            source = root / path
            try:
                resolved = source.resolve(strict=True)
                valid = (resolved.is_relative_to(root) and source.is_file()
                         and not any(part.is_symlink() for part in (source, *source.parents)
                                     if part != root and part.is_relative_to(root))
                         and sha256_file(source) == digest)
            except OSError:
                valid = False
            require(valid, f"{step}: evidence bytes missing or mismatched")
            if fresh:
                require(str(resolved) not in evidence_seen, f"{step}: duplicate evidence path")
                evidence_seen.add(str(resolved))

    for step in sequences[scenario]:
        row = adapter.run_step(step, deepcopy(observations))
        require(isinstance(row, Mapping), f"{step}: observation missing")
        row = deepcopy(dict(row))
        command = row.get("command")
        require(isinstance(command, list) and bool(command)
                and all(isinstance(part, str) and bool(part) for part in command),
                f"{step}: command missing")
        require(type(row.get("command_exit")) is int and row["command_exit"] == 0,
                f"{step}: command exit missing or failed")
        inner = row.get("inner_exit")
        require(type(inner) is int and (inner != 0 if step in {"interrupt", "fail_candidate"}
                                      else inner == 0),
                f"{step}: inner exit missing or unexpected")
        check_evidence(row, step, fresh=True)
        if step == "baseline":
            require(isinstance(row.get("installation_id"), str) and bool(row["installation_id"]),
                    "baseline: installed identity missing")
            require(row.get("direct_release0_fixture") is True
                    and row.get("native_service_healthy") is True
                    and row.get("populated_data") is True,
                    "baseline: installed Release-0, native health or populated data missing")
            for field in ("predecessor_sha256", "candidate_sha256", "data_sha256"):
                require(_sha256(row.get(field)), f"baseline: {field} missing")
            require(row["predecessor_sha256"] != row["candidate_sha256"],
                    "baseline: predecessor and candidate identities coincide")
            sentinel = row.get("sentinel")
            require(isinstance(sentinel, Mapping) and type(sentinel.get("pid")) is int
                    and sentinel["pid"] > 0 and isinstance(sentinel.get("birth"), str)
                    and bool(sentinel["birth"]) and sentinel.get("alive") is True,
                    "baseline: live birth-bound sentinel missing")
        elif step in {"interrupt", "fail_candidate"}:
            require(row.get("installation_id") == observations["baseline"]["installation_id"]
                    and row.get("candidate_sha256") == observations["baseline"]["candidate_sha256"],
                    f"{step}: installed or candidate identity changed")
            require(isinstance(row.get("operation_id"), str) and bool(row["operation_id"])
                    and row.get("fault_observed") is True,
                    f"{step}: operation or fault observation missing")
            if step == "interrupt":
                require(row.get("journal_step") == "switched",
                        "interrupt: durable switched journal missing")
            else:
                require(row.get("journal_step") == "rolled_back"
                        and row.get("candidate_started") is True
                        and row.get("terminal_operation_id") == row["operation_id"],
                        "fail_candidate: same-operation terminal rollback journal missing")
        elif step == "recover":
            require(row.get("installation_id") == observations["baseline"]["installation_id"],
                    "recover: installed identity changed")
            require(row.get("operation_id") == observations["interrupt"]["operation_id"]
                    and row.get("journal_step") in {"complete", "rolled_back"},
                    "recover: same-operation terminal journal missing")
        else:
            baseline = observations["baseline"]
            require(row.get("installation_id") == baseline["installation_id"]
                    and row.get("native_service_healthy") is True
                    and row.get("data_sha256") == baseline["data_sha256"]
                    and row.get("sentinel") == baseline["sentinel"],
                    "verify: service, data or sentinel preservation missing")
            if scenario == "rollback":
                expected = baseline["predecessor_sha256"]
            else:
                expected = baseline["candidate_sha256"] if observations["recover"]["journal_step"] == "complete" else baseline["predecessor_sha256"]
            require(row.get("selected_sha256") == expected,
                    "verify: selected installed program does not match terminal state")
        observations[step] = row
    # A later callback must not silently alter an earlier step's evidence.
    for step, row in observations.items():
        check_evidence(row, step, fresh=False)
    return {"scenario": scenario, "steps": observations}


def execute(
    slot_id: str,
    candidate: Mapping,
    fixture: Path,
    work_root: Path,
    *,
    agent_repo: Path | None = None,
    runtime_repo: Path | None = None,
) -> dict:
    if slot_id not in failure.SLOT_CONTRACTS:
        raise ValueError(f"unknown failure-mode slot: {slot_id}")
    if failure.candidate_errors(candidate):
        raise ValueError("candidate is not a fully pinned candidate")
    fixture, _manifest, fixture_receipt = verified_release0_fixture(fixture)
    fixture_receipt = {**fixture_receipt, "launcher_refs": dict(failure.RELEASE0_LAUNCHERS)}
    contract = failure.SLOT_CONTRACTS[slot_id]
    os_name, _runner_os, _image_os = failure.RUNNER_PROVENANCE[contract["runner"]]
    work_root = Path(work_root).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if not work_root.is_relative_to(temp_root):
        raise ValueError("work root must be confined to the system temporary directory")
    # Resolve both immutable historical objects before creating the disposable
    # execution root. A shallow checkout therefore fails as a prerequisite,
    # before any installer, service, or process state can be mutated.
    if runtime_repo is None:
        import runtime_protocol

        runtime_source_repo = _git_repo(
            Path(runtime_protocol.__file__).resolve(),
            failure.RELEASE0_REFS["runtime"],
            component="Runtime",
        )
    else:
        runtime_source_repo = _git_repo(
            runtime_repo,
            failure.RELEASE0_REFS["runtime"],
            component="Runtime",
        )
    agent_source_repo = _git_repo(
        agent_repo or Path(__file__).resolve(),
        failure.RELEASE0_REFS["agent"],
        component="Agent",
    )
    work_root.mkdir(parents=True, exist_ok=False)
    old_sources, candidate_sources = _source_inputs(
        work_root / "sources",
        candidate,
        agent_repo=agent_source_repo,
        runtime_repo=runtime_source_repo,
    )
    scenarios_root = work_root / "scenarios"
    scenarios_root.mkdir()
    values = {
        "interruption_recovery": run_interruption(
            scenarios_root / "interruption",
            fixture,
            fixture_receipt,
            old_sources,
            candidate_sources,
        ),
        "failed_candidate_rollback": run_rollback(
            scenarios_root / "rollback",
            fixture,
            fixture_receipt,
            old_sources,
            candidate_sources,
        ),
        "keep_data_uninstall": run_uninstall(
            scenarios_root / "uninstall",
            fixture,
            fixture_receipt,
            old_sources,
            candidate_sources,
        ),
        "stop_safety": {
            "status": "pass",
            "cases": {
                outcome: run_stop_case(
                    scenarios_root / f"stop-{outcome}",
                    fixture,
                    fixture_receipt,
                    outcome,
                    old_sources,
                    candidate_sources,
                )
                for outcome in failure.STOP_OUTCOMES
            },
        },
    }
    evidence = []
    for index, name in enumerate(failure.SCENARIOS, start=1):
        path = work_root / "evidence" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "execution_scope": "local-disposable",
            "scenario": name,
            "observation": values[name],
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        evidence.append(
            {
                "path": f"evidence/{name}.json",
                "sha256": sha256_file(path),
                "kind": "machine-observation",
                "scenario": name,
            }
        )
    provenance, os_build, execution_scope = _runner_observation(contract)
    observation = {
        "slot_id": slot_id,
        "job_id": slot_id,
        "mode": contract["mode"],
        "os": os_name,
        "runner_image": contract["runner"],
        "runner_provenance": provenance,
        "os_build": os_build,
        "shell": contract["shell"],
        "source_override_test_mode": False,
        "candidate_checkouts": {
            name: {"sha": candidate[name]} for name in ("app", "agent", "runtime", "pinokio")
        },
        "manifest_sha256": candidate["manifest_sha256"],
        "release0_fixture": fixture_receipt,
        "release0_source_provenance": old_sources.provenance,
        "evidence_files": evidence,
        "scenarios": values,
        "execution_scope": execution_scope,
    }
    executor_errors = execution_observation_errors(observation)
    if executor_errors:
        raise RuntimeError("; ".join(executor_errors))
    return observation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot-id", required=True, choices=failure.REQUIRED_SLOT_IDS)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path(__file__).parents[1] / "tests" / "fixtures" / "release0",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--agent-repo", type=Path)
    parser.add_argument("--release0-runtime-repo", type=Path)
    args = parser.parse_args(argv)
    work_root = args.work_root or (
        Path(tempfile.gettempdir()) / f"astrid-failure-modes-{uuid.uuid4().hex}"
    )
    try:
        candidate = failure.read_json(args.candidate)
        observation = execute(
            args.slot_id,
            candidate,
            args.fixture,
            work_root,
            agent_repo=args.agent_repo,
            runtime_repo=args.release0_runtime_repo,
        )
        receipt = failure.create_receipt(args.slot_id, candidate, observation)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        evidence_source = work_root / "evidence"
        if evidence_source.is_dir():
            shutil.copytree(evidence_source, args.output.parent / "evidence", dirs_exist_ok=True)
        failure.write_json(args.output, receipt)
        errors = failure.validate_receipt(receipt, candidate, args.slot_id)
    except HistoricalSourcePrerequisiteError as exc:
        # Keep the prerequisite failure machine-readable even though no
        # disposable work root was admitted. The evidence names the exact
        # object/ref a hosted checkout must provision.
        args.output.parent.mkdir(parents=True, exist_ok=True)
        prerequisite_path = args.output.parent / "historical-prerequisite.json"
        failure.write_json(
            prerequisite_path,
            {"status": "fail", "historical_release0": exc.prerequisite},
        )
        failure.write_json(
            args.output,
            {
                "schema_version": failure.SCHEMA_VERSION,
                "receipt_kind": failure.RECEIPT_KIND,
                "receipt_state": "recorded",
                "slot_id": args.slot_id,
                "status": "fail",
                "execution_scope": "local-disposable; not hosted OS evidence",
                "historical_release0": exc.prerequisite,
                "evidence_files": [],
                "verification": {"state": "prerequisite-fail", "failed_assertions": [str(exc)]},
            },
        )
        print(f"Failure-mode execution: FAIL: {exc}", file=sys.stderr)
        return 1
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
