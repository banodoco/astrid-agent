"""Focused admission checks for source and installed timeline schema copies."""

import hashlib
import json

import pytest

from astrid.installer.backend import Backend


SCHEMA = "astrid/core/timeline/schema/timeline-config.schema.json"


@pytest.fixture
def schema_roots(tmp_path):
    agent = tmp_path / "agent"
    product = tmp_path / "site-packages"
    content = b'{"type":"object"}\n'
    for root in (agent, product):
        path = root / SCHEMA
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
    (agent / "runtime.lock.json").write_text(json.dumps({"files": {SCHEMA: {
        "from": "contract/schemas/timeline-config.schema.json",
        "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
    }}}))
    return agent, product


def test_matching_source_and_installed_schema_are_admitted(schema_roots):
    Backend._verify_local_timeline_schema(*schema_roots)


@pytest.mark.parametrize("root_index", [0, 1], ids=["source", "installed"])
@pytest.mark.parametrize("fault", ["missing", "symlink", "mismatch"])
def test_schema_admission_rejects_invalid_copies(schema_roots, root_index, fault, tmp_path):
    path = schema_roots[root_index] / SCHEMA
    if fault == "missing":
        path.unlink()
    elif fault == "symlink":
        target = tmp_path / "target.json"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    else:
        path.write_text("{}")
    with pytest.raises(ValueError, match="timeline schema"):
        Backend._verify_local_timeline_schema(*schema_roots)


def test_schema_admission_rejects_unapproved_source(schema_roots):
    agent, product = schema_roots
    lock = agent / "runtime.lock.json"
    data = json.loads(lock.read_text())
    data["files"][SCHEMA]["from"] = "foreign/schema.json"
    lock.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="approved Runtime source pin"):
        Backend._verify_local_timeline_schema(agent, product)
