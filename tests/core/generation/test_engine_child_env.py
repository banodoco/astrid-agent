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
    """Probes, helpers and the admitted launch share the engine environment."""
    from copy import deepcopy
    from pathlib import Path
    import socket
    from types import SimpleNamespace

    from astrid.core.generation import engine_custody as custody, engine_recipe, engine_runner

    key = "linux-x86_64-cu130"
    recipe = engine_recipe.load_recipe("comfyui", key)
    primary = next(e for e in recipe.environments if e.name == recipe.primary_environment)
    root = str(tmp_path / "engine")
    source = next(s for s in recipe.sources if s.name == "vibecomfy")
    source_root = Path(root, source.relative_path)
    source_root.mkdir(parents=True)
    (source_root / "fixture.py").write_text("VALUE = 1\n")
    control = Path(root, ".astrid-engine")
    control.mkdir(mode=0o700)
    (control / "session").mkdir(mode=0o700)
    identity = dict(platform=key, root=root, engine=recipe.engine,
                    recipe_sha256=engine_recipe.recipe_digest(recipe),
                    helper_sha256=engine_runner._sha(engine_runner.helper_manifest(recipe)))
    sup = custody._Supervisor(identity, recipe, "fixture-owner")
    spawned = []

    class OwnedProcess:
        """The process boundary; supervisor admission and identity remain real."""
        def __init__(self, request, *, secrets=(), output_log=None):
            spawned.append(request)
            git = request.argv[0] == "git"
            if git:
                assert request.cwd == str(source_root)
                outputs = {("git", "rev-parse", "HEAD"): source.pin.revision + "\n",
                           ("git", "ls-files", "-z", "-c", "-o", "--exclude-standard"): "fixture.py\0"}
                stdout = outputs[request.argv]
            else:
                stdout = ""
            self.process = SimpleNamespace(pid=os.getpid(), returncode=0 if git else None)
            self.stdout = SimpleNamespace(text=lambda: stdout, truncated=lambda: False)
            self.stderr = SimpleNamespace(text=lambda: "", truncated=lambda: False)
            if output_log is not None:
                Path(output_log).touch()

        def live(self):
            return self.process.returncode is None

        def stop(self, deadline):
            self.process.returncode = 0

    def response(port, path, limit, deadline, check):
        assert sup.engine is not None and sup.engine.live()
        check()
        if path == recipe.health.object_info_path:
            return {name: {} for name in recipe.health.required_nodes}
        assert path == recipe.health.path
        return {"system": {}}

    monkeypatch.setattr(custody, "_Owned", OwnedProcess)
    monkeypatch.setattr(socket.socket, "bind", lambda self, address: None)
    monkeypatch.setattr(custody, "_listener_owned", lambda process, port: True)
    monkeypatch.setattr(custody, "_get_json", response)
    request = sup.adapter.start_request(recipe, root, deadline=1e12)
    payload = {"schema": custody._SCHEMA, **identity,
               "launch": {"argv": list(request.argv), "cwd": request.cwd,
                          "env": {**request.env, "CUDA_VISIBLE_DEVICES": None}},
               "owner_id": None, "timeout_seconds": recipe.health.timeout_seconds,
               "poll_interval_seconds": recipe.health.poll_interval_seconds}
    channel, peer = socket.socketpair()
    try:
        assert sup.launch is None
        admitted = sup.service("start", payload, 1e12, channel)
        assert admitted["state"] == "owned" and admitted["healthy"] is True
        assert sup.engine is not None and spawned[-1].argv == request.argv
        assert _ld(spawned[-1].env) == DRIVER
        before = deepcopy(sup.launch)
        assert before == payload["launch"]
        assert "LD_LIBRARY_PATH" not in sup.launch["env"]
        fingerprint = admitted["launch_sha256"]
        assert fingerprint == engine_runner._sha(before)

        sites = []
        for library in ("cu13", "cudnn"):
            site = f"{root}/{primary.relative_path}/lib/python3.12/site-packages/nvidia/{library}/lib"
            os.makedirs(site)
            sites.append(site)
            expected = [*sites, *DRIVER]
            for caller in ("", CALLER_LD, "/usr/local/cuda-13.0/lib64"):
                monkeypatch.setenv("LD_LIBRARY_PATH", caller)
                for env in ({}, {"LD_LIBRARY_PATH": caller}):
                    command = CommandRequest(("true",), root, env, 1e12)
                    child = sup.child_request(command)
                    assert _ld(child.env) == expected
                    assert sup.launch == before
                    owned = sup.spawn(command)
                    assert _ld(spawned[-1].env) == expected
                    sup.drain(owned, 1e12)
                    assert "LD_LIBRARY_PATH" not in sup.launch["env"]
                    assert sup.launch == before
                observed = sup.service("health", payload, 1e12, channel)
                assert observed["launch_sha256"] == fingerprint
                assert sup.launch == before
    finally:
        try:
            sup.stop(deadline=1e12)
        finally:
            channel.close()
            peer.close()
