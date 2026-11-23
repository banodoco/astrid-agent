from __future__ import annotations

from types import SimpleNamespace
import json

from astrid.core.generation import cli
from astrid.sdk.command_timing import stage


def test_generation_json_timings_are_opt_in(monkeypatch, capsys):
    def generate(**_kwargs):
        with stage("generation work"):
            return SimpleNamespace(ok=True, to_dict=lambda: {"image_paths": ["test.png"]})

    monkeypatch.setattr(cli, "_generate_facade", lambda: SimpleNamespace(image=generate))
    args = ["image", "a dog", "--dry-run", "--json"]

    assert cli.main(args) == 0
    plain = capsys.readouterr()
    assert plain.out == '{\n  "image_paths": [\n    "test.png"\n  ]\n}\n'
    assert "timings" not in json.loads(plain.out)
    assert plain.err == ""

    assert cli.main([*args, "--timings"]) == 0
    timed = capsys.readouterr()
    payload = json.loads(timed.out)
    assert payload["image_paths"] == ["test.png"]
    assert any(row["stage"] == "generation work" for row in payload["timings"])
    assert "generation work:" in timed.err
