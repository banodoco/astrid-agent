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


@pytest.mark.parametrize("as_json", [False, True, "mcp"])
def test_named_log_redacts_secrets_without_rewriting_object(tmp_path, monkeypatch, capsys, as_json):
    original = b"https://ghp_abc123@github.com/o/r\nAuthorization: Bearer abc.def.ghi\n"
    stored = tmp_path / "engine.log"
    stored.write_bytes(original)
    client = FakeClient(outputs())

    def read_bytes(digest):
        client.media.read.append(digest)
        return stored.read_bytes()

    client.media.read_bytes = read_bytes
    args = ["--name", "engine", *(["--json"] if as_json else [])]
    if as_json == "mcp":
        from astrid import mcp_server

        def run(argv, _cwd):
            command = argv[argv.index("tasks") + 1:]
            assert command == ["logs", TASK_ID, "--project", "demo", "--name", "engine", "--json"]
            parsed = build_parser(client).parse_args(command)
            code = parsed.handler(parsed)
            captured = capsys.readouterr()
            return subprocess.CompletedProcess(argv, code, captured.out, captured.err)

        monkeypatch.setattr(mcp_server, "_run", run)
        tools = mcp_server._tools_for_parsers({"tasks": build_parser(client)})
        result = mcp_server.call_tool(tools["tasks_logs"],
                                      {"project": "demo", "task_id": TASK_ID, "name": "engine"})
        assert result["isError"] is False
        output = json.dumps(result)
    else:
        assert invoke(client, *args) == 0
        output = capsys.readouterr().out
    assert stored.read_bytes() == original
    assert client.tasks.outputs == outputs()
    assert client.read == ["sha256:engine"]  # exactly one existing object read
    assert "ghp_abc123" not in output and "abc.def.ghi" not in output
    assert "<redacted>" in output


@pytest.mark.parametrize("as_json", [False, True])
def test_log_listing_redacts_without_mutating_runtime_rows(capsys, as_json):
    message = "https://ghp_abc123@github.com/o/r Authorization: Bearer abc.def.ghi"
    rows = [{"role": "log", "filename": message, "digest": "sha256:engine"}]
    client = FakeClient(rows)
    assert invoke(client, *(["--json"] if as_json else [])) == 0
    output = capsys.readouterr().out
    assert rows[0]["filename"] == message
    assert client.read == []
    assert "ghp_abc123" not in output and "abc.def.ghi" not in output
    assert "<redacted>" in output
    if as_json:
        assert json.loads(output)["ok"] is True


def test_json_contains_only_role_log_rows(capsys):
    assert invoke(FakeClient(outputs()), "--json") == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    assert envelope["data"] == outputs()[1:]


def test_json_logs_redacts_escaped_pem_without_extra_object_reads(capsys):
    key = "-----BEGIN PRIVATE KEY-----\nkey-bytes\n-----END PRIVATE KEY-----"
    rows = [{"role": "log", "filename": f"before {key} after", "digest": "sha256:engine"}]
    client = FakeClient(rows)
    assert invoke(client, "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"][0]["filename"] == "before <redacted PEM> after"
    assert rows[0]["filename"] == f"before {key} after"
    assert client.read == []  # formatting adds zero remote object reads


def test_named_json_log_redacts_escaped_pem_with_one_object_read(capsys):
    key = "-----BEGIN PRIVATE KEY-----\nkey-bytes\n-----END PRIVATE KEY-----"
    original = json.dumps({"tail": f"before {key} after"}).encode()
    client = FakeClient(outputs())

    def read_bytes(digest):
        client.media.read.append(digest)
        return original

    client.media.read_bytes = read_bytes
    assert invoke(client, "--name", "engine", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {"tail": "before <redacted PEM> after"}
    assert client.read == ["sha256:engine"]


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
