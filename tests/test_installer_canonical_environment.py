"""Fresh Runtime door selection and one environment across installer launchers."""

from __future__ import annotations

import ast
import base64
import re
import plistlib
import socket
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest
from subprocess import CompletedProcess

from astrid.installer import foreground, update_job, update_job_platform
from astrid.installer.backend import Backend
from astrid.installer.core import Version, read_json, switch, write_json
from astrid.installer.environment import canonical_environment, fresh_runtime_door_port
from astrid.installer.service import LABEL, Service, command, render


def _version(tmp_path: Path) -> tuple[Path, Path, Version, dict[str, str]]:
    root, data, home = tmp_path / "program", tmp_path / "workspace", tmp_path / "profile"
    version_root = root / "versions" / "v1"
    python = version_root / "py" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    profile = version_root / "profile.json"
    write_json(profile, {})
    version = Version(version_root, python, profile, {
        "HOME": str(home),
        "PATH": str(tmp_path / "pinned-tools") + ":/usr/bin:/bin",
        "ASTRID_RUNTIME_DOOR_PORT": "18473",
        "ASTRID_VERSION_MARKER": "v1",
    }, release=9, managed_by="pinokio")
    expected = canonical_environment(version.env, data)
    return root, data, version, expected


def _literal_environment(source: str) -> dict[str, str]:
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update" and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "environ" and node.args
                and isinstance(node.args[0], ast.Dict)):
            value = ast.literal_eval(node.args[0])
            if "ASTRID_RUNTIME_DOOR_PORT" in value:
                return value
    raise AssertionError("launcher did not embed the canonical environment")


def _systemd_environment(unit: str) -> dict[str, str]:
    values = {}
    for line in unit.splitlines():
        if line.startswith('Environment="'):
            key, value = line.removeprefix('Environment="').removesuffix('"').split("=", 1)
            values[key] = value
    return values


def _job(root: Path, data: Path, version: Version) -> dict:
    operation = "a" * 32
    return {
        "program_root": str(root), "data_root": str(data), "old_version": version.document(),
        "operation_id": operation, "job_name": "astrid-update-" + operation,
        "log_path": str(root / "logs" / "update.log"),
        "attempt": {"epoch": 1, "nonce": "b" * 32, "phase": "reserved", "submit": "not-issued"},
    }


