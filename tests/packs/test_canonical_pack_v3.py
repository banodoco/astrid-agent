from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
import jsonschema

from astrid.core.contracts.schema import CommandInputArg, CommandSpec, Output, Port
from astrid.core.pack.canonical import (
    BundledCatalog,
    CanonicalPackValidationError,
    ExternalPackSource,
    read_normalize_validate,
    validate_canonical_pack,
)
from astrid.core.pack.definition import PackDefinition


def _action() -> dict:
    return {
        "description": "Echo an input.",
        "invocation": {"kind": "python", "path": "actions/echo.py", "function": "echo"},
        "inputs": [{"name": "message", "type": "string", "required": True}],
        "outputs": [{"name": "result", "type": "json", "mode": "create", "artifact_type": "test/result"}],
    }


def _pack(tmp_path: Path, *, files: dict[str, str] | None = None, **sections) -> Path:
    root = tmp_path / "demo"
    root.mkdir(exist_ok=True, parents=True)
    for name, content in (files or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / "pack.yaml").write_text(yaml.safe_dump({
        "schema_version": 3, "id": "demo", "name": "Demo", "version": "1.0.0", **sections,
    }), encoding="utf-8")
    return root


def _python_pack(tmp_path: Path, **sections) -> Path:
    return _pack(tmp_path, files={"actions/echo.py": "raise RuntimeError('must not import pack code')\n"},
                 actions={"echo": _action()}, **sections)


def test_minimal_action_has_no_compulsory_categories_or_skill(tmp_path: Path) -> None:
    root = _python_pack(tmp_path)
    entry = validate_canonical_pack(root)
    definition = entry.definition
    assert definition.schema_version == 3
    assert definition.declaration_id("actions", "echo") == "demo.echo"
    assert not definition.ui and not definition.rendering and not definition.documents
    assert definition.documentation is None
    assert {p.name for p in root.iterdir()} == {"actions", "pack.yaml"}
    assert definition.to_dict()["actions"]["echo"] == _action()
    assert "content" not in definition.to_dict() and "extensions" not in definition.to_dict()
    assert Port(**definition.to_dict()["actions"]["echo"]["inputs"][0]).type == "string"
    assert Output(**definition.to_dict()["actions"]["echo"]["outputs"][0]).mode == "create"
    assert entry.capabilities.actions is definition.actions
    with pytest.raises(TypeError):
        definition.actions["echo"]["invocation"]["path"] = "other.py"
    handle, = entry.resources
    assert handle.path == "actions/echo.py"
    assert handle.sha256 == hashlib.sha256((root / handle.path).read_bytes()).hexdigest()


def test_structured_command_retains_existing_controls_without_extra_path(tmp_path: Path) -> None:
    action = _action()
    command = {"argv": ["{python_exec}", "-m", "dependency.tool", "{message}"],
               "cwd": "{out}", "env": {"MODE": "test"},
               "input_args": [{"input": "message", "flag": "--message", "optional": True}]}
    action.update(invocation={"kind": "command", "command": command},
                  isolation={"mode": "subprocess", "network": False, "binaries": ["ffmpeg"]},
                  cache={"mode": "none"}, graph={"depends_on": ["demo.other"]},
                  conditions=[{"kind": "always"}], metadata={"output_result_manifest": True},
                  scoped_configs=["render.defaults"])
    root = _pack(tmp_path, files={"actions/echo/support.txt": "support"}, actions={"echo": action})
    normalized = validate_canonical_pack(root).definition.to_dict()["actions"]["echo"]
    assert normalized == action
    spec = CommandSpec(**{**command, "argv": tuple(command["argv"]),
                         "input_args": tuple(CommandInputArg(**arg) for arg in command["input_args"])})
    assert spec.input_args[0].optional and spec.cwd == "{out}"


def test_ui_only_uses_the_existing_editor_host(tmp_path: Path) -> None:
    root = _pack(tmp_path, files={"ui/threejs-live-editor/extension.tsx": "export default extension;\n"},
                 ui={"threejs-live-editor": {"type": "editor", "entry": "ui/threejs-live-editor/extension.tsx"}})
    entry = validate_canonical_pack(root)
    assert entry.definition.declaration_id("ui", "threejs-live-editor") == "demo.threejs-live-editor"
    assert not entry.definition.actions
    assert {h.path for h in entry.resources} == {"ui/threejs-live-editor/extension.tsx"}
    assert not (root / "actions").exists()


