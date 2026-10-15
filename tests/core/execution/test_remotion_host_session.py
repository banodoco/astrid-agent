"""Host lifetime checks without Chrome, Runtime, or a detached owner."""
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from astrid.core.execution import remotion_host_session as host


def environment(tmp_path):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src/astrid-frame-worker.mjs").write_text("# fixture")
    cli = project / "node_modules/@remotion/cli/remotion-cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("// qualified fixture")
    return {"ASTRID_REMOTION_PROJECT_DIR": str(project), "ASTRID_NODE_EXECUTABLE": str(Path(sys.executable).resolve())}


def request(env, attempt=None):
    project = Path(env["ASTRID_REMOTION_PROJECT_DIR"])
    attempt = Path(attempt) if attempt is not None else project.parent
    (attempt / "props.json").write_text("{}")
    return {"identity": "qualified-renderer-a", "request": {
        "type": "render", "compositionId": "Main", "resolution": [320,180], "port":31500,
        "projectDir": env["ASTRID_REMOTION_PROJECT_DIR"],
        "nodeExecutable": env["ASTRID_NODE_EXECUTABLE"], "frames": [18],
        "remotionCli": str(project / "node_modules/@remotion/cli/remotion-cli.js"),
        "propsPath": str(attempt / "props.json"), "outputDir": str(attempt / "frames"), "environment": {},
    }}


def owner(tmp_path):
    events = []
    return host.HostRemotionSession(track=lambda p: events.append(("track", p)),
                                   untrack=lambda p: events.append(("untrack", p)),
                                   root_parent=str(tmp_path)), events


class Process:
    def __init__(self):
        self.returncode = None
        self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()
        self.waited = False

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.waited = True
        self.returncode = 0
        return 0


def fake_native(monkeypatch, manager, *, failure=None):
    processes = []
    def start(argv, **kwargs):
        assert "--server" not in argv
        assert kwargs["env"]["TMPDIR"].startswith(str(manager._root_parent))
        process = Process()
        processes.append(process)
        return process
    monkeypatch.setattr(host, "popen_owned_group", start)
    monkeypatch.setattr(host, "observe_tree", lambda p: {})
    def absent(process):
        assert process.waited
        if failure:
            raise failure
    monkeypatch.setattr(host, "verify_tree_absent", absent)
    monkeypatch.setattr(host, "release_group", lambda p: None)
    def exchange(req, **kw):
        if req["type"] == "shutdown":
            return {"ok": True, "shutdown": True, "exit": True}
        return {"ok": True, "pid": id(manager.process)}
    monkeypatch.setattr(manager, "_exchange", exchange)
    return processes


def test_two_task_leases_reuse_original_then_host_shutdown_waits_and_proves(tmp_path, monkeypatch):
    manager, events = owner(tmp_path)
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager)
    first = manager.lease(env, attempt=tmp_path)
    try:
        a = manager.request(first, request(env))
    finally:
        first.close()
    assert len(processes) == 1 and not processes[0].waited
    second = manager.lease(env, attempt=tmp_path)
    try:
        b = manager.request(second, request(env))
        assert a["pid"] == b["pid"]
    finally:
        second.close()
    manager.close()
    assert processes[0].waited and manager.process is None
    assert events == [("track", processes[0]), ("untrack", processes[0])]


def test_original_census_failure_is_sticky_and_forbids_replacement(tmp_path, monkeypatch):
    manager, events = owner(tmp_path)
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager, failure=RuntimeError("original descendant remains"))
    lease = manager.lease(env, attempt=tmp_path)
    try:
        manager.request(lease, request(env))
        with pytest.raises(RuntimeError, match="original descendant"):
            manager.close()
        assert manager.process is processes[0]
        assert events == [("track", processes[0])]
        with pytest.raises(RuntimeError, match="closed or cleanup"):
            manager.lease(env, attempt=tmp_path)
        assert len(processes) == 1
    finally:
        lease.close()


@pytest.mark.parametrize("field", ["projectDir", "nodeExecutable"])
def test_child_cannot_select_a_different_native_executable_or_project(tmp_path, monkeypatch, field):
    manager, _ = owner(tmp_path)
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager)
    lease = manager.lease(env, attempt=tmp_path)
    value = request(env)
    value["request"][field] = "/unadmitted/other"
    try:
        with pytest.raises(ValueError, match="host-selected"):
            manager.request(lease, value)
        assert not processes
    finally:
        lease.close()
        manager.close()


