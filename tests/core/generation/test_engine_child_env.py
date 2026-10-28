"""The engine owns the LD_LIBRARY_PATH of every child process it starts."""
from __future__ import annotations

import os

import pytest

from astrid.core.generation import _engine_python_bootstrap as bootstrap
from astrid.core.generation.engine_custody import _Owned
from astrid.core.generation.engine_contracts import CommandRequest

DRIVER = ["/usr/local/nvidia/lib", "/usr/local/nvidia/lib64"]
CALLER_LD = "/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/usr/local/cuda/lib64"


def _venv(tmp_path, *libs):
    venv = tmp_path / "venv"
    for lib in libs:
        (venv / "lib/python3.12/site-packages/nvidia" / lib / "lib").mkdir(parents=True)
    return str(venv)


def _ld(env):
    return env["LD_LIBRARY_PATH"].split(":")


def test_drops_cuda_entries_and_puts_venv_nvidia_first(tmp_path):
    venv = _venv(tmp_path, "cu13", "cudnn")
    caller = CALLER_LD + ":/usr/local/cuda-13.0/compat:/usr/local/cuda-13.0/lib64"
    ld = _ld(bootstrap.engine_child_env(venv, {"LD_LIBRARY_PATH": caller, "KEEP": "1"}))
    site = venv + "/lib/python3.12/site-packages/nvidia/"
    assert ld == [site + "cu13/lib", site + "cudnn/lib", *DRIVER]
    assert not any(p.startswith("/usr/local/cuda") for p in ld)


def test_missing_venv_dirs_are_skipped_and_other_env_is_kept(tmp_path):
    env = bootstrap.engine_child_env(str(tmp_path / "absent"), {"KEEP": "1"})
    assert env["KEEP"] == "1"
    assert _ld(env) == DRIVER


@pytest.mark.parametrize("caller", [None, "", CALLER_LD, "/opt/other/lib:/usr/local/cuda-12.8/compat"])
def test_result_does_not_depend_on_caller_ld_library_path(tmp_path, monkeypatch, caller):
    venv = _venv(tmp_path, "cu13")
    if caller is None:
        monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    else:
        monkeypatch.setenv("LD_LIBRARY_PATH", caller)
    base = {} if caller is None else {"LD_LIBRARY_PATH": caller}
    assert bootstrap.engine_child_env(venv, base) == bootstrap.engine_child_env(venv, {})


def test_owned_process_does_not_inherit_caller_ld_library_path(monkeypatch):
    seen = {}

    def fake_popen(argv, **kwargs):
        seen.update(kwargs["env"])
        raise RuntimeError("stop before spawning")

    import astrid.core.execution.process_group as pg
    monkeypatch.setattr(pg, "popen_owned_group", fake_popen)
    monkeypatch.setenv("LD_LIBRARY_PATH", CALLER_LD)
    with pytest.raises(RuntimeError):
        _Owned(CommandRequest(("true",), "/", {}, 1e12))
    assert "LD_LIBRARY_PATH" not in seen
    with pytest.raises(RuntimeError):
        _Owned(CommandRequest(("true",), "/", bootstrap.engine_child_env("/nonexistent/venv"), 1e12))
    assert seen["LD_LIBRARY_PATH"].split(":") == DRIVER


def test_supervisor_gives_every_spawn_the_engine_env_and_launch_fingerprint_is_stable(tmp_path, monkeypatch):
    """Probes, helpers and the launch share one spawn path; the fingerprint ignores LD."""
    from astrid.core.generation import engine_recipe
    from astrid.core.generation.engine_custody import _Supervisor

    key = "linux-x86_64-cu130"
    recipe = engine_recipe.load_recipe("vibecomfy-h3", key)
    primary = next(e for e in recipe.environments if e.name == recipe.primary_environment)
    root = str(tmp_path / "engine")
    sup = _Supervisor({"platform": key, "root": root, "engine": "vibecomfy-h3"}, recipe, "t")
    before = dict(sup.launch)
    site = f"{root}/{primary.relative_path}/lib/python3.12/site-packages/nvidia/cu13/lib"
    os.makedirs(site)  # the venv appears after the supervisor computed its fingerprint
    expected = [site, *DRIVER]
    for caller in ("", CALLER_LD, "/usr/local/cuda-13.0/lib64"):
        monkeypatch.setenv("LD_LIBRARY_PATH", caller)
        for env in ({}, {"LD_LIBRARY_PATH": caller}):
            request = sup.child_request(CommandRequest(("true",), root, env, 1e12))
            assert _ld(request.env) == expected
    assert "LD_LIBRARY_PATH" not in sup.launch["env"] and sup.launch == before