def test_tool_ui_projection_keeps_whole_tool_distinct_from_editor_extensions(tmp_path: Path) -> None:
    entry_path = "ui/video-editor/entry.json"
    resource_path = "ui/video-editor/service-contract.json"
    root = _pack(
        tmp_path,
        files={
            entry_path: '{"schema_version":1,"tool_id":"video-editor","host_entry":"video-editor"}\n',
            resource_path: '{"services":["timeline","shots","agent"]}\n',
            "ui/extension.tsx": "export default extension;\n",
        },
        ui={
            "video-editor": {
                "type": "tool",
                "entry": entry_path,
                "target": "astrid",
                "compatibility": {"host": "1"},
                "resources": [{"kind": "support", "path": resource_path}],
            },
            "threejs-live-editor": {"type": "editor", "entry": "ui/extension.tsx"},
        },
    )

    canonical = validate_canonical_pack(root)
    (projection,) = canonical.tool_entry_projections()
    handles = {handle.path: handle for handle in canonical.resource_handles}

    assert canonical.definition.declaration_id("ui", "video-editor") == "demo.video-editor"
    assert canonical.definition.ui["threejs-live-editor"]["type"] == "editor"
    assert projection.canonical_id == "demo.video-editor"
    assert projection.declaration_key == "video-editor"
    assert projection.entry is handles[entry_path]
    assert projection.resources == (handles[resource_path],)
    assert projection.resource_closure == (handles[entry_path], handles[resource_path])
    assert projection.target == "astrid" and dict(projection.compatibility) == {"host": "1"}


def test_mixed_maps_and_authored_skill_have_one_manifest_authority(tmp_path: Path) -> None:
    skill = "---\nname: authored-name\ndescription: Authored instructions.\n---\n# Use both surfaces\n"
    root = _pack(tmp_path, files={"actions/echo.py": "def echo(): pass\n",
                                 "ui/threejs-live-editor/extension.tsx": "export default extension;\n",
                                 "docs/SKILL.md": skill},
                 actions={"echo": _action()},
                 ui={"threejs-live-editor": {"type": "editor", "entry": "ui/threejs-live-editor/extension.tsx",
                                     "target": "video-editor", "compatibility": {"sdk": "1"}}},
                 documentation={"kind": "skill", "path": "docs/SKILL.md"})
    entry = validate_canonical_pack(root)
    assert entry.documentation.path == "docs/SKILL.md"
    assert entry.definition.to_dict()["documentation"] == {"kind": "skill", "path": "docs/SKILL.md"}
    assert (root / "docs/SKILL.md").read_text() == skill
    assert entry.capabilities.ui is entry.definition.ui
    serialized = entry.definition.to_dict()
    (root / "pack.yaml").write_text(yaml.safe_dump(serialized))
    assert validate_canonical_pack(root).definition.to_dict() == serialized


@pytest.mark.parametrize("kind", ["renderer", "planner", "finalizer"])
def test_rendering_references_existing_protocol_descriptors(tmp_path: Path, kind: str) -> None:
    path = f"rendering/{kind}s/example/{kind}.yaml"
    descriptor = {"id": "demo.example", "schema_version": 1, "protocol_version": 1,
                  "command": ["python3", "run.py"]}
    root = _pack(tmp_path, files={path: yaml.safe_dump(descriptor)},
                 rendering={"example": {"type": kind, "path": path}})
    original = (root / path).read_bytes()
    entry = validate_canonical_pack(root)
    assert entry.definition.rendering["example"]["type"] == kind
    assert entry.definition.declaration_id("rendering", "example") == "demo.example"
    assert (root / path).read_bytes() == original
    assert entry.resources[0].sha256 == hashlib.sha256(original).hexdigest()


def test_element_retains_kind_local_identity_and_descriptor_defaults(tmp_path: Path) -> None:
    path = "rendering/elements/animations/slide-left/element.yaml"
    descriptor = {"id": "slide-left", "kind": "animation", "pack_id": "demo", "schema_version": 1,
                  "runtime": {"adapter": "remotion"}, "defaults": {"durationFrames": 18}}
    root = _pack(tmp_path, files={path: yaml.safe_dump(descriptor)},
                 rendering={"animations/slide-left": {"type": "element", "path": path}})
    entry = validate_canonical_pack(root)
    assert entry.definition.declaration_id("rendering", "animations/slide-left") == "animations/slide-left"
    assert entry.id == "demo"
    assert yaml.safe_load((root / path).read_text()) == descriptor


