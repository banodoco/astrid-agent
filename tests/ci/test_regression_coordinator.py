import ast
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ci"))

import regression_coordinator as coordinator
import regression_deployment as deployment
import regression_finalizer as finalizer


SHA_A = "a" * 40
SHA_B = "b" * 40
HASH_A = "1" * 64
HASH_B = "2" * 64


class CoordinatorFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "state.sqlite3"
        self.coordinator = coordinator.Coordinator(self.database)
        self.addCleanup(self.coordinator.close)

    @staticmethod
    def tuple_for(sha=SHA_A):
        return {"version": 1, "repos": {"agent": {"sha": sha}}}

    @staticmethod
    def broker_source(job="job-1"):
        return {"job_id": job, "job_yaml_sha256": HASH_A}

    @staticmethod
    def hosted_source(sha=SHA_A, run=1):
        return {
            "repository": "banodoco/astrid-agent", "workflow_path": ".github/workflows/regression.yml",
            "workflow_revision": SHA_B, "run_id": run, "run_attempt": 1, "job_id": 10 + run,
            "artifact_id": 20 + run, "artifact_name": "artifact-%d" % run, "head_sha": sha,
            "dispatch_correlation": "dispatch-%d" % run,
        }

    def plan(self, sha=SHA_A, *, two=True, broker_job="job-1"):
        values = [{
            "producer_id": "broker-1", "kind": "broker", "source": self.broker_source(broker_job),
            "checkout_shas": {"agent": sha}, "deadline": "2026-12-01T00:00:00Z",
            "evidence_kind": "junit", "reviewed_identity": HASH_B,
        }]
        if two:
            values.append({
                "producer_id": "hosted-1", "kind": "hosted", "source": self.hosted_source(sha),
                "checkout_shas": {"agent": sha}, "deadline": "2026-12-01T00:00:00Z",
                "evidence_kind": "junit", "reviewed_identity": HASH_B,
            })
        return values

    def register_open(self, batch="batch-1", generation=1, sha=SHA_A, **plan_kwargs):
        self.coordinator.register_batch(
            batch_id=batch, generation=generation, target_repository=coordinator.TARGET_REPOSITORY,
            target_sha=sha, tup=self.tuple_for(sha), producer_plan=self.plan(sha, **plan_kwargs),
        )
        self.coordinator.open_host_verdict(
            batch, {"host_id": "m-int-host", "external_id": "c06:%s" % batch}
        )