def test_fresh_install_selects_and_persists_an_unoccupied_door_port(tmp_path: Path) -> None:
    """A busy historic default advances deterministically and is durable in Version.env."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(("127.0.0.1", 17340))
        occupied.listen()
        port = fresh_runtime_door_port()
        with pytest.raises(ValueError, match="requested Runtime door port 17340 is unavailable"):
            fresh_runtime_door_port("17340")

    assert port != "17340"
    root = tmp_path / "program" / "versions" / "fresh"
    root.mkdir(parents=True)
    version = Version(root, root / "py" / "bin" / "python", root / "profile.json", {
        "HOME": str(tmp_path / "profile"), "PATH": "/pinned/tools", "ASTRID_RUNTIME_DOOR_PORT": port,
    })
    write_json(root / "version.json", version.document())
    assert Version.load(read_json(root / "version.json")).env["ASTRID_RUNTIME_DOOR_PORT"] == port


def test_existing_version_rebuilds_the_owned_pinned_tool_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data, version_root = tmp_path / "workspace", tmp_path / "legacy"
    environment = version_root / "py"
    python = environment / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("python", encoding="utf-8")
    for relative in ("tools/omp/bin/omp", "tools/bun/bin/bun", "tools/node/bin/node",
                     "tools/ffmpeg/bin/ffmpeg", "tools/ffmpeg/bin/ffprobe"):
        tool = version_root / relative
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text("tool", encoding="utf-8")
    profile = data / "runtime" / "source-profiles" / "astrid.json"
    write_json(profile, {"runtime_environment": str(environment)})
    backend = Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv")
    monkeypatch.setattr("astrid.installer.backend.current", lambda _root: None)

    retained = backend.existing(data)

    assert retained is not None
    entries = retained.env["PATH"].split(":" if ":" in retained.env["PATH"] else ";")
    assert entries[:5] == [str(python.parent), str(version_root / "tools" / "omp" / "bin"),
                           str(version_root / "tools" / "bun" / "bin"),
                           str(version_root / "tools" / "node" / "bin"),
                           str(version_root / "tools" / "ffmpeg" / "bin")]
    assert retained.env["ASTRID_NODE_EXECUTABLE"] == str(version_root / "tools" / "node" / "bin" / "node")
    assert retained.env["ASTRID_RUNTIME_DATA_ROOT"] == str(data)


def test_every_installer_launcher_uses_one_canonical_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, data, version, expected = _version(tmp_path)
    version.root.mkdir(parents=True, exist_ok=True)
    write_json(version.root / "version.json", version.document())
    switch(root, version)
    write_json(root / "install.json", {
        "owner": LABEL, "managed_by": "pinokio", "data_root": str(data),
    })

    # The manager provides no usable profile or tool variables. All stable
    # values must therefore come from the persisted Version and one builder.
    monkeypatch.setattr(foreground.os, "environ", {})

    launchd = plistlib.loads(render("darwin", version, data, tmp_path / "install.log").encode())
    linux = render("linux", version, data, tmp_path / "install.log")
    task = ET.fromstring(render("win32", version, data, tmp_path / "install.log", user="S-1-5-21-1"))
    namespace = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    task_arguments = task.findtext(".//task:Actions/task:Exec/task:Arguments", namespaces=namespace)
    assert launchd["EnvironmentVariables"] == expected
    assert _systemd_environment(linux) == expected
    assert task_arguments is not None
    for key, value in expected.items():
        assert key in task_arguments and value in task_arguments
    for system in ("darwin", "linux", "win32"):
        argv = command(version, data, windows=system == "win32", log=tmp_path / "install.log")
        assert _literal_environment(argv[-1]) == expected

    # The unsupervised foreground fallback carries the same values to spawn_tree.
    manager = Service(root, tmp_path / "logs" / "install.log", lambda *_args: "",
                      home=tmp_path / "manager-home", system="linux")
    manager.log.parent.mkdir(parents=True)
    write_json(manager.selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data)})
    write_json(data / "runtime" / "catalog.json", {"selected_realm_id": "realm", "realms": [
        {"realm_id": "realm", "data_root": str(data / "realm")},
    ]})
    spawned = {}

    def spawn_tree(argv, **kwargs):
        spawned.update(argv=argv, env=kwargs["env"])
        return SimpleNamespace(pid=42)

    monkeypatch.setattr("astrid.installer.service.platform.spawn_tree", spawn_tree)
    monkeypatch.setattr("astrid.installer.service.platform.process_identity",
                        lambda _pid: {"birth_id": "birth"})
    manager.start_foreground(version, data)
    assert {key: spawned["env"][key] for key in expected} == expected
    assert _literal_environment(spawned["argv"][-1]) == expected

    # Pinokio foreground uses execvpe, rather than a login manager, and still
    # receives the exact persisted values instead of its manager environment.
    seen = {}

    class Executed(Exception):
        pass

    def execvpe(_file, argv, environment):
        seen.update(argv=argv, env=environment)
        raise Executed

    monkeypatch.setattr(foreground.os, "execvpe", execvpe)
    with pytest.raises(Executed):
        foreground._run(root, [], "linux", None)
    assert {key: seen["env"][key] for key in expected} == expected
    assert _literal_environment(seen["argv"][-1]) == expected

    # The worker command and all native transient forms carry the same map.
    job = _job(root, data, version)
    assert _literal_environment(update_job._worker_command_source(version, job)) == expected
    for system in ("linux", "darwin", "win32"):
        if system == "win32":
            monkeypatch.setattr(update_job_platform, "_windows_sid", lambda: "S-1-5-21-1")
        descriptor = update_job_platform.describe(job, [str(version.python), "-c", "pass"], system=system)
        assert descriptor["environment"] == expected
        calls = []
        monkeypatch.setattr(update_job_platform, "_run",
                            lambda argv: calls.append(argv) or CompletedProcess(argv, 0, "", ""))
        update_job_platform.launch(descriptor, descriptor["command"], descriptor["log_path"], system=system)
        if system == "linux":
            values = {item.removeprefix("--setenv=").split("=", 1)[0]: item.removeprefix("--setenv=").split("=", 1)[1]
                      for item in calls[0] if item.startswith("--setenv=")}
            assert values == expected
        elif system == "darwin":
            plist = ET.parse(descriptor["plist_path"]).getroot()
            pairs = list(plist.find("dict"))
            environment_node = next(pairs[index + 1] for index, item in enumerate(pairs)
                                    if item.tag == "key" and item.text == "EnvironmentVariables")
            values = {environment_node[index].text: environment_node[index + 1].text
                      for index in range(0, len(environment_node), 2)}
            assert values == expected
        else:
            task_source = base64.b64decode(calls[0][-1]).decode("utf-16le")
            encoded = re.search(r"-EncodedCommand ([A-Za-z0-9+/=]+)", task_source)
            assert encoded is not None
            source = base64.b64decode(encoded.group(1)).decode("utf-16le")
            for key, value in expected.items():
                assert f"$env:{key} = '{value}'" in source