def test_idle_expiration_uses_original_shutdown_proof(tmp_path, monkeypatch):
    manager, _ = owner(tmp_path)
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager)
    lease = manager.lease(env, attempt=tmp_path)
    manager.request(lease, request(env))
    lease.close()
    manager._idle_deadline = 0
    manager._expire()
    assert processes[0].waited and manager.process is None
    manager.close()


def test_closed_child_channel_is_not_native_cleanup_proof(tmp_path, monkeypatch):
    manager, _ = owner(tmp_path)
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager)
    lease = manager.lease(env, attempt=tmp_path)
    manager.request(lease, request(env))
    lease.close()
    assert manager.process is processes[0] and not processes[0].waited
    manager.close()


def test_raw_primary_and_cleanup_fields_cross_inherited_channel(tmp_path, monkeypatch):
    manager, _ = owner(tmp_path)
    env = environment(tmp_path)
    fake_native(monkeypatch, manager)
    primary = {"ok": False, "error": "primary E1", "error_details": {"message": "primary E1"},
               "cleanup_uncertain": True, "cleanup_error": "secondary E2"}
    monkeypatch.setattr(manager, "_exchange", lambda request: primary)
    lease = manager.lease(env, attempt=tmp_path)
    try:
        host._write(lease.child, request(env))
        assert host._read(lease.child) == primary
    finally:
        lease.close()
        # This unit substitutes exchange; do not assert native drain here.
        manager._closed = True
        if manager._timer:
            manager._timer.cancel()


def test_two_public_command_workers_settle_task_leases_then_host_drains_original(tmp_path, monkeypatch):
    """Actual host command dispatch + managed task release, without Chrome/Runtime."""
    from types import SimpleNamespace

    from astrid.core.execution import generic_host
    from astrid.core.execution.generic_host import GenericPackHost, _ManagedTaskAdapter
    env = environment(tmp_path)
    helper = Path(env["ASTRID_REMOTION_PROJECT_DIR"]) / "src/astrid-frame-worker.mjs"
    helper.write_text("""import json, os, sys
for line in sys.stdin:
    value = json.loads(line)
    if value['type'] == 'shutdown':
        print(json.dumps({'ok': True, 'shutdown': True, 'exit': True}), flush=True)
        break
    print(json.dumps({'ok': True, 'original_pid': os.getpid()}), flush=True)
""")
    client_code = """import json, sys
from pathlib import Path
from astrid.core.execution.remotion_host_session import host_remotion_client, _read
from astrid.core.execution import generic_host_worker
assert Path(generic_host_worker.__file__).resolve() == Path(sys.argv[3]).resolve()
c=host_remotion_client()
assert c is not None
reply=c.request('qualified-renderer-a', json.loads(sys.argv[1]), _read)
reply['worker_origin']=str(Path(generic_host_worker.__file__).resolve())
Path(sys.argv[2]).write_text(json.dumps(reply))
assert reply['ok'] is True, reply
"""
    identity_observations = []
    original_identity = host._file_identity
    identity_names = ('dev', 'inode', 'size', 'mtime_ns', 'ctime_ns', 'sha256')
    def observed_identity(path):
        before = Path(path).stat()
        value = original_identity(path)
        after = Path(path).stat()
        identity_observations.append({'path':str(path), 'identity':dict(zip(identity_names,value)),
            'stat_before':dict(zip(identity_names[:5],(before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns))),
            'stat_after':dict(zip(identity_names[:5],(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)))})
        return value
    monkeypatch.setattr(host, '_file_identity', observed_identity)
    running_host = GenericPackHost(pack_roots=[], attempt_base=tmp_path)
    record = SimpleNamespace(id="rendering.timeline_visualize", source_root=tmp_path,
        capability_digest="fixture", definition=SimpleNamespace(command=object(), metadata={}, inputs=[], outputs=[]))
    monkeypatch.setattr(generic_host, "_bind_host_owned_command_values", lambda record, values, **kw: values)
    monkeypatch.setattr(generic_host, "_bind_host_owned_command_outputs", lambda record, values, **kw: (values, None))
    monkeypatch.setattr(generic_host, "assert_provided_inputs_bound", lambda *a: None)
    monkeypatch.setattr(generic_host, "harvest_staged_outputs", lambda *a, **kw: [])
    monkeypatch.setattr(generic_host, "_command_worker_pythonpath", lambda record, path: path)
    monkeypatch.setattr(generic_host, "_network_sandbox_argv", lambda argv, *a: argv)
    monkeypatch.setattr(running_host, "_child_environment", lambda *a, **kw: ({**os.environ, **env, "PYTHONDONTWRITEBYTECODE":"1", "PYTHONPATH":str(Path(generic_host.__file__).resolve().parents[3])}, {}))
    def disposable_launch(argv, **kw):
        assert kw.get('inherited_fds', ()) == ()
        child_env = dict(kw['environment'])
        return SimpleNamespace(argv=tuple(argv), cwd=kw['cwd'], environment=tuple(sorted(child_env.items())),
            subprocess_options=lambda: {'cwd': str(kw['cwd']), 'env': child_env,
                'stdin': subprocess.PIPE, 'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE,
                'close_fds': True, 'pass_fds': ()})
    # This test models host custody with a disposable Python helper. Actual
    # confinement and descriptor admission are exercised in their own tests.
    monkeypatch.setattr(running_host, '_workload_launch_plan', disposable_launch)
    replies, task_pids = [], []
    original = None
    try:
        for number in (1,2):
            attempt = tmp_path / f"task-{number}"
            out = attempt / "outputs"
            out.mkdir(parents=True)
            values = request(env, attempt)["request"]
            monkeypatch.setattr(generic_host, "expand_command", lambda *a, values=values, attempt=attempt: SimpleNamespace(
                argv=[sys.executable, "-c", client_code, json.dumps(values), str(attempt/'reply.json'), str(Path(generic_host.__file__).with_name('generic_host_worker.py'))], env={}, cwd=str(attempt)))
            result = running_host._run_command_definition(record, {}, out, attempt)
            task_pids.append(result.process_id)
            replies.append(json.loads((attempt/'reply.json').read_text()))
            assert replies[-1]['worker_origin'] == str(Path(generic_host.__file__).with_name('generic_host_worker.py').resolve())
            adapter = _ManagedTaskAdapter(threading.Event(), process_census=lambda: bool(running_host._active_processes))
            assert adapter.release(reason="actual-task-settlement")['released'] is True
            assert not running_host._active_processes
            original = running_host._remotion_host_session.process
            assert original in running_host._host_lifetime_processes and original.poll() is None
            assert running_host.execution_policy.evidence_budget.live_observation()["live_attempts"] == 1
        assert task_pids[0] != task_pids[1]
        assert replies[0]["original_pid"] == replies[1]["original_pid"] == original.pid
    finally:
        (tmp_path/'native-input-identity-observations.json').write_text(json.dumps(identity_observations,indent=2))
        running_host.shutdown()
    assert original.returncode == 0
    assert not running_host._host_lifetime_processes
    assert running_host._remotion_host_session.process is None
    assert running_host.execution_policy.evidence_budget.charged_bytes == 0