class SealedCoordinatorTests(CoordinatorFixture):
    def test_seals_closed_plan_and_never_accepts_unplanned_or_wrong_source(self):
        self.register_open()
        broker = self.broker_source()
        hosted = self.hosted_source()
        with self.assertRaisesRegex(coordinator.CoordinatorError, "immutable producer plan"):
            self.coordinator.bind_source("batch-1", "broker-1", "broker", broker | {"job_id": "other"})
        with self.assertRaisesRegex(coordinator.CoordinatorError, "closed producer plan"):
            self.coordinator.bind_source("batch-1", "new-producer", "broker", broker)
        self.coordinator.bind_source("batch-1", "broker-1", "broker", broker)
        self.coordinator.bind_source("batch-1", "hosted-1", "hosted", hosted)
        self.coordinator.record_terminal("batch-1", "broker-1", "done", {"test_exit": 1}, identity=broker)
        sealed = self.coordinator.record_terminal("batch-1", "hosted-1", "done", {"test_exit": 0}, identity=hosted)
        self.assertEqual(sealed["state"], coordinator.FINALIZE_READY)
        sealed_input = self.coordinator.begin_finalization("batch-1")
        self.assertEqual(sealed_input["producer_results"][0]["outcome"], "completed")
        self.assertNotIn("producer_plan", sealed_input)
        self.assertNotIn("command", json.dumps(sealed_input))

    def test_duplicate_terminal_is_idempotent_but_conflict_and_late_input_are_rejected(self):
        self.register_open(two=False)
        source = self.broker_source()
        self.coordinator.bind_source("batch-1", "broker-1", "broker", source)
        first = self.coordinator.record_terminal("batch-1", "broker-1", "done", {"test_exit": 0}, identity=source)
        duplicate = self.coordinator.record_terminal("batch-1", "broker-1", "done", {"test_exit": 0}, identity=source)
        self.assertEqual(first["invocation_id"], duplicate["invocation_id"])
        with self.assertRaises(coordinator.DuplicateConflict):
            self.coordinator.record_terminal("batch-1", "broker-1", "failed", {"test_exit": 0}, identity=source)
        with self.assertRaisesRegex(coordinator.CoordinatorError, "forbidden|sealing"):
            self.coordinator.bind_source("batch-1", "broker-1", "broker", source)

    def test_source_binding_is_unique_across_batches_and_later_generation_rejects_old_results(self):
        self.register_open(batch="old", generation=1, two=False)
        old_source = self.broker_source()
        self.coordinator.bind_source("old", "broker-1", "broker", old_source)
        self.register_open(batch="new", generation=2, two=False)
        with self.assertRaises(coordinator.StaleGeneration):
            self.coordinator.record_terminal("old", "broker-1", "done", {}, identity=old_source)
        with self.assertRaises(coordinator.DuplicateConflict):
            self.coordinator.bind_source("new", "broker-1", "broker", old_source)

    def test_cancellation_deadline_and_dead_worker_map_to_red_terminal_facts(self):
        self.register_open(batch="cancelled")
        cancelled = self.coordinator.cancel_batch("cancelled", "operator cancelled")
        self.assertEqual(cancelled["state"], coordinator.FINALIZE_READY)
        results = self.coordinator.begin_finalization("cancelled")["producer_results"]
        self.assertEqual({item["outcome"] for item in results}, {"cancelled"})
        self.assertEqual(coordinator.normalize_terminal_outcome("worker-death"), "infrastructure_failed")
        self.assertEqual(coordinator.normalize_terminal_outcome("interrupted"), "infrastructure_failed")
        self.assertEqual(coordinator.normalize_terminal_outcome("cancelled"), "cancelled")
        self.assertEqual(coordinator.normalize_terminal_outcome("timed-out"), "timed_out")

    def test_broker_directory_hash_is_authenticated_from_authoritative_bytes(self):
        self.register_open(two=False)
        job = self.root / "broker" / "jobs" / "job-1"
        job.mkdir(parents=True)
        contents = b"approved: job\n"
        (job / "job.yaml").write_bytes(contents)
        # Registration used a different planned digest, so the host's actual
        # authoritative bytes cannot be swapped in after registration.
        with self.assertRaisesRegex(coordinator.CoordinatorError, "immutable producer plan"):
            self.coordinator.bind_broker_directory("batch-1", "broker-1", self.root / "broker", "job-1")


