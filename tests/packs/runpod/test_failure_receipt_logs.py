import json
from pathlib import Path
from types import SimpleNamespace

from astrid.core.generation import cli
from astrid.packs.runpod import failure_receipt


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
