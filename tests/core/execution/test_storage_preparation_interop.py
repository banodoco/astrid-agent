# Imported pytest fixture names intentionally match injected parameters.
# ruff: noqa: F811
from __future__ import annotations

import copy
import socket
import time
from dataclasses import asdict, replace

import pytest
from runtime_protocol import local_preparation_relay as runtime
from test_storage_preparation_transport import exchange
from test_storage_preparation_transport import world as world

from astrid.core.execution import storage_preparation_transport as host


def binding(config):
    return dict(
        **config.context["runtime"],
        **{k: v for k, v in config.context.items() if k != "runtime"},
        support_root=str(config.support_root),
        socket_path=str(config.socket_path),
        readiness_record_path=str(config.readiness_path),
        runtime_process=asdict(config.runtime_peer),
        host_process=asdict(config.host_peer),
        source_digest=config.executor_source_digest.removeprefix("sha256:"),
        dependency_digest=config.executor_dependency_digest.removeprefix("sha256:"),
        preparation_source_digest=config.source_digest,
        preparation_dependency_digest=config.dependency_digest,
        host_instance_id=config.host_instance_id,
        boot_manifest_hash=config.boot_manifest_hash,
        deadline_ms=300,
    )


def relay_for(w):
    return runtime.LocalPreparationRelay(binding(w.config))


