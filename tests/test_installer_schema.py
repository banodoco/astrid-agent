"""Installer staging must consume the schema artifact pinned by Remotion."""

import json
import subprocess
from pathlib import Path

import pytest

from astrid.installer.backend import Backend

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("failed_step", ["npm", "browser"])
def test_optional_remotion_failure_does_not_block_staging(tmp_path, monkeypatch, failed_step):
    import astrid.installer.backend as backend_module

    messages = []
    calls = []
    backend = Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv", say=messages.append)
    backend.uv.touch()
    monkeypatch.setattr(backend, "existing", lambda _data: None)
    monkeypatch.setattr(backend, "_installed_product_root", lambda python, _env: python.parent / "Lib/site-packages")

    def source(_name, _sha, destination, **_kwargs):
        destination.mkdir(parents=True)
        if destination.name == "a":
            (destination / "remotion").mkdir()
            (destination / "remotion/package-lock.json").write_text("{}\n")
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
            schema = modules / "@banodoco/timeline-schema/python"
            schema.mkdir(parents=True)
            (schema / "pyproject.toml").write_text("[project]\nname='banodoco-timeline-schema'\n")
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
    schema_installs = [call for call in calls if call[1:3] == ["pip", "install"] and any("timeline-schema" in v for v in call)]
    if failed_step == "npm":
        assert not schema_installs
        remotion = version.root / "a/remotion"
        assert (remotion / "node_modules/partial-output").is_file()
        assert not (remotion / "node_modules/.astrid-package-lock.sha256").exists()
    else:
        assert len(schema_installs) == 1
        assert schema_installs[0][-1].endswith("/a/remotion/node_modules/@banodoco/timeline-schema/python")


def test_staging_installs_the_npm_locked_schema_before_declaring_services(tmp_path, monkeypatch):
    import astrid.installer.backend as backend_module

    calls = []
    backend = Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv")
    backend.uv.touch()
    monkeypatch.setattr(backend, "existing", lambda _data: None)
    monkeypatch.setattr(backend, "_installed_product_root", lambda python, _env: python.parent / "Lib/site-packages")

    def source(_name, _sha, destination, **_kwargs):
        destination.mkdir(parents=True)
        if destination.name == "a":
            (destination / "remotion").mkdir()
            (destination / "remotion/package-lock.json").write_text("{}\n")
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
            package = tmp_path / "program" / "versions"
            agent_root = next(package.iterdir()) / "a"
            schema = agent_root / "remotion" / "node_modules" / "@banodoco" / "timeline-schema" / "python"
            (schema / "banodoco_timeline_schema").mkdir(parents=True)
            (schema / "pyproject.toml").write_text("[project]\nname='banodoco-timeline-schema'\n", encoding="utf-8")
        if "source_checkout_digest" in " ".join(call):
            return "digest"
        return ""

    monkeypatch.setattr(backend, "run", run)

    declarations = []

    def declare(*, folder, agent, **_kwargs):
        schema = (
            next((tmp_path / "program" / "versions").iterdir())
            / "a" / "remotion" / "node_modules" / "@banodoco" / "timeline-schema" / "python"
        )
        assert (schema / "pyproject.toml").is_file()
        assert agent == folder / "py" / "bin" / "Lib" / "site-packages"
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
    schema_install_index = next(
        i for i, call in enumerate(calls)
        if call[1:3] == ["pip", "install"] and schema_path in call
    )
    assert npm_index < schema_install_index < calls.index(["declare-services"])
    assert schema_path.endswith("/a/remotion/node_modules/@banodoco/timeline-schema/python")
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
    assert python_installs[2] == [
        str(backend.uv), "pip", "install", "--python", str(version.python), "--no-deps", schema_path,
    ]
    assert version.profile.is_file()


def test_remotion_lock_pins_the_transitive_schema_package():
    package = json.loads((ROOT / "remotion/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "remotion/package-lock.json").read_text(encoding="utf-8"))
    composition = lock["packages"]["node_modules/@banodoco/timeline-composition"]
    schema = lock["packages"]["node_modules/@banodoco/timeline-schema"]

    assert package["dependencies"]["@banodoco/timeline-composition"] == (
        "https://github.com/banodoco/timeline-composition/archive/refs/tags/v0.0.6.tar.gz"
    )
    assert composition["dependencies"]["@banodoco/timeline-schema"] == (
        "https://github.com/banodoco/timeline-schema/archive/refs/tags/v0.0.1.tar.gz"
    )
    assert schema["version"] == "0.0.1"
    assert schema["resolved"] == (
        "https://github.com/banodoco/timeline-schema/archive/refs/tags/v0.0.1.tar.gz"
    )
    assert schema["integrity"] == (
        "sha512-m7oUjUV+q1dPb78N6+5B3iayTXkV/WBrQLxNDrlt32j4GXY2sQOIOwpGBw4kNjjngVpSwSaAGneQKUWCgUS8lQ=="
    )
