"""OpenRouter validation and private OMP credential import contracts."""

import hashlib
import io
import json
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from astrid import omp_agent

KEY = "sk-or-test-private-key-for-local-fixtures"


@pytest.fixture
def provider(monkeypatch):
    requests = []
    behavior = {"status": 200, "delay": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            time.sleep(behavior["delay"])
            self.send_response(behavior["status"])
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ASTRID_OPENROUTER_API_BASE", f"http://127.0.0.1:{server.server_port}")
    try:
        yield behavior, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def fake_omp(tmp_path, monkeypatch):
    command = tmp_path / "omp"
    calls = tmp_path / "calls.jsonl"
    state = tmp_path / "roles.json"
    state.write_text(json.dumps({"smol": "local/small"}))
    command.write_text(f'''#!{sys.executable}
import hashlib, json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path({str(calls)!r}).open("a") as stream:
    stream.write(json.dumps({{"argv": args, "cwd": os.getcwd()}}) + "\\n")
if args[:1] == ["--profile"]:
    args = args[2:]
state = Path({str(state)!r})
if args[:2] == ["auth", "import"]:
    document = json.loads(Path(args[2]).read_text())
    credential = document["credentials"][0]["credential"]
    assert hashlib.sha256(credential["key"].encode()).hexdigest() == {hashlib.sha256(KEY.encode()).hexdigest()!r}
    # Deliberately hostile child output must never reach the caller.
    print(credential["key"])
    print(credential["key"], file=sys.stderr)
    sys.exit(int(os.environ.get("FAKE_IMPORT_EXIT", "0")))
elif args[:3] == ["config", "get", "modelRoles"]:
    print(json.dumps({{"value": json.loads(state.read_text())}}))
elif args[:3] == ["config", "set", "modelRoles"]:
    state.write_text(args[3])
    print(json.dumps({{"value": json.loads(args[3])}}))
elif args[:2] == ["models", "ls"]:
    model = json.loads(state.read_text()).get("default", "openrouter/automatic")
    Path(os.environ["ASTRID_OMP_MODELS_RESULT"]).write_text(json.dumps({{
        "schemaVersion": 1, "currentModel": model, "providers": [],
    }}))
    print(json.dumps({{"models": []}}))
else:
    sys.exit(8)
''')
    command.chmod(0o755)
    monkeypatch.setenv("OMP_BIN", str(command))
    monkeypatch.setattr(omp_agent, "_service_profile", lambda: "app-profile")
    monkeypatch.setattr(omp_agent.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))
    imports = []
    run = subprocess.run

    def observe(argv, **kwargs):
        assert KEY not in json.dumps(argv)
        assert KEY not in json.dumps(dict(kwargs.get("env") or {}))
        if "import" in argv:
            path = Path(argv[-1])
            imports.append((path, json.loads(path.read_text()), stat.S_IMODE(path.stat().st_mode),
                            stat.S_IMODE(path.parent.stat().st_mode)))
        return run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", observe)
    return state, calls, imports


def assert_private_import(fake_omp, tmp_path, capsys):
    _state, calls, imports = fake_omp
    assert len(imports) == 1
    path, document, file_mode, dir_mode = imports[0]
    assert document == {"version": 1, "credentials": [{"provider": "openrouter", "credential": {
        "type": "api_key", "key": KEY, "source": "login",
    }}]}
    assert (file_mode, dir_mode) == (0o600, 0o700)
    assert not path.parent.exists()
    records = [json.loads(line) for line in calls.read_text().splitlines()]
    assert all(record["argv"][:2] == ["--profile", "app-profile"] for record in records)
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err
    for remaining in tmp_path.rglob("*"):
        if remaining.is_file():
            assert KEY.encode() not in remaining.read_bytes()
    return json.loads(captured.out), records


def test_key_import_uses_private_file_and_leaves_automatic_model(provider, fake_omp, tmp_path, capsys):
    assert omp_agent.main(["agent", "key", "set", "openrouter"]) == 0
    response, records = assert_private_import(fake_omp, tmp_path, capsys)
    assert response == {"provider": "openrouter", "signedIn": True, "currentModel": "openrouter/automatic"}
    assert provider[1] == [("/api/v1/auth/key", "Bearer " + KEY)]
    assert not any(record["argv"][2:3] == ["config"] for record in records)


def test_key_import_reports_null_when_no_model_is_available(provider, fake_omp, tmp_path, capsys):
    fake_omp[0].write_text(json.dumps({"default": None}))
    assert omp_agent.main(["agent", "key", "set", "openrouter"]) == 0
    response, _records = assert_private_import(fake_omp, tmp_path, capsys)
    assert response == {"provider": "openrouter", "signedIn": True, "currentModel": None}


