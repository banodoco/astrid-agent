"""A02's isolated launch service: fake children, native observations and HTTP."""
from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import target_launch as launch
from astrid.core.generation.targets import TargetRecord, TargetSelection, resolve_target

REAL_HEALTH_PROBE = launch._health_probe


@pytest.fixture(autouse=True)
def no_real_launch_or_http(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("test attempted a real human process or HTTP probe")
    monkeypatch.setattr(launch.subprocess, "Popen", forbidden)
    monkeypatch.setattr(launch, "_health_probe", forbidden)


@pytest.fixture
def target(tmp_path):
    root = tmp_path / "human ComfyUI"
    root.mkdir()
    app = root / "main.py"
    app.write_text("# selected human ComfyUI, never executed\n")
    python = tmp_path / "human venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fake interpreter, never executed")
    python.chmod(0o700)
    (python.parent.parent / "pyvenv.cfg").write_text("home = /selected-python\n")
    return TargetRecord(endpoint="http://127.0.0.1:8199", layout="git",
                        root=str(root), python=str(python), facts={
                            "launch_app": str(app),
                            "launch_argv": [str(python), str(app), "--port", "8199"],
                        })


class Harness:
    def __init__(self, target):
        self.target = target
        self.now = 0.0
        self.calls = []
        self.spawned = []
        self.running = None
        self.listening = False
        self.healthy = True
        self.probes = []
        self.on_spawn = self.on_probe = self.on_listeners = None
        self.expected = launch.ProcessObservation(
            4321, "boot:1234", tuple(target.facts["launch_argv"]),
            str(Path(target.root).resolve()), str(Path(target.python).resolve()))

    def process(self, pid, *, deadline):
        self.calls.append(("process", pid, deadline))
        return self.running if self.running is not None and self.running.pid == pid else None

    def listeners(self, endpoint, *, deadline):
        assert endpoint == self.target.endpoint
        self.calls.append(("listeners", endpoint, deadline))
        if self.on_listeners:
            self.on_listeners()
        return (self.running,) if self.listening else ()

    def spawn(self, argv, **kwargs):
        self.spawned.append((argv, kwargs))
        self.running = self.expected
        self.listening = True
        if self.on_spawn:
            self.on_spawn()
        return SimpleNamespace(pid=self.expected.pid)

    def probe(self, endpoint, *, timeout):
        assert endpoint == self.target.endpoint
        assert 0 < timeout <= 1.0
        self.calls.append(("health", endpoint, timeout))
        self.probes.append((endpoint, timeout))
        if self.on_probe:
            self.on_probe()
        return self.healthy

    def sleep(self, duration):
        assert duration >= 0
        self.now += duration

    def run(self, target=None, **kwargs):
        return launch.launch_target(target or self.target, observer=self, spawn=self.spawn,
                                    health_probe=self.probe, monotonic=lambda: self.now,
                                    sleep=self.sleep, **{"consent": ("user_env",),
                                                        "timeout": 0.3, **kwargs})


def reason(result):
    assert not result.ready
    assert result.readiness["status"] == "blocked"
    assert result.readiness["issues"][0]["details"]["manual_start"] is True
    return result.readiness["issues"][0]["details"]["reason"]


@pytest.fixture
def public_launch(target, tmp_path, monkeypatch):
    from astrid.core.generation import targets
    from astrid.sdk.generation import GenerationFacade

    path = tmp_path / 'config.toml'
    monkeypatch.setattr(targets, 'config_path', lambda: path)
    monkeypatch.delenv('VIBECOMFY_COMFYUI_URL', raising=False)
    targets.save_target(target)
    original = path.read_bytes()
    h = Harness(target)
    monkeypatch.setattr(launch, 'ProcLaunchObserver', lambda: h)
    monkeypatch.setattr(launch.subprocess, 'Popen', h.spawn)
    monkeypatch.setattr(launch, '_health_probe', h.probe)
    monkeypatch.setattr(launch.time, 'monotonic', lambda: h.now)
    monkeypatch.setattr(launch.time, 'sleep', h.sleep)
    yield GenerationFacade(), h, path
    assert path.read_bytes() == original


