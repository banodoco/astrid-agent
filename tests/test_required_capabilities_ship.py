"""A required capability ships with Astrid; a moved pack is optional and says so.

Release 11 shipped a matrix that still required 38 capabilities whose packs had
moved to poms_packs. The host dropped those rows with one log line, so a fresh
install had no editing and every check that would have named them was quiet.
These tests keep the matrix and the shipped packs in agreement.
"""

from __future__ import annotations

import json
from pathlib import Path

from astrid.core.execution.generic_host import GenericPackHost

ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "astrid" / "packs"
MATRIX = ROOT / "config" / "astrid-beta-capabilities.json"


def _rows() -> list[dict]:
    return json.loads(MATRIX.read_text(encoding="utf-8"))["capabilities"]


def _shipped_host() -> GenericPackHost:
    host = GenericPackHost(pack_roots=[PACKS], capability_matrix=MATRIX)
    host.discover()
    return host


def test_every_required_row_has_a_shipped_executor():
    host = _shipped_host()
    required = {row["id"] for row in _rows() if row["disposition"] == "required"}

    missing = sorted(required - set(host.capabilities))

    assert missing == [], f"required capabilities with no shipped executor: {missing}"
    assert host.missing_required_capabilities == ()


def test_a_row_for_a_pack_that_does_not_ship_is_optional_and_names_the_pack():
    host = _shipped_host()
    shipped = {capability_id.split(".", 1)[0] for capability_id in host.capabilities}

    for row in _rows():
        pack = row["id"].split(".", 1)[0]
        if pack in shipped:
            assert "external_pack" not in row, f"{row['id']}: the pack ships, so the row is not external"
            continue
        assert row.get("external_pack") == pack, f"{row['id']}: no shipped pack and no external_pack"
        assert row["disposition"] == "optional", f"{row['id']}: a pack that does not ship cannot be required"
        assert f"astrid packs add {pack}" in row["evidence_reason"], row["id"]


def test_the_shipped_matrix_has_no_unexplained_row():
    # Every row is either discovered or declared external; nothing is dropped by accident.
    assert _shipped_host().stale_matrix_entries == ()


def test_the_host_names_a_missing_required_row_and_expects_an_external_one(tmp_path):
    row = {"evidence_reason": "test fixture", "adapter_family": "cpu", "resource_keys": ["cpu"],
           "required_env": [], "required_binaries": [], "required_packages": []}
    matrix = tmp_path / "config" / "matrix.json"
    matrix.parent.mkdir()
    matrix.write_text(json.dumps({"schema_version": 1, "capabilities": [
        {"id": "ghost.gone", "disposition": "required", **row},
        {"id": "ghost.extra", "disposition": "optional", **row},
        {"id": "moved.thing", "disposition": "optional", "external_pack": "moved", **row},
    ]}), encoding="utf-8")
    (tmp_path / "packs").mkdir()

    host = GenericPackHost(pack_roots=[tmp_path / "packs"], capability_matrix=matrix)
    host.discover()

    assert host.stale_matrix_entries == ("ghost.extra", "ghost.gone")
    assert host.missing_required_capabilities == ("ghost.gone",)


def test_a_capability_whose_dependency_pack_is_absent_is_not_ready():
    host = _shipped_host()
    host.preflight()
    shipped = {capability_id.split(".", 1)[0] for capability_id in host.capabilities}

    checked = 0
    for record in host.capabilities.values():
        absent = sorted({
            dependency.split(".", 1)[0]
            for dependency in record.definition.graph.depends_on
            if "." in dependency and dependency.split(".", 1)[0] not in shipped
        })
        if not absent:
            continue
        checked += 1
        assert record.ready is False, f"{record.id} reads ready without {absent}"
        assert record.preflight["packs"] == {"ok": False, "missing": absent}
        assert record.matrix["disposition"] != "required", f"{record.id} is required but needs {absent}"
    assert checked, "training.pool_build and pool_merge depend on moved packs"