def test_documents_are_schema_resources_with_independent_format_version(tmp_path: Path) -> None:
    root = _pack(tmp_path, files={"shared/note.schema.json": '{"type":"object"}',
                                 "shared/note-example.json": "{}"},
                 documents={"note": {"format_version": 7, "schema": "shared/note.schema.json",
                                     "resources": [{"kind": "example", "path": "shared/note-example.json"}]},
                            "inline": {"format_version": "2026-10", "schema": {"type": "string"}}})
    definition = validate_canonical_pack(root).definition
    assert definition.version == "1.0.0" and definition.documents["note"]["format_version"] == 7
    assert definition.declaration_id("documents", "note") == "demo.note"
    assert definition.documentation is None
    assert {h.path for h in validate_canonical_pack(root).resources} == {
        "shared/note.schema.json", "shared/note-example.json"}
    assert not (root / "database").exists()


def test_action_schema_contracts_are_alternatives_to_port_arrays(tmp_path: Path) -> None:
    action = _action()
    action.update(inputs="actions/echo-input.schema.json", outputs={"type": "object"})
    root = _pack(tmp_path, files={"actions/echo.py": "def echo(): pass\n",
                                 "actions/echo-input.schema.json": '{"type":"object"}'},
                 actions={"echo": action})
    definition = validate_canonical_pack(root).definition
    assert definition.to_dict()["actions"]["echo"] == action


def test_ids_aliases_permissions_and_source_provenance_are_retained(tmp_path: Path) -> None:
    root = _python_pack(tmp_path,
                        aliases=[{"kind": "action", "alias": "demo.old", "canonical_id": "demo.echo"}],
                        permissions=[{"id": "project_files", "access": "read", "reason": "Read input."}],
                        secrets=[{"id": "TOKEN", "required": True}],
                        dependencies={"python": ["dependency"], "packs": ["other"]})
    entry = read_normalize_validate(root / "pack.yaml", source=ExternalPackSource.MANAGED)
    assert entry.source == "managed" and entry.root == root.resolve()
    data = entry.definition.to_dict()
    assert data["aliases"][0]["alias"] == "demo.old"
    assert data["permissions"][0]["access"] == "read"
    assert data["secrets"] == [{"id": "TOKEN", "required": True}]
    assert data["dependencies"]["packs"] == ["other"]
    assert entry.provenance.provenance_identity == read_normalize_validate(
        root / "pack.yaml", source=ExternalPackSource.EXTRA).provenance.provenance_identity
    data["actions"]["echo"]["description"] = "Changed public declaration."
    (root / "pack.yaml").write_text(yaml.safe_dump(data))
    assert entry.provenance.provenance_identity != validate_canonical_pack(root).provenance.provenance_identity


@pytest.mark.parametrize("documentation,files", [
    ({"kind": "agents", "path": "AGENTS.md"}, {"AGENTS.md": "# Instructions\n"}),
    ({"kind": "none", "reason": "No guidance needed."}, {}),
])
def test_agents_and_none_dispositions_remain_explicit(tmp_path: Path, documentation, files) -> None:
    root = _pack(tmp_path, files=files, documentation=documentation)
    assert validate_canonical_pack(root).definition.to_dict()["documentation"] == documentation


def test_v2_admission_and_manifestless_core_shell_remain_compatible(tmp_path: Path) -> None:
    root = _pack(tmp_path, schema_version=2, files={"skill/SKILL.md": "# Existing skill\n"},
                 capabilities=["render"], content={}, extensions={},
                 documentation={"kind": "skill", "path": "skill/SKILL.md"})
    entry = validate_canonical_pack(root)
    assert entry.definition.schema_version == 2 and entry.definition.actions == {}
    assert entry.definition.to_dict()["content"] == {}
    assert entry.definition.to_dict()["extensions"] == {}
    assert entry.capabilities.capabilities == ("render",)
    assert {h.path for h in entry.resources} == {"skill/SKILL.md"}
    (tmp_path / "_core").mkdir()
    (tmp_path / "_core/SKILL.md").write_text("# Core shell\n")
    assert [pack.id for pack in BundledCatalog.from_root(tmp_path).entries] == ["demo"]