def padded(value, target):
    value = copy.deepcopy(value)
    value["request"]["spec"]["params"]["padding"] = "雪"
    remaining = target - len(host._encoded(value))
    assert remaining >= 0
    value["request"]["spec"]["params"]["padding"] += "界" * (remaining // 3) + "x" * (remaining % 3)
    assert len(host._encoded(value)) == target
    return value


def test_real_runtime_serializer_host_parser_writer_runtime_validator(world):
    w = world
    w.config = replace(w.config, executor_source_digest="sha256:" + "b" * 64)
    original = w.host.prepare_storage_estimate

    def prepare(request):
        return replace(original(request), spec_digest=runtime.digest(request.spec))

    w.host.prepare_storage_estimate = prepare
    listener = w.host.start_storage_preparation(w.config)
    relay = relay_for(w)
    envelope = relay.request_envelope(w.frame["request"], "cross-package")
    assert runtime.canonical(envelope) + b"\n" == host._encoded(envelope)
    relay._paths()  # Actual Host readiness writer -> actual Runtime reader.
    assert listener._record == relay.readiness_record()
    response = exchange(w, raw=runtime.canonical(envelope) + b"\n")
    preparation = relay.validate_response(runtime.decode(host._encoded(response)), envelope)
    assert preparation == response["preparation"]
    assert preparation["spec"] == envelope["request"]["spec"]
    assert len(w.calls) == 1
    assert preparation["source_digest"] != relay.binding["source_digest"]


@pytest.mark.parametrize(
    "mutation",
    [
        "runtime_epoch",
        "runtime_instance_id",
        "realm_id",
        "runtime_session_id",
        "executor_id",
        "source_epoch",
        "capability_digest",
        "estimator",
        "capability_scope",
        "executor_scope",
        "dependency_scope",
    ],
)
def test_cross_package_correlation_and_independent_readiness_scopes(world, mutation):
    w = world
    w.host.start_storage_preparation(w.config)
    b = binding(w.config)
    if mutation in b and mutation not in {"capability_digest", "executor_id", "source_epoch"}:
        b[mutation] = 8 if mutation == "runtime_epoch" else "foreign"
    elif mutation == "estimator":
        b["storage_estimate_contract"]["estimator"]["version"] += 1
    elif mutation == "capability_scope":
        b["preparation_source_digest"] = "sha256:" + "b" * 64
    elif mutation == "executor_scope":
        b["source_digest"] = "b" * 64
    elif mutation == "dependency_scope":
        b["preparation_dependency_digest"] = "sha256:" + "b" * 64
    else:
        b[mutation] = "sha256:" + "b" * 64 if mutation == "capability_digest" else "foreign"
    relay = runtime.LocalPreparationRelay(b)
    with pytest.raises(runtime.CapabilityUnavailableError, match="unavailable"):
        relay._paths()


def test_unicode_exact_request_ceiling_and_output_refusal(world):
    w = world
    w.host.start_storage_preparation(w.config)
    relay = relay_for(w)
    envelope = padded(relay.request_envelope(w.frame["request"], "unicode"), 65536)
    assert len(runtime.canonical(envelope) + b"\n") == 65536
    # Actual reader/parser accepts exact edge; the larger complete preparation
    # is refused by the bounded Host encoder, with no spec truncation/fallback.
    reply = exchange(w, raw=runtime.canonical(envelope) + b"\n")
    assert len(w.calls) == 1 and reply["refusal"] == {"code": "oversized_frame"}
    envelope["request"]["spec"]["params"]["padding"] += "é"
    with pytest.raises(host.PreparationTransportError, match="oversized_frame"):
        host._encoded(envelope)
    assert exchange(w, raw=runtime.canonical(envelope) + b"\n") is None
    assert len(w.calls) == 1


def test_unicode_reply_ceiling_preserves_full_spec_and_digest(world):
    w = world
    relay = relay_for(w)
    # Real flat producer value and Host encoder, independently Runtime validated.
    from astrid.core.execution.storage_estimator import StorageEstimateRequest, StoragePreparation

    request = copy.deepcopy(w.frame["request"])
    request["spec"]["params"]["padding"] = "雪"

    def response():
        r = StorageEstimateRequest(
            request["capability_id"], request["capability_digest"], 1, request["spec"], ()
        )
        return {
            **{
                k: v
                for k, v in relay.request_envelope(request, "reply-edge").items()
                if k != "request"
            },
            "preparation": StoragePreparation(
                r,
                runtime.digest(request["spec"]),
                w.config.source_digest,
                w.config.estimator,
                {"scratch_bytes": 0, "output_bytes": 0},
            ).to_dict(),
        }

    remaining = 65536 - len(host._encoded(response()))
    request["spec"]["params"]["padding"] += "界" * (remaining // 3) + "x" * (remaining % 3)
    # spec_digest derives from the whole exact request.
    reply = response()
    assert len(host._encoded(reply)) == 65536
    result = relay.validate_response(
        runtime.decode(host._encoded(reply)), relay.request_envelope(request, "reply-edge")
    )
    assert result["spec"] == request["spec"] and result["storage_estimate"] == {
        "scratch_bytes": 0,
        "output_bytes": 0,
    }
    request["spec"]["params"]["padding"] += "é"
    with pytest.raises(host.PreparationTransportError, match="oversized_frame"):
        host._encoded(response())


@pytest.mark.parametrize("overrun", [False, True])
def test_unicode_readiness_ceiling_actual_writer_and_reader(world, overrun):
    w = world
    # Build expected projection independently, then fill a legal Unicode identity.
    b = binding(w.config)
    b["host_instance_id"] = "雪"
    size = len(host._encoded(runtime.LocalPreparationRelay(b).readiness_record()))
    n = 65536 - size
    name = "雪" + "界" * (n // 3) + "x" * (n % 3) + ("é" if overrun else "")
    w.config = replace(w.config, host_instance_id=name)
    if overrun:
        with pytest.raises(host.PreparationTransportError, match="oversized_frame"):
            w.host.start_storage_preparation(w.config)
        assert not w.config.readiness_path.exists()
    else:
        w.host.start_storage_preparation(w.config)
        assert w.config.readiness_path.stat().st_size == 65536
        relay_for(w)._paths()


@pytest.mark.parametrize("raw", [b'{"protocol":1,"protocol":2}\n', b"{}\n{}\n", b'{"x":NaN}\n'])
def test_real_host_reader_rejects_duplicate_and_trailing_frames(raw):
    a, b = socket.socketpair()
    try:
        a.sendall(raw)
        a.shutdown(socket.SHUT_WR)
        with pytest.raises(host.PreparationTransportError):
            host.read_frame(b, time.monotonic() + 0.3)
    finally:
        a.close()
        b.close()
