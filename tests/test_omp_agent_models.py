"""Hermetic subprocess contracts for the Astrid OMP provider commands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SNAPSHOT: dict[str, Any] = {
    "schemaVersion": 1,
    "currentModel": "alpha/model/team/name",
    "providers": [
        {
            "id": "alpha",
            "name": "Alpha Provider",
            "signedIn": True,
            "usable": True,
            "reason": None,
            "login": "oauth",
            "models": [
                {"id": "model/team/name", "name": "Team Model", "selector": "alpha/model/team/name", "usable": True},
                {"id": "small", "name": "Small", "selector": "alpha/small", "usable": True},
            ],
        },
        {"id": "closed", "name": "Closed Provider", "signedIn": False, "usable": False,
         "reason": "needs_sign_in", "login": "api-key", "models": []},
        {
            "id": "keyless",
            "name": "Keyless Provider",
            "signedIn": False,
            "usable": True,
            "reason": None,
            "login": "api-key",
            "models": [{"id": "public", "name": "Public Model", "selector": "keyless/public", "usable": True}],
        },
    ],
}

FAKE_OMP = r'''#!{python}
import json
import os
from pathlib import Path
import sys

log = Path(os.environ["FAKE_OMP_LOG"])
args = sys.argv[1:]
record = {{"argv": args, "cwd": os.getcwd()}}
if args[:1] == ["--profile"]:
    record["profile"] = args[1]
    args = args[2:]
if args[:2] == ["--mode", "rpc"]:
    record["input"] = sys.stdin.read()
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\n")

if args[:2] == ["--mode", "rpc"]:
    rpc_mode = os.environ.get("FAKE_OMP_RPC_MODE", "ok")
    if rpc_mode == "fail":
        print(json.dumps({{"id": "astrid-login-providers", "type": "response", "command": "get_login_providers", "success": False, "data": {{}}}}))
    elif rpc_mode == "missing":
        print(json.dumps({{"type": "notification", "message": "no provider response"}}))
    else:
        snapshot = json.loads(os.environ["FAKE_OMP_SNAPSHOT"])
        providers = json.loads(os.environ["FAKE_OMP_LOGIN_PROVIDERS"]) if "FAKE_OMP_LOGIN_PROVIDERS" in os.environ else [
            {{"id": p["id"], "name": p["name"], "authenticated": p["signedIn"]}}
            for p in snapshot["providers"]
        ]
        print(json.dumps({{"id": "astrid-login-providers", "type": "response", "command": "get_login_providers", "success": True, "data": {{"providers": providers}}}}))
    raise SystemExit(int(os.environ.get("FAKE_OMP_RPC_EXIT", "0")))

if args and args[0] == "usage":
    print(json.dumps(json.loads(os.environ.get("FAKE_OMP_USAGE", '{{"reports": []}}'))))
    raise SystemExit(int(os.environ.get("FAKE_OMP_USAGE_EXIT", "0")))

state_path = Path(os.environ["FAKE_OMP_STATE"])
if args[:3] == ["config", "get", "modelRoles"]:
    mode = os.environ.get("FAKE_OMP_CONFIG_GET_MODE", "ok")
    if mode == "fail":
        raise SystemExit(1)
    if mode == "malformed":
        print("not-json")
        raise SystemExit(0)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    roles = state["roles"]
    if not Path.cwd().name.startswith("astrid-agent-config-") and os.environ.get("FAKE_OMP_PROJECT_ROLE"):
        roles = {{"default": os.environ["FAKE_OMP_PROJECT_ROLE"], "project-only": "project-only/model"}}
    print(json.dumps({{"value": roles}}))
    raise SystemExit(0)

if args[:3] == ["config", "set", "modelRoles"]:
    mode = os.environ.get("FAKE_OMP_CONFIG_SET_MODE", "ok")
    requested = json.loads(args[3])
    if mode == "fail":
        raise SystemExit(1)
    if mode == "no-roundtrip":
        state = json.loads(state_path.read_text(encoding="utf-8"))
        print(json.dumps({{"value": state["roles"]}}))
        raise SystemExit(0)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["roles"] = requested
    state_path.write_text(json.dumps(state), encoding="utf-8")
    print(json.dumps({{"value": requested}}))
    raise SystemExit(0)

result_path = os.environ.get("ASTRID_OMP_MODELS_RESULT")
mode = os.environ.get("FAKE_OMP_MODE", "ok")
if mode == "no-result":
    raise SystemExit(0)
if mode == "malformed":
    Path(result_path).write_text("not-json", encoding="utf-8")
    raise SystemExit(0)

snapshot = json.loads(os.environ["FAKE_OMP_SNAPSHOT"])
state = json.loads(state_path.read_text(encoding="utf-8"))
snapshot["currentModel"] = state["roles"].get("default")

Path(result_path).write_text(json.dumps(snapshot), encoding="utf-8")
available = os.environ.get("FAKE_OMP_AVAILABLE_SELECTORS")
selectors = json.loads(available) if available is not None else [
    model["selector"] for provider in snapshot["providers"] for model in provider["models"]
]
print(json.dumps({{"models": [{{"selector": selector}} for selector in selectors]}}))
raise SystemExit(int(os.environ.get("FAKE_OMP_EXIT", "0")))
'''


def _setup(
    tmp_path: Path,
    *,
    snapshot: dict[str, Any] | None = None,
    initial_default: str | None = None,
) -> tuple[dict[str, str], Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    fake_omp = tmp_path / "omp-fake"
    fake_omp.write_text(FAKE_OMP.format(python=sys.executable), encoding="utf-8")
    fake_omp.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    state = tmp_path / "omp-state.json"
    initial_default = initial_default or (snapshot or SNAPSHOT)["currentModel"]
    state.write_text(json.dumps({"roles": {"default": initial_default, "coding": "keep/coding", "title": "keep/title"}, "other": "preserve"}), encoding="utf-8")
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("FAKE_OMP_"):
            env.pop(key)
    for key in (
        "BANODOCO_LOCAL_DATA_ROOT",
        "BANODOCO_LOCAL_SOURCE_MANIFEST",
        "ASTRID_LOCAL_DATA_ROOT",
        "ASTRID_RUNTIME_DATA_ROOT",
        "ASTRID_LOCAL_SOURCE_MANIFEST",
        "ASTRID_AGENT_LAUNCHER",
        "OMP_AGENT_LAUNCHER",
    ):
        env.pop(key, None)
    fake_snapshot = json.loads(json.dumps(snapshot if snapshot is not None else SNAPSHOT))
    fake_snapshot["_catalogModel"] = "alpha/model/team/name"
    fake_snapshot.setdefault("_hiddenProviderIds", [])
    fake_snapshot["_needsSignInProviderIds"] = ["closed"]
    fake_snapshot.setdefault("_oauthProviderIds", ["alpha"])
    env.update(
        {
            "OMP_BIN": str(fake_omp),
            "ASTRID_STOCK_OMP": "1",
            "FAKE_OMP_LOG": str(log),
            "FAKE_OMP_STATE": str(state),
            "FAKE_OMP_SNAPSHOT": json.dumps(fake_snapshot),
            "HOME": str(tmp_path / "home"),
        }
    )
    return env, log, state


def _run(tmp_path: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "from astrid.omp_agent import main; raise SystemExit(main())", *args],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _calls(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_models_json_returns_selector_snapshot_and_uses_only_fake_omp(tmp_path: Path) -> None:
    env, log, _state = _setup(tmp_path)

    result = _run(tmp_path, env, "agent", "models", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == SNAPSHOT
    assert payload["providers"][0]["models"][0]["selector"] == "alpha/model/team/name"
    calls = _calls(log)
    assert calls[0]["argv"][:3] == ["models", "ls", "--json"]
    assert calls[0]["argv"][3] == "-e"
    assert calls[0]["argv"][4].endswith("astrid/omp_extensions/astrid-models.ts")
    assert [call["argv"][:2] for call in calls] == [
        ["models", "ls"], ["--mode", "rpc"], ["usage", "--json"]
    ]
    assert "--model" in calls[1]["argv"]
    assert calls[1]["argv"][calls[1]["argv"].index("--model") + 1] == "alpha/model/team/name"
    assert calls[1]["input"] == '{"id":"astrid-login-providers","type":"get_login_providers"}\n'
    assert "_catalogModel" not in result.stdout and "_hiddenProviderIds" not in result.stdout
    assert "_needsSignInProviderIds" not in result.stdout
    assert "_oauthProviderIds" not in result.stdout


def test_models_only_advertises_omp_available_selectors_and_read_does_not_save(tmp_path: Path) -> None:
    snapshot = json.loads(json.dumps(SNAPSHOT))
    snapshot["providers"].extend([
        {"id": "amazon-bedrock", "name": "Amazon Bedrock", "signedIn": False,
         "models": [{"id": "claude", "name": "Claude", "selector": "amazon-bedrock/claude"}]},
        {"id": "cursor", "name": "Cursor", "signedIn": False,
         "models": [{"id": "composer", "name": "Composer", "selector": "cursor/composer"}]},
        {"id": "ollama", "name": "Ollama", "signedIn": False, "models": []},
    ])
    env, log, state_path = _setup(tmp_path, snapshot=snapshot)
    env["FAKE_OMP_AVAILABLE_SELECTORS"] = json.dumps(["alpha/small", "keyless/public"])
    fake = json.loads(env["FAKE_OMP_SNAPSHOT"])
    fake["_needsSignInProviderIds"] = ["closed", "amazon-bedrock", "cursor"]
    env["FAKE_OMP_SNAPSHOT"] = json.dumps(fake)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["roles"].pop("default")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = state_path.read_text(encoding="utf-8")

    listed = _run(tmp_path, env, "agent", "models", "--json")

    assert listed.returncode == 0, listed.stderr
    payload = json.loads(listed.stdout)
    assert payload["currentModel"] is None
    providers = {provider["id"]: provider for provider in payload["providers"]}
    assert [model["selector"] for model in providers["alpha"]["models"]] == ["alpha/small"]
    assert providers["keyless"]["models"][0]["usable"] is True
    for name in ("amazon-bedrock", "cursor"):
        assert providers[name]["signedIn"] is False
        assert providers[name]["usable"] is False
        assert providers[name]["reason"] == "needs_sign_in"
        assert providers[name]["models"] == []
    assert providers["ollama"]["reason"] == "no_available_models"
    assert providers["ollama"]["login"] == "local"
    assert state_path.read_text(encoding="utf-8") == before
    assert not any(call["argv"][:2] == ["config", "set"] for call in _calls(log))

    refused = _run(tmp_path, env, "agent", "use", "amazon-bedrock/claude")
    assert refused.returncode != 0
    assert state_path.read_text(encoding="utf-8") == before
    assert not any(call["argv"][:2] == ["config", "set"] for call in _calls(log))


def test_models_and_use_pass_installed_service_profile_to_fake_omp(tmp_path: Path) -> None:
    env, log, _state = _setup(tmp_path)
    home = Path(env["HOME"])
    data_home = Path(env.get("XDG_DATA_HOME", home / ".local" / "share"))
    selection = data_home / "astrid-install.json"
    selection.parent.mkdir(parents=True, exist_ok=True)
    selection.write_text(json.dumps({"chat_settings": {"ASTRID_ACP_PROFILE": "app-profile"}}), encoding="utf-8")

    listed = _run(tmp_path, env, "agent", "models", "--json")
    used = _run(tmp_path, env, "agent", "use", "alpha/small")
    explicit = _run(tmp_path, env, "agent", "models", "--profile", "user-profile", "--json")

    assert listed.returncode == used.returncode == explicit.returncode == 0
    calls = _calls(log)
    assert calls
    assert all(call.get("profile") == "app-profile" for call in calls[:-3])
    assert all(call.get("profile") == "user-profile" for call in calls[-3:])


def test_login_rpc_cannot_downgrade_configured_auth_to_keyless(tmp_path: Path) -> None:
    env, _log, _state = _setup(tmp_path)
    env["FAKE_OMP_LOGIN_PROVIDERS"] = json.dumps([
        {"id": "alpha", "name": "Alpha Provider", "authenticated": False},
    ])

    result = _run(tmp_path, env, "agent", "models", "--json")

    assert result.returncode == 0, result.stderr
    alpha = next(provider for provider in json.loads(result.stdout)["providers"] if provider["id"] == "alpha")
    assert alpha["signedIn"] is True
    assert alpha["usable"] is True


def test_models_human_output_includes_provider_state_and_provider_only_hint(tmp_path: Path) -> None:
    env, log, _state = _setup(tmp_path)

    result = _run(tmp_path, env, "agent", "models")

    assert result.returncode == 0, result.stderr
    assert "Astrid default model: alpha/model/team/name" in result.stdout
    assert "Alpha Provider (alpha) — signed in" in result.stdout
    assert "alpha/model/team/name  Team Model" in result.stdout
    assert "Keyless Provider (keyless) — not signed in" in result.stdout
    assert "keyless/public  Public Model" in result.stdout
    assert "Closed Provider (closed) — not signed in" in result.stdout
    assert "Sign in: run `omp`, then /login and pick Closed Provider" in result.stdout
    assert _calls(log)[1]["argv"][:2] == ["--mode", "rpc"]


def test_login_only_provider_is_merged_and_hidden_provider_is_excluded(tmp_path: Path) -> None:
    snapshot = json.loads(json.dumps(SNAPSHOT))
    snapshot["_hiddenProviderIds"] = ["secret-hidden"]
    env, _log, _state = _setup(tmp_path, snapshot=snapshot)
    env["FAKE_OMP_LOGIN_PROVIDERS"] = json.dumps(
        [
            {"id": "alpha", "name": "Alpha Provider", "authenticated": True},
            {"id": "plugin-only", "name": "Custom Login Plugin", "authenticated": False},
            {"id": "secret-hidden", "name": "Must Stay Hidden", "authenticated": True},
        ]
    )

    result = _run(tmp_path, env, "agent", "models", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert [provider["id"] for provider in payload["providers"]] == ["alpha", "closed", "keyless", "plugin-only"]
    plugin = payload["providers"][-1]
    assert plugin == {"id": "plugin-only", "name": "Custom Login Plugin", "signedIn": False,
                      "usable": False, "reason": "needs_sign_in", "login": "api-key", "models": []}
    assert "secret-hidden" not in result.stdout and "Must Stay Hidden" not in result.stdout


def test_login_only_signed_out_provider_gets_named_sign_in_hint(tmp_path: Path) -> None:
    env, _log, state_path = _setup(tmp_path)
    env["FAKE_OMP_LOGIN_PROVIDERS"] = json.dumps(
        [{"id": "plugin-only", "name": "Custom Login Plugin", "authenticated": False}]
    )
    before = state_path.read_text(encoding="utf-8")

    result = _run(tmp_path, env, "agent", "use", "plugin-only/model")

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.strip() == "astrid: Sign in: run `omp`, then /login and pick Custom Login Plugin"
    assert state_path.read_text(encoding="utf-8") == before


def test_models_json_classifies_known_login_methods_and_only_recommends_openrouter(tmp_path: Path) -> None:
    snapshot = json.loads(json.dumps(SNAPSHOT))
    snapshot["providers"].extend([
        {"id": "openrouter", "name": "OpenRouter", "signedIn": False, "models": []},
        {"id": "mystery", "name": "Mystery", "signedIn": False, "models": []},
        {"id": "llama-cpp", "name": "llama.cpp", "signedIn": False, "models": []},
    ])
    env, _log, _state = _setup(tmp_path, snapshot=snapshot)
    env["FAKE_OMP_LOGIN_PROVIDERS"] = json.dumps([
        {"id": "alpha", "name": "Alpha Provider", "authenticated": True},
        {"id": "closed", "name": "Closed Provider", "authenticated": False},
        {"id": "openrouter", "name": "OpenRouter", "authenticated": False},
    ])

    result = _run(tmp_path, env, "agent", "models", "--json")

    assert result.returncode == 0, result.stderr
    providers = {provider["id"]: provider for provider in json.loads(result.stdout)["providers"]}
    assert providers["alpha"]["login"] == "oauth"
    assert providers["closed"]["login"] == "api-key"
    assert providers["openrouter"]["login"] == "api-key"
    assert providers["openrouter"]["recommended"] is True
    assert providers["llama-cpp"]["login"] == "local"
    assert "login" not in providers["mystery"]
    assert "recommended" not in providers["alpha"]


def test_models_json_does_not_emit_raw_account_or_scope_usage_fields(tmp_path: Path) -> None:
    env, _log, _state = _setup(tmp_path)
    env["FAKE_OMP_USAGE"] = json.dumps(
        {
            "reports": [
                {
                    "provider": "alpha",
                    "fetchedAt": 1792404000000,
                    "account": "ACCOUNT_CANARY",
                    "scope": "SCOPE_CANARY",
                    "raw": "RAW_CANARY",
                    "limits": [{"label": "daily", "amount": {"remaining": 42, "unit": "usd"}, "window": {"id": "day", "label": "Day"}, "status": "ok", "account": "LIMIT_CANARY"}],
                }
            ]
        }
    )

    result = _run(tmp_path, env, "agent", "models", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["providers"][0]["usage"] == [
        {"fetchedAt": 1792404000000, "limits": [{"label": "daily", "amount": {"remaining": 42, "unit": "usd"}, "window": {"id": "day", "label": "Day"}, "status": "ok"}]}
    ]
    for canary in ("ACCOUNT_CANARY", "SCOPE_CANARY", "RAW_CANARY", "LIMIT_CANARY"):
        assert canary not in result.stdout


def test_use_saves_exact_model_selector_and_preserves_other_roles(tmp_path: Path) -> None:
    env, log, state_path = _setup(tmp_path, initial_default="alpha/small")
    env["FAKE_OMP_PROJECT_ROLE"] = "project-only/model"

    result = _run(tmp_path, env, "agent", "use", "alpha/model/team/name", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["currentModel"] == "alpha/model/team/name"
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "roles": {
            "default": "alpha/model/team/name",
            "coding": "keep/coding",
            "title": "keep/title",
        },
        "other": "preserve",
    }
    calls = _calls(log)
    assert len(calls) == 4
    assert calls[0]["argv"][:3] == ["models", "ls", "--json"]
    assert calls[1]["argv"][:2] == ["--mode", "rpc"]
    assert calls[2]["argv"] == ["config", "get", "modelRoles", "--json"]
    assert calls[3]["argv"][:3] == ["config", "set", "modelRoles"]
    assert json.loads(calls[3]["argv"][3]) == {
        "default": "alpha/model/team/name",
        "coding": "keep/coding",
        "title": "keep/title",
    }
    assert calls[3]["argv"][4] == "--json"
    for call in calls[2:]:
        assert Path(call["cwd"]).name.startswith("astrid-agent-config-")
    assert calls[2]["cwd"] == calls[3]["cwd"]


def test_use_accepts_signed_out_provider_model(tmp_path: Path) -> None:
    env, log, state_path = _setup(tmp_path)

    result = _run(tmp_path, env, "agent", "use", "keyless/public", "--json")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["currentModel"] == "keyless/public"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["roles"]["default"] == "keyless/public"
    assert len(_calls(log)) == 4


def test_models_displays_persisted_default_even_when_provider_is_signed_out(tmp_path: Path) -> None:
    env, _log, _state = _setup(tmp_path, initial_default="closed/retired-model")

    result = _run(tmp_path, env, "agent", "models", "--json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["currentModel"] == "closed/retired-model"
    closed = next(provider for provider in payload["providers"] if provider["id"] == "closed")
    assert closed["signedIn"] is False
    assert closed["models"] == []


def test_use_persists_default_for_a_later_models_subprocess(tmp_path: Path) -> None:
    env, _log, _state = _setup(tmp_path)

    selected = _run(tmp_path, env, "agent", "use", "alpha/small", "--json")
    listed = _run(tmp_path, env, "agent", "models", "--json")

    assert selected.returncode == 0, selected.stderr
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout)["currentModel"] == "alpha/small"


def test_provider_only_signed_out_selection_prints_one_line_sign_in_hint_without_mutation(tmp_path: Path) -> None:
    for selector in ("closed", "closed/model"):
        env, _log, state_path = _setup(tmp_path / selector.replace("/", "-"))
        before = state_path.read_text(encoding="utf-8")

        result = _run(tmp_path, env, "agent", "use", selector)

        assert result.returncode != 0
        assert result.stdout == ""
        assert result.stderr.count("\n") == 1
        assert "Sign in: run `omp`, then /login and pick Closed Provider" in result.stderr
        assert state_path.read_text(encoding="utf-8") == before


def test_invalid_provider_or_model_fails_without_state_mutation(tmp_path: Path) -> None:
    env, log, state_path = _setup(tmp_path)
    before = state_path.read_text(encoding="utf-8")

    for selector in ("unknown/model", "alpha/not-enabled", ""):
        result = _run(tmp_path, env, "agent", "use", selector)
        assert result.returncode != 0
        assert result.stdout == ""
        assert result.stderr.strip()
        assert state_path.read_text(encoding="utf-8") == before
    assert len(_calls(log)) == 6


def test_models_reports_missing_or_invalid_explicit_omp_path_without_starting_launcher(tmp_path: Path) -> None:
    env, log, _state = _setup(tmp_path)
    env.pop("OMP_BIN")
    env["PATH"] = str(tmp_path / "empty-path")

    missing = _run(tmp_path, env, "agent", "models", "--json")
    assert missing.returncode != 0
    assert "could not locate the OMP binary" in missing.stderr
    assert _calls(log) == []

    env["OMP_BIN"] = str(tmp_path / "does-not-exist")
    invalid = _run(tmp_path, env, "agent", "models", "--json")
    assert invalid.returncode != 0
    assert "does-not-exist" in invalid.stderr
    assert _calls(log) == []


def test_help_describes_model_commands_without_invoking_omp(tmp_path: Path) -> None:
    env, log, _state = _setup(tmp_path)

    result = _run(tmp_path, env, "--help")

    assert result.returncode == 0
    assert "astrid agent models [--json]" in result.stdout
    assert "astrid agent use PROVIDER/MODEL" in result.stdout
    assert _calls(log) == []
    for command in ("models", "use"):
        result = _run(tmp_path, env, "agent", command, "--help")
        assert result.returncode == 0
        assert "schemaVersion" in result.stdout
        assert "currentModel" in result.stdout
        assert "signedIn" in result.stdout
        assert _calls(log) == []


def test_profile_models_section_has_one_command_signin_guidance_and_exact_fallback() -> None:
    profile = Path(__file__).parents[1] / "astrid" / "agent_profiles" / "astrid.md"
    text = profile.read_text(encoding="utf-8")
    section = text.split("## Models and providers\n", 1)[1].split("\n## ", 1)[0]

    assert "astrid --onboard" in section
    assert "omp`, then `/login" in section
    assert "that exact provider name" in section
    assert "astrid agent models --json" in section
    assert "astrid agent use PROVIDER/MODEL" in section


def test_native_config_failures_do_not_report_success_or_mutate_default(tmp_path: Path) -> None:
    for get_mode, set_mode in (("fail", "ok"), ("malformed", "ok"), ("ok", "fail"), ("ok", "no-roundtrip")):
        env, log, state_path = _setup(tmp_path / f"{get_mode}-{set_mode}")
        env["FAKE_OMP_CONFIG_GET_MODE"] = get_mode
        env["FAKE_OMP_CONFIG_SET_MODE"] = set_mode
        before = state_path.read_text(encoding="utf-8")

        result = _run(tmp_path, env, "agent", "use", "alpha/small", "--json")

        assert result.returncode != 0
        assert result.stdout == ""
        assert result.stderr.strip()
        assert state_path.read_text(encoding="utf-8") == before
        calls = _calls(log)
        if get_mode != "ok":
            assert [call["argv"][:3] for call in calls[-1:]] == [["config", "get", "modelRoles"]]
        else:
            assert calls[-1]["argv"][:3] == ["config", "set", "modelRoles"]


def test_usage_failure_or_unavailable_usage_is_optional(tmp_path: Path) -> None:
    env, _log, _state = _setup(tmp_path)
    env["FAKE_OMP_USAGE"] = json.dumps({"reports": [{"provider": "alpha", "account": "NO_USAGE_CANARY"}]})
    env["FAKE_OMP_USAGE_EXIT"] = "1"

    failed = _run(tmp_path, env, "agent", "models", "--json")
    assert failed.returncode == 0
    assert "usage" not in json.loads(failed.stdout)["providers"][0]
    assert "NO_USAGE_CANARY" not in failed.stdout

    env["FAKE_OMP_USAGE"] = "not-json"
    malformed = _run(tmp_path, env, "agent", "models", "--json")
    assert malformed.returncode == 0
    assert "usage" not in json.loads(malformed.stdout)["providers"][0]


def test_usage_timeout_keeps_model_discovery_available(monkeypatch) -> None:
    from astrid import omp_agent

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 3
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    snapshot = {"providers": [{"id": "openrouter", "signedIn": True, "models": []}]}
    omp_agent._add_model_usage("omp", snapshot)
    assert "usage" not in snapshot["providers"][0]


def test_usage_skips_accounts_that_are_not_signed_in(monkeypatch) -> None:
    from astrid import omp_agent

    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    omp_agent._add_model_usage("omp", {"providers": [{"id": "openrouter", "signedIn": False}]})
    assert calls == []


def test_missing_or_malformed_snapshot_and_nonzero_omp_are_errors(tmp_path: Path) -> None:
    for mode, exit_code in (("no-result", "0"), ("malformed", "0"), ("ok", "2")):
        env, _log, _state = _setup(tmp_path / mode)
        env["FAKE_OMP_MODE"] = mode
        env["FAKE_OMP_EXIT"] = exit_code
        result = _run(tmp_path, env, "agent", "models", "--json")
        assert result.returncode != 0
        assert result.stdout == ""
        assert result.stderr.strip()


def test_login_provider_rpc_failure_or_missing_response_fails_without_mutation(tmp_path: Path) -> None:
    for mode in ("fail", "missing"):
        env, log, state_path = _setup(tmp_path / mode)
        env["FAKE_OMP_RPC_MODE"] = mode
        before = state_path.read_text(encoding="utf-8")

        models = _run(tmp_path, env, "agent", "models", "--json")
        assert models.returncode != 0
        assert models.stdout == ""
        assert "sign-in providers" in models.stderr
        assert len(_calls(log)) == 2
        assert state_path.read_text(encoding="utf-8") == before

        selection = _run(tmp_path, env, "agent", "use", "alpha/model/team/name")
        assert selection.returncode != 0
        assert selection.stdout == ""
        assert "sign-in providers" in selection.stderr
        assert len(_calls(log)) == 4
        assert state_path.read_text(encoding="utf-8") == before
