from __future__ import annotations

from pathlib import Path

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