@pytest.mark.parametrize("existing", [None, "other/existing"])
def test_key_sets_only_missing_global_default(provider, fake_omp, tmp_path, capsys, monkeypatch, existing):
    state, _calls, _imports = fake_omp
    roles = {"smol": "local/small", **({"default": existing} if existing else {})}
    state.write_text(json.dumps(roles))
    monkeypatch.setattr(omp_agent, "ASTRID_OPENROUTER_DEFAULT_MODEL", "openrouter/chosen")
    assert omp_agent.main(["agent", "key", "set", "openrouter"]) == 0
    response, records = assert_private_import(fake_omp, tmp_path, capsys)
    assert response["currentModel"] == (existing or "openrouter/chosen")
    assert json.loads(state.read_text()) == {**roles, "default": existing or "openrouter/chosen"}
    config_calls = [record for record in records if record["argv"][2:3] == ["config"]]
    assert config_calls and all(Path(record["cwd"]).name.startswith("astrid-agent-config-") for record in config_calls)


@pytest.mark.parametrize("status,expected", [(401, 3), (403, 3), (500, 4)])
def test_provider_failure_never_imports(provider, fake_omp, capsys, status, expected):
    provider[0]["status"] = status
    assert omp_agent.main(["agent", "key", "set", "openrouter"]) == expected
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error": "invalid_key" if expected == 3 else "provider_unreachable"}
    assert not captured.err
    assert not fake_omp[1].exists()


def test_provider_timeout(provider, fake_omp, capsys, monkeypatch):
    provider[0]["delay"] = 0.2
    urlopen = omp_agent.urllib.request.urlopen
    monkeypatch.setattr(omp_agent.urllib.request, "urlopen", lambda request, **_kwargs: urlopen(request, timeout=0.02))
    assert omp_agent.main(["agent", "key", "set", "openrouter"]) == 4
    assert json.loads(capsys.readouterr().out) == {"error": "provider_unreachable"}
    assert not fake_omp[1].exists()


def test_provider_network_error(provider, fake_omp, capsys, monkeypatch):
    monkeypatch.setenv("ASTRID_OPENROUTER_API_BASE", "http://127.0.0.1:0")
    assert omp_agent.main(["agent", "key", "set", "openrouter"]) == 4
    assert json.loads(capsys.readouterr().out) == {"error": "provider_unreachable"}
    assert not fake_omp[1].exists()


@pytest.mark.parametrize("base", ["http://openrouter.ai", "http://[::1]"])
def test_untrusted_provider_base_falls_back_to_openrouter(provider, fake_omp, capsys, monkeypatch, base):
    monkeypatch.setenv("ASTRID_OPENROUTER_API_BASE", base)
    seen = []

    def unavailable(request, **_kwargs):
        seen.append(request.full_url)
        raise OSError("offline")

    monkeypatch.setattr(omp_agent.urllib.request, "urlopen", unavailable)
    assert omp_agent.main(["agent", "key", "set", "openrouter"]) == 4
    assert seen == ["https://openrouter.ai/api/v1/auth/key"]
    assert json.loads(capsys.readouterr().out) == {"error": "provider_unreachable"}


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_import_failure_removes_private_file(provider, fake_omp, tmp_path, capsys, monkeypatch, failure):
    if failure == "exit":
        monkeypatch.setenv("FAKE_IMPORT_EXIT", "7")
    else:
        run = subprocess.run

        def timeout(argv, **kwargs):
            result = run(argv, **kwargs)
            if "import" in argv:
                raise subprocess.TimeoutExpired(argv, 15, output=KEY, stderr=KEY)
            return result

        monkeypatch.setattr(subprocess, "run", timeout)
    assert omp_agent.main(["agent", "key", "set", "openrouter"]) == 1
    response, _records = assert_private_import(fake_omp, tmp_path, capsys)
    assert response == {"error": "acp_bridge_error"}


@pytest.mark.parametrize("key", ["", "short", "x" * 513, "sk-or-test-key with-whitespace"])
def test_malformed_stdin_is_rejected(fake_omp, monkeypatch, capsys, key):
    monkeypatch.setattr(sys, "stdin", io.StringIO(key))
    assert omp_agent.main(["agent", "key", "set", "openrouter"]) == 3
    assert json.loads(capsys.readouterr().out) == {"error": "invalid_key"}
    assert not fake_omp[1].exists()


def test_argv_key_is_rejected_without_echo(fake_omp, capsys):
    assert omp_agent.main(["agent", "key", "set", "openrouter", KEY]) == 2
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err
    assert not fake_omp[1].exists()