def test_sdk_launch_and_reprobe_require_fresh_consent_each_time(public_launch):
    facade, h, _ = public_launch
    blocked = facade.launch()
    assert blocked['issues'][0]['details']['reason'] == 'user_env_required'
    assert not h.calls and not h.spawned
    result = facade.launch(consent=('user_env',), timeout=0.3)
    assert result['status'] == 'ready'
    assert result['target']['facts']['launch_observation']['birth_id'] == h.expected.birth_id
    assert facade.launch(consent=('paid',))['status'] == 'blocked'
    assert facade.launch(consent=('user_env',))['status'] == 'ready'
    assert len(h.spawned) == 1


@pytest.mark.parametrize('override', ['url', 'env'])
@pytest.mark.parametrize('same_endpoint', [False, True])
def test_sdk_override_drops_saved_launch_authority(public_launch, monkeypatch, override, same_endpoint):
    facade, h, _ = public_launch
    url = h.target.endpoint if same_endpoint else 'http://127.0.0.1:8200'
    kwargs = {}
    if override == 'url':
        kwargs['target'] = url
    else:
        monkeypatch.setenv('VIBECOMFY_COMFYUI_URL', url)
    result = facade.launch(consent=('user_env',), **kwargs)
    assert result['status'] == 'blocked'
    assert result['target']['endpoint'] == url
    assert result['target']['root'] is None and result['target']['python'] is None
    assert result['target']['facts'] == {}
    assert not h.spawned and not h.probes and not h.calls


def test_public_cli_timeout_is_blocked_with_manual_start_and_no_retry(public_launch, capsys):
    import json
    from astrid import console

    _, h, _ = public_launch
    h.healthy = False
    assert console.main(['generate', 'launch', '--consent', 'user_env',
                         '--timeout', '0.3', '--json']) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'blocked'
    issue = result['issues'][0]
    assert issue['code'] == 'target_unreachable'
    assert issue['details']['reason'] == 'launch_timeout'
    assert issue['details']['manual_start'] is True
    assert result['fixes'][0]['argv'] == h.target.facts['launch_argv']
    assert result['fixes'][0]['details']['cwd'] == h.target.root
    assert len(h.spawned) == 1 and 0 < len(h.probes) <= 100


def test_sdk_launch_refuses_another_listener_without_fallback(public_launch):
    facade, h, _ = public_launch
    h.running = replace(h.expected, birth_id='other:birth', argv=('/other/python', 'main.py'))
    h.listening = True
    result = facade.launch(consent=('user_env',))
    assert result['status'] == 'blocked'
    assert result['issues'][0]['details']['reason'] == 'launch_unverified'
    assert result['target']['endpoint'] == h.target.endpoint
    assert not h.spawned and not h.probes


def test_success_exact_argv_no_shell_and_independent_same_target_reprobe(target):
    h = Harness(target)
    plan = launch.prepare_target_launch(target)
    original = copy.deepcopy(target.as_dict())
    result = h.run(TargetSelection(target, "global", True), plan=plan)
    assert result.ready and result.launched
    assert result.pid == h.expected.pid and result.process_birth_id == h.expected.birth_id
    assert result.target.root == target.root and result.target.python == target.python
    assert result.target.facts["launch_observation"] == {
        "endpoint": target.endpoint, "pid": h.expected.pid, "birth_id": h.expected.birth_id}
    assert target.as_dict() == original  # no standing grant or config mutation
    assert len(h.spawned) == 1
    argv, options = h.spawned[0]
    assert argv == target.facts["launch_argv"]
    assert options == {"cwd": target.root, "shell": False, "close_fds": True,
                       "stdin": launch.subprocess.DEVNULL, "stdout": launch.subprocess.DEVNULL,
                       "stderr": launch.subprocess.DEVNULL, "start_new_session": True}
    health_index = next(i for i, call in enumerate(h.calls) if call[0] == "health")
    assert any(call[0] == "process" for call in h.calls[:health_index])
    assert [call[0] for call in h.calls[health_index + 1:]] == ["process", "listeners"]
    # The next call re-probes this exact existing server without a second spawn.
    assert h.run(plan=plan).ready
    assert len(h.spawned) == 1


