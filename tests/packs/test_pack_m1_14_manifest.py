from __future__ import annotations

from pathlib import Path

import pytest

from astrid.core.pack.cli import main as packs_cli_main
from astrid.core.pack._common import PackValidationError
from astrid.core.pack.loader import load_pack_manifest, scan_packs


def _write_pack(root: Path, pack_id: str, manifest: str) -> Path:
    pack = root / pack_id
    pack.mkdir(parents=True)
    (pack / "pack.yaml").write_text(manifest, encoding="utf-8")
    return pack


def test_v3_parser_preserves_pack_api_dependencies_action_secrets_cli_and_engines(tmp_path):
    pack = _write_pack(tmp_path, "demo", """\
schema_version: 3
id: demo
name: Demo
version: 1.0.0
pack_api: "1.0"
dependencies:
  packs: [audio]
  python_requirements: [numpy>=1.20]
actions:
  render:
    description: Render the input.
    invocation:
      kind: python
      path: actions/render.py
      function: run
    inputs: []
    outputs: {}
    secrets: [DEMO_TOKEN]
cli:
  render:
    action: render
    arguments: schemas/render.json
engines:
  local:
    recipe: engines/local.json
""")
    (pack / "schemas").mkdir()
    (pack / "schemas/render.json").write_text("{}", encoding="utf-8")
    (pack / "engines").mkdir()
    (pack / "engines/local.json").write_text("{}", encoding="utf-8")
    (pack / "actions").mkdir()
    (pack / "actions/render.py").write_text("def run(): pass\n", encoding="utf-8")

    parsed = load_pack_manifest(pack / "pack.yaml")

    assert parsed.pack_api == "1.0"
    assert parsed.dependencies["packs"] == ["audio"]
    assert parsed.dependencies["python_requirements"] == ["numpy>=1.20"]
    assert parsed.actions["render"]["secrets"] == ["DEMO_TOKEN"]
    assert parsed.cli["render"] == {"action": "render", "arguments": "schemas/render.json"}
    assert parsed.engines["local"] == {"recipe": "engines/local.json"}


@pytest.mark.parametrize("manifest", [
    """schema_version: 3\nid: demo\nname: Demo\nversion: 1.0.0\npack_api: 1.0\ndependencies: {packs: bad}\n""",
    """schema_version: 3\nid: demo\nname: Demo\nversion: 1.0.0\npack_api: 1.0\nactions: {render: {secrets: TOKEN}}\n""",
    """schema_version: 3\nid: demo\nname: Demo\nversion: 1.0.0\npack_api: 1.0\ncli: {render: {action: render, arguments: ../outside.json}}\n""",
    """schema_version: 3\nid: demo\nname: Demo\nversion: 1.0.0\npack_api: 1.0\nengines: {local: {recipe: /etc/passwd}}\n""",
    """schema_version: 3\nid: demo\nname: Demo\nversion: 1.0.0\npack_api: 1.0\ncli: {render: {action: render}}\n""",
    """schema_version: 3\nid: demo\nname: Demo\nversion: 1.0.0\npack_api: 1.0\nactions: {render: {description: Render, secrets: [bad_secret]}}\n""",
])
def test_v3_manifest_refuses_malformed_types_and_unsafe_declaration_paths(tmp_path, manifest):
    pack = _write_pack(tmp_path, "demo", manifest)
    with pytest.raises((PackValidationError, ValueError)):
        load_pack_manifest(pack / "pack.yaml")


def test_discovery_quarantines_incompatible_external_pack_without_aborting_siblings(tmp_path):
    _write_pack(tmp_path, "alpha", """schema_version: 3\nid: alpha\nname: Alpha\nversion: 1.0.0\npack_api: \"1.0\"\ndocumentation: {kind: none, reason: No docs.}\n""")
    bad = _write_pack(tmp_path, "beta", """schema_version: 3\nid: beta\nname: Beta\nversion: 1.0.0\npack_api: \"2.0\"\ndocumentation: {kind: none, reason: No docs.}\n""")

    scan = scan_packs(tmp_path, strict=True)

    assert [pack.id for pack in scan.packs] == ["alpha"]
    assert len(scan.quarantined) == 1
    assert scan.quarantined[0].pack_dir == bad
    assert "incompatible pack_api major" in scan.quarantined[0].error


def test_discovery_inventories_legacy_v1_as_upgrade_required(tmp_path):
    legacy = _write_pack(tmp_path, "legacy", "schema_version: 1\nid: legacy\n")

    scan = scan_packs(tmp_path, strict=True)

    assert scan.packs == ()
    assert len(scan.quarantined) == 1
    assert scan.quarantined[0].pack_dir == legacy
    assert "explicit schema upgrade" in scan.quarantined[0].error


def test_discovery_preflights_python_requirements_with_current_interpreter(tmp_path):
    pack = _write_pack(tmp_path, "needs_python", """\
schema_version: 3
id: needs_python
name: Needs Python
version: 1.0.0
pack_api: "1.0"
documentation: {kind: none, reason: No docs.}
dependencies:
  python_requirements: [astrid-pk-m1-14-package-that-does-not-exist]
""")

    scan = scan_packs(tmp_path, strict=True)

    assert scan.packs == ()
    assert scan.quarantined[0].pack_dir == pack
    assert "Python dependency is unavailable" in scan.quarantined[0].error


def test_packs_check_alias_uses_shared_compatibility_validator(tmp_path, capsys):
    pack = _write_pack(tmp_path, "missing_api", """\
schema_version: 3
id: missing_api
name: Missing API
version: 1.0.0
documentation: {kind: none, reason: No docs.}
""")

    result = packs_cli_main(["check", str(pack), "--json"])

    assert result == 1
    assert "pack_api compatibility" in capsys.readouterr().out
