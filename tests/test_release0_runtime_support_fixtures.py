"""Checks for the source-modeled R0 bootstrap support records."""

import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).parent / "fixtures" / "release0" / "runtime-support"


class Release0RuntimeSupportFixtures(unittest.TestCase):
    def test_fixture_checksums_and_runtime_serialization(self):
        for line in (ROOT / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            digest, name = line.split("  ")
            data = (ROOT / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
            value = json.loads(data)
            self.assertEqual(data, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())

    def test_bootstrap_discovery_and_marker_shapes_match(self):
        discovery = json.loads((ROOT / "discovery.json").read_text(encoding="utf-8"))
        marker = json.loads((ROOT / "instance.lock").read_text(encoding="utf-8"))
        self.assertEqual(set(marker), {"pid", "process_birth_id", "runtime_instance_id", "realm_id", "realm_root"})
        self.assertEqual(set(discovery), {
            "version", "endpoint", "pid", "process_birth_id", "runtime_instance_id",
            "active_realm", "realm_root", "coordinator_epoch", "protocol_version",
            "schema_version", "capability_digest", "credential_file", "worker_credential_file",
            "worker_credential_pending", "worker_actor", "worker_scopes", "advertised_at",
        })
        for key in ("pid", "process_birth_id", "runtime_instance_id", "realm_root"):
            self.assertEqual(discovery[key], marker[key])
        self.assertEqual(discovery["active_realm"], marker["realm_id"])
        self.assertEqual(discovery["coordinator_epoch"], marker["runtime_instance_id"])
        self.assertRegex(marker["process_birth_id"], r"^proc-start-ticks:[0-9]+$")
        self.assertEqual((discovery["version"], discovery["protocol_version"], discovery["schema_version"]),
                         (1, "workspace.v1", "workspace-schema-v1"))
        self.assertEqual(discovery["worker_actor"], "astrid-pack-host")
        self.assertEqual(discovery["worker_scopes"], [
            "handshake", "worker:register", "worker:execute", "projects:read", "tasks:read",
            "objects:read", "objects:write",
        ])
        self.assertNotIn("owner.lock", marker)


if __name__ == "__main__":
    unittest.main()
