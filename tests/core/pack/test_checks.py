"""Pack V3 timeline Check discovery and invocation use the canonical admission route."""
from __future__ import annotations

import pytest
import yaml

from astrid.core.pack.canonical import CanonicalPackValidationError, validate_canonical_pack
from astrid.core.pack.checks import CheckHostContext, UnsupportedCheck, invoke_check, resolve_check
from astrid.packs.rendering.actions.timeline_visualize.layers.base import CheckContext
from astrid.sdk.timeline.commands import CommandError

IDENTITY = "timeline.check/demo.damaged-art"


@pytest.fixture
def check_pack(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    packs = tmp_path / "packs"
    root = packs / "demo"
    descriptor_path = "extensions/timeline.check/damaged_art.yaml"
    implementation_path = "extensions/timeline.check/damaged_art.py"
    descriptor = {
        "point": "timeline.check",
        "id": "demo.damaged-art",
        "label": "Damaged pixel art",
        "description": "Report selected sprite measurements.",
        "scope": "timeline",
        "reads": ["timeline.document", "timeline.selected_assets"],
        "finding": {"code": "DAMAGED-ART", "severity": "warn", "location": "message"},
        "thresholds": {"damaged_art_below": 0.75},
        "implementation": {"path": implementation_path, "function": "run"},
    }
    descriptor_file = root / descriptor_path
    implementation_file = root / implementation_path
    descriptor_file.parent.mkdir(parents=True, exist_ok=True)
    implementation_file.parent.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "implementation-ran"
    marker.unlink(missing_ok=True)
    implementation_file.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran')\n"
        "def run(ctx):\n"
        "    ctx.document\n"
        "    return [f\"{row['address']}: {row['asset']} {row['outline_integrity']:.2f}\" "
        "for row in ctx.selected_assets()]\n",
        encoding="utf-8",
    )
    descriptor_file.write_text(yaml.safe_dump(descriptor), encoding="utf-8")
    (root / "pack.yaml").write_text(yaml.safe_dump({
        "schema_version": 3,
        "id": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "extensions": {"timeline": {"checks": [descriptor_path]}},
        "resources": [
            {"kind": "support", "path": descriptor_path},
            {"kind": "implementation", "path": implementation_path},
        ],
    }), encoding="utf-8")
    kwargs = {"project_root": tmp_path / "empty-project", "extra_pack_roots": (str(packs),)}
    return resolve_check(IDENTITY, **kwargs), kwargs, marker


def test_discovery_reads_metadata_without_running_contribution(check_pack):
    check, _kwargs, marker = check_pack
    assert check.identity == IDENTITY
    assert check.eligible and check.source_kind == "extra"
    assert check.descriptor["scope"] == "timeline"
    assert check.descriptor["reads"] == ["timeline.document", "timeline.selected_assets"]
    assert check.finding == {"code": "DAMAGED-ART", "severity": "warn", "location": "message"}
    assert not marker.exists()


def test_existing_check_runner_adapts_descriptor_metadata_once(check_pack):
    check, _kwargs, marker = check_pack
    from astrid.core.pack.checks import registered_checks

    adapted = next(
        item for item in registered_checks(
            project_root=check.pack_root.parent.parent,
            extra_pack_roots=(str(check.pack_root.parent),),
        )
        if item.name == "demo-damaged-art"
    )
    host = CheckHostContext("sdk", {"timeline_id": "t"},
                            lambda: [{"address": "c1.rocket", "asset": "R", "outline_integrity": 0.70}])
    context = CheckContext(cut=None, cuts=[], fps=30, elements=[], all_elements=[], words=[], beats={}, sfx=[],
                           tracks=[], params={}, extension_context=host)
    findings = adapted.run(context)
    assert [(finding.code, finding.severity, finding.message) for finding in findings] == [
        ("DAMAGED-ART", "warn", "c1.rocket: R 0.70")
    ]
    assert findings[0].line() == "DAMAGED-ART c1.rocket: R 0.70"
    assert marker.exists()


@pytest.mark.parametrize("host_context,host", [
    (None, "sdk"),
    (CheckHostContext("app", {}, lambda: []), "sdk"),
    (CheckHostContext("sdk", {}, None), "sdk"),
    (CheckHostContext("sdk", {}, lambda: []), "app"),
])
def test_unavailable_check_host_refuses_before_contribution_execution(check_pack, host_context, host):
    check, _kwargs, marker = check_pack
    with pytest.raises(UnsupportedCheck, match="unsupported|unavailable"):
        invoke_check(check, host_context, host=host)
    assert not marker.exists()


def test_unknown_identity_and_changed_resource_are_refused(check_pack):
    check, kwargs, marker = check_pack
    for identity in ("damaged-art", "timeline.view/demo.damaged-art", "timeline.check/missing.check"):
        with pytest.raises(UnsupportedCheck, match="unsupported"):
            resolve_check(identity, **kwargs)
    implementation = check.pack_root / check.descriptor["implementation"]["path"]
    implementation.write_text("def run(ctx):\n    return ['changed']\n", encoding="utf-8")
    with pytest.raises(CommandError, match="implementation changed"):
        invoke_check(check, CheckHostContext("sdk", {}, lambda: []))
    assert not marker.exists()


def test_descriptor_and_implementation_must_be_admitted_resources(check_pack):
    check, _kwargs, _marker = check_pack
    manifest = check.pack_root / "pack.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["resources"] = [row for row in data["resources"] if row["path"] != check.descriptor_path]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(CanonicalPackValidationError, match="declared file resource"):
        validate_canonical_pack(check.pack_root)


def test_unadmitted_implementation_identity_is_rejected(check_pack):
    check, _kwargs, _marker = check_pack
    descriptor_path = check.pack_root / check.descriptor_path
    descriptor = yaml.safe_load(descriptor_path.read_text(encoding="utf-8"))
    descriptor["implementation"]["path"] = "extensions/timeline.check/neighbour.py"
    (check.pack_root / descriptor["implementation"]["path"]).write_text(
        "def run(ctx): return ['must not run']\n", encoding="utf-8")
    descriptor_path.write_text(yaml.safe_dump(descriptor), encoding="utf-8")
    with pytest.raises(CanonicalPackValidationError, match="declared Python file resource"):
        validate_canonical_pack(check.pack_root)