class DeploymentAdapterTests(CoordinatorFixture):
    def setUp(self):
        super().setUp()
        self.coordinator.close()
        self.adapter = deployment.DeploymentAdapter(self.database, self.root / "invocations")
        self.coordinator = self.adapter.coordinator
        self.addCleanup(self.adapter.close)

    def ready_batch(self, name="ready"):
        self.register_open(batch=name, two=False)
        source = self.broker_source()
        self.coordinator.bind_source(name, "broker-1", "broker", source)
        self.coordinator.record_terminal(name, "broker-1", "done", {"test_exit": 0}, identity=source)

    def test_finalizer_runs_once_with_sealed_results_not_a_producer_command(self):
        self.ready_batch()
        calls = []

        def evaluator(sealed):
            calls.append(sealed)
            return {"result": "pass", "reasons": []}

        value = self.adapter.finalise("ready", evaluator=evaluator)
        self.assertEqual(value["state"], coordinator.VERDICT_READY)
        self.assertEqual(value["verdict"]["result"], "fail")
        self.assertEqual(len(calls), 0)
        with self.assertRaisesRegex(coordinator.CoordinatorError, "not ready"):
            self.adapter.finalise("ready", evaluator=evaluator)
        self.assertEqual(len(calls), 0)

    def test_recovery_after_started_marker_maps_dead_worker_to_failure_without_rerun(self):
        self.ready_batch("dead")
        sealed = self.coordinator.begin_finalization("dead")
        directory = self.adapter.invocation_directory(sealed)
        directory.mkdir(parents=True)
        (directory / "started.json").write_text("{}")
        called = []
        result = self.adapter.recover("dead", evaluator=lambda value: called.append(value))
        self.assertEqual(result["state"], coordinator.VERDICT_READY)
        self.assertEqual(result["verdict"]["result"], "fail")
        self.assertIn("died", result["verdict"]["reasons"][0])
        self.assertEqual(called, [])

    def legacy_pass(self, name):
        """Write the unversioned pass format produced by the parent finalizer."""
        self.ready_batch(name)
        sealed = self.coordinator.begin_finalization(name)
        directory = self.adapter.invocation_directory(sealed)
        finalizer._start_marker(directory / "started.json", sealed)
        status = finalizer.status_for(sealed, "pass", [])
        finalizer._atomic_json(directory / "status.json", status)
        return sealed, directory, status

    def assert_refused(self, value):
        self.assertEqual(value["state"], coordinator.VERDICT_READY)
        self.assertEqual(value["verdict"]["result"], "fail")
        self.assertEqual(value["verdict"]["reasons"], [coordinator.UNVERIFIED_PASS])

    def test_parent_pass_after_restart_is_refused_without_replay(self):
        _, directory, _ = self.legacy_pass("restart")
        persisted = {name: (directory / name).read_bytes() for name in ("started.json", "status.json")}
        self.adapter.close()
        self.adapter = deployment.DeploymentAdapter(self.database, self.root / "invocations")
        self.coordinator = self.adapter.coordinator
        self.addCleanup(self.adapter.close)
        with mock.patch.object(finalizer, "run", side_effect=AssertionError("finalizer replayed")):
            self.assert_refused(self.adapter.recover("restart"))
        self.assertEqual(self.coordinator.batch("restart")["verdict"]["result"], "fail")
        self.assertEqual(persisted, {name: (directory / name).read_bytes() for name in persisted})

    def test_parent_pass_after_already_started_is_refused_without_body_replay(self):
        self.legacy_pass("already-started")
        with mock.patch.object(finalizer.aggregate, "evaluate_sealed",
                               side_effect=AssertionError("finalizer body replayed")) as evaluate:
            self.assert_refused(self.adapter.finalise("already-started"))
        evaluate.assert_not_called()

    def test_recorded_parent_pass_is_quarantined_durably_on_recovery(self):
        _, _, status = self.legacy_pass("recorded")
        # Model the parent's durable host verdict: current record_verdict would
        # refuse the same status before it reaches SQLite.
        self.coordinator.fail_finalization("recorded", "fixture")
        existing = self.coordinator.batch("recorded")["verdict"]
        existing.update(status)
        with self.coordinator._transaction() as connection:
            connection.execute("UPDATE batches SET verdict_json = ?, verdict_digest = ? WHERE batch_id = ?",
                               (coordinator.canonical_json(existing), coordinator.digest(existing), "recorded"))
        self.adapter.close()
        self.adapter = deployment.DeploymentAdapter(self.database, self.root / "invocations")
        self.coordinator = self.adapter.coordinator
        self.addCleanup(self.adapter.close)
        with mock.patch.object(finalizer, "run", side_effect=AssertionError("finalizer replayed")):
            self.assert_refused(self.adapter.recover("recorded"))
            self.assert_refused(self.adapter.recover("recorded"))
        row = self.coordinator._connection.execute(
            "SELECT verdict_json, verdict_digest FROM batches WHERE batch_id = ?", ("recorded",)
        ).fetchone()
        self.assertEqual(json.loads(row["verdict_json"])["result"], "fail")
        self.assertEqual(row["verdict_digest"], coordinator.digest(json.loads(row["verdict_json"])))
        self.assertEqual(sum(event["event_type"] == "unverified_verdict_refused"
                             for event in self.coordinator.events("recorded")), 1)

    def test_coordinator_supervisor_refuses_parent_status_without_replay(self):
        _, directory, _ = self.legacy_pass("supervised")
        with mock.patch.object(finalizer, "run", side_effect=AssertionError("finalizer replayed")):
            self.assert_refused(self.coordinator.recover_finalization(
                "supervised", directory, directory / "status.json", worker_dead=True))

    def test_caller_claimed_evaluator_revision_cannot_authorize_pass(self):
        _, _, status = self.legacy_pass("claimed-revision")
        status["evaluator_revision"] = SHA_A
        with self.assertRaisesRegex(coordinator.CoordinatorError, "invalid finalizer status fields"):
            self.coordinator.record_verdict("claimed-revision", status)
        self.assertEqual(self.coordinator.batch("claimed-revision")["state"], coordinator.FINALIZING)
        self.assert_refused(self.adapter.recover("claimed-revision"))

    def test_finalizer_source_contains_no_subprocess_or_producer_plan_compatibility_path(self):
        source = (ROOT / "scripts" / "ci" / "regression_finalizer.py").read_text()
        tree = ast.parse(source)
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names}
        self.assertNotIn("subprocess", imported)
        self.assertNotIn("producer-plan", source)


if __name__ == "__main__":
    unittest.main()
