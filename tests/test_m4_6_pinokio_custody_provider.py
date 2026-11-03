"""Focused source-only checks for the fail-closed Pinokio custody provider."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.m4_6_pinokio_custody_provider import (
    PINOKIO_CUSTODY_PROVIDER,
    PinokioActionGraph,
    PinokioCustodyObserver,
    PinokioCustodyRefused,
    PinokioRequestSettlement,
    PinokioSelectedApp,
    PinokioSourceMismatch,
    PreparedPinokioRequest,
    validate_pinokio_session_row,
)
from scripts.journey_matrix import M4_6_MATRIX
from scripts.m4_6_process_observer import ObservationRefused


def _installed_app(tmp_path: Path) -> Path:
    app = tmp_path / "installed-app"
    actions = app / "pinokio"
    actions.mkdir(parents=True, exist_ok=True)
    (actions / "helper.js").write_text("module.exports = { ready: true };\n", encoding="utf-8")
    (actions / "start.js").write_text("require('./helper');\n", encoding="utf-8")
    (actions / "stop.js").write_text("require('./helper');\n", encoding="utf-8")
    (actions / "update.js").write_text("require('./helper');\n", encoding="utf-8")
    (actions / "uninstall.js").write_text("require('./helper');\n", encoding="utf-8")
    return app


def _prepared(tmp_path: Path) -> PreparedPinokioRequest:
    app = _installed_app(tmp_path)
    graph = PinokioActionGraph.collect(app)
    selected = PinokioSelectedApp.bind(
        ref="pinokio://127.0.0.1:42000/api/astrid",
        expected_app_id="astrid",
        expected_app_path=app,
        status={"id": "astrid", "path": str(app)},
    )
    return PreparedPinokioRequest.prepare(
        request_id="m46-request-1",
        operation="update",
        graph=graph,
        selected_app=selected,
        input={"keep_data": True},
    )


def test_action_graph_binds_all_descriptors_and_resolved_helpers(tmp_path: Path) -> None:
    app = _installed_app(tmp_path)

    graph = PinokioActionGraph.collect(app)

    roles = {item.role for item in graph.files}
    assert {
        "pinokio/start.js",
        "pinokio/stop.js",
        "pinokio/update.js",
        "pinokio/uninstall.js",
        "helper:pinokio/helper.js",
    } <= roles
    assert graph.action_for("stop").path == str(app / "pinokio" / "stop.js")
    assert (
        graph.action_for("update").sha256
        == hashlib.sha256((app / "pinokio" / "update.js").read_bytes()).hexdigest()
    )


def test_action_graph_refuses_helper_drift_and_selected_app_mismatch(tmp_path: Path) -> None:
    app = _installed_app(tmp_path)
    graph = PinokioActionGraph.collect(app)
    (app / "pinokio" / "helper.js").write_text(
        "module.exports = { ready: false };\n", encoding="utf-8"
    )
    with pytest.raises(PinokioSourceMismatch, match="action/helper bytes changed"):
        graph.verify_current_bytes()

    with pytest.raises(PinokioSourceMismatch, match="status path differs"):
        PinokioSelectedApp.bind(
            ref="pinokio://127.0.0.1:42000/api/astrid",
            expected_app_id="astrid",
            expected_app_path=app,
            status={"id": "astrid", "path": str(tmp_path)},
        )


@pytest.mark.parametrize("operation", ["update", "uninstall"])
def test_action_graph_refuses_dynamic_helper_resolution(tmp_path: Path, operation: str) -> None:
    app = _installed_app(tmp_path)
    (app / "pinokio" / f"{operation}.js").write_text("require('./' + 'helper');\n", encoding="utf-8")

    with pytest.raises(
        PinokioSourceMismatch, match=rf"dynamic action/helper import cannot be bound: pinokio/{operation}\.js"
    ):
        PinokioActionGraph.collect(app)


def test_action_graph_binds_static_cycle_and_refuses_equal_size_leaf_drift(tmp_path: Path) -> None:
    app = _installed_app(tmp_path)
    actions = app / "pinokio"
    (actions / "helper.js").write_text("require('./leaf');\n", encoding="utf-8")
    leaf = actions / "leaf.js"
    original = "require('./helper'); module.exports = 1;\n"
    leaf.write_text(original, encoding="utf-8")

    graph = PinokioActionGraph.collect(app)

    assert {Path(item.path).relative_to(app).as_posix() for item in graph.files} == {
        "pinokio/start.js", "pinokio/stop.js", "pinokio/update.js", "pinokio/uninstall.js",
        "pinokio/helper.js", "pinokio/leaf.js",
    }
    bound_leaf = next(item for item in graph.files if item.path == str(leaf))
    assert bound_leaf.sha256 == hashlib.sha256(leaf.read_bytes()).hexdigest()
    assert PinokioActionGraph.collect(app) == graph
    graph.verify_current_bytes()

    leaf.write_text(original.replace("= 1", "= 2"), encoding="utf-8")
    assert leaf.stat().st_size == len(original.encode("utf-8"))
    with pytest.raises(PinokioSourceMismatch, match="action/helper bytes changed"):
        graph.verify_current_bytes()


@pytest.mark.parametrize(
    "expression",
    [
        "'./session.mjs'",  # Selected lib.js literal edge still needs a reviewed binder.
        "'./missing.mjs'",
        "modulePath",
        "'./' + moduleName",
        "'node:fs/promises'",  # Selected update-stage.mjs:137 foreign edge.
        "'foreign-package'",
        "'https://example.invalid/helper.mjs'",
    ],
)
def test_transitive_dynamic_closure_remains_refused(tmp_path: Path, expression: str) -> None:
    app = _installed_app(tmp_path)
    actions = app / "pinokio"
    (actions / "helper.js").write_text("require('./update-stage.mjs');\n", encoding="utf-8")
    (actions / "session.mjs").write_text("export const ready = true;\n", encoding="utf-8")
    (actions / "update-stage.mjs").write_text(
        f"export async function stage() {{ return import({expression}); }}\n", encoding="utf-8"
    )

    with pytest.raises(
        PinokioSourceMismatch,
        match=r"dynamic action/helper import cannot be bound: pinokio/update-stage\.mjs",
    ):
        PinokioActionGraph.collect(app)


def test_action_graph_refuses_missing_transitive_static_helper(tmp_path: Path) -> None:
    app = _installed_app(tmp_path)
    (app / "pinokio" / "helper.js").write_text("require('./missing');\n", encoding="utf-8")

    with pytest.raises(PinokioSourceMismatch, match="static helper cannot be resolved"):
        PinokioActionGraph.collect(app)


def test_install_does_not_alias_a_supported_custody_operation(tmp_path: Path) -> None:
    request = _prepared(tmp_path)
    app = Path(request.selected_app.app_path)
    (app / "pinokio" / "install.js").write_text("require('./helper');\n", encoding="utf-8")
    graph = PinokioActionGraph.collect(app)

    with pytest.raises(ValueError, match="only start, stop, update, and uninstall"):
        graph.action_for("install")
    with pytest.raises(ValueError, match="only start, stop, update, and uninstall"):
        PreparedPinokioRequest.prepare(
            request_id="unsupported-install", operation="install", graph=graph,
            selected_app=request.selected_app, input={},
        )


def test_uninstall_helper_closure_is_bound_and_drift_refuses(tmp_path: Path) -> None:
    app = _installed_app(tmp_path)
    actions = app / "pinokio"
    (actions / "uninstall.js").write_text("require('./removal-helper');\n", encoding="utf-8")
    (actions / "removal-helper.js").write_text("require('./removal-leaf');\n", encoding="utf-8")
    leaf = actions / "removal-leaf.js"
    leaf.write_text("module.exports = { keepData: true };\n", encoding="utf-8")
    graph = PinokioActionGraph.collect(app)

    assert graph.action_for("uninstall").path == str(actions / "uninstall.js")
    assert {"helper:pinokio/removal-helper.js", "helper:pinokio/removal-leaf.js"} <= {
        item.role for item in graph.files
    }
    leaf.write_text("module.exports = { keepData: false };\n", encoding="utf-8")
    with pytest.raises(PinokioSourceMismatch, match="action/helper bytes changed"):
        graph.verify_current_bytes()


def test_action_graph_requires_uninstall_descriptor(tmp_path: Path) -> None:
    app = _installed_app(tmp_path)
    (app / "pinokio" / "uninstall.js").unlink()

    with pytest.raises(PinokioSourceMismatch, match="action/helper cannot be resolved"):
        PinokioActionGraph.collect(app)


def test_uninstall_request_is_only_data_and_cannot_settle_from_result_stop(tmp_path: Path) -> None:
    app = _installed_app(tmp_path)
    graph = PinokioActionGraph.collect(app)
    selected = PinokioSelectedApp.bind(
        ref="pinokio://127.0.0.1:42000/api/astrid", expected_app_id="astrid",
        expected_app_path=app, status={"id": "astrid", "path": str(app)},
    )
    prepared = PreparedPinokioRequest.prepare(
        request_id="uninstall-request-1", operation="uninstall", graph=graph,
        selected_app=selected, input={"keep_data": True},
    )
    assert prepared.process_payload() == {
        "uri": str(app / "pinokio" / "uninstall.js"), "id": "uninstall-request-1",
        "action": "run", "input": {"keep_data": True},
    }
    lifecycle = PinokioRequestSettlement(prepared)
    lifecycle.record_sent()
    lifecycle.ingest({"id": prepared.request_id, "type": "result", "data": {"uninstalled": True}})
    lifecycle.ingest({"id": prepared.request_id, "type": "event", "data": "stop"})
    assert lifecycle.require_terminal_readback().status == "unknown"
    assert lifecycle.require_terminal_readback().reason == "missing-engine-task-readback"


def test_queued_acceptance_is_not_terminal_settlement(tmp_path: Path) -> None:
    lifecycle = PinokioRequestSettlement(_prepared(tmp_path))

    accepted = lifecycle.record_sent()
    assert accepted.queued is True
    assert (
        lifecycle.ingest({"id": accepted.request_id, "type": "info", "data": "<b>Queued</b>"})
        is None
    )
    assert lifecycle.settlement is None

    lifecycle.ingest({"id": accepted.request_id, "type": "result", "data": {}})
    lifecycle.ingest({"id": accepted.request_id, "type": "event", "data": "stop"})
    terminal = lifecycle.require_terminal_readback()
    assert terminal.status == "unknown"
    assert terminal.reason == "missing-engine-task-readback"


def test_prepared_request_is_daemon_process_data_not_a_direct_descriptor_command(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)

    assert prepared.process_payload() == {
        "uri": str(tmp_path / "installed-app" / "pinokio" / "update.js"),
        "id": "m46-request-1",
        "action": "run",
        "input": {"keep_data": True},
    }


@pytest.mark.parametrize(
    ("packet", "status", "reason"),
    [
        ({"id": "m46-request-1", "type": "error", "data": "no action"}, "error", "engine-error"),
        (
            {
                "id": "m46-request-1",
                "type": "launch.requirements.control",
                "data": {"action": "blocked"},
            },
            "refused",
            "engine-launch-requirements-refusal",
        ),
        (
            {"id": "m46-request-1", "type": "disconnect"},
            "unknown",
            "transport-lost-before-terminal-evidence",
        ),
    ],
)
def test_error_refusal_and_disconnect_never_settle_success(
    tmp_path: Path, packet: dict[str, object], status: str, reason: str
) -> None:
    lifecycle = PinokioRequestSettlement(_prepared(tmp_path))
    lifecycle.record_sent()

    terminal = lifecycle.ingest(packet)

    assert terminal is not None
    assert terminal.status == status
    assert terminal.reason == reason


def test_zero_client_exit_and_foreign_packet_fail_closed(tmp_path: Path) -> None:
    lifecycle = PinokioRequestSettlement(_prepared(tmp_path))
    lifecycle.record_sent()
    foreign = lifecycle.ingest({"id": "other-request", "type": "result"})
    assert foreign is not None
    assert foreign.status == "unknown"
    assert foreign.reason == "foreign-or-malformed-engine-packet"

    other = PinokioRequestSettlement(_prepared(tmp_path))
    other.record_sent()
    exit_result = other.client_exit(0)
    assert exit_result.status == "unknown"
    assert exit_result.reason == "client-exit-0-is-not-settlement"


def test_engine_settlement_timeout_is_unknown_not_success(tmp_path: Path) -> None:
    lifecycle = PinokioRequestSettlement(_prepared(tmp_path))
    lifecycle.record_sent()

    terminal = lifecycle.expire()

    assert terminal.status == "unknown"
    assert terminal.reason == "engine-settlement-timeout"


def test_pinokio_provider_is_distinct_from_direct_and_live_owner_refuses() -> None:
    with pytest.raises(PinokioCustodyRefused, match="fixed six Pinokio slots"):
        PINOKIO_CUSTODY_PROVIDER.create_owner(
            {"slot_id": "direct-linux", "contract": {"mode": "direct"}},
            runner=object(),
            process_probe=object(),
            data_probe=object(),
            durable_journal_probe=object(),
        )

    owner = PINOKIO_CUSTODY_PROVIDER.create_owner(
        {
            "slot_id": "pinokio-backend-linux",
            "contract": next(row for row in M4_6_MATRIX if row["job_id"] == "pinokio-backend-linux"),
        },
        runner=object(),
        process_probe=object(),
        data_probe=object(),
        durable_journal_probe=object(),
    )
    assert owner.os_name == "linux"
    assert owner.target.startswith("pinokio-direct-custody:")
    for method in (
        "custody_binding",
        "register_start",
        "registration",
        "observe",
        "revalidate",
        "removal_census",
        "observe_external_controller",
        "terminate_owned_runtime",
        "observe_absent",
        "remove_cleanup",
    ):
        assert callable(getattr(owner, method))
    with pytest.raises(PinokioCustodyRefused, match="live Pinokio admission is closed"):
        owner.probe()
    assert owner.raw[-1] == {"event": "refused", "operation": "probe"}


def test_pinokio_session_validator_accepts_exact_six_rows_and_owner_stays_closed() -> None:
    for row in M4_6_MATRIX:
        if not row["job_id"].startswith("pinokio-"):
            continue
        mode, os_name = validate_pinokio_session_row(row["job_id"], row)
        assert (mode, os_name) == (row["mode"], row["os"])
        owner = PINOKIO_CUSTODY_PROVIDER.create_owner(
            {"slot_id": row["job_id"], "contract": row},
            runner=object(), process_probe=object(), data_probe=object(),
            durable_journal_probe=object(),
        )
        assert owner.os_name == row["os"]
        with pytest.raises(PinokioCustodyRefused, match="live Pinokio admission is closed"):
            owner.probe()


def test_pinokio_session_validator_rejects_crossed_mode_os_runner_and_shell_rows() -> None:
    pinokio_rows = [row for row in M4_6_MATRIX if row["job_id"].startswith("pinokio-")]
    for source in pinokio_rows:
        for target in pinokio_rows:
            if source["job_id"] == target["job_id"]:
                continue
            with pytest.raises(PinokioCustodyRefused):
                validate_pinokio_session_row(source["job_id"], target)

    valid = dict(next(row for row in pinokio_rows if row["job_id"] == "pinokio-backend-linux"))
    for field, value in (
        ("mode", "pinokio-fully-local"),
        ("os", "windows"),
        ("runner", "macos-15"),
        ("shell", "powershell.exe"),
    ):
        crossed = dict(valid, **{field: value})
        with pytest.raises(PinokioCustodyRefused):
            validate_pinokio_session_row(valid["job_id"], crossed)

    with pytest.raises(PinokioCustodyRefused, match="selected mode/OS differs"):
        PINOKIO_CUSTODY_PROVIDER.create_owner(
            {
                "slot_id": valid["job_id"], "contract": valid,
                "mode": "pinokio-fully-local", "os": "linux",
            },
            runner=object(), process_probe=object(), data_probe=object(),
            durable_journal_probe=object(),
        )


@pytest.mark.parametrize(
    "slot_id,contract",
    [
        (None, {}),
        ("direct-linux", {}),
        ("pinokio-backend-linux", None),
        ("pinokio-backend-linux", {}),
        (
            "pinokio-backend-linux",
            {"job_id": "pinokio-backend-linux", "mode": "pinokio-backend", "os": "linux", "runner": "ubuntu-24.04"},
        ),
        (
            "pinokio-backend-linux",
            {
                "job_id": "pinokio-backend-linux", "mode": "pinokio-backend", "os": "linux",
                "runner": "ubuntu-24.04", "shell": "sh", "unexpected": True,
            },
        ),
    ],
)
def test_pinokio_session_validator_rejects_malformed_or_missing_inputs(slot_id, contract) -> None:
    with pytest.raises(PinokioCustodyRefused):
        validate_pinokio_session_row(slot_id, contract)


def test_custody_observer_gap_refuses_authorization() -> None:
    observer = PinokioCustodyObserver()

    with pytest.raises(PinokioCustodyRefused, match="no B-owned Pinokio controller"):
        observer.assert_authorizable("authorization")

    assert observer.report()["authorization"] == "refused"


def test_imported_b_observer_gap_is_preserved_as_pinokio_refusal() -> None:
    class GapObserver:
        def assert_authorizable(self, phase: str) -> None:
            assert phase == "finalization"
            raise ObservationRefused("source sequence discontinuity")

    observer = PinokioCustodyObserver(process_observer=GapObserver())  # type: ignore[arg-type]

    with pytest.raises(PinokioCustodyRefused, match="B process observer refused"):
        observer.assert_authorizable("finalization")
