"""Capture pytest origins, phase custody, and per-node outcomes for D21."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import sys


EVIDENCE = Path(os.environ["D21_EVIDENCE_DIR"]).resolve()
RUN_ID = os.environ.get("D21_RUN_ID", "d21")


def _safe_worker() -> str:
    return os.environ.get("PYTEST_XDIST_WORKER", "controller")


def _origins(phase: str, config=None) -> None:
    values = {}
    for name in (
        "astrid",
        "runtime_protocol",
        "astrid_runtime_client",
        "astrid.core.timeline.banodoco_schema",
        "banodoco_timeline_schema",
    ):
        try:
            module = __import__(name, fromlist=["*"])
            path = Path(module.__file__).resolve()
            values[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        except Exception as exc:  # pragma: no cover - diagnostics must survive import failures
            values[name] = {"error": repr(exc)}
    record = {
        "phase": phase,
        "phase_id": f"{RUN_ID}:{_safe_worker()}:{phase}",
        "pid": os.getpid(),
        "worker": _safe_worker(),
        "root": str(Path.cwd().resolve()),
        "executable": sys.executable,
        "argv": sys.argv,
        "TMPDIR": os.environ.get("TMPDIR"),
        "PYTEST_PLUGINS": os.environ.get("PYTEST_PLUGINS"),
        "PYTEST_ADDOPTS": os.environ.get("PYTEST_ADDOPTS"),
        "observer_origin": str(Path(__file__).resolve()),
        "pytest_args": list(getattr(getattr(config, "invocation_params", None), "args", ())),
        "origins": values,
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / f"{RUN_ID}-origins-{_safe_worker()}-{os.getpid()}-{phase}.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n"
    )


def pytest_configure(config):
    _origins("before", config)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 - pytest hook signature
    _origins("after", session.config)
    (EVIDENCE / f"{RUN_ID}-exit-{_safe_worker()}-{os.getpid()}.json").write_text(
        json.dumps(
            {
                "exit": int(exitstatus),
                "collected": session.testscollected,
                "failed": session.testsfailed,
                "worker": _safe_worker(),
                "observer_origin": str(Path(__file__).resolve()),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def pytest_runtest_logreport(report):
    record = {
        "nodeid": report.nodeid,
        "when": report.when,
        "outcome": report.outcome,
        "duration": report.duration,
        "longrepr": str(report.longrepr) if report.longrepr else None,
        "wasxfail": getattr(report, "wasxfail", None),
        "pid": os.getpid(),
        "worker": _safe_worker(),
        "observer_origin": str(Path(__file__).resolve()),
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with gzip.open(EVIDENCE / f"{RUN_ID}-outcomes-{_safe_worker()}-{os.getpid()}.jsonl.gz", "at") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def pytest_collectreport(report):
    if report.failed:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        with gzip.open(EVIDENCE / f"{RUN_ID}-collection-{_safe_worker()}-{os.getpid()}.jsonl.gz", "at") as stream:
            stream.write(
                json.dumps(
                    {
                        "nodeid": report.nodeid,
                        "longrepr": str(report.longrepr),
                        "worker": _safe_worker(),
                        "observer_origin": str(Path(__file__).resolve()),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