def test_actual_frame_session_uses_inherited_host_lease_not_legacy_namespace(tmp_path, monkeypatch):
    from astrid.packs.rendering.rendering.renderers.remotion.run import (
        PersistentRemotionFrameSession,
    )
    calls = []
    class Client:
        def request(self, identity, value, parser):
            calls.append((identity, value))
            return {"ok": True, "findings": [{"code": "host-fixture"}], "released": True, "exit": True}
    monkeypatch.setattr(host, "_client", Client())
    monkeypatch.setattr(PersistentRemotionFrameSession, "_owner_paths", lambda *args: pytest.fail("legacy namespace selected"))
    session = PersistentRemotionFrameSession()
    session.render(project_dir=tmp_path, composition_id="Main", node_executable=Path(sys.executable),
                   remotion_cli=tmp_path/'cli', props_path=tmp_path/'props.json', output_dir=tmp_path/'out',
                   frames=[18], resolution=(320,180), port=31500, environment={}, identity="qualified-renderer-a")
    assert session.findings == [{"code": "host-fixture"}]
    assert calls[0][1]["frames"] == [18]
    session.close(force=True)
    assert calls[1] == ("qualified-renderer-a", {"type": "release", "force": True})


def test_failed_acquisition_retains_exception_original_process(tmp_path, monkeypatch):
    manager, events = owner(tmp_path)
    env = environment(tmp_path)
    original = Process()
    failure = RuntimeError("acquisition uncertain")
    failure.process = original
    def start(*a, **kw):
        raise failure
    monkeypatch.setattr(host, "popen_owned_group", start)
    lease = manager.lease(env, attempt=tmp_path)
    try:
        with pytest.raises(RuntimeError, match="acquisition uncertain") as caught:
            manager.request(lease, request(env))
        assert caught.value is failure
        assert manager.process is original and manager.uncertain is failure
        assert events == [("track", original)]
        with pytest.raises(RuntimeError, match="closed or cleanup"):
            manager.lease(env, attempt=tmp_path)
    finally:
        lease.close()


