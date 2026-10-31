from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import fully_local_lifecycle_adapter as adapter

APP_REF = "7" * 40
RUNTIME_REF = "8" * 40
PINOKIO_REF = "9" * 40
PTERM_REF = "6" * 40
PINOKIO_APP_REF = "pinokio://127.0.0.1:42000/api/astrid-disposable"


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], text=True, capture_output=True, check=True
    ).stdout.strip()


def _repo(path: Path, files: dict[str, bytes], *, executable: tuple[str, ...] = ()) -> str:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _git(path, "config", "user.name", "adapter test")
    _git(path, "config", "user.email", "adapter-test@example.invalid")
    for name, body in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        if name in executable:
            target.chmod(0o755)
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "fixture")
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def custody(tmp_path: Path, monkeypatch) -> dict[str, Path | str]:
    monkeypatch.setattr(adapter.platform, "system", lambda: "Linux")
    pinokio_source = tmp_path / "pinokio-source"
    pinokio_home = tmp_path / "pinokio-home"
    pinokio = pinokio_home / "api" / "astrid-disposable"
    runtime = tmp_path / "runtime-source"
    app = tmp_path / "app-source"
    editor = pinokio / "app" / "editor"
    state = pinokio / "app" / "astrid"
    pterm = tmp_path / "pterm-source"
    pinokiod = tmp_path / "pinokiod-source"
    owner = tmp_path / "owner-source"
    update = b"module.exports = async () => ({ run: [] });\n"
    pin_ref = _repo(
        pinokio_source,
        {"pinokio/update.js": update, ".gitignore": b"/app/\n/ENVIRONMENT\n"},
    )
    pinokio.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--local", str(pinokio_source), str(pinokio)], check=True)
    (pinokio / "ENVIRONMENT").write_text("ASTRID_APP_REF=main\n", encoding="utf-8")
    runtime_ref = _repo(runtime, {"README.md": b"Runtime source\n"})
    app_ref = _repo(app, {"scripts/dev-local-workspace.mjs": b"// pinned build\n"})
    editor.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--local", str(app), str(editor)], check=True)
    state.mkdir()
    (state / adapter.DISPOSABLE_MARKER).write_bytes(adapter.MARKER_BYTES)
    data_root = state / "data"
    data_root.mkdir()
    (state / "install.json").write_text(
        json.dumps(
            {
                "managed_by": "pinokio",
                "owner": "haus.astrid.runtime",
                "data_root": str(data_root),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (state.parent / "install-mode.json").write_text(
        '{"schema_version":1,"mode":"local"}\n', encoding="utf-8"
    )
    pterm_home = repr(str(pinokio_home))
    pterm_script = (
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        f"print(json.dumps({pterm_home}) if sys.argv[1:] == ['home','--json'] else "
        "json.dumps({'evidence_class':'source_fake'}))\n"
    ).encode()
    pterm_ref = _repo(
        pterm,
        {
            "package.json": b'{"name":"pterm","version":"0.0-test","bin":{"pterm":"index.js"}}\n',
            "package-lock.json": b'{"name":"pterm","lockfileVersion":3}\n',
            "index.js": pterm_script,
        },
        executable=("index.js",),
    )
    pinokiod_ref = _repo(
        pinokiod,
        {
            "package.json": b'{"name":"pinokiod","version":"8.1.1"}\n',
            "package-lock.json": b'{"name":"pinokiod","lockfileVersion":3}\n',
        },
    )
    owner_script = (
        "#!/usr/bin/env python3\nimport json\nprint(json.dumps({'evidence_class':'source_fake'}))\n"
    ).encode()
    owner_ref = _repo(owner, {"owner.py": owner_script}, executable=("owner.py",))
    monkeypatch.setattr(adapter, "PINOKIO_REF", pin_ref)
    monkeypatch.setattr(adapter, "RUNTIME_REF", runtime_ref)
    monkeypatch.setattr(adapter, "APP_REF", app_ref)
    monkeypatch.setattr(adapter, "PTERM_REF", pterm_ref)
    monkeypatch.setattr(adapter, "PINOKIOD_REF", pinokiod_ref)
    monkeypatch.setattr(adapter, "PINOKIO_UPDATE_SHA256", hashlib.sha256(update).hexdigest())
    marker = adapter.editor_marker_path(editor)
    marker.write_bytes(adapter.editor_marker_bytes(editor))
    adapter.pinokio_marker_path(pinokio).write_bytes(adapter.pinokio_marker_bytes(pinokio))
    return {
        "pinokio_source": pinokio_source,
        "pinokio": pinokio,
        "pinokio_home": pinokio_home,
        "runtime": runtime,
        "app": app,
        "editor": editor,
        "state": state,
        "pterm": pterm,
        "pterm_executable": pterm / "index.js",
        "pinokiod": pinokiod,
        "owner": owner,
        "owner_executable": owner / "owner.py",
        "owner_ref": owner_ref,
        "owner_sha256": hashlib.sha256(owner_script).hexdigest(),
        "pin_ref": pin_ref,
        "runtime_ref": runtime_ref,
        "app_ref": app_ref,
        "pterm_ref": pterm_ref,
        "pinokiod_ref": pinokiod_ref,
    }


def _authority_args(custody):
    return {
        "pinokiod_checkout": Path(custody["pinokiod"]),
        "owner_checkout": Path(custody["owner"]),
        "owner_executable": Path(custody["owner_executable"]),
        "owner_ref": str(custody["owner_ref"]),
        "owner_sha256": str(custody["owner_sha256"]),
    }


def _auth(custody):
    return adapter.authenticate_inputs(
        pinokio_source=Path(custody["pinokio_source"]),
        pinokio_checkout=Path(custody["pinokio"]),
        runtime_source=Path(custody["runtime"]),
        app_source=Path(custody["app"]),
        editor_checkout=Path(custody["editor"]),
        state_root=Path(custody["state"]),
        pterm_checkout=Path(custody["pterm"]),
        pterm_executable=Path(custody["pterm_executable"]),
        pinokio_app_ref=PINOKIO_APP_REF,
        **_authority_args(custody),
    )


def _good_evidence(provenance, *, pinokio_app_ref=PINOKIO_APP_REF):
    events = [{"sequence": i, "name": name} for i, name in enumerate(adapter.EVENT_ORDER, start=1)]
    events[9]["status"] = "failed"
    events[17].update({"ready": True, "local_set": True})
    snapshots = {}
    snapshot_values = {
        "checkout": {"head": provenance["editor"]["head"], "branch": "main", "porcelain": ""},
        "runtime": {"root": provenance["state_root"], "install_digest": "sha256:runtime"},
        "lock": {"held": False, "released": True, "probe_exit_status": 0},
        "mode": {
            "value": provenance["state"]["mode"],
            "sha256": provenance["state"]["mode_sha256"],
        },
        "data": {
            "root": provenance["state"]["data_root"],
            "inventory_digest": "sha256:data",
        },
    }
    for name, snapshot in snapshot_values.items():
        snapshots[name] = {"before": snapshot, "after": dict(snapshot)}
    snapshots["readiness"] = {
        "before": {"ready": False, "status": 503},
        "after": {
            "ready": True,
            "status": 200,
            "route_id": adapter.ROUTE_ID,
            "mode": "local",
            "local_set": True,
            "hosted": False,
        },
    }
    argv = adapter._exact_argv(provenance, pinokio_app_ref)
    return {
        "evidence_class": "real_subprocess",
        "protocol": adapter.ENGINE_PROTOCOL,
        "engine": {
            "pterm": provenance["pterm"],
            "pinokiod": provenance["pinokiod"],
            "owner": provenance["owner"],
        },
        "route_id": adapter.ROUTE_ID,
        "mode": "local",
        "backend": False,
        "foreground_service": False,
        "provenance": provenance,
        "request": {
            "request_id": "req-1",
            "argv": argv,
            "operation": "start",
            "action": "pinokio/update.js",
            "pinokio_app_ref": pinokio_app_ref,
        },
        "owner_process": {
            "pid": 13579,
            "argv": adapter._owner_argv(provenance, argv),
            "exit_status": 0,
        },
        "pterm_process": {"pid": 13580, "argv": argv, "exit_status": 0},
        "task": {
            "request_id": "req-1",
            "task_id": "task-1",
            "accepted": True,
            "settled": True,
            "terminal": "failed",
        },
        "action": {
            "route_id": adapter.ROUTE_ID,
            "ref": provenance["pinokio"]["head"],
            "sha256": provenance["pinokio"]["update_action_sha256"],
        },
        "events": events,
        "failure": {
            "step": "editor_build",
            "injected": True,
            "exit_status": 23,
            "pid": 24680,
            "injection_authority": provenance["owner"],
            "injection_mechanism": "real-build-process-fault",
            "pinokio_failure_marker": "ASTRID_PINOKIO_FAILURE",
            "pinokio_failure_marker_seen": True,
            "argv": ["node", "scripts/dev-local-workspace.mjs", "--build"],
        },
        "processes": [
            {"pid": 24680, "argv": ["node", "scripts/dev-local-workspace.mjs", "--build"]}
        ],
        "snapshots": snapshots,
    }


def test_authenticates_candidate_runtime_app_editor_and_pterm(custody):
    result = _auth(custody)
    assert result["pinokio"]["head"] == custody["pin_ref"]
    assert result["runtime"]["head"] == custody["runtime_ref"]
    assert result["app"]["head"] == custody["app_ref"]
    assert result["editor"]["head"] == custody["app_ref"]
    assert result["pterm"]["head"] == custody["pterm_ref"]
    assert result["pinokiod"]["head"] == custody["pinokiod_ref"]
    assert result["owner"]["head"] == custody["owner_ref"]
    assert result["pterm"]["protocol"] == adapter.ENGINE_PROTOCOL
    assert result["pinokio"]["update_action_sha256"] == f"sha256:{adapter.PINOKIO_UPDATE_SHA256}"


def test_linux_gate_uses_real_platform_before_custody(monkeypatch, tmp_path):
    monkeypatch.setattr(adapter.platform, "system", lambda: "Darwin")
    with pytest.raises(adapter.AdapterError, match="requires Linux-native"):
        adapter.authenticate_inputs(
            pinokio_source=tmp_path / "absent",
            pinokio_checkout=tmp_path / "absent",
            runtime_source=tmp_path / "absent",
            app_source=tmp_path / "absent",
            editor_checkout=tmp_path / "absent",
            state_root=tmp_path / "absent",
            pterm_checkout=tmp_path / "absent",
            pterm_executable=tmp_path / "absent",
            pinokiod_checkout=tmp_path / "absent",
            owner_checkout=tmp_path / "absent",
            owner_executable=tmp_path / "absent",
            owner_ref="bad",
            owner_sha256="bad",
            pinokio_app_ref="bad",
        )
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("kind", ["editor", "state"])
def test_refuses_symlink_and_nested_disposable_roots(custody, tmp_path, kind):
    editor = Path(custody["editor"])
    state = Path(custody["state"])
    if kind == "editor":
        link = tmp_path / "editor-link"
        link.symlink_to(editor, target_is_directory=True)
        adapter.editor_marker_path(link).write_bytes(adapter.editor_marker_bytes(editor))
        args = {"editor_checkout": link}
        message = "symlink"
    else:
        nested = state / "nested"
        nested.mkdir()
        (nested / adapter.DISPOSABLE_MARKER).write_bytes(adapter.MARKER_BYTES)
        args = {"state_root": nested}
        message = "nested inside another"
    inputs = {
        "pinokio_source": Path(custody["pinokio_source"]),
        "pinokio_checkout": Path(custody["pinokio"]),
        "runtime_source": Path(custody["runtime"]),
        "app_source": Path(custody["app"]),
        "editor_checkout": editor,
        "state_root": state,
        "pterm_checkout": Path(custody["pterm"]),
        "pterm_executable": Path(custody["pterm_executable"]),
        "pinokio_app_ref": PINOKIO_APP_REF,
        **_authority_args(custody),
    }
    inputs.update(args)
    with pytest.raises(adapter.AdapterError, match=message):
        adapter.authenticate_inputs(**inputs)


def test_external_editor_marker_is_bound_to_path_and_app_ref(custody):
    marker = adapter.editor_marker_path(Path(custody["editor"]))
    marker.write_bytes(b'{"schema_version":1}\n')
    with pytest.raises(adapter.AdapterError, match="does not bind"):
        _auth(custody)


def test_missing_owner_authority_fails_closed_before_route(custody, tmp_path):
    inputs = {
        "pinokio_source": Path(custody["pinokio_source"]),
        "pinokio_checkout": Path(custody["pinokio"]),
        "runtime_source": Path(custody["runtime"]),
        "app_source": Path(custody["app"]),
        "editor_checkout": Path(custody["editor"]),
        "state_root": Path(custody["state"]),
        "pterm_checkout": Path(custody["pterm"]),
        "pterm_executable": Path(custody["pterm_executable"]),
        "pinokiod_checkout": Path(custody["pinokiod"]),
        "owner_checkout": Path(custody["owner"]),
        "owner_executable": tmp_path / "missing-owner",
        "owner_ref": str(custody["owner_ref"]),
        "owner_sha256": str(custody["owner_sha256"]),
        "pinokio_app_ref": PINOKIO_APP_REF,
    }
    with pytest.raises(adapter.AdapterError, match="owner settlement wrapper"):
        adapter.authenticate_inputs(**inputs)


def test_refuses_mismatched_update_action_and_dirty_engine(custody):
    action = Path(custody["pinokio"]) / "pinokio/update.js"
    action.write_bytes(b"changed\n")
    with pytest.raises(adapter.AdapterError, match="digest mismatch"):
        _auth(custody)


def test_refuses_network_pull_origin_and_app_branch_mismatch(custody):
    editor = Path(custody["editor"])
    _git(editor, "remote", "set-url", "origin", "https://example.invalid/editor.git")
    with pytest.raises(adapter.AdapterError, match="network pulls are forbidden"):
        _auth(custody)
    _git(editor, "remote", "set-url", "origin", str(custody["app"]))
    (Path(custody["pinokio"]) / "ENVIRONMENT").write_text(
        "ASTRID_APP_REF=other-branch\n", encoding="utf-8"
    )
    with pytest.raises(adapter.AdapterError, match="ASTRID_APP_REF"):
        _auth(custody)


def test_pterm_invocation_is_exact_and_rejects_direct_node_or_fallback(custody):
    provenance = _auth(custody)
    expected = [
        str(custody["pterm_executable"]),
        "start",
        "pinokio/update.js",
        "--ref",
        PINOKIO_APP_REF,
    ]
    assert adapter._exact_argv(provenance, PINOKIO_APP_REF) == expected
    owner_argv = adapter._owner_argv(provenance, expected)
    assert owner_argv[owner_argv.index("--inject-failure-step") + 1] == "editor_build"
    assert owner_argv[owner_argv.index("--injection-mechanism") + 1] == ("real-build-process-fault")
    assert "node" not in expected[0]
    assert "backend" not in expected and "foreground" not in expected
    with pytest.raises(adapter.AdapterError, match="full local pinokio"):
        adapter.authenticate_inputs(
            pinokio_source=Path(custody["pinokio_source"]),
            pinokio_checkout=Path(custody["pinokio"]),
            runtime_source=Path(custody["runtime"]),
            app_source=Path(custody["app"]),
            editor_checkout=Path(custody["editor"]),
            state_root=Path(custody["state"]),
            pterm_checkout=Path(custody["pterm"]),
            pterm_executable=Path(custody["pterm_executable"]),
            pinokio_app_ref="node pinokio/update.js",
            **_authority_args(custody),
        )


def test_pterm_home_must_resolve_ref_to_disposable_checkout(custody, monkeypatch):
    provenance = _auth(custody)
    result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps("/wrong/pinokio-home"), stderr=""
    )
    monkeypatch.setattr(adapter.subprocess, "run", lambda *_args, **_kwargs: result)
    with pytest.raises(adapter.AdapterError, match="does not contain the authenticated disposable"):
        adapter._authenticate_pterm_home(provenance, PINOKIO_APP_REF)


def test_source_fake_is_rejected_before_boundary_claims_and_never_written(custody, tmp_path):
    provenance = _auth(custody)
    fake = {"evidence_class": "source_fake"}
    with pytest.raises(adapter.AdapterError, match="source_fake"):
        adapter._validate_engine_evidence(
            fake,
            provenance,
            expected_argv=adapter._exact_argv(provenance, PINOKIO_APP_REF),
            expected_owner_argv=adapter._owner_argv(
                provenance, adapter._exact_argv(provenance, PINOKIO_APP_REF)
            ),
            pinokio_app_ref=PINOKIO_APP_REF,
        )
    output = tmp_path / "fake-receipt.json"
    with pytest.raises(adapter.AdapterError, match="source_fake"):
        adapter.run_route(
            pinokio_source=Path(custody["pinokio_source"]),
            pinokio_checkout=Path(custody["pinokio"]),
            runtime_source=Path(custody["runtime"]),
            app_source=Path(custody["app"]),
            editor_checkout=Path(custody["editor"]),
            state_root=Path(custody["state"]),
            pterm_checkout=Path(custody["pterm"]),
            pterm_executable=Path(custody["pterm_executable"]),
            pinokio_app_ref=PINOKIO_APP_REF,
            output=output,
            **_authority_args(custody),
        )
    assert not output.exists()


def test_accepts_only_settled_exact_provenance_and_unchanged_restore_snapshots(custody):
    provenance = _auth(custody)
    evidence = _good_evidence(provenance)
    adapter._validate_engine_evidence(
        evidence,
        provenance,
        expected_argv=adapter._exact_argv(provenance, PINOKIO_APP_REF),
        expected_owner_argv=adapter._owner_argv(
            provenance, adapter._exact_argv(provenance, PINOKIO_APP_REF)
        ),
        pinokio_app_ref=PINOKIO_APP_REF,
    )
    bad_cases = []
    unsettled = json.loads(json.dumps(evidence))
    unsettled["task"]["settled"] = False
    bad_cases.append((unsettled, "accepted and settled"))
    mismatched = json.loads(json.dumps(evidence))
    mismatched["provenance"]["app"]["head"] = "f" * 40
    bad_cases.append((mismatched, "provenance"))
    changed = json.loads(json.dumps(evidence))
    changed["snapshots"]["data"]["after"]["digest"] = "changed"
    bad_cases.append((changed, "data before/after"))
    bad_action = json.loads(json.dumps(evidence))
    bad_action["request"]["argv"][1] = "node"
    bad_cases.append((bad_action, "exact pterm start"))
    fallback = json.loads(json.dumps(evidence))
    fallback["backend"] = True
    bad_cases.append((fallback, "backend and foreground-service routes are forbidden"))
    for bad_evidence, error in bad_cases:
        with pytest.raises(adapter.AdapterError, match=error):
            adapter._validate_engine_evidence(
                bad_evidence,
                provenance,
                expected_argv=adapter._exact_argv(provenance, PINOKIO_APP_REF),
                expected_owner_argv=adapter._owner_argv(
                    provenance, adapter._exact_argv(provenance, PINOKIO_APP_REF)
                ),
                pinokio_app_ref=PINOKIO_APP_REF,
            )


def test_atomic_exclusive_output_refuses_existing_target(tmp_path):
    output = tmp_path / "evidence.json"
    output.write_text("owner data")
    with pytest.raises(FileExistsError):
        adapter._atomic_exclusive_json(output, {"replacement": True})
    assert output.read_text() == "owner data"
