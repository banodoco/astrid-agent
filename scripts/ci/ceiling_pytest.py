"""Additional native pytest reporter; no selection, marks or outcomes change."""
from __future__ import annotations
import json
import os
from pathlib import Path
import uuid
import pytest


def pytest_configure(config):
    directory = os.environ.get("CEILING_STEP_DIR")
    if directory and not hasattr(config, "workerinput") and not os.environ.get("CEILING_REPORTER_ACTIVE"):
        os.environ["CEILING_REPORTER_ACTIVE"] = str(os.getpid())
        config.pluginmanager.register(Recorder(Path(directory), config), "plan39-ceiling-reporter")


class Recorder:
    def __init__(self, directory, config):
        self.config = config
        self.exitstatus = None
        self.collected = None
        self.stream = (directory / ("pytest-" + uuid.uuid4().hex + ".jsonl")).open("x", encoding="utf-8", buffering=1)
        self.write("start", args=list(config.invocation_params.args), collect_only=config.option.collectonly)

    def write(self, event, **values):
        self.stream.write(json.dumps({"event": event, **values}, ensure_ascii=False, default=str) + "\n")
        self.stream.flush()

    def pytest_collection_finish(self, session):
        if not getattr(session.config.option, "numprocesses", 0):
            self.write("collection", nodeids=[item.nodeid for item in session.items])

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        self.write("collection", nodeids=list(ids), worker=node.gateway.id)

    def pytest_collectreport(self, report):
        if report.failed or report.skipped:
            self.write("collection_error", nodeid=report.nodeid, outcome=report.outcome, diagnostic=str(report.longrepr))

    def pytest_runtest_logreport(self, report):
        crash = getattr(report.longrepr, "reprcrash", None)
        message = getattr(crash, "message", str(report.longrepr))
        if report.skipped and isinstance(report.longrepr, tuple):
            message = report.longrepr[-1]
        if getattr(report, "wasxfail", None):
            message = report.wasxfail
        self.write("report", native_id=report.nodeid, stage=report.when, outcome=report.outcome,
                   subtest=str(report.context) if hasattr(report, "context") else None,
                   wasxfail=getattr(report, "wasxfail", None), diagnostic=message,
                   detail=str(report.longrepr) if report.failed or report.skipped else "")

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        self.exitstatus = int(session.exitstatus)
        self.collected = session.testscollected

    def pytest_unconfigure(self, config):
        if self.exitstatus is not None:
            self.write("finish", exitstatus=self.exitstatus, collected=self.collected)
        self.stream.close()
