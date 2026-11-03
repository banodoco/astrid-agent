"""Installer staging must verify the Runtime-pinned local timeline schema."""

import base64
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from astrid.installer.backend import Backend

ROOT = Path(__file__).parents[1]
_OBSOLETE_EXTERNALS = ("timeline-schema", "timeline-theme-2rp")


@pytest.mark.parametrize("failed_step", ["npm", "browser"])
def test_optional_remotion_failure_does_not_block_staging(tmp_path, monkeypatch, failed_step):
    import astrid.installer.backend as backend_module

    messages = []
    calls = []
    backend = Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv", say=messages.append)
    backend.uv.touch()
    monkeypatch.setattr(backend, "existing", lambda _data: None)
    def installed_root(python, _env):
        root = python.parent / "Lib/site-packages"
        schema = root / "astrid/core/timeline/schema/timeline-config.schema.json"
        schema.parent.mkdir(parents=True, exist_ok=True)
        schema.write_bytes((ROOT / "astrid/core/timeline/schema/timeline-config.schema.json").read_bytes())
        return root

    monkeypatch.setattr(backend, "_installed_product_root", installed_root)

    def source(_name, _sha, destination, **_kwargs):
        destination.mkdir(parents=True)
        if destination.name == "a":
            (destination / "remotion").mkdir()
            (destination / "remotion/package-lock.json").write_text("{}\n")
            canonical = ROOT / "astrid/core/timeline/schema/timeline-config.schema.json"
            staged_canonical = destination / canonical.relative_to(ROOT)
            staged_canonical.parent.mkdir(parents=True)
            staged_canonical.write_bytes(canonical.read_bytes())
            (destination / "runtime.lock.json").write_bytes(
                (ROOT / "runtime.lock.json").read_bytes()
            )
        else:
            (destination / "packages/python").mkdir(parents=True)
        return destination

    monkeypatch.setattr(backend_module, "source", source)
    monkeypatch.setattr(backend_module, "install_tools", lambda *_args: {
        "PATH": "/tools", "ASTRID_NODE_EXECUTABLE": "/tools/node", "ASTRID_BUN_EXECUTABLE": "/tools/bun",
    })
    monkeypatch.setattr(backend_module, "binary", lambda *_args: Path("/tools/npm-cli.js"))

    def run(argv, **_kwargs):
        call = [str(value) for value in argv]
        calls.append(call)
        if call[0].endswith("uv") and call[1:] == ["--version"]:
            return "uv 1.0.0"
        npm_ci = call[1:3] == ["/tools/npm-cli.js", "ci"]
        browser_fetch = any(value.endswith("fetch-shared-browser.mjs") for value in call)
        if npm_ci:
            remotion = next((tmp_path / "program/versions").iterdir()) / "a/remotion"
            modules = remotion / "node_modules"
            modules.mkdir(exist_ok=True)
            if failed_step == "npm":
                (modules / "partial-output").write_text("partial")
                raise subprocess.CalledProcessError(1, "npm", output="registry unavailable")
        if browser_fetch and failed_step == "browser":
            raise subprocess.CalledProcessError(1, "browser", output="browser host unavailable")
        if "source_checkout_digest" in " ".join(call):
            return "digest"
        return ""

    monkeypatch.setattr(backend, "run", run)
    declared = []

    def declare(*, folder, **_kwargs):
        calls.append(["declare-services"])
        declared.append(True)
        destination = folder / "services.json"
        destination.write_text('{"version":1,"services":[]}', encoding="utf-8")
        return destination

    monkeypatch.setattr(backend, "_declare_services", declare)
    manifest = {
        "agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40},
        "tools": {"uv": {"version": "1.0.0"}, "python": {"version": "3.11.9"}},
    }

    version = backend.stage(manifest, tmp_path / "program", tmp_path / "workspace")

    assert version.profile.is_file()
    assert declared
    assert messages and "Rendering unavailable" in messages[0]
    assert ("npm ci" if failed_step == "npm" else "browser setup") in messages[0]
    assert "retry" in messages[0]
    schema_installs = [call for call in calls if call[1:3] == ["pip", "install"]
                       and any("timeline-schema" in v for v in call)]
    assert not schema_installs
    if failed_step == "npm":
        remotion = version.root / "a/remotion"
        assert (remotion / "node_modules/partial-output").is_file()
        assert not (remotion / "node_modules/.astrid-package-lock.sha256").exists()


def _integrity(path: Path) -> str:
    digest = base64.b64encode(hashlib.sha512(path.read_bytes()).digest()).decode("ascii")
    return "sha512-" + digest


