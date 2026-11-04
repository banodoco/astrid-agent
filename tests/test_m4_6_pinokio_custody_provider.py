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
)
from scripts.m4_6_process_observer import ObservationRefused


def _installed_app(tmp_path: Path) -> Path:
    app = tmp_path / "installed-app"
    actions = app / "pinokio"
    actions.mkdir(parents=True, exist_ok=True)
    (actions / "helper.js").write_text("module.exports = { ready: true };\n", encoding="utf-8")
    (actions / "start.js").write_text("require('./helper');\n", encoding="utf-8")
    (actions / "stop.js").write_text("require('./helper');\n", encoding="utf-8")
    (actions / "update.js").write_text("require('./helper');\n", encoding="utf-8")
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


def test_action_graph_refuses_dynamic_helper_resolution(tmp_path: Path) -> None:
    app = _installed_app(tmp_path)
    (app / "pinokio" / "update.js").write_text("require('./' + 'helper');\n", encoding="utf-8")

    with pytest.raises(PinokioSourceMismatch, match="dynamic action/helper import"):
        PinokioActionGraph.collect(app)


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
        {"slot_id": "pinokio-backend-linux", "contract": {"mode": "pinokio-backend"}},
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