def test_selected_symlinked_venv_python_is_preserved_in_successful_launch(target):
    python = Path(target.python)
    binary = python.with_name("base-python")
    python.rename(binary)
    python.symlink_to(binary)
    target.facts["launch_argv"].insert(1, "-s")
    h = Harness(target)
    assert h.run().ready
    assert h.spawned[0][0][0] == target.python
    assert h.expected.executable == str(binary)


@pytest.mark.parametrize("script", ["main.py", "./main.py"])
def test_relative_script_argv_is_preserved_with_exact_selected_working_directory(target, script):
    target.facts["launch_argv"][1] = script
    h = Harness(target)
    assert h.run().ready
    assert h.spawned[0][0][1] == script
    assert h.spawned[0][1]["cwd"] == target.root


@pytest.mark.parametrize("consent", [(), ("paid",), "user_env", True, {"saved": "user_env"}])
def test_no_consent_even_with_saved_facts_and_retained_plan(target, consent):
    target.facts.update(consent=["user_env"], user_env=True, prefer="attached")
    h = Harness(target)
    result = h.run(plan=launch.prepare_target_launch(target), consent=consent)
    assert reason(result) == "user_env_required"
    assert h.spawned == h.calls == []


@pytest.mark.parametrize("field", ["argv", "app", "root", "python", "endpoint"])
def test_selected_facts_tampered_after_plan_are_rejected(target, tmp_path, field):
    h = Harness(target)
    plan = launch.prepare_target_launch(target)
    changed = copy.deepcopy(target)
    if field == "argv":
        changed.facts["launch_argv"].append("--cpu")
    elif field == "app":
        changed.facts["launch_app"] = str(tmp_path / "other.py")
    elif field == "root":
        changed = replace(changed, root=str(tmp_path))
    elif field == "python":
        changed = replace(changed, python="/other/python")
    else:
        changed = replace(changed, endpoint="http://127.0.0.1:8200")
    assert reason(h.run(changed, plan=plan)) == "launch_unverified"
    assert not h.spawned and not h.probes


@pytest.mark.parametrize("path_kind", ["app", "python", "venv", "root"])
@pytest.mark.parametrize("stage", ["before_spawn", "during_health"])
def test_path_tampering_at_each_boundary(target, path_kind, stage, tmp_path):
    h = Harness(target)
    plan = launch.prepare_target_launch(target)

    def tamper():
        if path_kind == "root":
            root = Path(target.root)
            root.rename(tmp_path / "old-root")
            root.mkdir()
            (root / "main.py").write_text("# replacement root")
        else:
            path = Path({"app": target.facts["launch_app"], "python": target.python,
                         "venv": str(Path(target.python).parent.parent / "pyvenv.cfg")}[path_kind])
            path.write_bytes(b"tampered")
    if stage == "before_spawn":
        tamper()
    else:
        h.on_probe = tamper
    result = h.run(plan=plan)
    assert reason(result) == "launch_unverified"
    assert len(h.spawned) == (stage == "during_health")


@pytest.mark.parametrize("link_kind", ["python", "parent", "app"])
@pytest.mark.parametrize("stage", ["before_spawn", "during_health"])
def test_symlink_replacement_even_to_same_bytes_is_rejected(target, tmp_path, link_kind, stage):
    if link_kind == "parent":
        link = Path(target.python).parent
        real = link.with_name("real-bin")
        link.rename(real)
        link.symlink_to(real, target_is_directory=True)
    else:
        link = Path(target.python if link_kind == "python" else target.facts["launch_app"])
        real = link.with_name("real-" + link.name)
        link.rename(real)
        link.symlink_to(real)
    h = Harness(target)
    plan = launch.prepare_target_launch(target)
    replacement = tmp_path / "replacement-link"
    replacement.symlink_to(real, target_is_directory=link_kind == "parent")

    def tamper():
        replacement.replace(link)
    if stage == "before_spawn":
        tamper()
    else:
        h.on_probe = tamper
    result = h.run(plan=plan)
    assert reason(result) == "launch_unverified"
    assert len(h.spawned) == (stage == "during_health")