@pytest.mark.parametrize("change", ["cli", "environment", "props", "temp"])
def test_task_cannot_modify_host_scope_inputs_or_escape_attempt(tmp_path, monkeypatch, change):
    manager, _ = owner(tmp_path)
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager)
    lease = manager.lease(env, attempt=tmp_path)
    value = request(env)
    if change == "cli": value['request']['remotionCli'] = '/unadmitted/cli.js'
    if change == "environment": value['request']['environment'] = {'NODE_OPTIONS':'--require=/foreign.js'}
    if change == "props": value['request']['propsPath'] = str(tmp_path.parent/'outside.json')
    if change == "temp": value['request']['environment'] = {'TMPDIR':str(tmp_path.parent)}
    try:
        with pytest.raises((ValueError, FileNotFoundError)):
            manager.request(lease, value)
        assert not processes
    finally:
        lease.close()
        manager.close()


def test_child_start_environment_does_not_include_runtime_credentials(tmp_path, monkeypatch):
    manager, _ = owner(tmp_path)
    env = environment(tmp_path)
    env['BANODOCO_RUNTIME_CREDENTIAL'] = 'fixture-secret-do-not-forward'
    env['NODE_OPTIONS'] = '--require=/unadmitted.js'
    observed = []
    def start(argv, **kw):
        observed.append(kw['env'])
        raise RuntimeError('before process acquisition')
    monkeypatch.setattr(host, 'popen_owned_group', start)
    lease = manager.lease(env, attempt=tmp_path)
    try:
        with pytest.raises(RuntimeError, match='before process'):
            manager.request(lease, request(env))
        assert 'BANODOCO_RUNTIME_CREDENTIAL' not in observed[0]
        assert 'NODE_OPTIONS' not in observed[0]
    finally:
        lease.close()
        manager.close()


def test_pipe_write_deadline_restores_original_descriptor_mode(tmp_path, monkeypatch):
    manager, _ = owner(tmp_path)
    read_fd, write_fd = os.pipe()
    process = Process()
    process.stdin = os.fdopen(write_fd, 'wb', buffering=0)
    manager.process = process
    monkeypatch.setattr(host, 'observe_tree', lambda p: {})
    monkeypatch.setattr(manager, '_guard', lambda: None)
    ticks = iter([0.0, 301.0])
    monkeypatch.setattr(host.time, 'monotonic', lambda: next(ticks))
    try:
        with pytest.raises(TimeoutError, match='request write timed out'):
            manager._exchange({'type':'render'})
        assert os.get_blocking(write_fd) is True
    finally:
        process.stdin.close()
        os.close(read_fd)


def test_unacknowledged_shutdown_retains_original_and_scratch(tmp_path, monkeypatch):
    manager, events = owner(tmp_path)
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager)
    lease = manager.lease(env, attempt=tmp_path)
    manager.request(lease, request(env))
    original_root = manager.root
    monkeypatch.setattr(manager, '_exchange', lambda request, **kw: {'ok':True, 'exit':False})
    try:
        with pytest.raises(RuntimeError, match='acknowledge shutdown'):
            manager.close()
        assert manager.process is processes[0] and original_root.exists()
        assert not processes[0].waited
        assert events == [('track', processes[0])]
    finally:
        lease.close()


def test_scratch_cleanup_failure_never_forgets_original(tmp_path, monkeypatch):
    manager, events = owner(tmp_path)
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager)
    lease = manager.lease(env, attempt=tmp_path)
    manager.request(lease, request(env))
    original_root = manager.root
    def refused(root):
        raise PermissionError('original private root retained')
    monkeypatch.setattr(host.shutil, 'rmtree', refused)
    try:
        with pytest.raises(PermissionError, match='private root'):
            manager.close()
        assert manager.process is processes[0] and manager.uncertain is not None
        assert original_root.exists()
        assert events == [('track', processes[0])]
    finally:
        lease.close()


