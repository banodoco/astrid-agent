from __future__ import annotations

import json

from astrid.core.cli.domain_tasks import COMMANDS, build_parser
from astrid.sdk.contracts import DomainResult


TASK_ID = "a123456789abcdef"


class FakeTasks:
    def __init__(self, outputs):
        self.outputs = outputs

    def list(self, project_id, *, cursor=None, limit=50):
        return DomainResult.success([{"task_id": TASK_ID}])

    def list_managed_outputs(self, task_id):
        assert task_id == TASK_ID
        return DomainResult.success(self.outputs)


class FakeClient:
    def __init__(self, outputs):
        self.tasks = FakeTasks(outputs)
        self.read = []

    def read_bytes(self, digest):
        self.read.append(digest)
        return b"log bytes\n"


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
