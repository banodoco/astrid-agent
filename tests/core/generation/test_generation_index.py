import json
from pathlib import Path
from types import SimpleNamespace

import yaml

from astrid.core.execution.executor.registry import load_default_registry as load_executors
from astrid.core.execution.orchestrator.registry import load_default_registry as load_orchestrators
from astrid.core.generation import listing
from scripts import gen_capability_index


def test_capability_index_excludes_installed_external_pack_rows(monkeypatch, tmp_path):
    rows = [
        SimpleNamespace(
            id="bundled.render",
            short_description="Bundled executor",
            description="",
            metadata={"pack_root": gen_capability_index.PACKS_ROOT / "rendering"},
        ),
        SimpleNamespace(
            id="personal.render",
            short_description="Personal executor",
            description="",
            metadata={"pack_root": tmp_path / "personal"},
        ),
    ]
    monkeypatch.setattr(
        gen_capability_index,
        "load_executors",
        lambda: SimpleNamespace(list=lambda: rows),
    )

    assert gen_capability_index._executor_rows() == [("bundled.render", "Bundled executor")]


def test_generation_list_public_command_uses_shared_catalog(monkeypatch, capsys):
    from astrid import console

    monkeypatch.setattr(listing, "catalog_rows", lambda: [{
        "id": "z-image", "kind": "image", "mode": "t2i",
        "backend": "local", "availability": "unavailable", "reason": "test | reason",
        "template": "image/z_image_turbo", "source": "catalog",
    }])

    assert console.main(["generate", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [{
        "id": "z-image", "kind": "image", "mode": "t2i",
        "backend": "local", "availability": "unavailable", "reason": "test | reason",
        "template": "image/z_image_turbo", "source": "catalog",
        "target": "declaration", "ready": False,
        "fix": "Use an implemented generation route; see the availability declaration.",
        "models_present": [],
    }]


def test_real_installed_pack_is_discovered_but_not_written_to_bundled_index(
    monkeypatch, tmp_path
):
    installed_root = tmp_path / "installed-packs"
    pack_root = installed_root / "installed_fixture"
    pack_root.mkdir(parents=True)
    manifest = {
        "schema_version": 3,
        "id": "installed_fixture",
        "name": "Installed Fixture",
        "version": "1.0.0",
        "pack_api": "1.0",
        "status": "active",
        "domain": "media",
        "stability": "experimental",
        "support": "core",
        "visibility": "visible",
        "description": "Temporary installed-only registry fixture.",
        "capabilities": [],
        "actions": {
            "child": {
                "name": "Installed child",
                "version": "1.0.0",
                "short_description": "Installed child action",
                "description": "Installed child action",
                "invocation": {"kind": "command", "command": {"argv": ["python", "-c", "pass"]}},
                "inputs": [],
                "outputs": [],
                "isolation": {"mode": "subprocess", "network": False},
                "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"]},
            },
            "parent": {
                "name": "Installed parent",
                "version": "1.0.0",
                "short_description": "Installed parent orchestrator",
                "description": "Installed parent orchestrator",
                "invocation": {"kind": "command", "command": {"argv": ["python", "-c", "pass"]}},
                "inputs": [],
                "outputs": [],
                "isolation": {"mode": "subprocess", "network": False},
                "metadata": {
                    "adapter_family": "cpu",
                    "resource_keys": ["cpu"],
                    "local_orchestrator": True,
                },
                "graph": {"child_actions": ["installed_fixture.child"], "child_orchestrators": []},
            },
        },
    }
    (pack_root / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(installed_root))
    monkeypatch.setenv("ASTRID_HOME", str(tmp_path / "astrid-home"))

    executors = load_executors()
    installed_executors = [
        item for item in executors.list()
        if Path(item.metadata["pack_root"]).resolve() == pack_root.resolve()
    ]
    assert len(installed_executors) == 1
    assert [item.id for item in installed_executors] == ["installed_fixture.child"]

    orchestrators = load_orchestrators(executor_registry=executors)
    installed_orchestrators = [
        item for item in orchestrators.list()
        if Path(item.metadata["pack_root"]).resolve() == pack_root.resolve()
    ]
    assert len(installed_orchestrators) == 1
    assert [item.id for item in installed_orchestrators] == ["installed_fixture.parent"]

    block = gen_capability_index.render_block()
    assert "installed_fixture.child" not in block
    assert "installed_fixture.parent" not in block


def test_committed_capability_index_is_generated_from_registry():
    path = Path(__file__).resolve().parents[3] / "astrid/packs/_core/docs/references/capabilities.md"
    text = path.read_text(encoding="utf-8")
    assert gen_capability_index.render_block() in text
