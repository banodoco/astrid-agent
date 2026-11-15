from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.core.cli.domain_tasks import COMMANDS, build_parser
from astrid.sdk.contracts import DomainResult

TASK_ID = "a123456789abcdef"


class FakeTasks:
    """Shaped like the real SDK: a task's terminal result owns its outputs."""

    def __init__(self, outputs, *, state="succeeded"):
        self.outputs = outputs
        self.state = state

    def list(self, project_id, *, cursor=None, limit=50):
        return DomainResult.success([{"task_id": TASK_ID}])

    def show(self, task_id):
        assert task_id == TASK_ID
        return DomainResult.success({"task_id": TASK_ID, "state": self.state,
                                     "result": {"status": self.state, "outputs": self.outputs}})

    def list_managed_outputs(self, task_id):
        # A failed task has no managed outputs; its logs live only in its result.
        return DomainResult.success([] if self.state == "failed" else self.outputs)


class FakeMedia:
    def __init__(self):
        self.read = []

    def read_bytes(self, digest):
        self.read.append(digest)
        return b"log bytes\n"


class FakeClient:
    """Byte reads live on ``client.media``, as in ``astrid.sdk.remote.RemoteMedia``."""

    def __init__(self, outputs, *, state="succeeded"):
        self.tasks = FakeTasks(outputs, state=state)
        self.media = FakeMedia()

    @property
    def read(self):
        return self.media.read


def outputs():
    return [
        {"role": "artifact", "variant_key": "image", "media_type": "image/png", "size_bytes": 4, "digest": "sha256:image"},
        {"role": "log", "variant_key": "engine", "filename": "engine.log", "media_type": "text/plain", "size_bytes": 10, "digest": "sha256:engine"},
        {"role": "log", "filename": "worker.log", "media_type": "text/plain", "size_bytes": 12, "digest": "sha256:worker"},
    ]


def invoke(client, *args):
    parser = build_parser(client)
    parsed = parser.parse_args(["logs", "--project", "demo", TASK_ID, *args])
    return parsed.handler(parsed)


def test_lists_only_log_rows_in_creation_order(capsys):
    assert invoke(FakeClient(outputs())) == 0
    assert capsys.readouterr().out.splitlines() == [
        "engine  text/plain  10  sha256:engine",
        "worker.log  text/plain  12  sha256:worker",
    ]


def test_name_prints_selected_log_bytes(capsys):
    client = FakeClient(outputs())
    assert invoke(client, "--name", "engine") == 0
    assert client.read == ["sha256:engine"]
    assert capsys.readouterr().out == "log bytes\n"


def test_json_contains_only_role_log_rows(capsys):
    assert invoke(FakeClient(outputs()), "--json") == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["data"] == outputs()[1:]


def test_empty_logs_fails_with_task_message(capsys):
    assert invoke(FakeClient([])) == 1
    assert f"no logs recorded for task {TASK_ID}" in capsys.readouterr().err


def test_logs_registered_in_commands_and_parser():
    assert "logs" in {command.name for command in COMMANDS}
    parser = build_parser(FakeClient([]))
    parsed = parser.parse_args(["logs", "--project", "demo", TASK_ID])
    assert parsed.handler.__name__ == "_cmd_logs"


def test_a_failed_tasks_logs_come_from_its_terminal_result(capsys):
    client = FakeClient(outputs(), state="failed")
    assert invoke(client) == 0
    assert capsys.readouterr().out.splitlines() == [
        "engine  text/plain  10  sha256:engine",
        "worker.log  text/plain  12  sha256:worker",
    ]
    assert invoke(client, "--name", "engine") == 0
    assert client.read == ["sha256:engine"]
    assert capsys.readouterr().out == "log bytes\n"


def test_fake_client_matches_the_real_sdk_surface():
    from astrid.sdk.remote import RemoteMedia, RemoteTasks

    assert callable(getattr(RemoteTasks, "show", None))
    assert callable(getattr(RemoteMedia, "read_bytes", None))


@pytest.mark.parametrize("explicit,expected", [(None, "selected"), ("override", "override")])
def test_logs_uses_selected_or_explicit_project(explicit, expected, capsys):
    client = FakeClient(outputs())
    client.projects = type("Projects", (), {"current": lambda self: DomainResult.success(
        {"project": {"project_id": "selected"}})})()
    seen = []
    original = client.tasks.list
    client.tasks.list = lambda project_id, **kwargs: (seen.append(project_id) or original(project_id, **kwargs))
    args = ["logs", TASK_ID[:12]]
    if explicit:
        args += ["--project", explicit]
    parsed = build_parser(client).parse_args(args)
    assert parsed.handler(parsed) == 0
    assert seen == [expected]
    assert "engine" in capsys.readouterr().out


def test_logs_without_selection_names_the_fix(capsys):
    client = FakeClient(outputs())
    client.projects = type("Projects", (), {"current": lambda self: DomainResult.success({"project": None})})()
    with pytest.raises(SystemExit) as exc:
        build_parser(client).parse_args(["logs", TASK_ID])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "astrid projects select <id>" in err and "--project" in err


def test_logs_without_project_at_module_entrypoint(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    (tmp_path / "sitecustomize.py").write_text(
        "from contextlib import nullcontext\n"
        "from astrid.sdk.client import AstridClient\n"
        "from astrid.sdk.contracts import DomainResult\n"
        "class Projects:\n"
        "  def current(self): return DomainResult.success({'project': {'project_id': 'selected'}})\n"
        "class Tasks:\n"
        "  def list(self, project, **kwargs):\n"
        "    assert project == 'selected'\n"
        f"    return DomainResult.success([{{'task_id': '{TASK_ID}'}}])\n"
        "  def show(self, task):\n"
        "    return DomainResult.success({'result': {'outputs': [{'role': 'log', 'filename': 'worker.log'}]}})\n"
        "class Client:\n"
        "  projects = Projects()\n"
        "  tasks = Tasks()\n"
        "AstridClient.open_from_launcher = classmethod(lambda cls, **kwargs: nullcontext(Client()))\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(repo)))
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "astrid", "tasks", "logs", TASK_ID[:12]],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "worker.log" in result.stdout


def test_tasks_show_displays_stored_host_timing_in_json_and_human(capsys):
    from types import SimpleNamespace
    timing = {"schema": "astrid.generation.timing.v2", "host": {
        "total": {"seconds": 12.0}, "phases": {"execution": {"seconds": 10.0}},
        "unaccounted": {"seconds": 2.0, "raw_seconds": 2.0}}}
    task = {"task_id": TASK_ID, "result": {"timing": timing}}
    client = SimpleNamespace(tasks=SimpleNamespace(show=lambda _: DomainResult.success(task),
        list=lambda *args, **kwargs: DomainResult.success([task])))
    for flags in ([], ["--json"]):
        parser = build_parser(client)
        parsed = parser.parse_args(["show", "--project", "demo", TASK_ID, *flags])
        assert parsed.handler(parsed) == 0
        text = capsys.readouterr().out
        if flags:
            assert json.loads(text)["data"]["result"]["timing"] == timing
        else:
            assert "timing: 12.0s = execution 10.0 + unaccounted 2.0" in text
