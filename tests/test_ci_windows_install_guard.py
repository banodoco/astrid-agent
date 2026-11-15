"""CPU proof for the fresh-install guard; no Windows or download processes."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts import ci_windows_install_guard as guard


@pytest.mark.parametrize("exit_code", [0, 1])
def test_guard_budget_is_one_real_bootstrap_without_retry(tmp_path, monkeypatch, exit_code):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    local = tmp_path / "local"
    logs = local / "Astrid Runtime" / "logs"
    logs.mkdir(parents=True)
    (logs / "install.log").write_text("Source staging operations require Git on PATH")
    (logs / "last-run.json").write_text('{"outcome": "failed"}')
    manifest = {"agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40}}
    (evidence / "install-manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(guard.windows, "EVIDENCE", evidence)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(guard.windows, "version", lambda: {"root": str(tmp_path / "version")})
    version = tmp_path / "version"
    version.mkdir()
    (version / "manifest.json").write_text(json.dumps(manifest))
    calls = []

    def bootstrap(argv, log_path, mode, *, env, timeout):
        calls.append((argv, env, timeout))
        assert mode == "install"
        log_path.write_text("bootstrap output")
        if exit_code:
            raise subprocess.CalledProcessError(exit_code, argv)

    monkeypatch.setattr(guard.windows, "run_bootstrap_process", bootstrap)
    if exit_code:
        with pytest.raises(subprocess.CalledProcessError) as failure:
            guard.install()
        assert failure.value.returncode == exit_code
    else:
        guard.install()

    # This budget counts the actual launch boundary on both success and failure.
    # A retry would hide the fresh-profile failure and make a second install warm.
    assert len(calls) == 1
    argv, env, timeout = calls[0]
    assert argv[0] == "powershell.exe"
    assert "--manifest" in argv
    assert "--autostart" in argv and "--command" in argv
    assert "--skills" in argv and "--skill-copies" in argv
    assert "--yes" in argv  # unattended installs stop for consent without it
    assert timeout == 3600
    assert env["LOCALAPPDATA"] == str(local)
    assert (evidence / "install.log").read_text() == (logs / "install.log").read_text()
    assert (evidence / "last-run.json").read_text() == (logs / "last-run.json").read_text()
    receipt = json.loads((evidence / "guard-result.json").read_text())
    assert receipt["status"] == ("failed" if exit_code else "passed")


@pytest.mark.parametrize(("message", "guards"), [
    ("Astrid directories require a protected owner/SYSTEM DACL.", ["F4 DACL"]),
    ("FileNotFoundError: C:\\Users\\long\\a.unpack\\archive-member", ["B1/B2 long path"]),
    ("Source staging operations require Git on PATH", ["B3 Git missing"]),
    ("CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate", ["F1 TLS"]),
    ("Unexpected bootstrap failure", []),
])
def test_guard_early_failure_keeps_receipt_without_installer_logs(tmp_path, monkeypatch, message, guards):
    monkeypatch.setattr(guard.windows, "EVIDENCE", tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    (tmp_path / "install-manifest.json").write_text("{}")

    def bootstrap(argv, log_path, mode, **kwargs):
        log_path.write_text(message)
        raise RuntimeError("bootstrap exit 1")

    monkeypatch.setattr(guard.windows, "run_bootstrap_process", bootstrap)
    with pytest.raises(RuntimeError, match="bootstrap exit 1"):
        guard.install()
    receipt = json.loads((tmp_path / "guard-result.json").read_text())
    assert receipt["guards"] == guards
    assert receipt["files"]["install.log"] == "unavailable"
    assert receipt["files"]["last-run.json"] == "unavailable"


def test_prepare_selects_checkout_archives_without_local_git_sources(tmp_path, monkeypatch):
    app = tmp_path / "journey-app" / "public"
    app.mkdir(parents=True)
    (app / "install-manifest.json").write_text(json.dumps({
        "release": 13, "tools": {"unchanged": True},
        "agent": {"sha": "0" * 40},
        "runtime": {"sha": "0" * 40, "schema_digest": "old"},
    }))
    (tmp_path / "runtime.lock.json").write_text(json.dumps({"source_commit": "b" * 40}))
    pins = tmp_path / "astrid" / "packs" / "runpod"
    pins.mkdir(parents=True)
    (pins / "pins.json").write_text(json.dumps({"sources": {
        "lifecycle": {"repository": "https://example.invalid/lifecycle.git",
                      "revision": "c" * 40, "version": "1.2.3"},
        "vibecomfy": {"repository": "https://example.invalid/vibecomfy.git", "revision": "d" * 40},
    }}))
    monkeypatch.setattr(guard.windows, "ROOT", tmp_path)
    monkeypatch.setattr(guard.windows, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(guard.windows, "source", lambda path: {
        "sha": ("b" if path.name == "journey-runtime" else "a") * 40,
        "repo": str(path),
    })
    guard.prepare()
    manifest = json.loads((tmp_path / "evidence" / "install-manifest.json").read_text())
    assert manifest["agent"] == {"sha": "a" * 40}
    assert manifest["runtime"] == {"sha": "b" * 40}
    assert manifest["tools"] == {"unchanged": True}
    assert manifest["release"] == 13
    # The candidate's own engine pins replace the published manifest's.
    assert manifest["lifecycle"] == {"repo": "https://example.invalid/lifecycle.git",
                                     "sha": "c" * 40, "version": "1.2.3"}
    assert manifest["vibecomfy"] == {"sha": "d" * 40}


def _smoke_environment(tmp_path, monkeypatch, *, generation):
    """Run command_smoke against the real stdlib fixture; ``generation`` fakes only the installed generate call."""
    import types
    import urllib.request
    (tmp_path / 'evidence').mkdir()  # main() creates this directory before any step runs
    monkeypatch.setattr(guard.windows, 'EVIDENCE', tmp_path / 'evidence')
    monkeypatch.setattr(guard.windows, 'version', lambda: {
        'python': 'installed-python.exe', 'env': {'INSTALLED_PROFILE': 'selected'},
    })
    local = tmp_path / 'local'
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.setenv('PYTHONPATH', 'checkout-must-not-leak')
    monkeypatch.setenv('PYTHONHOME', 'checkout-must-not-leak')
    monkeypatch.setattr(guard.windows, 'wait_for', lambda label, probe, **kwargs: None)
    calls = []

    def run(argv, **kwargs):
        if argv[0] == 'tasklist':
            return subprocess.CompletedProcess(argv, 0, 'TASKLIST /v rows\n', '')
        calls.append(argv)
        assert argv[:4] == ['installed-python.exe', '-I', '-m', 'astrid']
        assert kwargs['cwd'] == guard.windows.EVIDENCE
        assert kwargs['env']['INSTALLED_PROFILE'] == 'selected'
        assert not {'PYTHONPATH', 'PYTHONHOME'} & kwargs['env'].keys()
        result = {}
        if '--out' in argv:
            assert kwargs['timeout'] == 420
            return generation(argv, kwargs, urllib.request)
        if kwargs['timeout'] != 60:
            raise AssertionError(kwargs['timeout'])
        if '--comfy-url' in argv:
            url = argv[argv.index('--comfy-url') + 1]
            with urllib.request.urlopen(url + '/internal/folder_paths') as response:
                folders = json.load(response)
            assert all(__import__('pathlib').Path(paths[0]).is_dir() for paths in folders.values())
            with urllib.request.urlopen(url + '/object_info') as response:
                assert json.load(response)['UNETLoader']['input']['required']['unet_name'] == [[]]
            result = {'plan_sha256': 'a' * 64, 'files': [{}, {}, {}]}
        if '--dry-run' in argv:
            url = argv[argv.index('--placement') + 1]
            with urllib.request.urlopen(url + '/system_stats') as response:
                assert 'system' in json.load(response)
            with urllib.request.urlopen(url + '/object_info') as response:
                assert json.load(response)['UNETLoader']['input']['required']['unet_name'][0]
        return subprocess.CompletedProcess(argv, 0, json.dumps(result), '')

    monkeypatch.setattr(guard.subprocess, 'run', run)
    return calls


def _submitting_generation(argv, kwargs, urllib_request):
    """Behave like the installed product: one POST /prompt to the attached URL, then one delivered PNG."""
    url = argv[argv.index('--placement') + 1]
    request = urllib_request.Request(url + '/prompt', data=b'{"prompt": {}}', method='POST')
    with urllib_request.urlopen(request) as response:
        assert response.status == 200
    out = Path(argv[argv.index('--out') + 1])
    out.mkdir(parents=True, exist_ok=True)
    image = out / 'Qwen_image_2.1_teapot.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * 16)
    return subprocess.CompletedProcess(argv, 0, json.dumps({'image_paths': [str(image)]}), '')


def test_command_smoke_uses_installed_isolated_python_and_real_http_fixture(tmp_path, monkeypatch):
    calls = _smoke_environment(tmp_path, monkeypatch, generation=_submitting_generation)
    guard.command_smoke()
    assert len(calls) == 8
    assert calls[0][4:] == ['doctor', '--json']
    assert '--dry-run' in calls[-2] and '--apply' not in sum(calls, [])
    generate = calls[-1]
    assert generate[4:6] == ['generate', 'image']
    assert generate[generate.index('--mode') + 1] == 't2i'
    assert generate[generate.index('--entry') + 1] == 'qwen-image-2.1-turbo'
    assert generate[generate.index('--seed') + 1] == '42'
    assert generate[generate.index('--out') + 1] == str(tmp_path / 'evidence' / 'generation-output')
    assert '--dry-run' not in generate
    assert len(list((tmp_path / 'evidence').glob('command-*.log'))) == 8
    timing = json.loads((tmp_path / 'evidence' / 'generation-timing.json').read_text())
    assert timing['status'] == 'passed' and timing['prompt_posts'] == 1
    assert timing['seconds'] >= 0 and timing['budget_seconds'] == 240 and timing['timeout_seconds'] == 420
    assert not (tmp_path / 'evidence' / 'generation-failure').exists()


def test_real_generation_over_budget_fails_with_timing_and_evidence(tmp_path, monkeypatch):
    import types
    ticks = iter([0.0, 300.0])
    monkeypatch.setattr(guard, 'time', types.SimpleNamespace(monotonic=lambda: next(ticks)))
    _smoke_environment(tmp_path, monkeypatch, generation=_submitting_generation)
    with pytest.raises(RuntimeError, match='Installed real generation failed'):
        guard.command_smoke()
    evidence = tmp_path / 'evidence'
    timing = json.loads((evidence / 'generation-timing.json').read_text())
    assert timing['status'] == 'failed' and timing['seconds'] == 300.0
    assert 'took 300.0 s; budget is 240 s' in timing['error']
    assert (evidence / 'generation-failure.json').is_file()
    assert (evidence / 'generation-failure' / 'tasklist-python.txt').read_text() == 'TASKLIST /v rows\n'


def test_real_generation_timeout_keeps_logs_attempts_and_process_list(tmp_path, monkeypatch):
    def hangs(argv, kwargs, urllib_request):
        raise subprocess.TimeoutExpired(argv, kwargs['timeout'], output=b'partial stdout', stderr=b'partial stderr')

    _smoke_environment(tmp_path, monkeypatch, generation=hangs)
    local = tmp_path / 'local' / 'Astrid Runtime'
    (local / 'logs').mkdir(parents=True)
    (local / 'logs' / 'generic-host.log').write_text('lease_lost sample')
    attempt = local / 'attempts' / 'digest' / 'astrid-attempt-task-1-token'
    attempt.mkdir(parents=True)
    (attempt / 'task-stderr-tail.log').write_text('child stderr')
    with pytest.raises(RuntimeError, match='Installed real generation failed'):
        guard.command_smoke()
    evidence = tmp_path / 'evidence'
    failure = evidence / 'generation-failure'
    assert (failure / 'runtime-logs' / 'generic-host.log').read_text() == 'lease_lost sample'
    assert (failure / 'attempts' / '0-astrid-attempt-task-1-token' / 'task-stderr-tail.log').read_text() == 'child stderr'
    assert (failure / 'tasklist-python.txt').is_file()
    assert json.loads((evidence / 'generation-failure.json').read_text())['runtime_logs'] == 'captured'
    assert (evidence / 'command-generate-image.log').read_text() == 'partial stdoutpartial stderr'
    timing = json.loads((evidence / 'generation-timing.json').read_text())
    assert timing['status'] == 'failed' and 'TimeoutExpired' in timing['error']


def test_real_generation_without_a_submitted_prompt_fails(tmp_path, monkeypatch):
    def delivers_without_submitting(argv, kwargs, urllib_request):
        return subprocess.CompletedProcess(argv, 0, json.dumps({'image_paths': []}), '')

    _smoke_environment(tmp_path, monkeypatch, generation=delivers_without_submitting)
    with pytest.raises(RuntimeError, match='sent 0 POST /prompt'):
        guard.command_smoke()
    timing = json.loads((tmp_path / 'evidence' / 'generation-timing.json').read_text())
    assert timing['status'] == 'failed' and 'sent 0 POST /prompt' in timing['error']


@pytest.mark.parametrize('returncode, stdout, stderr', [
    (0, '{}', 'Traceback (most recent call last):\nAttributeError: O_DIRECTORY'),
    (0, 'Traceback (most recent call last):\n{}', ''),
    (1, '{"error":"refused"}', ''),
])
def test_command_smoke_rejects_tracebacks_even_with_zero_exit(tmp_path, monkeypatch, returncode, stdout, stderr):
    monkeypatch.setattr(guard.windows, 'EVIDENCE', tmp_path)
    monkeypatch.setattr(guard.windows, 'version', lambda: {'python': 'installed.exe', 'env': {}})
    monkeypatch.setattr(guard.subprocess, 'run', lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], returncode, stdout, stderr))
    with pytest.raises(RuntimeError, match='command smoke doctor failed'):
        guard.command_smoke()
    assert (tmp_path / 'command-doctor.log').read_text() == stdout + stderr


LONG_PATH_MESSAGE = (
    "FileNotFoundError: [WinError 3] The system cannot find the path specified: "
    "'C:\\Users\\AstridLongUserName_Tester01\\AppData\\Local\\Astrid Runtime\\.vibecomfy-runtime\\x'"
)


def _failing_generation(output):
    def fails(argv, kwargs, urllib_request):
        return subprocess.CompletedProcess(argv, 1, output, '')
    return fails


@pytest.mark.parametrize('long_paths', ['off', 'on', None])
def test_generation_failure_raises_whatever_the_long_paths_variant(tmp_path, monkeypatch, long_paths):
    # No variant tolerates a path-length failure: generation must work with long paths off too.
    if long_paths is None:
        monkeypatch.delenv('ASTRID_GUARD_LONG_PATHS', raising=False)
    else:
        monkeypatch.setenv('ASTRID_GUARD_LONG_PATHS', long_paths)
    _smoke_environment(tmp_path, monkeypatch, generation=_failing_generation(LONG_PATH_MESSAGE))
    with pytest.raises(RuntimeError, match='Installed real generation failed'):
        guard.command_smoke()
    evidence = tmp_path / 'evidence'
    assert not (evidence / 'generation-known-failure.json').exists()
    assert json.loads((evidence / 'generation-timing.json').read_text())['status'] == 'failed'


@pytest.mark.parametrize('long_paths', ['off', 'on'])
def test_generation_success_passes_in_both_variants(tmp_path, monkeypatch, long_paths):
    monkeypatch.setenv('ASTRID_GUARD_LONG_PATHS', long_paths)
    _smoke_environment(tmp_path, monkeypatch, generation=_submitting_generation)
    guard.command_smoke()
    assert json.loads((tmp_path / 'evidence' / 'generation-timing.json').read_text())['status'] == 'passed'
