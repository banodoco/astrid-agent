"""Completion diagnostics must retain failure evidence without receipt secrets."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from astrid.core.rendering import remotion_runtime as runtime


SENTINEL = 'private-argv-environment-output-must-not-cross-diagnostic'


def _receipt():
    return {'original_wait_exit_code': 0, 'original_child_pid': 71,
            'retained_set_final_census_complete': True, 'custody_admitted': True,
            'argv': [SENTINEL], 'environment': {'TOKEN': SENTINEL}, 'cwd': SENTINEL,
            'stdout': SENTINEL, 'stderr': SENTINEL}


@pytest.mark.parametrize('field,value', [
    ('original_wait_exit_code', 1), ('original_wait_exit_code', None),
    ('retained_set_final_census_complete', False), ('custody_admitted', False),
])
def test_failed_probe_reports_exact_check_without_receipt_data(field, value):
    receipt = _receipt()
    if value is None:
        receipt.pop(field)
    else:
        receipt[field] = value
    with pytest.raises(runtime.RemotionValidationError) as caught:
        runtime._require_completed_probe(receipt, phase='registry_discovery')
    error = caught.value
    assert error.probe_check['phase'] == 'registry_discovery'
    assert error.probe_check['failed_checks'] == [field]
    assert error.probe_check['original_child_pid'] == 71
    assert error.probe_check['exit_code_missing'] is (value is None)
    assert SENTINEL not in str(error) + json.dumps(error.probe_check)
    assert not {'argv', 'environment', 'cwd', 'stdout', 'stderr'}.intersection(error.probe_check)
    assert error.__cause__ is None


@pytest.mark.parametrize('code', [0, False])
def test_completed_probe_keeps_original_acceptance_semantics(code):
    receipt = _receipt()
    receipt['original_wait_exit_code'] = code
    before = dict(receipt)
    assert runtime._require_completed_probe(receipt, phase='node_version') is None
    assert receipt == before


def test_probe_diagnostic_never_formats_untrusted_values_or_type_names():
    secret_type = type(SENTINEL, (), {'__repr__': lambda _: SENTINEL})
    receipt = _receipt()
    receipt.update(original_wait_exit_code=secret_type(), original_child_pid=True,
                   retained_set_final_census_complete='true', custody_admitted='true')
    with pytest.raises(runtime.RemotionValidationError) as caught:
        runtime._require_completed_probe(receipt, phase=SENTINEL)
    safe = caught.value.probe_check
    assert safe['phase'] == 'unknown' and safe['exit_code_type'] == 'other'
    assert safe['original_wait_exit_code'] is None and safe['original_child_pid'] is None
    assert safe['custody_admitted'] is False and safe['census_complete'] is False
    assert safe['custody_admitted_type'] == safe['census_complete_type'] == 'str'
    assert safe['failed_checks'] == ['original_wait_exit_code', 'retained_set_final_census_complete', 'custody_admitted']
    assert SENTINEL not in str(caught.value) + json.dumps(safe)


def test_registry_generator_nonzero_keeps_failure_and_safe_phase(monkeypatch, tmp_path):
    from astrid.packs.rendering.rendering.renderers.remotion import run, lock

    expected = {'content_hashes': {'fixed': 'registry-state'}}
    lock_path = tmp_path / 'held-lock'
    monkeypatch.setattr(lock, 'remotion_render_lock_held', lambda: True)
    monkeypatch.setattr(lock, '_resolved_lock_path', lambda: lock_path)
    monkeypatch.setattr(lock, 'remotion_render_lock_child_env', lambda: {'TOKEN': SENTINEL})
    monkeypatch.setattr(run, '_timeline_composition_src', lambda _: tmp_path / 'composition')
    monkeypatch.setattr(run, '_registry_output_paths', lambda _: ())
    monkeypatch.setattr(run, 'gen_effect_registry', object())
    monkeypatch.setattr(run, '_read_registry_state', lambda _: None)
    monkeypatch.setattr(run, '_registry_output_content_hashes', lambda _: {})
    monkeypatch.setattr(runtime, '_registry_context', lambda env, **_: dict(env))
    monkeypatch.setattr(runtime, '_registry_inputs', lambda _: {'bound': True})
    observation = {'state': expected, 'lock_path': str(lock_path)}
    monkeypatch.setattr(runtime, '_registry_probe', lambda *_a, **_k: (observation, _receipt()))
    calls = []

    def failed_generator(argv, **kwargs):
        calls.append((argv, kwargs))
        receipt = _receipt()
        receipt['original_wait_exit_code'] = 1
        return subprocess.CompletedProcess([SENTINEL], 1, SENTINEL, SENTINEL), receipt

    monkeypatch.setattr(runtime, '_owned_probe', failed_generator)
    with pytest.raises(runtime.RemotionValidationError) as caught:
        runtime._settle_registries(tmp_path, {'renderer': {'TOKEN': SENTINEL}}, 'fake-python')
    safe = caught.value.probe_check
    assert len(calls) == 1 and safe['phase'] == 'registry_generator'
    assert safe['failed_checks'] == ['original_wait_exit_code']
    assert safe['original_wait_exit_code'] == 1 and safe['original_child_pid'] == 71
    assert safe['custody_admitted'] is True and safe['census_complete'] is True
    assert SENTINEL not in str(caught.value) + json.dumps(safe)
    assert caught.value.__cause__ is None


def test_existing_failure_writer_retains_safe_check_and_original_error(monkeypatch, tmp_path):
    receipt = _receipt()
    receipt['original_wait_exit_code'] = -9
    try:
        runtime._require_completed_probe(receipt, phase='qualifying')
    except runtime.RemotionValidationError as error:
        original = error
    cause = RuntimeError('original fixed cause')
    original.__cause__ = cause
    process = SimpleNamespace(pid=71)
    original.process = process
    monkeypatch.setattr(runtime, '_UNRESOLVED_VALIDATION_PROBES', [])

    def fail(**kwargs):
        raise original

    monkeypatch.setattr(runtime, '_prepare_remotion_validation', fail)
    with pytest.raises(runtime.RemotionValidationError) as caught:
        runtime.prepare_remotion_validation(artifact_directory=tmp_path)
    assert caught.value is original and caught.value.__cause__ is cause
    assert caught.value.process is process
    evidence = json.loads(next(tmp_path.glob('failed-validation-*.json')).read_text())
    assert evidence['status'] == 'FAILED_NOT_READINESS'
    assert evidence['probe_check'] == original.probe_check
    assert evidence['probe_check']['original_wait_exit_code'] == -9
    assert evidence['retained_original_child_pid'] == 71
    assert evidence['original_probe_receipt'] is None
    assert SENTINEL not in json.dumps(evidence) + str(caught.value)


@pytest.mark.parametrize("phase", ["complete_inputs", "owned_probe_acquire", "owned_probe_execute"])
def test_validation_phase_begins_before_blocking_and_retains_first_failure(phase, capsys):
    error = runtime.RemotionValidationError(SENTINEL)
    @runtime._validation_timing(phase)
    def fail():
        rows = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
        assert rows[-1]["outcome"] == "begun" and rows[-1]["phase"] == phase
        raise error
    with pytest.raises(runtime.RemotionValidationError) as caught:
        fail()
    assert caught.value is error and error.validation_phase == phase
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert rows[-1]["outcome"] == "failed" and rows[-1]["elapsed_ms"] >= 0
    assert SENTINEL not in json.dumps(rows)


def test_validation_artifact_write_failure_keeps_primary_and_phase(monkeypatch, tmp_path, capsys):
    error = runtime.RemotionValidationError(SENTINEL)
    error.validation_phase = "complete_inputs"
    monkeypatch.setattr(runtime, "_UNRESOLVED_VALIDATION_PROBES", [])
    monkeypatch.setattr(runtime, "_prepare_remotion_validation", lambda **_: (_ for _ in ()).throw(error))
    monkeypatch.setattr(runtime.Path, "mkdir", lambda *_a, **_k: (_ for _ in ()).throw(OSError(SENTINEL)))
    with pytest.raises(runtime.RemotionValidationError) as caught:
        runtime.prepare_remotion_validation(artifact_directory=tmp_path)
    assert caught.value is error and error.validation_evidence_error == "OSError"
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert rows[-1]["last_begun_phase"] == "complete_inputs"
    assert rows[-1]["persistence_error_class"] == "OSError"
    assert SENTINEL not in json.dumps(rows)


@pytest.mark.parametrize("seam", ["inputs", "acquisition", "execution"])
def test_actual_input_and_owned_probe_failure_phases_survive_cleanup(seam, monkeypatch, tmp_path, capsys):
    from astrid.core.execution import process_group
    primary = runtime.RemotionValidationError(SENTINEL)
    cleanup = TimeoutError(SENTINEL)
    process = SimpleNamespace(pid=71, returncode=None)
    def acquire(*args, **kwargs):
        if seam == "acquisition":
            raise primary
        return process
    process.communicate = lambda **_: (_ for _ in ()).throw(primary)
    monkeypatch.setattr(process_group, "popen_owned_group", acquire)
    monkeypatch.setattr(process_group, "terminate_group", lambda *_a, **_k: (_ for _ in ()).throw(cleanup))
    monkeypatch.setattr(process_group, "verify_tree_absent", lambda *_: None)
    monkeypatch.setattr(runtime, "_UNRESOLVED_VALIDATION_PROBES", [])
    def prepare(**kwargs):
        if seam == "inputs":
            return runtime._runtime_inputs(tmp_path, {}, "unused-python", {})
        return runtime._owned_probe([SENTINEL], cwd=tmp_path, env={"LANG": "C"}, timeout=1)
    monkeypatch.setattr(runtime, "_prepare_remotion_validation", prepare)
    with pytest.raises(runtime.RemotionValidationError) as caught:
        runtime.prepare_remotion_validation(artifact_directory=tmp_path)
    expected = {"inputs": "complete_inputs", "acquisition": "owned_probe_acquire", "execution": "owned_probe_execute"}[seam]
    assert caught.value.validation_phase == expected
    if seam != "inputs":
        assert caught.value is primary
    if seam == "execution":
        assert primary.cleanup_error is cleanup and primary.process is process
        assert runtime._UNRESOLVED_VALIDATION_PROBES == [primary]
    evidence = json.loads(next(tmp_path.glob("failed-validation-*.json")).read_text())
    assert evidence["last_begun_phase"] == expected
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert any(row.get("phase") == expected and row.get("outcome") == "begun" for row in rows)
    assert rows[-1]["last_begun_phase"] == expected
    assert SENTINEL not in json.dumps(rows) + json.dumps(evidence)


def _provisioned_launch_fixture(tmp_path, monkeypatch, **kwargs):
    # Reuse the existing production-shaped registry/schema/closure fixture;
    # only owned process outcomes are injected, never the binding/publisher.
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "packs/rendering/test_render_export_task_adapter.py"
    spec = importlib.util.spec_from_file_location("_remotion_readonly_launch_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._owned_validation_fixture(tmp_path, monkeypatch, **kwargs)


def test_provisioned_launch_keeps_installed_tree_immutable_and_binds_readiness(monkeypatch, tmp_path):
    from pathlib import Path
    from astrid.packs.rendering.rendering.renderers.remotion import lock
    source_lock = tmp_path / "installed-site/remotion/.astrid-registry.lock"
    monkeypatch.setattr(lock, "REMOTION_LOCK_PATH", source_lock)
    project, _, _, environment, reference, calls = _provisioned_launch_fixture(tmp_path, monkeypatch)
    assert not source_lock.parent.parent.exists()
    assert not lock.remotion_render_lock_held()
    entry = runtime._validated_runtime(project, environment={**environment, **reference})
    assert entry["registry"]["mode"] == "provisioned_only" and len(calls) == 3
    assert not (project / ".astrid-registry.lock").exists()
    assert not (project / ".astrid-registry-state.json").exists()
    for binding in entry["registry"]["outputs"]:
        assert runtime._file_binding(Path(binding["path"])) == binding


def test_real_isolated_schema_probes_preserve_selected_inventory(monkeypatch, tmp_path):
    import sys
    from pathlib import Path
    from astrid.core.execution import remotion_closure

    project, schema, _, environment, _, _ = _provisioned_launch_fixture(
        tmp_path, monkeypatch, environment_overrides={"LC_ALL": "C", "LC_CTYPE": "C"})
    package = schema / "banodoco_timeline_schema"
    # Import real temporary Python modules with the probe's required API. A
    # writable, cache-free package exposes bytecode writes hidden by warm caches.
    sources = {
        "__init__.py": (
            "from .generated import AssetEntry, TimelineClip, TimelineConfig, TimelineOutput\n"
            "from .materialize import materialize_output\n"
            "from .theme import Theme, ThemeOverrides, resolve_theme\n"
            "from .validate import load_schema, validate_timeline\n"
        ),
        "generated.py": "AssetEntry = TimelineClip = TimelineConfig = TimelineOutput = object\n",
        "materialize.py": "def materialize_output(): pass\n",
        "theme.py": "Theme = ThemeOverrides = object\ndef resolve_theme(): pass\n",
        "validate.py": "def load_schema(): return {}\ndef validate_timeline(): pass\n",
    }
    for name, source in sources.items():
        (package / name).write_text(source)

    def inventory():
        return [runtime._file_binding(path) for root in (project, schema)
                for path in sorted(remotion_closure._tree_files(root))]

    before = inventory()
    schema_calls = []

    def real_schema_probe(argv, **kwargs):
        receipt = _receipt()
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "v20.19.4\n", ""), receipt
        # Node and custody outcomes are synthetic; execute the production Python
        # argv/source/environment without inheriting pytest's no-bytecode flags.
        result = subprocess.run(argv, cwd=kwargs["cwd"], env=kwargs["env"],
                                capture_output=True, text=True, timeout=kwargs["timeout"])
        assert result.returncode == 0, result.stderr
        schema_calls.append(argv)
        assert inventory() == before
        assert not list(schema.rglob("*.pyc"))
        assert not list(schema.rglob("__pycache__"))
        receipt["original_wait_exit_code"] = result.returncode
        return result, receipt

    monkeypatch.setattr(runtime, "_owned_probe", real_schema_probe)
    reference = runtime.prepare_remotion_validation(
        artifact_directory=tmp_path / "real-probe-artifacts", host_environment=environment,
        python_executable=sys.executable, launch_id="b" * 32, contexts={"host": environment})
    assert len(schema_calls) == 2  # Discovery and qualifying imports both ran.
    assert all(argv[:4] == [sys.executable, "-I", "-B", "-c"] for argv in schema_calls)
    entry = runtime._validated_runtime(project, environment={**environment, **reference})
    assert entry["closure"]["origin"] == str(package / "__init__.py")
    assert inventory() == before
    assert Path(reference["ASTRID_REMOTION_VALIDATION_PATH"]).is_file()


def test_provisioned_launch_rejects_registry_change_before_publication(monkeypatch, tmp_path):
    from astrid.packs.rendering.rendering.renderers.remotion import run, lock
    source_lock = tmp_path / "installed-site/remotion/.astrid-registry.lock"
    monkeypatch.setattr(lock, "REMOTION_LOCK_PATH", source_lock)
    def change_after_binding(closure, _call):
        run._registry_output_paths(tmp_path / "runtime")[0].write_text("// changed registry")
    with pytest.raises(runtime.RemotionValidationError):
        _provisioned_launch_fixture(tmp_path, monkeypatch, schema_result_transform=change_after_binding)
    assert not list((tmp_path / "artifacts").glob("remotion-validation-*.json"))
    assert not source_lock.exists()


def test_provisioned_readiness_publication_remains_exclusive(monkeypatch, tmp_path):
    import sys
    from pathlib import Path
    project, _, _, environment, reference, _ = _provisioned_launch_fixture(tmp_path, monkeypatch)
    artifact = Path(reference["ASTRID_REMOTION_VALIDATION_PATH"])
    before = artifact.read_bytes()
    closure = json.loads(before)["contexts"]["host"]["variants"][0]["closure"]
    def owned(argv, **kwargs):
        output = "v20.19.4\n" if argv[-1] == "--version" else json.dumps(closure)
        return subprocess.CompletedProcess(argv, 0, output, ""), _receipt()
    monkeypatch.setattr(runtime, "_owned_probe", owned)
    with pytest.raises(FileExistsError):
        runtime.prepare_remotion_validation(artifact_directory=artifact.parent,
            host_environment=environment, python_executable=sys.executable,
            launch_id="a" * 32, contexts={"host": environment})
    assert artifact.read_bytes() == before


def test_source_validation_preserves_shared_writer_lock_and_child_identity(monkeypatch, tmp_path):
    import threading
    from astrid.packs.rendering.rendering.renderers.remotion import run, lock
    if lock.fcntl is None:
        pytest.skip("the shared POSIX writer lock uses fcntl")
    source_lock = tmp_path / "source/remotion/.astrid-registry.lock"
    monkeypatch.setattr(lock, "REMOTION_LOCK_PATH", source_lock)
    monkeypatch.setattr(run, "gen_effect_registry", object())
    first_entered = threading.Event()
    second_acquiring = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    failures = []
    active = []
    acquire = lock._FcntlLock.acquire
    def observed_acquire(self, *args, **kwargs):
        if threading.current_thread().name == "second":
            second_acquiring.set()
        return acquire(self, *args, **kwargs)
    monkeypatch.setattr(lock._FcntlLock, "acquire", observed_acquire)
    def body(**_kwargs):
        assert lock.remotion_render_lock_held() and source_lock.is_file()
        marker = lock.remotion_render_lock_child_env()[lock.REMOTION_LOCK_OWNER_ENV]
        assert marker.split(":", 1)[1] == str(source_lock)
        with pytest.raises(RuntimeError, match="non-recursive"):
            with lock.remotion_render_lock():
                pass
        assert not active
        active.append(threading.current_thread().name)
        try:
            if active[0] == "first":
                first_entered.set()
                assert release.wait(5)
            else:
                second_entered.set()
        finally:
            active.pop()
    monkeypatch.setattr(runtime, "_prepare_remotion_validation_locked", body)
    def start():
        try:
            runtime._prepare_remotion_validation()
        except BaseException as error:
            failures.append(error)
    first = threading.Thread(target=start, name="first")
    second = threading.Thread(target=start, name="second")
    try:
        first.start()
        assert first_entered.wait(5)
        second.start()
        assert second_acquiring.wait(5) and not second_entered.is_set()
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not failures and not first.is_alive() and not second.is_alive()
    assert second_entered.is_set() and not lock.remotion_render_lock_held()


def test_source_registry_settling_still_requires_original_writer_lock(monkeypatch, tmp_path):
    from astrid.packs.rendering.rendering.renderers.remotion import run
    monkeypatch.setattr(run, "gen_effect_registry", object())
    (tmp_path / "node_modules/@banodoco/timeline-composition/typescript/src").mkdir(parents=True)
    with pytest.raises(runtime.RemotionValidationError, match="original held Remotion lock"):
        runtime._settle_registries(tmp_path, {"host": {}}, "unused-python")


@pytest.mark.parametrize("missing", [0, 1, 2])
def test_provisioned_registry_still_requires_every_output_without_writer_lock(monkeypatch, tmp_path, missing):
    from astrid.packs.rendering.rendering.renderers.remotion import run, lock
    monkeypatch.setattr(run, "gen_effect_registry", None)
    source_lock = tmp_path / "installed-site/remotion/.astrid-registry.lock"
    monkeypatch.setattr(lock, "REMOTION_LOCK_PATH", source_lock)
    (tmp_path / "node_modules/@banodoco/timeline-composition/typescript/src").mkdir(parents=True)
    for index, output in enumerate(run._registry_output_paths(tmp_path)):
        if index != missing:
            output.write_text("// provisioned registry")
    with pytest.raises(FileNotFoundError):
        runtime._settle_registries(tmp_path, {"host": {"HOME": str(tmp_path)}}, "unused-python")
    assert not source_lock.exists() and not lock.remotion_render_lock_held()