@pytest.mark.parametrize("sections,match", [
    ({"ui": {"tool": {"type": "theme", "entry": "ui/theme.json"}}}, "ui"),
    ({"ui": {"tool": {"type": "editor", "entry": "ui/tool.tsx", "host_id": "duplicate"}}}, "ui"),
    ({"ui": {"tool": {"type": "tool", "entry": "ui/tool.json", "target": "app", "compatibility": {"host": "1"}}}}, "ui"),
    ({"ui": {"tool": {"type": "tool", "entry": "ui/tool.json", "target": "astrid", "compatibility": {"sdk": "1"}}}}, "ui"),
    ({"actions": {"echo": {**_action(), "id": "other.echo"}}}, "actions"),
    ({"actions": {"echo": {**_action(), "input_schema": {"type": "object"}}}}, "actions"),
    ({"actions": {"echo": {**_action(), "invocation": {"kind": "command", "command": "echo hi"}}}}, "actions"),
    ({"actions": {"echo": {**_action(), "invocation": {"kind": "command", "path": "actions/run.py", "command": {"argv": ["echo"]}}}}}, "actions"),
    ({"actions": {"echo": {**_action(), "invocation": {"kind": "python", "path": "../run.py", "function": "main"}}}}, "actions"),
    ({"actions": {"echo": {**_action(), "invocation": {"kind": "python", "path": "shared/run.py", "function": "main"}}}}, "beneath actions"),
    ({"ui": {"tool": {"type": "editor", "entry": "actions/tool.tsx"}}}, "beneath ui"),
    ({"documents": {"note": {"format_version": 1, "schema": "../schema.json"}}}, "documents"),
    ({"content": {}, "actions": {}}, "Additional properties"),
    ({"extensions": {}, "ui": {}}, "Additional properties"),
    ({"documentation": {"kind": "skill", "path": "docs/SKILL.md", "name": "duplicate"}}, "documentation"),
    ({"documentation": {"skills": {"main": "docs/SKILL.md"}}}, "documentation"),
    ({"documentation": {"kind": "skill", "path": "skill/SKILL.md"}}, "documentation"),
    ({"database": {}, "actions": {}}, "database contributions are forbidden"),
    ({"schema_version": True, "actions": {}}, "schema_version"),
    ({"actions": {"echo": {**_action(), "inputs": [{"name": "value"}, {"name": "value"}]}}}, "duplicate port"),
])
def test_v3_rejects_ambiguous_unsafe_and_unimplemented_shapes(tmp_path: Path, sections, match: str) -> None:
    root = _pack(tmp_path, **sections)
    with pytest.raises(CanonicalPackValidationError, match=match):
        validate_canonical_pack(root)


def test_duplicate_yaml_and_public_identities_fail(tmp_path: Path) -> None:
    root = _python_pack(tmp_path)
    with (root / "pack.yaml").open("a") as stream:
        stream.write("actions: {}\n")
    with pytest.raises(CanonicalPackValidationError, match="duplicate key"):
        validate_canonical_pack(root)
    root = _python_pack(tmp_path, ui={"echo": {"type": "editor", "entry": "ui/echo.tsx"}})
    with pytest.raises(CanonicalPackValidationError, match="duplicates public identity"):
        validate_canonical_pack(root)


def test_declared_resources_require_files_and_reject_exclusion_or_symlinks(tmp_path: Path) -> None:
    root = _pack(tmp_path, actions={"echo": _action()})
    with pytest.raises(CanonicalPackValidationError, match="missing"):
        validate_canonical_pack(root)
    (root / "actions/echo.py").mkdir(parents=True)
    with pytest.raises(CanonicalPackValidationError, match="regular file"):
        validate_canonical_pack(root)
    (root / "actions/echo.py").rmdir()
    root = _python_pack(tmp_path, authoring_only=[{"path": "actions", "kind": "source", "reason": "Excluded."}])
    with pytest.raises(CanonicalPackValidationError, match="overlaps authoring-only"):
        validate_canonical_pack(root)
    (root / "actions/escape.py").symlink_to(tmp_path / "outside.py")
    with pytest.raises(CanonicalPackValidationError, match="symlink"):
        validate_canonical_pack(root)


