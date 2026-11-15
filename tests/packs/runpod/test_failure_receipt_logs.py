import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import cli
from astrid.packs.runpod import failure_receipt


@pytest.mark.parametrize("args", [[], ["--json"], ["--logs"], ["--logs", "--json"],
                                 ["--name", "generation.stdout"],
                                 ["--name", "generation.stdout", "--json"], ["mcp"]])
def test_failure_output_redacts_secrets_without_changing_receipt_or_logs(tmp_path, monkeypatch, capsys, args):
    message = "https://ghp_abc123@github.com/o/r Authorization: Bearer abc.def.ghi"
    log = tmp_path / "generation.stdout.json"
    original = json.dumps({"issues": [{"message": message}]}).encode()
    log.write_bytes(original)
    receipt = failure_receipt.build_receipt(
        {"failure": RuntimeError("failed"), "stage": "claim",
         "lease": SimpleNamespace(failure_logs={"files": [
             {"name": "generation.stdout", "path": str(log)}]})},
        job=SimpleNamespace(task_id="task"), machine=None, timing=None)
    assert receipt["cause"]["issue_message"] == message
    path = failure_receipt.write_receipt(receipt, root=tmp_path / "failures")
    stored = path.read_bytes()
    copied = Path(json.loads(stored)["logs"][0]["path"])
    monkeypatch.setattr(failure_receipt, "find_receipt", lambda _task_id: path)

    if args == ["mcp"]:
        from astrid import mcp_server

        def run(argv, _cwd):
            command = argv[argv.index("generate") + 1:]
            assert command == ["failure", "--json"]
            code = cli._failure_command(cli._parser().parse_args(command))
            captured = capsys.readouterr()
            return subprocess.CompletedProcess(argv, code, captured.out, captured.err)

        monkeypatch.setattr(mcp_server, "_run", run)
        result = mcp_server.call_tool(mcp_server._generation_tools()["generate_failure"], {})
        assert result["isError"] is False
        output = json.dumps(result)
    else:
        assert cli._failure_command(cli._parser().parse_args(["failure", *args])) == 0
        output = capsys.readouterr().out
    assert path.read_bytes() == stored
    assert log.read_bytes() == copied.read_bytes() == original
    assert "ghp_abc123" not in output and "abc.def.ghi" not in output
    assert "<redacted>" in output
    if args == ["--json"]:
        assert "<redacted>" in json.loads(output)["cause"]["issue_message"]


@pytest.mark.parametrize("boundary", ["render", "kept-details"])
def test_receipt_display_returns_redacted_cause_without_rewriting_file(tmp_path, monkeypatch, boundary):
    message = "https://ghp_abc123@github.com/o/r Authorization: Bearer abc.def.ghi"
    receipt = {"cause": {"issue_message": message}, "identity": {"lease": "lease", "generation": 1}}
    path = failure_receipt.write_receipt(receipt, root=tmp_path)
    original = path.read_bytes()
    monkeypatch.setattr(failure_receipt, "failures_root", lambda: tmp_path)
    output = (failure_receipt.render(receipt) if boundary == "render" else
              json.dumps(failure_receipt.kept_failure_details("lease", 1)))
    assert path.read_bytes() == original
    assert receipt["cause"]["issue_message"] == message
    assert "ghp_abc123" not in output and "abc.def.ghi" not in output
    assert "<redacted>" in output