def test_listener_races_last_pre_spawn_check(target):
    h = Harness(target)
    def appeared():
        if sum(call[0] == "listeners" for call in h.calls) == 2:
            h.running, h.listening = replace(h.expected, pid=999), True
    h.on_listeners = appeared
    assert reason(h.run()) == "launch_unverified"
    assert not h.spawned and not h.probes


def test_path_replaced_during_last_port_observation_is_rechecked_before_spawn(target):
    h = Harness(target)
    def changed():
        if sum(call[0] == "listeners" for call in h.calls) == 2:
            Path(target.facts["launch_app"]).write_text("# changed before spawn")
    h.on_listeners = changed
    assert reason(h.run()) == "launch_unverified"
    assert not h.spawned


@pytest.mark.parametrize("stage", ["before_spawn", "before_health", "during_health"])
def test_other_listener_is_never_accepted_or_replaced(target, stage):
    h = Harness(target)
    def other():
        h.running = replace(h.expected, pid=999, birth_id="other:999", argv=("other",))
        h.listening = True
    if stage == "before_spawn":
        other()
    elif stage == "before_health":
        h.on_spawn = other
    else:
        h.on_probe = other
    assert reason(h.run()) == "launch_unverified"
    assert len(h.spawned) == (stage != "before_spawn")


@pytest.mark.parametrize("field", ["birth_id", "argv", "root", "executable"])
def test_same_pid_changed_identity_during_healthy_response_is_rejected(target, field):
    h = Harness(target)
    def tamper():
        h.running = replace(h.expected, **{field: ("other",) if field == "argv" else "other"})
    h.on_probe = tamper
    assert reason(h.run()) == "launch_unverified"
    assert len(h.spawned) == 1


@pytest.mark.parametrize("live", [False, True])
def test_recorded_birth_is_checked_before_spawn(target, live):
    h = Harness(target)
    target.facts["launch_process"] = {"pid": h.expected.pid, "birth_id": h.expected.birth_id}
    if live:
        h.running = replace(h.expected, birth_id="reused-pid")
        assert reason(h.run()) == "launch_unverified"
        assert not h.spawned
    else:
        assert h.run().ready
        assert len(h.spawned) == 1


def test_existing_exact_recorded_process_waits_without_duplicate_spawn(target):
    h = Harness(target)
    target.facts["launch_process"] = {"pid": h.expected.pid, "birth_id": h.expected.birth_id}
    h.running = h.expected
    def sleep(duration):
        h.now += duration
        h.listening = True
    h.sleep = sleep
    result = h.run()
    assert result.ready and not result.launched and not h.spawned


def test_existing_process_timeout_reports_that_it_may_still_be_running(target):
    h = Harness(target)
    target.facts["launch_process"] = {"pid": h.expected.pid, "birth_id": h.expected.birth_id}
    h.running = h.expected
    result = h.run()
    assert reason(result) == "launch_timeout"
    assert result.readiness["issues"][0]["details"]["process_may_be_running"] is True
    assert not result.launched and not h.spawned