def _contains_obsolete_external(value) -> bool:
    if isinstance(value, str):
        lowered = value.lower()
        return any(token in lowered for token in _OBSOLETE_EXTERNALS)
    if isinstance(value, dict):
        return any(_contains_obsolete_external(key) or _contains_obsolete_external(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_obsolete_external(item) for item in value)
    return False


def test_staging_verifies_the_local_schema_before_declaring_services(tmp_path, monkeypatch):
    import astrid.installer.backend as backend_module

    calls = []
    backend = Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv")
    backend.uv.touch()
    monkeypatch.setattr(backend, "existing", lambda _data: None)
    def installed_root(python, _env):
        root = python.parent / "Lib/site-packages"
        schema = root / "astrid/core/timeline/schema/timeline-config.schema.json"
        schema.parent.mkdir(parents=True, exist_ok=True)
        schema.write_bytes((ROOT / "astrid/core/timeline/schema/timeline-config.schema.json").read_bytes())
        return root

    monkeypatch.setattr(backend, "_installed_product_root", installed_root)

    def source(_name, _sha, destination, **_kwargs):
        destination.mkdir(parents=True)
        if destination.name == "a":
            (destination / "remotion").mkdir()
            (destination / "remotion/package-lock.json").write_text("{}\n")
            canonical = ROOT / "astrid/core/timeline/schema/timeline-config.schema.json"
            staged_canonical = destination / canonical.relative_to(ROOT)
            staged_canonical.parent.mkdir(parents=True)
            staged_canonical.write_bytes(canonical.read_bytes())
            (destination / "runtime.lock.json").write_bytes(
                (ROOT / "runtime.lock.json").read_bytes()
            )
        else:
            (destination / "packages" / "python").mkdir(parents=True)
        return destination

    monkeypatch.setattr(backend_module, "source", source)
    monkeypatch.setattr(backend_module, "install_tools", lambda *_args: {
        "PATH": "/tools",
        "ASTRID_NODE_EXECUTABLE": "/tools/node",
        "ASTRID_BUN_EXECUTABLE": "/tools/bun",
    })
    monkeypatch.setattr(backend_module, "binary", lambda *_args: Path("/tools/npm-cli.js"))

    def run(argv, **_kwargs):
        call = [str(value) for value in argv]
        calls.append(call)
        if call[0].endswith("uv") and call[1:] == ["--version"]:
            return "uv 1.0.0"
        if call[1:3] == ["/tools/npm-cli.js", "ci"]:
            agent_root = next((tmp_path / "program" / "versions").iterdir()) / "a"
            (agent_root / "remotion/node_modules").mkdir()
        if "source_checkout_digest" in " ".join(call):
            return "digest"
        return ""

    monkeypatch.setattr(backend, "run", run)

    declarations = []

    def declare(*, folder, agent, **_kwargs):
        assert agent == folder / "py" / "bin" / "Lib" / "site-packages"
        schema = agent / "astrid/core/timeline/schema/timeline-config.schema.json"
        assert schema.is_file()
        calls.append(["declare-services"])
        declarations.append(schema)
        destination = folder / "services.json"
        destination.write_text('{"version":1,"services":[]}', encoding="utf-8")
        return destination

    monkeypatch.setattr(backend, "_declare_services", declare)
    manifest = {
        "agent": {"sha": "a" * 40},
        "runtime": {"sha": "b" * 40},
        "tools": {"uv": {"version": "1.0.0"}, "python": {"version": "3.11.9"}},
    }

    version = backend.stage(manifest, tmp_path / "program", tmp_path / "workspace")

    npm_index = next(i for i, call in enumerate(calls) if call[1:3] == ["/tools/npm-cli.js", "ci"])
    schema_path = str(declarations[0])
    initial_install = next(call for call in calls if any(value.endswith("packages/python") for value in call))
    assert not any("timeline-schema" in value for value in initial_install)
    python_installs = [call for call in calls if call[1:3] == ["pip", "install"]]
    assert python_installs[0] == [
        str(backend.uv), "pip", "install", "--python", str(version.python),
        "--require-hashes", "-r", str(version.root / "a/requirements/runtime.lock"),
    ]
    assert python_installs[1] == [
        str(backend.uv), "pip", "install", "--python", str(version.python), "--no-deps",
        str(version.root / "r"), str(version.root / "r/packages/python"), str(version.root / "a"),
        str(version.root / "v"),
    ]
    assert len(python_installs) == 2
    assert version.env["ASTRID_INSTALL_UV"] == str(backend.uv)
    assert npm_index < calls.index(["declare-services"])
    assert schema_path == version.env["ASTRID_TIMELINE_SCHEMA_PYTHONPATH"]
    assert schema_path.endswith("/site-packages/astrid/core/timeline/schema/timeline-config.schema.json")
    assert version.profile.is_file()


def test_local_schema_verification_rejects_a_changed_schema(tmp_path):
    agent = tmp_path / "agent"
    schema = agent / "astrid/core/timeline/schema/timeline-config.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_text("{}\n", encoding="utf-8")
    (agent / "runtime.lock.json").write_bytes((ROOT / "runtime.lock.json").read_bytes())

    with pytest.raises(ValueError, match="does not match its Runtime source pin"):
        Backend._verify_local_timeline_schema(agent)


def test_staging_rejects_missing_local_schema_before_environment_setup(tmp_path, monkeypatch):
    import astrid.installer.backend as backend_module

    calls = []
    backend = Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv")
    backend.uv.touch()

    def source(_name, _sha, destination, **_kwargs):
        destination.mkdir(parents=True)
        if destination.name == "a":
            (destination / "remotion").mkdir()
            (destination / "remotion/package-lock.json").write_text("{}\n")
            (destination / "runtime.lock.json").write_bytes(
                (ROOT / "runtime.lock.json").read_bytes()
            )
        else:
            (destination / "packages/python").mkdir(parents=True)
        return destination

    monkeypatch.setattr(backend_module, "source", source)

    def run(argv, **_kwargs):
        calls.append([str(value) for value in argv])
        return "uv 1.0.0"

    monkeypatch.setattr(backend, "run", run)
    manifest = {
        "agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40},
        "tools": {"uv": {"version": "1.0.0"}, "python": {"version": "3.11.9"}},
    }

    with pytest.raises(ValueError, match="missing the local canonical timeline schema"):
        backend.stage(manifest, tmp_path / "program", tmp_path / "workspace")

    assert calls == [[str(backend.uv), "--version"]]


def test_remotion_lock_pins_the_immutable_composition_public_entry():
    package = json.loads((ROOT / "remotion/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "remotion/package-lock.json").read_text(encoding="utf-8"))
    composition = lock["packages"]["node_modules/@banodoco/timeline-composition"]
    composition_spec = package["dependencies"]["@banodoco/timeline-composition"]
    artifact = ROOT / "remotion" / composition_spec.removeprefix("file:")
    integrity = _integrity(artifact)
    public_exports = (".", "./evaluation", "./theme-api", "./registries",
                      "./effects.generated", "./animations.generated",
                      "./transitions.generated", "./registry.generated")

    assert composition_spec.startswith("file:artifacts/banodoco-timeline-composition-")
    assert composition["resolved"] == composition_spec
    assert composition["integrity"] == integrity
    assert "node_modules/@banodoco/timeline-schema" not in lock["packages"]
    assert "node_modules/@banodoco/timeline-theme-2rp" not in lock["packages"]

    with tarfile.open(artifact) as archive:
        manifest = json.load(archive.extractfile("package/package.json"))
        files = {member.name for member in archive.getmembers()}
    assert manifest["name"] == "@banodoco/timeline-composition"
    assert manifest["version"] == composition["version"]
    for export in public_exports:
        for target in manifest["exports"][export].values():
            assert target.startswith("./dist/")
            assert "package/" + target.removeprefix("./") in files


# D72 migration of upstream-924 selector 10444 to the approved local
# Composition artifact and Runtime-owned canonical schema.
def test_d72_runtime_and_composition_pins_have_no_obsolete_externals():
    package = json.loads((ROOT / "remotion/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "remotion/package-lock.json").read_text(encoding="utf-8"))
    composition = lock["packages"]["node_modules/@banodoco/timeline-composition"]
    composition_spec = package["dependencies"]["@banodoco/timeline-composition"]
    artifact = ROOT / "remotion" / composition_spec.removeprefix("file:")

    assert composition_spec.startswith("file:artifacts/")
    assert composition["resolved"] == composition_spec
    assert composition["integrity"] == _integrity(artifact)
    assert composition["integrity"] == _integrity(artifact)

    runtime_lock = json.loads((ROOT / "runtime.lock.json").read_text(encoding="utf-8"))
    schema_pin = runtime_lock["files"]["astrid/core/timeline/schema/timeline-config.schema.json"]
    canonical_schema = ROOT / "astrid/core/timeline/schema/timeline-config.schema.json"
    assert schema_pin["from"] == "contract/schemas/timeline-config.schema.json"
    assert schema_pin["sha256"] == "sha256:" + hashlib.sha256(canonical_schema.read_bytes()).hexdigest()
    assert schema_pin["sha256"] == (
        "sha256:f870150ffd0e42365c048965e2f7c08efc959c36db76ed4cba59de2e58ab4e08"
    )

    assert not _contains_obsolete_external(package)
    assert not _contains_obsolete_external(lock)
    with tarfile.open(artifact) as archive:
        for member in archive.getmembers():
            assert not _contains_obsolete_external(member.name)
            if member.isfile():
                content = archive.extractfile(member).read()
                assert not any(token.encode() in content.lower() for token in _OBSOLETE_EXTERNALS)
