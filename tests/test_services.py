from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest

from astrid.services import service_declaration


def test_declaration_matches_runtime_contract_with_space_containing_paths(tmp_path, monkeypatch):
    source = tmp_path / "agent checkout"
    root = tmp_path / "workspace data"
    python = str(tmp_path / "python environment" / "python")
    bun = str(tmp_path / "Bun tools" / "bun")
    program = str(tmp_path / "custom Astrid program")
    monkeypatch.setenv("ASTRID_INSTALL_PROGRAM_ROOT", program)
    declaration = service_declaration(source_checkout=source, data_root=root, python=python, bun=bun)
    schema = json.loads((Path(__file__).parent / "fixtures" / "services.schema.json").read_text())
    jsonschema.validate(declaration, schema)
    host, chat = declaration["services"]
    assert host["env"]["ASTRID_INSTALL_PROGRAM_ROOT"] == program
    assert chat["env"]["ASTRID_INSTALL_PROGRAM_ROOT"] == program
    assert host["argv"][0] == python
    assert host["argv"][host["argv"].index("--source-checkout") + 1] == str(source)
    assert host["argv"][host["argv"].index("--support-root") + 1] == str(root / "runtime")
    assert chat["argv"] == [bun, str(Path(__file__).parents[1] / "astrid/chat/service.mjs")]
    assert chat["env"]["ASTRID_WORKSPACE_DISCOVERY"] == str(root / "runtime/discovery.json")
    assert host["argv"][1:4] == ["-X", "utf8", "-m"]
    assert chat["env"]["ASTRID_SUPPORT_ROOT"] == str(root)
    assert chat["env"]["ASTRID_ACP_CWD"] == str(root / "chat")
    assert chat["env"]["ASTRID_ACP_LOCAL_MEDIA_ONLY"] == "1"
    assert chat["env"]["ASTRID_SERVICE_PYTHON"] == python
    expected_command = str(Path(python).with_name("astrid.exe")) if sys.platform == "win32" else "astrid"
    assert chat["env"]["ASTRID_ACP_COMMAND"] == expected_command
    assert {row["restart"] for row in declaration["services"]} == {"always"}


def test_declaration_requires_absolute_interpreters_and_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("astrid.services.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match="Bun 1.3.14"):
        service_declaration(source_checkout=tmp_path, data_root=tmp_path)
    with pytest.raises(ValueError, match="must be absolute"):
        service_declaration(source_checkout=Path("relative"), data_root=tmp_path)
    with pytest.raises(ValueError, match="interpreters must be absolute"):
        service_declaration(source_checkout=tmp_path, data_root=tmp_path, bun="bun")


def test_declaration_retains_the_existing_chat_store_and_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_ACP_CWD", str(tmp_path / "previous app cwd"))
    monkeypatch.setenv("ASTRID_ACP_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("ASTRID_ACP_PROFILE", "existing-profile")
    monkeypatch.setenv("ASTRID_ACP_COMMAND", str(tmp_path / "installed astrid"))
    declaration = service_declaration(source_checkout=tmp_path, data_root=tmp_path,
        python=str(tmp_path / "python"), bun=str(tmp_path / "bun"))
    chat = declaration["services"][1]["env"]
    assert chat["ASTRID_ACP_CWD"] == str(tmp_path / "previous app cwd")
    assert chat["ASTRID_ACP_SESSION_DIR"] == str(tmp_path / "sessions")
    assert chat["ASTRID_ACP_PROFILE"] == "existing-profile"
    assert chat["ASTRID_ACP_COMMAND"] == str(tmp_path / "installed astrid")
    assert "ASTRID_ACP_BRIDGE_TOKEN" not in chat


def test_declaration_binds_installed_product_to_pinned_nonwheel_resources(tmp_path, monkeypatch):
    values = {
        "ASTRID_HOST_EXECUTION_PROFILE": "installed",
        "ASTRID_SOURCE_PROVENANCE_ROOT": str(tmp_path / "a"),
        "ASTRID_REMOTION_PROJECT_DIR": str(tmp_path / "a/remotion"),
        "ASTRID_TIMELINE_SCHEMA_PYTHONPATH": str(tmp_path / "a/remotion/node_modules/schema/python"),
        "ASTRID_CAPABILITY_MATRIX_PATH": str(tmp_path / "a/config/astrid-beta-capabilities.json"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    declaration = service_declaration(
        source_checkout=tmp_path / "py/Lib/site-packages", data_root=tmp_path / "data",
        python=str(tmp_path / "py/Scripts/python.exe"), bun=str(tmp_path / "bun.exe"),
    )
    host, chat = declaration["services"]
    assert host["argv"][host["argv"].index("--source-checkout") + 1] == str(tmp_path / "py/Lib/site-packages")
    assert all(host["env"][key] == value for key, value in values.items())
    assert not any(key in chat["env"] for key in values)


def test_windows_chat_binds_to_selected_launcher_and_preserves_override(tmp_path, monkeypatch):
    monkeypatch.setattr("astrid.services.sys.platform", "win32")
    monkeypatch.delenv("ASTRID_ACP_COMMAND", raising=False)
    python = tmp_path / "selected version" / "py" / "Scripts" / "python.exe"
    monkeypatch.setenv("PATH", str(tmp_path / "old version" / "Scripts"))
    options = dict(source_checkout=tmp_path, data_root=tmp_path,
                   python=str(python), bun=str(tmp_path / "bun.exe"))
    chat = service_declaration(**options)["services"][1]["env"]
    assert chat["ASTRID_ACP_COMMAND"] == str(python.with_name("astrid.exe"))
    override = str(tmp_path / "custom agent" / "astrid.cmd")
    monkeypatch.setenv("ASTRID_ACP_COMMAND", override)
    assert service_declaration(**options)["services"][1]["env"]["ASTRID_ACP_COMMAND"] == override
