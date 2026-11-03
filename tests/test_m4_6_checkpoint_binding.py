import json
from dataclasses import replace

import pytest

from scripts.m4_6_checkpoint_binding import (
    BindingRefused, CheckpointBinding, CheckpointPair, ProcessAnchor,
)
from scripts.m4_6_process_observer import ManagerIdentity, ProcessSnapshot


def anchor(pid):
    return ProcessAnchor(pid, str(pid * 10), 1002, "/usr/bin/program", "a" * 64,
                         ("/usr/bin/program", "arg"), "/home/user")


def binding():
    return CheckpointBinding("pinokio-backend-linux", tuple((name, "a" * 40) for name in
        ("app", "agent", "runtime", "pinokio")), 123, 1, "b" * 32, "c" * 64,
        "astrid.service", anchor(10), anchor(11))


def snapshot(state="absent"):
    return ProcessSnapshot("linux-bounded-proc-systemd-poll",
        ManagerIdentity("systemd-user", "astrid.service", state,
                        None if state == "absent" else "invocation", None, None),
        None, (), ())


def test_retains_two_immutable_records_and_never_claims_authority():
    pair = CheckpointPair(binding(), snapshot("present"))
    encoded = pair.finish(binding(), snapshot())
    result = json.loads(encoded)
    assert result["initial"]["manager"]["state"] == "present"
    assert result["final"]["manager"]["state"] == "absent"
    assert result["authority"] == "none"
    result["initial"]["manager"]["state"] = "forged"
    assert json.loads(encoded)["initial"]["manager"]["state"] == "present"
    with pytest.raises(BindingRefused, match="consumed"):
        pair.finish(binding(), snapshot())


@pytest.mark.parametrize("changes", [
    {"slot": "direct-linux"}, {"candidate": tuple((n, "d" * 40) for n in
        ("app", "agent", "runtime", "pinokio"))}, {"run_id": 124}, {"attempt": 2},
    {"request_id": "d" * 32}, {"plan_sha256": "d" * 64},
    {"manager_target": "other.service"},
    {"manager": replace(anchor(10), birth="other")},
    {"manager": replace(anchor(10), executable_sha256="d" * 64)},
    {"child": replace(anchor(11), pid=12)}, {"child": replace(anchor(11), birth="other")},
    {"child": replace(anchor(11), executable="/usr/bin/other")},
    {"child": replace(anchor(11), executable_sha256="d" * 64)},
    {"child": replace(anchor(11), argv=("/usr/bin/program", "forged"))},
    {"child": replace(anchor(11), cwd="/other")},
])
def test_foreign_binding_or_identity_consumes_pair(changes):
    pair = CheckpointPair(binding(), snapshot())
    with pytest.raises(BindingRefused, match="differs"):
        pair.finish(replace(binding(), **changes), snapshot())
    with pytest.raises(BindingRefused, match="consumed"):
        pair.finish(binding(), snapshot())


@pytest.mark.parametrize("bad", [
    replace(snapshot(), source="forged"),
    replace(snapshot(), manager=replace(snapshot().manager, target="other.service")),
    replace(snapshot(), manager=replace(snapshot().manager, manager="other")),
])
def test_wrong_source_or_target_refused_and_consumed(bad):
    with pytest.raises(BindingRefused, match="source or target"):
        CheckpointPair(binding(), bad)
    pair = CheckpointPair(binding(), snapshot())
    with pytest.raises(BindingRefused, match="source or target"):
        pair.finish(binding(), bad)
    with pytest.raises(BindingRefused, match="consumed"):
        pair.finish(binding(), snapshot())


def test_malformed_bindings_refused():
    for changes in ({"attempt": True}, {"run_id": 0}, {"request_id": "x" * 32},
                    {"candidate": binding().candidate[:-1]}, {"child": anchor(10)}):
        with pytest.raises(BindingRefused):
            replace(binding(), **changes)
    with pytest.raises(BindingRefused):
        replace(anchor(11), uid=True)