def test_receipt_copies_declared_logs_and_keeps_missing_row(tmp_path):
    engine_error = tmp_path / "engine-error.json"
    engine = tmp_path / "engine.log"
    engine_error.write_bytes(b'{"error":"bad"}')
    engine.write_bytes(b"engine output\x00")
    lease = SimpleNamespace(failure_logs={"files": [
        {"name": "engine-error", "role": "engine", "path": str(engine_error), "bytes": 15,
         "truncated": False, "missing": False},
        {"name": "engine", "role": "engine", "path": str(engine), "bytes": 14,
         "truncated": False, "missing": False},
        {"name": "job.stderr", "role": "stderr", "path": None, "bytes": 0,
         "truncated": False, "missing": True, "reason": "FileNotFoundError"},
    ]})
    receipt = failure_receipt.build_receipt(
        {"failure": RuntimeError("failed"), "stage": "claim", "lease": lease},
        job=SimpleNamespace(task_id="task"), machine=None, timing=None)

    path = failure_receipt.write_receipt(receipt, root=tmp_path / "failures")
    written = json.loads(path.read_text())
    assert len(written["logs"]) == 3
    copied = {row["name"]: row for row in written["logs"]}
    assert Path(copied["engine-error"]["path"]).suffix == ".json"
    assert Path(copied["engine"]["path"]).suffix == ".log"
    assert Path(copied["engine-error"]["path"]).read_bytes() == engine_error.read_bytes()
    assert Path(copied["engine"]["path"]).read_bytes() == engine.read_bytes()
    assert copied["job.stderr"]["path"] is None and copied["job.stderr"]["missing"]


def test_render_orders_by_role_and_shows_missing_reason():
    rendered = failure_receipt.render({"logs": [
        {"name": "job.stderr", "role": "stderr", "bytes": 3, "path": "/stderr"},
        {"name": "engine", "role": "engine", "bytes": 4, "path": "/engine"},
        {"name": "engine-error", "role": "engine", "bytes": 8, "path": "/error.json"},
        {"name": "gone", "role": "run", "bytes": 0, "missing": True,
         "reason": "FileNotFoundError"},
    ]})
    assert rendered.index("Log: engine ") < rendered.index("Log: engine-error ")
    assert rendered.index("Log: engine-error ") < rendered.index("Log: job.stderr ")
    assert "Log: gone missing (FileNotFoundError)" in rendered


def test_failure_command_prints_logs_and_selects_one(tmp_path, monkeypatch, capsys):
    engine_error = tmp_path / "engine-error.json"
    engine = tmp_path / "engine.log"
    engine_error.write_text("engine error text\n")
    engine.write_text("engine log text\n")
    receipt_path = tmp_path / "failure-receipt.json"
    receipt_path.write_text(json.dumps({"logs": [
        {"name": "engine", "role": "engine", "path": str(engine)},
        {"name": "engine-error", "role": "engine", "path": str(engine_error)},
    ]}))
    monkeypatch.setattr(failure_receipt, "find_receipt", lambda _task_id: receipt_path)

    parsed = cli._parser().parse_args(["failure", "--logs"])
    assert cli._failure_command(parsed) == 0
    output = capsys.readouterr().out
    assert "==> engine-error <==\nengine error text" in output
    assert "==> engine <==\nengine log text" in output

    parsed = cli._parser().parse_args(["failure", "--logs", "--name", "engine"])
    assert cli._failure_command(parsed) == 0
    output = capsys.readouterr().out
    assert "==> engine <==" in output and "engine log text" in output
    assert "engine-error" not in output and "engine error text" not in output

    parsed = cli._parser().parse_args(["failure", "--logs", "--name", "nope"])
    assert cli._failure_command(parsed) == 1
    assert "nope" in capsys.readouterr().err


def test_issue_message_is_bounded_and_unreadable_output_is_ignored(tmp_path):
    log = tmp_path / "generation.stdout.json"
    row = {"name": "generation.stdout", "path": str(log)}
    log.write_text(json.dumps({"issues": [{"message": "x" * 2000}, {"message": "later"}]}))
    assert failure_receipt._issue_message([row]) == "x" * failure_receipt.ISSUE_MESSAGE_LIMIT
    for content in ("{", json.dumps({"issues": [None]}), json.dumps({"issues": [{"message": 3}]})):
        log.write_text(content)
        assert failure_receipt._issue_message([row]) is None
    log.unlink()
    assert failure_receipt._issue_message([row]) is None
