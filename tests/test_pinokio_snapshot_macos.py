from __future__ import annotations

import hashlib
import json

import pytest

from scripts import pinokio_snapshot_macos as collector


PS = "/bin/ps"
LAUNCHCTL = "/bin/launchctl"
PS_TEXT = (
    "  42   1 Mon Nov  2 18:00:01 2026 /Applications/Pinokio.app/Contents/MacOS/Pinokio --serve\n"
    "  99  42 Mon Nov  2 18:00:02 2026 /usr/local/bin/node child.js --flag value\n"
)


class FakeRunner:
    def __init__(self, *, ps: collector.CommandResult | None = None, services=None):
        self.ps = ps or collector.CommandResult(0, PS_TEXT.encode(), b"")
        self.services = services or {}
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))
        if argv[0] == PS:
            return self.ps
        assert argv[:2] == (LAUNCHCTL, "print")
        value = self.services[argv[2]]
        if isinstance(value, Exception):
            raise value
        return value


def path_for(pid: int) -> str:
    return {42: "/Applications/Pinokio.app/Contents/MacOS/Pinokio", 99: "/usr/local/bin/node"}[pid]


def capture(runner: FakeRunner, **kwargs):
    ticks = iter((100, 150))
    return collector.collect_snapshot(
        kwargs.pop("targets", ["gui/501/com.pinokio.backend"]),
        runner=runner,
        pid_path_resolver=kwargs.pop("pid_path_resolver", path_for),
        monotonic_ns=lambda: next(ticks),
        utc_now=lambda: "2026-11-02T18:14:13.000000Z",
        collector_pid=700,
        **kwargs,
    )


def test_present_and_absent_services_and_process_tree_are_bound_by_digest():
    present = "gui/501/com.pinokio.backend"
    absent = "system/com.pinokio.missing"
    runner = FakeRunner(services={
        present: collector.CommandResult(0, b"gui/501/com.pinokio.backend = {\n\tpid = 42\n}\n", b""),
        absent: collector.CommandResult(113, b"", b"Could not find service\n"),
    })
    observation = capture(runner, targets=[absent, present])

    assert observation["source_profile"] == collector.SOURCE_PROFILE
    assert observation["capture_started_monotonic_ns"] == "100"
    assert observation["capture_finished_monotonic_ns"] == "150"
    assert observation["collector_pid"] == 700
    assert [(row["pid"], row["parent_pid"]) for row in observation["processes"]] == [
        (42, 1), (99, 42)
    ]
    assert observation["processes"][0]["command_line"].endswith("--serve")
    assert [row["state"] for row in observation["launchd"]] == ["present", "absent"]
    assert observation["launchd"][0]["raw_print"].endswith("}\n")
    assert observation["launchd"][1]["raw_print"] is None
    unsigned = dict(observation)
    digest = unsigned.pop("structural_sha256")
    assert digest == hashlib.sha256(collector.canonical_json_bytes(unsigned)).hexdigest()
    assert json.loads(collector.canonical_json_bytes(observation)) == observation


def test_process_that_vanishes_during_libproc_resolution_is_classified():
    runner = FakeRunner(services={})

    def resolver(pid):
        if pid == 99:
            raise ProcessLookupError("gone")
        return path_for(pid)

    observation = capture(runner, targets=[], pid_path_resolver=resolver)
    vanished = observation["processes"][1]
    assert vanished["collection_state"] == "vanished"
    assert vanished["executable_path"] is None


@pytest.mark.parametrize(
    "ps_text, message",
    [
        ("42 1 malformed\n", "malformed"),
        (
            "42 1 Mon Nov 2 18:00:01 2026 first\n"
            "42 1 Mon Nov 2 18:00:01 2026 second\n",
            "duplicate PID",
        ),
        ("42 1 Mon Feb 31 18:00:01 2026 impossible\n", "invalid birth"),
        ("42 1 Mon Nov 2 18:00:01 2026 unterminated", "unterminated"),
    ],
)
def test_malformed_or_duplicate_ps_rows_fail_closed(ps_text, message):
    runner = FakeRunner(ps=collector.CommandResult(0, ps_text.encode(), b""))
    with pytest.raises(collector.CollectorError, match=message):
        capture(runner, targets=[])


def test_permission_error_for_stable_process_fails_closed():
    runner = FakeRunner(services={})

    def denied(_pid):
        raise PermissionError("denied")

    with pytest.raises(collector.CollectorError, match="permission denied"):
        capture(runner, targets=[], pid_path_resolver=denied)


def test_process_and_output_capacity_fail_closed():
    runner = FakeRunner(services={})
    with pytest.raises(collector.CollectorError, match="process table exceeds"):
        capture(runner, targets=[], limits=collector.Limits(max_processes=1))

    runner = FakeRunner(services={})
    with pytest.raises(collector.CollectorError, match="bounded capacity"):
        capture(runner, targets=[], limits=collector.Limits(max_ps_bytes=8))


def test_ps_command_failure_fails_closed():
    runner = FakeRunner(ps=collector.CommandResult(2, b"", b"denied\n"))
    with pytest.raises(collector.CollectorError, match="ps failed"):
        capture(runner, targets=[])


def test_launchctl_collection_failures_are_explicit_error_rows():
    target = "gui/501/com.pinokio.backend"
    runner = FakeRunner(services={target: collector.CommandResult(77, b"", b"denied\n")})
    observation = capture(runner)
    assert observation["launchd"] == [{
        "error": "launchctl exited 77",
        "raw_print": None,
        "returncode": 77,
        "state": "error",
        "target": target,
    }]


@pytest.mark.parametrize(
    "targets, message",
    [
        (["gui/501/com.pinokio.backend", "gui/501/com.pinokio.backend"], "duplicate"),
        (["gui/501/com.pinokio.backend;rm"], "unsafe"),
        (["system"], "unsafe"),
        ("gui/501/com.pinokio.backend", "sequence"),
    ],
)
def test_duplicate_or_unsafe_launchd_targets_are_refused(targets, message):
    with pytest.raises(collector.CollectorError, match=message):
        capture(FakeRunner(), targets=targets)


def test_service_target_and_raw_output_capacity_are_bounded():
    target = "gui/501/com.pinokio.backend"
    runner = FakeRunner(services={target: collector.CommandResult(0, b"x" * 32, b"")})
    observation = capture(runner, limits=collector.Limits(max_service_bytes=8))
    assert observation["launchd"][0]["state"] == "error"
    assert "bounded capacity" in observation["launchd"][0]["error"]

    with pytest.raises(collector.CollectorError, match="target set exceeds"):
        capture(
            FakeRunner(),
            targets=[target, "system/com.pinokio.other"],
            limits=collector.Limits(max_service_targets=1),
        )