@pytest.mark.parametrize("failure", ["unhealthy", "no_listener", "exception", "late_health"])
def test_bounded_timeout_returns_manual_start_without_false_readiness_or_retry(target, failure):
    h = Harness(target)
    if failure == "unhealthy":
        h.healthy = False
    elif failure == "no_listener":
        h.on_spawn = lambda: setattr(h, "listening", False)
    elif failure == "exception":
        def failed():
            raise TimeoutError("fake timeout")
        h.on_probe = failed
    else:
        h.on_probe = lambda: setattr(h, "now", 1.0)
    result = h.run()
    assert reason(result) == "launch_timeout"
    assert result.launched and len(h.spawned) == 1
    assert h.now >= 0.3
    assert len(h.probes) <= 4
    assert all(probe[0] == target.endpoint for probe in h.probes)
    assert h.running is not None  # no terminate, restart, managed adoption or fallback
    assert result.readiness["fixes"] == [{
        "argv": target.facts["launch_argv"], "scope": "user_env", "details": {
            "manual_start": True, "cwd": target.root, "check_existing_process": True}}]


def test_probe_count_is_bounded_even_with_a_nonadvancing_fake_clock(target):
    h = Harness(target)
    h.healthy = False
    h.sleep = lambda _: None
    assert reason(h.run()) == "launch_timeout"
    assert len(h.probes) == 100 and len(h.spawned) == 1


def test_native_observation_timeout_gives_manual_start_without_spawn(target):
    h = Harness(target)
    def expired():
        raise TimeoutError("fake native deadline")
    h.on_listeners = expired
    assert reason(h.run()) == "launch_timeout"
    assert not h.spawned and not h.probes


@pytest.mark.parametrize("consent", [(), ("user_env",)])
@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:8200", "http://127.0.0.1:8199"])
def test_endpoint_override_discards_all_prior_facts_even_at_same_address(target, endpoint, consent):
    h = Harness(target)
    result = h.run(plan=launch.prepare_target_launch(target), endpoint_override=endpoint, consent=consent)
    assert reason(result) == "endpoint_override"
    assert result.target == TargetRecord(endpoint=endpoint)
    assert not h.calls and not h.spawned


def test_resolver_override_cannot_launch_saved_installation(target, tmp_path):
    from astrid.core.generation.targets import save_target
    path = tmp_path / "config.toml"
    save_target(target, path=path)
    before = path.read_bytes()
    selection = resolve_target(path=path, environ={"VIBECOMFY_COMFYUI_URL": target.endpoint})
    h = Harness(target)
    assert reason(h.run(selection)) == "launch_unverified"
    assert not h.spawned and path.read_bytes() == before


@pytest.mark.parametrize("change", ["managed", "runpod", "unknown", "missing_app", "shell", "python_c"])
def test_unsupported_or_unselected_target_never_falls_back(target, change):
    h = Harness(target)
    if change == "managed":
        target = replace(target, owner="astrid")
    elif change == "runpod":
        target = replace(target, location="runpod")
    elif change == "unknown":
        target = TargetSelection(None, "request:managed", True)
    elif change == "missing_app":
        target.facts.pop("launch_app")
    elif change == "shell":
        target.facts["launch_argv"] = "python main.py; other"
    else:
        target.facts["launch_argv"] = [target.python, "-c", "evil()"]
    assert reason(h.run(target)) == "launch_unverified"
    assert h.calls == h.spawned == []


@pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan"), 31, True])
def test_invalid_bounds_do_not_spawn(target, bad):
    h = Harness(target)
    assert reason(h.run(timeout=bad)) == "invalid_bound"
    assert not h.spawned


@pytest.mark.parametrize("port_args", [[], ["--port"], ["--port", "8200"],
                                       ["--port", "8199", "--port=8199"], ["--port=bad"]])
def test_argv_must_address_the_selected_endpoint_before_spawn(target, port_args):
    target.facts["launch_argv"] = [target.python, target.facts["launch_app"], *port_args]
    h = Harness(target)
    assert reason(h.run()) == "launch_unverified"
    assert not h.calls and not h.spawned


def test_missing_native_proof_blocks_before_spawn(target, monkeypatch):
    monkeypatch.setattr(launch.sys, "platform", "unsupported")
    result = launch.launch_target(target, consent=("user_env",))
    assert reason(result) == "launch_unverified"
    assert not result.launched


