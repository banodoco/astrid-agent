from __future__ import annotations

from pathlib import Path

import pytest

from astrid.pack_imports import ImportContract, check_pack_imports


def _pack(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    (root / "actions" / "nested").mkdir(parents=True)
    return root


def _manifest() -> dict[str, object]:
    return {
        "id": "demo",
        "dependencies": {"packs": [], "python_requirements": []},
    }


def test_declared_optional_namespace_does_not_authorize_siblings(tmp_path):
    pack = _pack(tmp_path)
    (pack / "entry.py").write_text(
        "from pyannote.audio import Pipeline\nimport pyannote.audio.core\n"
        "import pyannote.database\nimport pyannote\n", encoding="utf-8"
    )
    manifest = _manifest()
    manifest["dependencies"] = {"packs": [], "python_requirements": [
        "pyannote.audio>=3; extra == 'diarize'"
    ]}
    found = check_pack_imports(pack, contract=ImportContract(), manifest=manifest)
    assert [(item.module, item.code) for item in found] == [
        ("pyannote.database", "undeclared_python"), ("pyannote", "undeclared_python")
    ]
    manifest["dependencies"] = {"packs": [], "python_requirements": []}
    assert len(check_pack_imports(pack, contract=ImportContract(), manifest=manifest)) == 4


def test_default_distribution_mapping_requires_real_declared_packages(tmp_path):
    pack = _pack(tmp_path)
    (pack / "entry.py").write_text(
        "import PIL.Image\nfrom yaml import safe_load\nimport astrid.core.secret\n",
        encoding="utf-8",
    )
    manifest = _manifest()
    manifest["dependencies"] = {"packs": [], "python_requirements": ["Pillow>=9", "PyYAML>=6"]}
    found = check_pack_imports(pack, contract=ImportContract(), manifest=manifest)
    assert [(item.module, item.code) for item in found] == [("astrid.core.secret", "deep_import")]

    manifest["dependencies"] = {"packs": [], "python_requirements": []}
    found = check_pack_imports(pack, contract=ImportContract(), manifest=manifest)
    assert [(item.module, item.code) for item in found] == [
        ("PIL.Image", "undeclared_python"), ("yaml", "undeclared_python"),
        ("astrid.core.secret", "deep_import"),
    ]

    # Import names are not substitute distributions, even if similarly named
    # distributions happen to exist on an ambient machine.
    manifest["dependencies"] = {"packs": [], "python_requirements": ["PIL", "yaml"]}
    found = check_pack_imports(pack, contract=ImportContract(), manifest=manifest)
    assert [item.code for item in found] == ["undeclared_python", "undeclared_python", "deep_import"]


def test_relative_imports_resolve_from_each_source_file_and_allow_own_pack_private_modules(tmp_path):
    pack = _pack(tmp_path)
    (pack / "entry.py").write_text("from .helpers import make\n", encoding="utf-8")
    (pack / "actions" / "run.py").write_text("from .helpers import make\n", encoding="utf-8")
    (pack / "actions" / "nested" / "run.py").write_text(
        "from ..helpers import make\n", encoding="utf-8"
    )

    assert check_pack_imports(pack, contract=ImportContract(), manifest=_manifest()) == []


def test_relative_import_that_climbs_above_pack_root_is_rejected(tmp_path):
    pack = _pack(tmp_path)
    (pack / "entry.py").write_text("from .....outside import secret\n", encoding="utf-8")
    (pack / "actions" / "nested" / "escape.py").write_text(
        "from ........otherpack.private import secret\n", encoding="utf-8"
    )

    found = check_pack_imports(pack, contract=ImportContract(), manifest=_manifest())

    assert [(item.path, item.line, item.code) for item in found] == [
        ("actions/nested/escape.py", 1, "relative_escape"),
        ("entry.py", 1, "relative_escape"),
    ]
    assert all("within the pack" in item.fix for item in found)


def test_relative_import_resolution_is_independent_of_other_files(tmp_path):
    pack = _pack(tmp_path)
    (pack / "actions" / "nested" / "first.py").write_text(
        "from ..helpers import build\n", encoding="utf-8"
    )
    (pack / "actions" / "second.py").write_text(
        "from .....outside import build\n", encoding="utf-8"
    )

    found = check_pack_imports(pack, contract=ImportContract(), manifest=_manifest())

    assert [(item.path, item.code) for item in found] == [("actions/second.py", "relative_escape")]


def test_relative_sibling_pack_imports_obey_declared_public_api_policy(tmp_path):
    pack = _pack(tmp_path)
    # From this nested module, four dots climb to astrid.packs and select a
    # sibling pack. Its public API is permitted only when declared.
    source = pack / "actions" / "nested" / "uses_sibling.py"
    source.write_text(
        "from ....sibling.api import public_call\n"
        "from ....sibling.private import implementation\n"
        "from .... import undeclared\n",
        encoding="utf-8",
    )

    declared = {
        "id": "demo",
        "dependencies": {"packs": ["sibling"], "python_requirements": []},
    }
    found = check_pack_imports(pack, contract=ImportContract(), manifest=declared)

    assert [(item.module, item.code) for item in found] == [
        ("astrid.packs.sibling.private", "deep_import"),
        ("astrid.packs.undeclared", "undeclared_pack"),
    ]


def test_relative_import_can_use_declared_pack_public_api(tmp_path):
    pack = _pack(tmp_path)
    (pack / "entry.py").write_text("from ..audio.api import render\n", encoding="utf-8")
    manifest = _manifest()
    manifest["dependencies"] = {"packs": ["audio"], "python_requirements": []}

    assert check_pack_imports(pack, contract=ImportContract(), manifest=manifest) == []


def test_relative_import_to_undeclared_or_private_pack_target_explains_policy(tmp_path):
    pack = _pack(tmp_path)
    (pack / "entry.py").write_text(
        "from ..audio.api import render\nfrom ..audio.internal import helper\n",
        encoding="utf-8",
    )

    found = check_pack_imports(pack, contract=ImportContract(), manifest=_manifest())

    assert [(item.module, item.code) for item in found] == [
        ("astrid.packs.audio.api", "undeclared_pack"),
        ("astrid.packs.audio.internal", "undeclared_pack"),
    ]
    assert all("dependencies.packs" in item.fix for item in found)


def test_relative_import_to_declared_private_pack_module_is_rejected(tmp_path):
    pack = _pack(tmp_path)
    (pack / "entry.py").write_text("from ..audio.internal import helper\n", encoding="utf-8")
    manifest = _manifest()
    manifest["dependencies"] = {"packs": ["audio"], "python_requirements": []}

    found = check_pack_imports(pack, contract=ImportContract(), manifest=manifest)

    assert [(item.module, item.code) for item in found] == [
        ("astrid.packs.audio.internal", "deep_import"),
    ]
    assert "public api" in found[0].fix


def test_relative_facade_import_checks_exported_names(tmp_path):
    pack = _pack(tmp_path)
    (pack / "entry.py").write_text(
        "from ...pack_api import PublicName\nfrom ...pack_api import HiddenName\n",
        encoding="utf-8",
    )
    contract = ImportContract(facade_exports=frozenset({"PublicName"}))
    found = check_pack_imports(pack, contract=contract, manifest=_manifest())
    assert [(item.line, item.module, item.code) for item in found] == [
        (2, "astrid.pack_api.HiddenName", "deep_import"),
    ]


def test_production_caller_supplies_personal_pins_markers(tmp_path, monkeypatch):
    """PK-7: the production inventory caller hands the pure validator its trust facts.

    The canonical personal root, the release default pins and the running
    interpreter's PEP 508 marker environment are supplied by the caller (never
    re-derived by the validator), so a marker-qualified requirement is judged
    against the interpreter that will execute the pack.
    """
    import sys

    import yaml
    from packaging.markers import default_environment

    from astrid.pack_api import VERSION
    from astrid.core.pack import loader

    bundled = tmp_path / "bundled"
    bundled.mkdir()
    root = tmp_path / "external" / "demo"
    root.mkdir(parents=True)
    (root / "pack.yaml").write_text(yaml.safe_dump({
        "schema_version": 3, "id": "demo", "name": "demo", "version": "1.0.0", "pack_api": f"{VERSION[0]}.{VERSION[1]}",
        "documentation": {"kind": "none", "reason": "PK-7 fixture."},
        "dependencies": {"packs": [], "python_requirements": [f"pytest; python_version >= '{sys.version_info[0]}.0'"]},
    }), encoding="utf-8")
    monkeypatch.setattr(loader, "packs_root", lambda: bundled)
    captured = []
    real = loader.validate_pack_compatibility

    def spy(**kwargs):
        captured.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(loader, "validate_pack_compatibility", spy)
    scan = loader.scan_packs(root.parent, strict=False, personal_packs_root=tmp_path / "home-packs")
    assert [pack.id for pack in scan.packs] == ["demo"], [record.error for record in scan.quarantined]
    (call,) = captured
    assert call["canonical_personal_root"] == str((tmp_path / "home-packs" / "personal").resolve())
    assert isinstance(call["release_pins"], dict)
    assert call["python_environment"] == default_environment()


def test_private_and_unknown_sdk_imports_are_rejected(tmp_path):
    """Ported from the retired import_compat checker's SDK assertions."""
    pack = _pack(tmp_path)
    contract = ImportContract(sdk_names=frozenset({"dto"}))
    for statement, module in (
        ("from astrid.sdk._private import value\n", "astrid.sdk._private"),
        ("from astrid.sdk import _private\n", "astrid.sdk._private"),
        ("from astrid.sdk.missing import value\n", "astrid.sdk.missing"),
    ):
        (pack / "entry.py").write_text(statement, encoding="utf-8")
        found = check_pack_imports(pack, contract=contract, manifest=_manifest())
        assert [(item.module, item.code) for item in found] == [(module, "deep_import")], statement
    (pack / "entry.py").write_text("from astrid.sdk import dto\n", encoding="utf-8")
    assert check_pack_imports(pack, contract=contract, manifest=_manifest()) == []


# A script run from the pack root (a raw-command renderer, a command action, ...)
# reaches its helpers by absolute top-level imports; the same rule applies to
# every entrypoint kind.


def test_pack_root_sibling_module_and_package_are_pack_private(tmp_path):
    pack = _pack(tmp_path)
    (pack / "_demo_pack_shared.py").write_text("value = 1\n", encoding="utf-8")
    (pack / "demo_pack_helpers").mkdir()
    (pack / "demo_pack_helpers" / "__init__.py").write_text("", encoding="utf-8")
    (pack / "demo_pack_helpers" / "util.py").write_text("x = 1\n", encoding="utf-8")
    (pack / "entry.py").write_text(
        "import _demo_pack_shared\nfrom _demo_pack_shared import value\nimport demo_pack_helpers\n"
        "import demo_pack_helpers.util\nfrom demo_pack_helpers.util import x\n"
        "from demo_pack_helpers import util\n",
        encoding="utf-8",
    )
    assert check_pack_imports(pack, contract=ImportContract(), manifest=_manifest()) == []


def test_module_only_outside_the_pack_root_is_still_undeclared(tmp_path):
    pack = _pack(tmp_path)
    (tmp_path / "outside_helper.py").write_text("value = 1\n", encoding="utf-8")
    # A dotted import is pack-private only when the top-level name is a package in the pack.
    (pack / "flat.py").write_text("value = 1\n", encoding="utf-8")
    # A module in a subdirectory is not at the pack root.
    (pack / "actions" / "nested" / "deep_helper.py").write_text("value = 1\n", encoding="utf-8")
    (pack / "entry.py").write_text(
        "import outside_helper\nimport flat.missing\nimport deep_helper\n", encoding="utf-8"
    )
    found = check_pack_imports(pack, contract=ImportContract(), manifest=_manifest())
    assert [(item.module, item.code) for item in found] == [
        ("outside_helper", "undeclared_python"),
        ("flat.missing", "undeclared_python"),
        ("deep_helper", "undeclared_python"),
    ]


def test_pack_root_symlink_pointing_outside_the_pack_is_not_pack_private(tmp_path):
    pack = _pack(tmp_path)
    outside = tmp_path / "elsewhere"
    (outside / "demo_linked_pkg").mkdir(parents=True)
    (outside / "demo_linked_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (outside / "demo_linked_mod.py").write_text("value = 1\n", encoding="utf-8")
    (pack / "demo_linked_mod.py").symlink_to(outside / "demo_linked_mod.py")
    (pack / "demo_linked_pkg").symlink_to(outside / "demo_linked_pkg", target_is_directory=True)
    (pack / "entry.py").write_text("import demo_linked_mod\nimport demo_linked_pkg\n", encoding="utf-8")
    found = check_pack_imports(pack, contract=ImportContract(), manifest=_manifest())
    assert [(item.module, item.code) for item in found] == [
        ("demo_linked_mod", "undeclared_python"),
        ("demo_linked_pkg", "undeclared_python"),
    ]


def test_symlink_below_the_top_level_pointing_outside_the_pack_is_not_pack_private(tmp_path):
    """Every dotted component must stay in the pack, so no outside file is accepted unscanned."""
    pack = _pack(tmp_path)
    outside = tmp_path / "elsewhere" / "sub"
    outside.mkdir(parents=True)
    (outside / "__init__.py").write_text("", encoding="utf-8")
    (outside / "mod.py").write_text("import totally_undeclared\n", encoding="utf-8")
    (pack / "demo_pkg").mkdir()
    (pack / "demo_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (pack / "demo_pkg" / "sub").symlink_to(outside, target_is_directory=True)
    (pack / "entry.py").write_text(
        "import demo_pkg\nimport demo_pkg.sub.mod\nfrom demo_pkg import sub\n", encoding="utf-8"
    )
    found = check_pack_imports(pack, contract=ImportContract(), manifest=_manifest())
    assert [(item.module, item.code) for item in found] == [
        ("demo_pkg.sub.mod", "undeclared_python"),  # line 2
        ("demo_pkg.sub", "undeclared_python"),      # line 3: from demo_pkg import sub
    ]


def test_pack_root_module_shadowing_a_declared_requirement_is_refused(tmp_path):
    pack = _pack(tmp_path)
    (pack / "requests.py").write_text("value = 1\n", encoding="utf-8")
    (pack / "yaml").mkdir()
    (pack / "yaml" / "__init__.py").write_text("", encoding="utf-8")
    (pack / "entry.py").write_text("import requests\nimport yaml\n", encoding="utf-8")
    manifest = _manifest()
    manifest["dependencies"] = {"packs": [], "python_requirements": ["requests>=2", "PyYAML>=6"]}
    found = check_pack_imports(pack, contract=ImportContract(), manifest=manifest)
    assert [(item.module, item.code) for item in found] == [
        ("requests", "shadowed_dependency"),
        ("yaml", "shadowed_dependency"),
    ]
    assert "shadows the declared Python requirement 'requests'" in found[0].fix
    assert "'pyyaml'" in found[1].fix


@pytest.mark.parametrize("name", ["vibecomfy", "astrid_runtime", "runtime_protocol", "astrid_extra"])
def test_pack_root_decoy_of_a_reserved_name_is_refused(tmp_path, name):
    """Reserved names are refused whether or not the real package is installed here."""
    pack = _pack(tmp_path)
    (pack / name).mkdir()
    (pack / name / "__init__.py").write_text("", encoding="utf-8")
    (pack / "entry.py").write_text(f"import {name}.anything\n", encoding="utf-8")
    found = check_pack_imports(pack, contract=ImportContract(), manifest=_manifest())
    assert [(item.module, item.code) for item in found] == [(f"{name}.anything", "shadowed_dependency")]
    assert "reserved for Astrid and its runtimes" in found[0].fix


def test_pack_root_decoy_of_a_host_importable_package_is_refused(tmp_path, monkeypatch):
    """An empty pack-root torch/ must not stand in for an installed torch the runtime would load."""
    from astrid import pack_imports

    real = pack_imports._host_can_import
    monkeypatch.setattr(
        pack_imports, "_host_can_import",
        lambda name, *, pack_root: name == "torch" or real(name, pack_root=pack_root),
    )
    pack = _pack(tmp_path)
    for name in ("torch", "pytest"):  # torch simulated as installed; pytest really is
        (pack / name).mkdir()
        (pack / name / "__init__.py").write_text("", encoding="utf-8")
    (pack / "entry.py").write_text("import torch.nn\nfrom pytest import fixture\n", encoding="utf-8")
    found = check_pack_imports(pack, contract=ImportContract(), manifest=_manifest())
    assert [(item.module, item.code) for item in found] == [
        ("torch.nn", "shadowed_dependency"),
        ("pytest", "shadowed_dependency"),
    ]
    assert all("importable on this host" in item.fix for item in found)


def test_host_import_lookup_excludes_the_pack_root(tmp_path, monkeypatch):
    """The pack's own directory on sys.path does not make its helper look installed."""
    from astrid import pack_imports

    pack = _pack(tmp_path)
    (pack / "_demo_only_in_pack.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(pack))
    assert pack_imports._host_can_import("_demo_only_in_pack", pack_root=pack) is False
    assert pack_imports._host_can_import("json", pack_root=pack) is True


def test_pack_root_astrid_package_does_not_bypass_the_sdk_surface(tmp_path):
    pack = _pack(tmp_path)
    (pack / "astrid" / "sdk").mkdir(parents=True)
    (pack / "astrid" / "__init__.py").write_text("", encoding="utf-8")
    (pack / "astrid" / "sdk" / "__init__.py").write_text("", encoding="utf-8")
    (pack / "entry.py").write_text("from astrid.sdk.rendering import renderer_main\n", encoding="utf-8")
    found = check_pack_imports(pack, contract=ImportContract(), manifest=_manifest())
    assert [(item.module, item.code) for item in found] == [("astrid.sdk.rendering", "deep_import")]
