"""Synthetic privacy, schema and failure-propagation checks; not native evidence."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts/verify_macos_admission_capture.py"
WRAPPER = ROOT / "scripts/run_macos_admission_capture.sh"
EXPECTED = "76257fe1b3939f0ce8fbaf5a8a21ef5e4e3b430a1badc933aefffd8f12f4f729"
SECRET = "SYNTHETIC_PRIVATE_CONTENT_DO_NOT_ECHO"


def record():
    return {"format": "astrid-macos-admission-diagnostic/v1",
            "checks": [["selection_read_started", True], ["service_owned", False]],
            "stats": [{"phase": "publication", **{k: 1 for k in (
                "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns")}}],
            "digests": [{"phase": "recheck", "sha256": "a" * 64}],
            "truncated": False, "current_uid": os.getuid(), "effective_uid": os.geteuid(),
            "source_sha256": EXPECTED, "outcome": "returned", "exception_category": None,
            "pid": 1, "started_ns": 1}


def run_verifier(directory):
    return subprocess.run([sys.executable, "-I", str(VERIFIER)], text=True, capture_output=True,
                          env={**os.environ, "ASTRID_MAC_ADMISSION_CAPTURE_DIR": str(directory)})


def write_record(directory, value):
    file = directory / ("admission-" + "a" * 32 + ".json")
    file.write_text(json.dumps(value))
    file.chmod(0o600)
    return file


def test_schema_accepts_only_complete_redacted_capture(tmp_path):
    capture = tmp_path / "capture"
    capture.mkdir(mode=0o700)
    write_record(capture, record())
    result = run_verifier(capture)
    assert result.returncode == 0
    assert result.stdout == "Validated redacted admission capture files: 1\n"
    assert not result.stderr


@pytest.mark.parametrize("change", [
    lambda r: r.update(extra=SECRET),
    lambda r: r.update(source_sha256="b" * 64),
    lambda r: r.update(truncated=True),
    lambda r: r.update(checks=[["secret", SECRET]]),
    lambda r: r.update(checks=[["service_owned", True]]),
    lambda r: r.update(stats={}),
    lambda r: r.update(digests={}),
    lambda r: r.update(digests=[{"phase": "recheck", "sha256": SECRET}]),
    lambda r: r.update(pid=True),
    lambda r: r.update(outcome="returned", exception_category="other"),
])
def test_schema_rejects_unexpected_or_incomplete_without_echo(tmp_path, change):
    capture = tmp_path / "capture"
    capture.mkdir(mode=0o700)
    value = record()
    change(value)
    write_record(capture, value)
    result = run_verifier(capture)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Capture unavailable, incomplete, unsafe, or source mismatch\n"
    assert SECRET not in result.stderr
    assert str(tmp_path) not in result.stderr


def test_schema_rejects_duplicate_json_fields_and_unsafe_files(tmp_path):
    capture = tmp_path / "capture"
    capture.mkdir(mode=0o700)
    file = write_record(capture, record())
    file.write_text(file.read_text().replace('"truncated": false', '"truncated": false, "truncated": false'))
    assert run_verifier(capture).returncode == 1
    file.write_text(json.dumps(record()))
    file.chmod(0o644)
    assert run_verifier(capture).returncode == 1
    file.chmod(0o600)
    (capture / "unexpected").write_text(SECRET)
    assert run_verifier(capture).returncode == 1


def executable(path, text):
    path.write_text(text)
    path.chmod(0o700)


def test_wrapper_encrypts_only_validated_capture_and_preserves_journey_exit(tmp_path):
    # Linux stand-ins simulate only macOS ACL commands; native ACL behavior requires a Mac.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable(bindir / "uname", "#!/bin/sh\necho Darwin\n")
    executable(bindir / "chmod", "#!/bin/sh\n[ \"$1\" = -N ] && exit 0\nexec /bin/chmod \"$@\"\n")
    executable(bindir / "ls", "#!/bin/sh\necho private\n")
    runner = tmp_path / "runner"
    runner.mkdir()
    scripts = tmp_path / "agent/scripts"
    scripts.mkdir(parents=True)
    (scripts / "verify_macos_admission_capture.py").write_bytes(VERIFIER.read_bytes())
    synthetic = json.dumps(record())
    (scripts / "ci_journey.py").write_text(
        "import os, pathlib, sys\n"
        f"payload = {synthetic!r}\n"
        "p = pathlib.Path(os.environ['ASTRID_MAC_ADMISSION_CAPTURE_DIR']) / ('admission-' + 'a'*32 + '.json')\n"
        "p.write_text(payload if os.environ.get('BAD_RECORD') != '1' else '{\\\"secret\\\":\\\"' + 'PRIVATE' + '\\\"}')\n"
        "p.chmod(0o600)\n"
        "sys.exit(int(os.environ.get('JOURNEY_TEST_EXIT', '17')))\n")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=synthetic", "-keyout", str(key), "-out", str(cert)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    der = subprocess.check_output(["openssl", "x509", "-in", str(cert), "-outform", "DER"])
    output = tmp_path / "outputs"
    env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
           "ASTRID_CI_EXPECTED_RUNNER": "macos-15", "RUNNER_TEMP": str(runner),
           "GITHUB_OUTPUT": str(output), "MAC_ADMISSION_RECIPIENT_CERT": cert.read_text(),
           "MAC_ADMISSION_CERT_SHA256": hashlib.sha256(der).hexdigest()}
    result = subprocess.run(["bash", str(WRAPPER)], cwd=tmp_path, env=env, text=True, capture_output=True)
    assert result.returncode == 17
    assert "journey_exit=17 validation_exit=0 retention_exit=0" in result.stdout
    assert output.read_text() == "capture_encrypted=true\n"
    encrypted = runner / "macos-admission-capture.p7m"
    assert encrypted.is_file() and EXPECTED.encode() not in encrypted.read_bytes()
    plain = subprocess.check_output(["openssl", "smime", "-decrypt", "-inform", "DER", "-in", str(encrypted),
                                     "-recip", str(cert), "-inkey", str(key)])
    assert EXPECTED.encode() in plain
    assert not list(runner.glob("astrid-admission.*"))
    encrypted.unlink()
    output.unlink()
    bad = subprocess.run(["bash", str(WRAPPER)], cwd=tmp_path, env={**env, "BAD_RECORD": "1"},
                         text=True, capture_output=True)
    assert bad.returncode == 17
    assert "validation_exit=1 retention_exit=1" in bad.stdout
    assert not encrypted.exists() and not output.exists()
    assert "PRIVATE" not in bad.stdout + bad.stderr
    assert not list(runner.glob("astrid-admission.*"))
    bad_after_pass = subprocess.run(["bash", str(WRAPPER)], cwd=tmp_path,
                                    env={**env, "BAD_RECORD": "1", "JOURNEY_TEST_EXIT": "0"},
                                    text=True, capture_output=True)
    assert bad_after_pass.returncode == 1
    assert "journey_exit=0 validation_exit=1 retention_exit=1" in bad_after_pass.stdout
    assert not encrypted.exists() and not output.exists()
    mismatch = subprocess.run(["bash", str(WRAPPER)], cwd=tmp_path,
                              env={**env, "MAC_ADMISSION_CERT_SHA256": "0" * 64},
                              text=True, capture_output=True)
    assert mismatch.returncode == 1
    assert not encrypted.exists() and not output.exists()
    assert not list(runner.glob("astrid-admission.*"))


def test_workflow_diagnostic_input_requires_exact_tuple_and_private_recipient(tmp_path):
    import textwrap

    workflow = (ROOT / ".github/workflows/journey.yml").read_text()
    config_step = workflow.split("      - id: config\n", 1)[1]
    program = textwrap.dedent(config_step.split("          python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0])
    (tmp_path / "driver").symlink_to(ROOT, target_is_directory=True)
    output = tmp_path / "config-output"
    candidate = {"app": "a" * 40, "agent": "743c8f3d8c9bc6fd363fc267f634fad33060233c",
                 "runtime": "2776ba973b6c8f3555b5629b0840d9691a98d3c2", "pinokio": "d" * 40}
    env = {**os.environ, "CANDIDATE": json.dumps(candidate), "DRIVER_AGENT_REF": "e" * 40,
           "WORKFLOW_REPOSITORY": "banodoco/astrid-agent", "EVENT_NAME": "workflow_dispatch",
           "OLD_LAUNCHER_REF": "bbba620e3d89eeff8021417f365e6ea584203105",
           "STOPPED_PATH_AGENT_REF": candidate["agent"], "M4_4A_EXECUTOR_CONFIGS": '{"linux":{},"macos":{},"windows":{}}',
           "DIRECT_ONLY": "true", "M4_4A_BACKEND_ONLY": "false", "M4_5_ENABLED": "false",
           "M4_5_ONLY": "false", "M4_5_BOUNDARY": "", "M4_5_BOUNDARY_ARGS": "[]",
           "M4_4B_ENABLED": "false", "M4_6_ENABLED": "false", "M4_6_CANDIDATE": "",
           "MAC_ADMISSION_DIAGNOSTIC": "true", "MAC_ADMISSION_CERT_SHA256": "f" * 64,
           "GITHUB_OUTPUT": str(output)}
    def run(**changes):
        return subprocess.run([sys.executable, "-c", program], cwd=tmp_path,
                              env={**env, **changes}, capture_output=True, text=True)
    assert run().returncode == 0
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["macos_admission_diagnostic"] == "true"
    assert {item["job_id"] for item in json.loads(values["matrix"])["include"]} == {
        "direct-macos", "direct-linux", "direct-windows"}
    for changes in ({"EVENT_NAME": "push"}, {"DIRECT_ONLY": "false"},
                    {"MAC_ADMISSION_CERT_SHA256": ""}, {"M4_6_ENABLED": "true"},
                    {"CANDIDATE": json.dumps({**candidate, "runtime": "f" * 40})}):
        assert run(**changes).returncode != 0
    normal_step = workflow.split("      - name: Run the matrix-owned lifecycle driver\n", 1)[1].split("      - name:", 1)[0]
    assert "needs.config.outputs.macos_admission_diagnostic != 'true'" in normal_step
    encrypted = workflow.split("      - name: Retain encrypted validated macOS admission capture\n", 1)[1].split("      - name:", 1)[0]
    assert "steps.mac_admission.outputs.capture_encrypted == 'true'" in encrypted
    assert "macos-admission-capture.p7m" in encrypted
