from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core import subprocess_env
from astrid.core.execution.executor import runner as executor_runner
from astrid.core.rendering import transport as rendering_transport
from astrid.core.rendering.contracts import SCHEMA_VERSION, SupportReport
from astrid.core.rendering.transport import CommandTransport
from astrid.core.subprocess_env import build_child_subprocess_env


def test_windows_child_environment_keeps_only_required_windows_variables(monkeypatch):
    monkeypatch.setattr(subprocess_env, "sys", SimpleNamespace(platform="win32"))
    windows_values = {
        name: f"value-{name}"
        for name in (
            "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
            "SYSTEMROOT", "TEMP", "TMP", "PATHEXT", "COMSPEC",
        )
    }
    env = build_child_subprocess_env(base=windows_values, parent={})
    assert env == windows_values


def test_non_windows_child_environment_does_not_admit_windows_only_variables(monkeypatch):
    monkeypatch.setattr(subprocess_env, "sys", SimpleNamespace(platform="linux"))
    env = build_child_subprocess_env(
        base={"USERPROFILE": "profile", "APPDATA": "appdata", "SYSTEMROOT": "root", "TEMP": "temp"},
        parent={},
    )
    assert env == {}


@pytest.mark.parametrize("marker,value", [
    ("ASTRID_RENDER_INHERIT_PROCESS_GROUP", "1"),
    ("ASTRID_INSTALL_PROGRAM_ROOT", "/custom/Astrid program"),
])
def test_parent_marker_survives_executor_and_transport(
    tmp_path: Path, monkeypatch, marker: str, value: str,
) -> None:
    monkeypatch.setenv(marker, value)
    monkeypatch.setattr(executor_runner, "_external_pack_pythonpath_env", lambda *_: {})
    monkeypatch.setattr(executor_runner, "_project_subprocess_env", lambda *_: {})
    monkeypatch.setattr(executor_runner, "_emit_scoped_config_env", lambda *_: {})
    monkeypatch.setattr(executor_runner, "dependency_pythonpath", lambda: ())
    executor = SimpleNamespace(
        isolation=SimpleNamespace(env_passthrough=(), secrets_required=()),
        metadata={},
        scoped_configs=(),
    )
    request = SimpleNamespace(project="render-project")

    executor_environment = executor_runner._command_subprocess_env(
        executor, request, {}
    )
    assert executor_environment[marker] == value

    launched = {}

    def fake_popen(argv, **kwargs):
        launched.update(kwargs)
        result_path = Path(argv[argv.index("--result") + 1])
        result_path.write_text(
            json.dumps(
                SupportReport(
                    schema_version=SCHEMA_VERSION,
                    supported=True,
                    reasons=[],
                    features={},
                    alternatives=[],
                    backend="rendering.remotion",
                ).to_dict()
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            pid=12345,
            returncode=0,
            communicate=lambda **_: ("", ""),
        )

    monkeypatch.setattr(rendering_transport, "_resolve_executable", lambda name, **_: name)
    monkeypatch.setattr(rendering_transport, "popen_owned_process", fake_popen)
    monkeypatch.setattr(rendering_transport, "observe_tree", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        rendering_transport, "_terminate_leftover_group", lambda *_args, **_kwargs: None
    )

    report = CommandTransport("rendering.remotion").run(
        "support",
        ["python3"],
        request_path=tmp_path / "request.json",
        result_path=tmp_path / "result.json",
        cwd=tmp_path,
        env=executor_environment,
    )

    assert report.supported is True
    assert launched["start_new_session"] is (marker != "ASTRID_RENDER_INHERIT_PROCESS_GROUP")
    assert launched["env"][marker] == value