@pytest.mark.parametrize("key", ["browserExecutable", "unexpected"])
def test_unexpected_render_fields_refused_before_native_allocation(tmp_path, monkeypatch, key):
    manager, _ = owner(tmp_path)
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager)
    lease = manager.lease(env, attempt=tmp_path)
    value = request(env)
    value['request'][key] = '/unadmitted/executable'
    try:
        with pytest.raises(ValueError, match='unsupported fields'):
            manager.request(lease, value)
        assert processes == []
    finally:
        lease.close()
        manager.close()


def test_host_browser_identity_is_bound_and_only_added_to_native_request(tmp_path, monkeypatch):
    browser = tmp_path / "host-selected-browser"
    browser.write_bytes(b"fixed installed browser fixture")
    monkeypatch.setattr(host, "_host_browser_executable", lambda: browser)
    manager, _ = owner(tmp_path)
    env = environment(tmp_path)
    fake_native(monkeypatch, manager)
    native_requests = []
    def exchange(value, **kwargs):
        native_requests.append(value)
        return {"ok": True, **({"shutdown": True, "exit": True} if value["type"] == "shutdown" else {})}
    monkeypatch.setattr(manager, "_exchange", exchange)
    lease = manager.lease(env, attempt=tmp_path)
    value = request(env)
    try:
        identity = host._file_identity(browser)
        assert lease.browser == browser
        assert lease.input_identities[browser] == identity
        assert (str(browser), identity) in lease.selected_identity[2]
        manager.request(lease, value)
        assert native_requests[0]["browserExecutable"] == str(browser)
        assert "browserExecutable" not in value["request"]
    finally:
        lease.close()
        manager.close()


def test_changed_host_browser_refused_before_native_allocation(tmp_path, monkeypatch):
    browser = tmp_path / "host-selected-browser"
    browser.write_bytes(b"selected browser")
    monkeypatch.setattr(host, "_host_browser_executable", lambda: browser)
    manager, _ = owner(tmp_path)
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager)
    lease = manager.lease(env, attempt=tmp_path)
    browser.write_bytes(b"changed browser bytes")
    try:
        with pytest.raises(ValueError, match="input changed after task admission"):
            manager.request(lease, request(env))
        assert processes == []
    finally:
        lease.close()
        manager.close()


@pytest.mark.parametrize("kind", ["missing", "symlink", "not-executable"])
def test_fixed_darwin_browser_requires_existing_canonical_executable(tmp_path, monkeypatch, kind):
    from types import SimpleNamespace
    browser = tmp_path / "browser"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"browser")
        target.chmod(0o700)
        browser.symlink_to(target)
    elif kind == "not-executable":
        browser.write_bytes(b"browser")
        browser.chmod(0o600)
    monkeypatch.setattr(host, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(host, "_DARWIN_BROWSER", browser)
    with pytest.raises(ValueError, match="host-selected"):
        host._host_browser_executable()


def test_fixed_darwin_browser_selection_ignores_executor_environment(tmp_path, monkeypatch):
    from types import SimpleNamespace
    browser = tmp_path / "browser"
    browser.write_bytes(b"installed browser")
    browser.chmod(0o700)
    monkeypatch.setattr(host, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(host, "_DARWIN_BROWSER", browser)
    monkeypatch.setenv("CHROME_PATH", "/unadmitted/browser")
    monkeypatch.setenv("ASTRID_BROWSER_EXECUTABLE", "/unadmitted/browser")
    assert host._host_browser_executable() == browser


def test_same_caller_identity_cannot_reuse_changed_host_selected_helper(tmp_path, monkeypatch):
    manager, _ = owner(tmp_path)
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager)
    first = manager.lease(env, attempt=tmp_path)
    manager.request(first, request(env))
    first.close()
    helper = Path(env['ASTRID_REMOTION_PROJECT_DIR'])/'src/astrid-frame-worker.mjs'
    helper.write_text('# newly admitted selected helper')
    second = manager.lease(env, attempt=tmp_path)
    try:
        manager.request(second, request(env))
        assert len(processes) == 2 and processes[0].waited
        assert manager.process is processes[1]
    finally:
        second.close()
        manager.close()


@pytest.mark.parametrize('change, message', [
    ('frames-empty', 'frame_budget_exceeded'), ('frames-many', 'frame_budget_exceeded'),
    ('frames-bool', 'frame_budget_exceeded'), ('resolution', 'frame_resolution_budget_exceeded'),
    ('evaluate-composition', 'evaluation composition'), ('release', 'invalid renderer release'),
])
def test_hosted_owner_rejects_invalid_budgets_before_native_start(tmp_path, monkeypatch, change, message):
    """Backend/request validation only; fake native adapter is not render proof."""
    manager, _ = owner(tmp_path)
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager)
    lease = manager.lease(env, attempt=tmp_path)
    value = request(env)
    if change == 'frames-empty': value['request']['frames'] = []
    if change == 'frames-many': value['request']['frames'] = list(range(121))
    if change == 'frames-bool': value['request']['frames'] = [True]
    if change == 'resolution': value['request']['resolution'] = [4097, 180]
    if change == 'evaluate-composition': value['request']['type'] = 'evaluate'
    if change == 'release': value['request'] = {'type': 'release', 'force': 'yes'}
    try:
        with pytest.raises(ValueError, match=message):
            manager.request(lease, value)
        assert processes == []
    finally:
        lease.close()
        manager.close()


