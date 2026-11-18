"""Census before filtering, independent selector calculation, and durable reports.

Loaded by scripts.ci.run_pytest for both its collect-only reference and the run.
The existing lane registry remains the only source of intentional module hiding.
"""

from __future__ import annotations

import collections
import fnmatch
import json
from pathlib import Path

import pytest
from _pytest.junitxml import mangle_test_address
from _pytest.mark import Expression, KeywordMatcher, MarkMatcher

from scripts.ci.static_inventory import StaticInventory


def pytest_addoption(parser):
    group = parser.getgroup("Astrid collection evidence")
    group.addoption("--collection-manifest")
    group.addoption("--collection-reference")
    group.addoption("--test-event-log")


def pytest_configure(config):
    for option in ("deselect", "ignore", "ignore_glob"):
        if config.getoption(option, default=None):
            raise pytest.UsageError(f"undeclared collection exclusion: --{option.replace('_', '-')}")
    if not config.getoption("collection_manifest"):
        raise pytest.UsageError("collection guard requires --collection-manifest")
    config.pluginmanager.register(CollectionEvidence(config), "astrid-collection-evidence")


def pytest_plugin_registered(plugin):
    if Path(getattr(plugin, "__file__", "") or "").name == "conftest.py":
        for name in ("collect_ignore", "collect_ignore_glob"):
            if getattr(plugin, name, None):
                raise pytest.UsageError(f"undeclared {name} in {plugin.__file__}; use the lane registry")


def selected_items(config, items, selectors):
    """Recompute the job's declared -k/-m and shard selection from the census."""
    selected = list(items)
    if config.option.keyword:
        expression = Expression.compile(config.option.keyword.lstrip())
        selected = [item for item in selected if expression.evaluate(selectors[item.nodeid][0])]
    if config.option.markexpr:
        expression = Expression.compile(config.option.markexpr)
        selected = [item for item in selected if expression.evaluate(selectors[item.nodeid][1])]
    splits = config.getoption("splits", default=None)
    if splits:
        from pytest_split.algorithms import Algorithms

        duration_path = Path(config.option.durations_path)
        durations = json.loads(duration_path.read_text()) if duration_path.is_file() else {}
        if isinstance(durations, list):
            durations = dict(durations)
        algorithm = Algorithms[config.option.splitting_algorithm].value
        selected = algorithm(splits, selected, durations)[config.option.group - 1].selected
    return selected


class CollectionEvidence:
    def __init__(self, config):
        self.config = config
        self.items = []
        self.deselected = []
        self.static = StaticInventory(config)
        self.selectors = {}
        self.reports = None
        path = config.getoption("test_event_log")
        if path:
            output = Path(path)
            output.parent.mkdir(parents=True, exist_ok=True)
            self.reports = output.open("w", encoding="utf-8", buffering=1)
            self.write({"type": "start", "args": list(config.invocation_params.args)})

    def write(self, value):
        if self.reports is not None:
            self.reports.write(json.dumps(value, ensure_ascii=True) + "\n")
            self.reports.flush()

    def pytest_itemcollected(self, item):
        self.items.append(item)
        fixture_lane = self.static.record(item)
        markers = list(item.iter_markers())
        keyword = KeywordMatcher.from_item(item)
        if fixture_lane:
            markers.append(pytest.mark.integration.mark)
            keyword._names.add("integration")
        self.selectors[item.nodeid] = (keyword, MarkMatcher.from_markers(markers))

    def pytest_deselected(self, items):
        self.deselected.extend(item.nodeid for item in items)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_ignore_collect(self, collection_path, config):
        outcome = yield
        if not outcome.get_result():
            return
        path = Path(collection_path)
        requested = [(Path(config.invocation_params.dir) / arg.split("::", 1)[0]).resolve()
                     for arg in config.args]
        # Pytest asks ignore hooks about siblings while resolving an explicit
        # file selector. Those siblings were never part of this job's census.
        if requested and not any(path == root or root in path.parents or path in root.parents
                                 for root in requested):
            return
        if path.is_dir():
            # Pytest's ordinary traversal exclusions are part of the config,
            # protected against task-PR edits by the controls gate.
            if path.name.startswith(".") or any(fnmatch.fnmatch(path.as_posix(), pattern) or
                                               fnmatch.fnmatch(path.name, pattern)
                                               for pattern in config.getini("norecursedirs")):
                return
            matches = any(path.rglob("test_*.py"))
        else:
            matches = any(fnmatch.fnmatch(path.name, pattern) for pattern in config.getini("python_files"))
        if not matches:
            return
        guard = config.pluginmanager.get_plugin("skip-guard")
        key = path.relative_to(config.rootpath).as_posix() if path.is_relative_to(config.rootpath) else str(path)
        if guard is not None and key in {name.replace("\\", "/") for name in guard.ignored}:
            return
        raise pytest.UsageError(f"undeclared ignored test path: {key}")

    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session):
        ignored = getattr(self.config.pluginmanager.get_plugin("skip-guard"), "ignored", {})
        static = self.static.verify(ignored)
        expected = collections.Counter(item.nodeid for item in selected_items(self.config, self.items, self.selectors))
        selected = collections.Counter(item.nodeid for item in session.items)
        collected = collections.Counter(item.nodeid for item in self.items)
        removed = collections.Counter(self.deselected)
        manifest = {
            "collected": sorted(collected.elements()), "selected": sorted(selected.elements()),
            "deselected": sorted(removed.elements()),
            "ignored": ignored, "static": static,
        }
        target = Path(self.config.getoption("collection_manifest"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        self.write({"type": "collection", **manifest})
        if selected != expected or selected + removed != collected:
            raise pytest.UsageError(
                "static/collected selection inventory mismatch (collection inventory mismatch): "
                f"missing={list((expected - selected).elements())[:20]} "
                f"unexpected={list((selected - expected).elements())[:20]} "
                f"unaccounted={list((collected - selected - removed).elements())[:20]}"
            )
        reference = self.config.getoption("collection_reference")
        if reference and manifest != json.loads(Path(reference).read_text(encoding="utf-8")):
            raise pytest.UsageError("collection differs from the independent --collect-only reference")

    def pytest_runtest_logreport(self, report):
        names = mangle_test_address(report.nodeid)
        category = self.config.hook.pytest_report_teststatus(report=report, config=self.config)[0]
        self.write({
            "type": "report", "nodeid": report.nodeid, "when": report.when,
            "outcome": report.outcome, "category": category,
            "classname": ".".join(names[:-1]), "name": names[-1],
            "xfail": hasattr(report, "wasxfail"), "subtest": hasattr(report, "context"),
            "context": repr(getattr(report, "context", None)),
            "detail": str(report.longrepr) if report.failed or report.skipped else "",
        })

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session, exitstatus):
        yield
        reporter = self.config.pluginmanager.get_plugin("terminalreporter")
        parts, _ = reporter.build_summary_stats_line() if reporter is not None else ([], "")
        self.write({"type": "finish", "exitstatus": int(session.exitstatus),
                    "summary": ", ".join(text for text, _ in parts)})

    def pytest_unconfigure(self):
        if self.reports is not None:
            self.reports.close()