@pytest.mark.parametrize("key,descriptor", [
    ("renderer", {"id": "other.renderer"}),
    ("animations/slide-left", {"id": "slide-right", "kind": "animation"}),
    ("animations/slide-left", {"id": "slide-left", "kind": "animation", "pack_id": "other"}),
])
def test_rendering_descriptor_identity_must_correspond(tmp_path: Path, key: str, descriptor: dict) -> None:
    path = "rendering/example/descriptor.yaml"
    root = _pack(tmp_path, files={path: yaml.safe_dump(descriptor)},
                 rendering={key: {"type": "element" if "/" in key else "renderer", "path": path}})
    with pytest.raises(CanonicalPackValidationError, match="descriptor"):
        validate_canonical_pack(root)


def test_staged_pack_identity_and_public_definition_role_serialization(tmp_path: Path) -> None:
    root = _python_pack(tmp_path)
    staged = root.rename(tmp_path / "staged")
    entry = read_normalize_validate(staged / "pack.yaml", source="local", expected_pack_id="demo")
    with pytest.raises(CanonicalPackValidationError, match="does not match"):
        read_normalize_validate(staged / "pack.yaml", source="local", expected_pack_id="other")
    public = PackDefinition(id="demo", name="Demo", version="1.0.0", root=staged,
                            manifest_path=staged / "pack.yaml", metadata={}, schema_version="3",
                            actions=entry.definition.to_dict()["actions"])
    assert json.loads(json.dumps(public.to_dict()))["actions"]["echo"] == _action()


def test_versioned_schema_is_valid_and_retains_shared_contract_fields() -> None:
    from astrid.core.pack import canonical

    schema = json.loads((Path(canonical.__file__).parent / "schemas/v3/pack.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    action = schema["properties"]["actions"]["additionalProperties"]["properties"]
    assert set(action["inputs"]["oneOf"][0]["items"]["properties"]) == set(Port.__dataclass_fields__)
    assert set(action["outputs"]["oneOf"][0]["items"]["properties"]) == set(Output.__dataclass_fields__)
    command = action["invocation"]["oneOf"][1]["properties"]["command"]
    assert set(command["properties"]) == set(CommandSpec.__dataclass_fields__)
    assert set(command["properties"]["input_args"]["items"]["properties"]) == set(CommandInputArg.__dataclass_fields__)


def test_v3_hyphenated_ids_and_alias_collision_checks(tmp_path: Path) -> None:
    alias = {"kind": "action", "alias": "demo.old-echo", "canonical_id": "demo.echo-message"}
    root = _pack(tmp_path, files={"actions/echo.py": "def echo(): pass\n"},
                 actions={"echo-message": _action()}, aliases=[alias])
    entry = validate_canonical_pack(root)
    assert entry.definition.declaration_id("actions", "echo-message") == "demo.echo-message"
    assert entry.definition.to_dict()["aliases"] == [alias]
    data = entry.definition.to_dict()
    data["aliases"] = [alias, alias]
    (root / "pack.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(CanonicalPackValidationError, match="duplicates public identity"):
        validate_canonical_pack(root)


@pytest.mark.parametrize('value', [3.0, 0.15, -2.5])
def test_v3_action_preserves_finite_fractional_input_defaults(tmp_path: Path, value: float) -> None:
    action = _action()
    action['inputs'] = [{'name': 'context', 'type': 'number', 'default': value}]
    root = _pack(tmp_path, files={'actions/echo.py': 'def echo(context): return context\n'},
                 actions={'echo': action})
    entry = validate_canonical_pack(root)
    assert entry.definition.to_dict()['actions']['echo']['inputs'][0]['default'] == value


@pytest.mark.parametrize('value', [float('inf'), float('nan')])
def test_v3_action_rejects_nonfinite_input_defaults(tmp_path: Path, value: float) -> None:
    action = _action()
    action['inputs'] = [{'name': 'context', 'type': 'number', 'default': value}]
    root = _pack(tmp_path, files={'actions/echo.py': 'def echo(context): return context\n'},
                 actions={'echo': action})
    with pytest.raises(CanonicalPackValidationError, match='finite JSON numbers'):
        validate_canonical_pack(root)