def test_hosted_request_uses_existing_owner_and_preserves_query_reply(tmp_path, monkeypatch):
    from astrid.core.execution.remotion_frame_owner import (
        RemotionFrameOwner,
        RetainedHostBackend,
        UnsupportedContext,
    )
    manager, _ = owner(tmp_path)
    assert isinstance(manager.frame_owner, RemotionFrameOwner)
    assert isinstance(manager.frame_owner.backend, RetainedHostBackend)
    assert manager.frame_owner.backend.session is manager
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager)
    primary = {'ok': True, 'evaluation': [{'frame': 18, 'layers': []}]}
    def exchange(value, **kw):
        if value['type'] == 'shutdown': return {'ok': True, 'shutdown': True, 'exit': True}
        assert value['type'] == 'evaluate'
        assert value['diagnostics'] is True
        return primary
    monkeypatch.setattr(manager, '_exchange', exchange)
    lease = manager.lease(env, attempt=tmp_path)
    value = request(env)
    value['request'].update(type='evaluate', compositionId='AstridTrackEvaluation', diagnostics=True)
    try:
        assert manager.request(lease, value) is primary
        assert len(processes) == 1
        # Hosting never grants the separate native model qualification.
        assert manager.frame_owner.pending is None
        assert manager.frame_owner._queued == manager.frame_owner._inflight == 0
        with pytest.raises(UnsupportedContext, match='cleanup belongs'):
            manager.frame_owner.drain()
        with pytest.raises(UnsupportedContext, match='invalidation belongs'):
            manager.frame_owner.invalidate()
    finally:
        lease.close()
        manager.close()


def test_hosted_owner_bounded_admission_and_revoked_waiter(tmp_path, monkeypatch):
    import time

    from astrid.core.execution.remotion_frame_owner import FrameOwnerError
    manager, _ = owner(tmp_path)
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager)
    lease = manager.lease(env, attempt=tmp_path)
    manager.frame_owner._queued = 8
    with pytest.raises(FrameOwnerError, match='frame_queue_full'):
        manager.request(lease, request(env))
    manager.frame_owner._queued = 0
    errors = []
    def waiting():
        try: manager.request(lease, request(env))
        except BaseException as error: errors.append(error)
    manager.frame_owner._exchange.acquire()
    thread = threading.Thread(target=waiting)
    thread.start()
    deadline = time.monotonic() + 3
    try:
        while manager.frame_owner._queued != 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert manager.frame_owner._queued == 1
        lease.close()
    finally:
        manager.frame_owner._exchange.release()
        thread.join(timeout=3)
        manager.close()
    assert not thread.is_alive() and len(errors) == 1
    assert 'not active' in str(errors[0])
    assert not processes


def test_invalid_renderer_reply_retains_original_uncertainty(tmp_path, monkeypatch):
    from astrid.core.execution.remotion_frame_owner import FrameOwnerError
    manager, events = owner(tmp_path)
    env = environment(tmp_path)
    processes = fake_native(monkeypatch, manager)
    monkeypatch.setattr(manager, '_exchange', lambda request: {'ok': 1})
    lease = manager.lease(env, attempt=tmp_path)
    try:
        with pytest.raises(FrameOwnerError, match='frame_response_invalid'):
            manager.request(lease, request(env))
        assert manager.process is processes[0] and manager.uncertain is not None
        assert events == [('track', processes[0])]
    finally:
        lease.close()
