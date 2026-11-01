"""Source-side Timeline schema admission; installed-consumer proof is separate."""

import hashlib
import json
from pathlib import Path

import pytest

from astrid.installer.backend import Backend


RELATIVE = Path("astrid/core/timeline/schema/timeline-config.schema.json")
PRODUCER_PATH = "contract/schemas/timeline-config.schema.json"


def _staged_agent(tmp_path: Path) -> Path:
    agent = tmp_path / "agent"
    schema = agent / RELATIVE
    schema.parent.mkdir(parents=True)
    schema.write_bytes(b'{"type":"object"}\n')
    pin = {"from": PRODUCER_PATH, "sha256": "sha256:" + hashlib.sha256(schema.read_bytes()).hexdigest()}
    (agent / "runtime.lock.json").write_text(json.dumps({"files": {str(RELATIVE): pin}}), encoding="utf-8")
    return agent


def test_matching_source_schema_is_admitted(tmp_path):
    Backend._verify_local_timeline_schema(_staged_agent(tmp_path))


@pytest.mark.parametrize("change", ["missing", "symlink", "mismatch", "wrong_source"])
def test_invalid_source_schema_is_rejected(tmp_path, change):
    agent = _staged_agent(tmp_path)
    schema = agent / RELATIVE
    if change == "missing":
        schema.unlink()
    elif change == "symlink":
        target = tmp_path / "other.json"
        target.write_bytes(schema.read_bytes())
        schema.unlink()
        schema.symlink_to(target)
    elif change == "mismatch":
        schema.write_bytes(b"{}")
    else:
        lock_path = agent / "runtime.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["files"][str(RELATIVE)]["from"] = "other/schema.json"
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(ValueError):
        Backend._verify_local_timeline_schema(agent)