def test_bounded_startup_reprobes_same_endpoint_until_healthy(target):
    h = Harness(target)
    h.healthy = False
    def warming():
        h.healthy = len(h.probes) == 3
    h.on_probe = warming
    assert h.run(timeout=1).ready
    assert len(h.spawned) == 1 and len(h.probes) == 3
    assert all(endpoint == target.endpoint for endpoint, _ in h.probes)


def test_health_probe_checks_comfy_shape_and_uses_bounded_existing_client(monkeypatch):
    # Exercise the production HTTP helper without any network request.
    observed = []
    response = {"system": {}, "devices": []}
    def request(self, path, **kwargs):
        observed.append((self.target.url, path, kwargs))
        return response
    monkeypatch.setattr(launch.ComfyTargetClient, "request_json", request)
    assert REAL_HEALTH_PROBE("http://127.0.0.1:8199", timeout=0.2)
    assert observed == [("http://127.0.0.1:8199", "/system_stats",
                         {"timeout": 0.2, "response_limit": 64 * 1024})]
    response = {"system": "unrelated server", "devices": []}
    assert not REAL_HEALTH_PROBE("http://127.0.0.1:8199", timeout=0.2)


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    native_path = Path
    def path(value):
        value = str(value)
        return native_path(str(root) + value[5:] if value == "/proc" or value.startswith("/proc/") else value)
    monkeypatch.setattr(launch, "Path", path)
    monkeypatch.setattr(launch.sys, "platform", "linux")
    monkeypatch.setattr(launch.time, "monotonic", lambda: 0)
    boot = root / "sys/kernel/random/boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text("fake-boot\n")
    return root


def write_process(root, *, ticks="1234"):
    folder = root / "4321"
    folder.mkdir(exist_ok=True)
    fields = ["S"] + ["0"] * 18 + [ticks]
    (folder / "stat").write_text("4321 (Python with spaces) " + " ".join(fields))
    (folder / "cmdline").write_bytes(b"/human/python\0/human/main.py\0")
    for name, value in (("cwd", "/human"), ("exe", "/human/python")):
        link = folder / name
        if not link.is_symlink():
            link.symlink_to(value)
    return folder


def test_native_observer_brackets_process_birth_without_starting_process(fake_proc, monkeypatch):
    folder = write_process(fake_proc)
    observer = launch.ProcLaunchObserver()
    assert observer.process(4321, deadline=1) == launch.ProcessObservation(
        4321, "fake-boot:1234", ("/human/python", "/human/main.py"), "/human", "/human/python")
    original_read = observer._read
    def changed(path, *args):
        result = original_read(path, *args)
        if Path(path).name == "cmdline":
            write_process(fake_proc, ticks="9999")
        return result
    monkeypatch.setattr(observer, "_read", changed)
    with pytest.raises(launch.LaunchVerificationError, match="birth changed"):
        observer.process(4321, deadline=1)
    monkeypatch.setattr(observer, "_read", original_read)
    (folder / "exe").unlink()
    with pytest.raises(launch.LaunchVerificationError):
        observer.process(4321, deadline=1)
    assert observer.process(9876, deadline=1) is None


def test_native_listener_requires_socket_owner_not_http_or_pid_alone(fake_proc):
    folder = write_process(fake_proc)
    fd = folder / "fd"
    fd.mkdir()
    (fd / "5").symlink_to("socket:[123]")
    net = fake_proc / "net"
    net.mkdir()
    table = net / "tcp"
    table.write_text("header\n0: 0100007F:2007 00000000:0000 0A 0:0 0:0 0 1000 0 123\n")
    observer = launch.ProcLaunchObserver()
    assert observer.listeners("http://127.0.0.1:8199", deadline=1) == (
        observer.process(4321, deadline=1),)
    (fd / "5").unlink()
    with pytest.raises(launch.LaunchVerificationError, match="ownership is unknown"):
        observer.listeners("http://127.0.0.1:8199", deadline=1)
    assert observer.listeners("http://127.0.0.1:8200", deadline=1) == ()
    with pytest.raises(TimeoutError):
        observer.listeners("http://127.0.0.1:8200", deadline=0)
