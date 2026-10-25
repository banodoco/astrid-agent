"""Launch the named Astrid agent through oh-my-pi (OMP).

The installed ``astrid`` command is the conversational surface.  The product
gateway remains available as ``astrid-tools`` and through ``python -m astrid``.
Keeping this wrapper small and stdlib-only means ``astrid --help`` remains
usable even when the optional Astrid runtime dependencies are not installed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .core.auth import AUTH_COMMANDS, contributor_auth_system_notice, run_auth
from .core.util.process import replace_process
from .version import ASTRID_VERSION

DEFAULT_AGENT = "astrid"
ASTRID_OPENROUTER_DEFAULT_MODEL: str | None = None
_AGENT_PROFILE_FILE = Path(__file__).resolve().parent / "agent_profiles" / "astrid.md"

# ``astrid`` is the single user-facing command. Product families are stable
# core routes. Pack routes are discovered from installed manifests at dispatch
# time; a pack does not need a second launcher allowlist entry. Reserved
# conversational/auth words stay blocked so they cannot be claimed by a pack.
_PRODUCT_TOOLKIT_FAMILIES = frozenset(
    {"projects", "timelines", "media", "tasks", "runs", "documents", "preferences", "doctor", "backup", "skills"}
)
_PACK_ROUTE_BLOCKLIST = frozenset(
    {"agent", "auth", "help", "install", "login", "setup", "status", "logout", "revoke"}
)


def _installed_pack_families() -> frozenset[str]:
    """Return visible installed/discovered pack ids without starting runtime."""
    try:
        from astrid.core.pack.discovery import discover_pack_metadata

        return frozenset(
            pack.id
            for pack in discover_pack_metadata()
            if pack.id not in _PACK_ROUTE_BLOCKLIST
        )
    except (ImportError, OSError, ValueError, RuntimeError):  # pragma: no cover - minimal install
        return frozenset()


def _toolkit_families() -> frozenset[str]:
    return _PRODUCT_TOOLKIT_FAMILIES | _installed_pack_families()


# Kept as a compatibility view for callers/tests that import the old name.
_TOOLKIT_FAMILIES = _PRODUCT_TOOLKIT_FAMILIES
_AUTH_ALIASES = frozenset(AUTH_COMMANDS)

_LAUNCHER_CANDIDATES: tuple[Path, ...] = (Path.home() / ".bun" / "bin" / "agent",)
_PATH_FALLBACK_BLOCKLIST: tuple[str, ...] = (".grok",)
_BRANDED_OMP = (
    Path.home() / "Documents" / "oh-my-pi" / "packages" / "coding-agent" / "dist" / "omp"
)
_LAUNCHER_PACKAGE = "@oh-my-pi/pi-coding-agent"

_HELP_FLAGS = frozenset({"-h", "--help"})
_VERSION_FLAGS = frozenset({"-v", "--version"})
_VALUE_FLAGS = frozenset(
    {
        "-r",
        "--resume",
        "--session-dir",
        "--profile",
        "--system-prompt",
        "--append-system-prompt",
        "--extension",
        "-e",
    }
)
_ONBOARD_HELP_MARKER = "detect-first provider onboarding"

def _read_agent_profile_body() -> str:
    """Read the installed named profile while keeping its OMP metadata out of the prompt."""
    text = _AGENT_PROFILE_FILE.read_text(encoding="utf-8").replace(
        "{{ASTRID_PACKAGE_ROOT}}", str(Path(__file__).resolve().parent),
    )
    if not text.startswith("---\n"):
        return text
    _, separator, body = text[4:].partition("\n---\n")
    if not separator:
        raise ValueError(f"Astrid agent profile has an unterminated frontmatter block: {_AGENT_PROFILE_FILE}")
    return body


try:
    # The body is also used as OMP's supported --system-prompt value when the
    # agent shell helper cannot see an Astrid profile without writing one into
    # the user's global OMP directory.
    ASTRID_SYSTEM_PROMPT = _read_agent_profile_body()
except (OSError, UnicodeError, ValueError):  # keep help/version usable in a damaged install
    ASTRID_SYSTEM_PROMPT = ""


_USAGE = """\
usage: astrid [flags] [message...]

Run Astrid. Bare = interactive OMP agent; a message = one-shot agent prompt.
Toolkit families dispatch to the Astrid product gateway. Use ``astrid agent``
when you want to make the agent route explicit.

  astrid                         interactive Astrid agent
  astrid agent [flags] [message] interactive/one-shot Astrid agent
  astrid agent models [--json] [--profile NAME] list OMP providers, models, and current default
  astrid agent use PROVIDER/MODEL [--profile NAME] set the default for the next Astrid launch
  astrid agent key set openrouter save an OpenRouter key read from stdin
  astrid <toolkit> ...           product toolkit gateway
  astrid login                   log in for Hivemind contributions
  astrid status                  show Hivemind contributor login status
  astrid logout                  remove the local Hivemind login
  astrid revoke                  revoke the Hivemind contributor login
  astrid help                    product toolkit help

  --agent NAME     talk to a different installed agent (default: astrid)
  --onboard        run OMP provider setup, then exit
  -v, --version    show the Astrid product version
  -c               continue the most recent conversation
  --resume ID      resume a specific session
  -h, --help       show this help

Launcher: ASTRID_AGENT_LAUNCHER env var, else ~/.bun/bin/agent.
OMP binary: OMP_BIN env var, else the branded Astrid-compatible build, then PATH.
"""


def _print_version(stream=None) -> None:
    """Print Astrid's product version without booting the OMP runtime."""
    stream = sys.stdout if stream is None else stream
    print(f"astrid/{ASTRID_VERSION}", file=stream)


def _print_agent_profile_file(stream=None) -> int:
    """Expose the shipped profile path to same-machine hosts without materializing it."""
    stream = sys.stdout if stream is None else stream
    if not _AGENT_PROFILE_FILE.is_file():
        print(f"astrid: installed agent profile is unavailable: {_AGENT_PROFILE_FILE}", file=sys.stderr)
        return 1
    print(_AGENT_PROFILE_FILE, file=stream)
    return 0


def _print_unified_help(stream=None) -> None:
    """Print the top-level command contract without starting OMP or a runtime."""
    stream = sys.stdout if stream is None else stream
    print(_USAGE, end="", file=stream)
    print("Toolkit families: " + " ".join(sorted(_toolkit_families())), file=stream)
    print(
        "\nExamples:\n"
        "  astrid projects list --json\n"
        "  astrid timelines list --project PROJECT\n"
        "  astrid agent --resume SESSION\n"
        "  astrid \"draft a cinematic shot list\"\n",
        file=stream,
    )


def _dispatch_toolkit(rest: list[str]) -> int:
    """Delegate a recognized product family to the canonical gateway."""
    # Delayed import keeps the interactive launcher usable in a minimal OMP
    # installation and makes ``astrid --help`` independent of runtime deps.
    from astrid.core.gateway import main as gateway_main

    return int(gateway_main(rest))


def _find_launcher(env=None) -> Path | None:
    """Find OMP's named-agent launcher, avoiding unrelated vendor binaries."""
    target = os.environ if env is None else env
    for variable in ("ASTRID_AGENT_LAUNCHER", "OMP_AGENT_LAUNCHER"):
        override = target.get(variable)
        if override:
            candidate = Path(override).expanduser()
            return candidate if candidate.is_file() else None
    for candidate in _LAUNCHER_CANDIDATES:
        if candidate.is_file():
            return candidate
    on_path = shutil.which("agent")
    if on_path and not any(marker in on_path for marker in _PATH_FALLBACK_BLOCKLIST):
        return Path(on_path)
    return None


def _missing_launcher_message() -> str:
    """Explain the missing named-agent launcher without suggesting a substitute."""

    message = (
        "astrid: could not locate the `agent` launcher that starts the named Astrid agent. "
        f"Astrid expects it from the oh-my-pi package {_LAUNCHER_PACKAGE}, and the stock install "
        "ships only `omp`, which cannot replace it. Put `agent` on PATH or in ~/.bun/bin, or set "
        "ASTRID_AGENT_LAUNCHER to the absolute path of that script."
    )
    stock = shutil.which("omp")
    if stock:
        message += f" Found `omp` at {stock}; it is not used because it has no `agent run` command."
    return message


def _resolve_omp_bin(env=None) -> str | None:
    """Resolve the OMP binary; an explicit ``OMP_BIN`` always wins."""
    target = os.environ if env is None else env
    override = target.get("OMP_BIN")
    if override:
        return override
    if target.get("ASTRID_STOCK_OMP") != "1" and _BRANDED_OMP.is_file():
        return str(_BRANDED_OMP)
    return shutil.which("omp")


def _select_omp_bin(env=None) -> None:
    """Expose the selected OMP binary to the ``agent`` launcher."""
    target = os.environ if env is None else env
    resolved = _resolve_omp_bin(target)
    if resolved and target.get("ASTRID_STOCK_OMP") != "1":
        target.setdefault("OMP_BIN", resolved)


def _model_command_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="astrid agent")
    commands = parser.add_subparsers(dest="command", required=True)
    contract = (
        "JSON v1: {schemaVersion: 1, currentModel: provider/model or null, providers: "
        "[{id, name, signedIn, usable, reason?, models: [{id, name, selector, usable}], usage?: "
        "[{fetchedAt, limits: [{label, amount, window?, status?}]}]}]}. "
        "currentModel shows the saved OMP profile default, even when unavailable, "
        "or OMP's automatic selection when no default is saved. "
        "Providers and models come from OMP's /model and /login selectors. "
        "usable follows OMP's own available-model listing and enabled-model scope; "
        "reason is needs_sign_in or no_available_models when false. "
        "signedIn means OMP has auth configured; keyless providers can also have usable models. "
        "Usage is optional and excludes account identifiers. No credentials are returned."
    )
    models = commands.add_parser("models", help="list OMP providers and usable models", description=contract)
    models.add_argument("--profile", help="OMP profile (defaults to Astrid's service profile)")
    models.add_argument("--json", action="store_true", help="print the stable JSON v1 contract above")
    use = commands.add_parser(
        "use", help="save OMP's default model for future App and terminal chats",
        description="Validate against `astrid agent models` and persist modelRoles.default via "
        "`omp config set`, the same setting as OMP's /model. The default is shared by App and "
        "terminal chats using this OMP profile. Explicit project defaults take precedence; "
        "existing conversations may retain their session model. " + contract,
    )
    use.add_argument("selector", metavar="PROVIDER/MODEL", help="exact model selector from `astrid agent models`")
    use.add_argument("--profile", help="OMP profile (defaults to Astrid's service profile)")
    use.add_argument("--json", action="store_true", help="print the updated JSON v1 snapshot")
    return parser


def _omp_model_snapshot(omp_bin: str, profile: str | None = None) -> dict[str, Any]:
    """Query OMP's registry inside OMP, without opening its credential files."""
    extension = Path(__file__).resolve().parent / "omp_extensions" / "astrid-models.ts"
    with tempfile.TemporaryDirectory(prefix="astrid-agent-models-") as directory:
        result_file = Path(directory) / "models.json"
        env = dict(os.environ)
        env["ASTRID_OMP_MODELS_RESULT"] = str(result_file)
        command = [omp_bin, "models", "ls", "--json", "-e", str(extension)]
        if profile:
            command[1:1] = ["--profile", profile]
        result = subprocess.run(
            command,
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, check=False,
        )
        if not result_file.is_file():
            raise ValueError("OMP could not report its providers; run `omp models ls --json` for details")
        snapshot = json.loads(result_file.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict):
        raise ValueError("OMP returned an invalid model snapshot")
    if isinstance(snapshot.get("error"), str):
        raise ValueError(snapshot["error"])
    if result.returncode:
        raise ValueError("OMP model command failed; run `omp models ls --json` for details")
    if snapshot.get("schemaVersion") != 1 or not isinstance(snapshot.get("providers"), list):
        raise ValueError("OMP returned an invalid model snapshot")
    # OMP's models CLI renders registry.getAvailable(), whereas the extension
    # also reads its full catalog to enumerate providers. The CLI listing is
    # the authority for what this OMP invocation can currently select.
    listing = json.loads(result.stdout)
    if not isinstance(listing, dict) or not isinstance(listing.get("models"), list):
        raise ValueError("OMP returned an invalid available-model listing")
    available = {model["selector"] for model in listing["models"] if isinstance(model, dict)
                 and isinstance(model.get("selector"), str)}
    needs_sign_in = set(snapshot.pop("_needsSignInProviderIds", []))
    for provider in snapshot["providers"]:
        provider["models"] = [model for model in provider["models"] if model["selector"] in available]
        for model in provider["models"]:
            model["usable"] = True
        provider["usable"] = bool(provider["models"])
        if not provider["usable"]:
            provider["reason"] = "needs_sign_in" if provider["id"] in needs_sign_in else "no_available_models"
    return snapshot


def _save_default_model(omp_bin: str, selector: str, profile: str | None = None, *, only_if_unset: bool = False) -> None:
    """Persist OMP's global default without copying project roles into it."""
    with tempfile.TemporaryDirectory(prefix="astrid-agent-config-") as directory:
        # OMP's config CLI reads effective settings for its cwd. An empty cwd
        # isolates the profile's global roles from project-specific overrides.
        command = [omp_bin, "config", "get", "modelRoles", "--json"]
        if profile:
            command[1:1] = ["--profile", profile]
        result = subprocess.run(
            command, cwd=directory,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if result.returncode:
            raise ValueError("OMP could not read modelRoles; run `omp config get modelRoles` for details")
        payload = json.loads(result.stdout)
        roles = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(roles, dict):
            raise ValueError("OMP returned invalid modelRoles")
        if only_if_unset and roles.get("default"):
            return
        command = [omp_bin, "config", "set", "modelRoles", json.dumps({**roles, "default": selector}), "--json"]
        if profile:
            command[1:1] = ["--profile", profile]
        result = subprocess.run(
            command,
            cwd=directory, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        payload = json.loads(result.stdout) if not result.returncode else None
        saved = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(saved, dict) or saved.get("default") != selector:
            raise ValueError("OMP could not save modelRoles.default; run `omp config get modelRoles` for details")


def _merge_login_providers(omp_bin: str, snapshot: dict[str, Any], profile: str | None = None) -> None:
    """Ask OMP's host for login entries, including extension-only providers."""
    catalog_model = snapshot.pop("_catalogModel", None)
    hidden = set(snapshot.pop("_hiddenProviderIds", []))
    oauth = set(snapshot.pop("_oauthProviderIds", []))
    local = {"ollama", "lm-studio", "llama-cpp", "vllm"}
    command = [omp_bin, "--mode", "rpc", "--no-session", "--no-tools", "--no-lsp", "--no-pty",
               "--no-skills", "--no-rules"]
    if profile:
        command[1:1] = ["--profile", profile]
    if catalog_model:
        # An explicit real catalog model lets OMP answer metadata requests even
        # before sign-in. No prompt or provider inference request is submitted.
        command += ["--model", catalog_model]
    result = subprocess.run(
        command, input='{"id":"astrid-login-providers","type":"get_login_providers"}\n',
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode:
        raise ValueError("OMP could not list sign-in providers; run `omp` for details")
    for line in result.stdout.splitlines():
        try:
            response = json.loads(line)
        except ValueError:
            continue
        if not isinstance(response, dict) or response.get("id") != "astrid-login-providers":
            continue
        data = response.get("data")
        if not response.get("success") or not isinstance(data, dict) or not isinstance(data.get("providers"), list):
            break
        providers = {provider["id"]: provider for provider in snapshot["providers"]}
        for login in data["providers"]:
            if login["id"] in hidden:
                continue
            provider = providers.setdefault(login["id"], {
                "id": login["id"], "models": [],
                "reason": "no_available_models" if login["authenticated"] else "needs_sign_in",
            })
            provider.update(name=login["name"], signedIn=provider.get("signedIn", False) or login["authenticated"])
            provider["login"] = "oauth" if login["id"] in oauth else "local" if login["id"] in local else "api-key"
            provider["usable"] = bool(provider["models"])
            if provider["usable"]:
                provider.pop("reason", None)
            elif provider["signedIn"] and provider.get("reason") == "needs_sign_in":
                provider["reason"] = "no_available_models"
            else:
                provider.setdefault("reason", "no_available_models")
        snapshot["providers"] = sorted(providers.values(), key=lambda provider: provider["id"])
        for provider in snapshot["providers"]:
            if provider["id"] in oauth:
                provider["login"] = "oauth"
            elif provider["id"] in local:
                provider["login"] = "local"
            if provider["id"] == "openrouter":
                provider["recommended"] = True
            provider["reason"] = (provider.get("reason") or "no_available_models") if not provider["usable"] else None
        return
    raise ValueError("OMP did not return its sign-in providers")


def _add_model_usage(omp_bin: str, snapshot: dict[str, Any], profile: str | None = None) -> None:
    """Attach OMP's optional quota reports, retaining only non-identity fields."""
    if not any(provider.get("signedIn") for provider in snapshot["providers"]):
        return
    try:
        command = [omp_bin, "usage", "--json", "--redact"]
        if profile:
            command[1:1] = ["--profile", profile]
        result = subprocess.run(
            command, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False, timeout=3,
        )
        if result.returncode:
            return
        usage = json.loads(result.stdout)
        reports = usage.get("reports", []) if isinstance(usage, dict) else []
        for provider in snapshot["providers"]:
            safe_reports = []
            for report in reports:
                if not isinstance(report, dict) or report.get("provider") != provider["id"]:
                    continue
                safe_reports.append({
                    "fetchedAt": report.get("fetchedAt"),
                    "limits": [
                        {key: limit[key] for key in ("label", "amount", "window", "status") if key in limit}
                        for limit in report.get("limits", []) if isinstance(limit, dict)
                    ],
                })
            if safe_reports:
                provider["usage"] = safe_reports
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Usage support/availability varies by OMP provider and build. The model
        # registry remains authoritative when no quota report can be obtained.
        return


def _run_model_command(argv: list[str]) -> int:
    try:
        args = _model_command_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    omp_bin = _resolve_omp_bin()
    if omp_bin is None:
        print("astrid: could not locate the OMP binary; set OMP_BIN or install oh-my-pi", file=sys.stderr)
        return 1
    try:
        profile = args.profile or _service_profile()
        snapshot = _omp_model_snapshot(omp_bin, profile)
        _merge_login_providers(omp_bin, snapshot, profile)
        if args.command == "models":
            _add_model_usage(omp_bin, snapshot, profile)
        else:
            provider_id, _, _model_id = args.selector.partition("/")
            provider = next((item for item in snapshot["providers"] if item["id"] == provider_id), None)
            if provider and provider.get("reason") == "needs_sign_in":
                raise ValueError(f"Sign in: run `omp`, then /login and pick {provider['name']}")
            if provider is None or not any(model["selector"] == args.selector for model in provider["models"]):
                raise ValueError("Unknown or unavailable model selector; use `astrid agent models`")
            _save_default_model(omp_bin, args.selector, profile)
            snapshot["currentModel"] = args.selector
    except (OSError, ValueError) as exc:
        print(f"astrid: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(snapshot, sort_keys=True))
    elif args.command == "use":
        print(f"Astrid default model saved: {snapshot['currentModel']}. New App and terminal chats inherit it unless their project sets a default.")
    else:
        print(f"Astrid default model: {snapshot['currentModel'] or '(none available)'}")
        for provider in snapshot["providers"]:
            state = "signed in" if provider["signedIn"] else "not signed in"
            print(f"{provider['name']} ({provider['id']}) — {state}")
            for model in provider["models"]:
                print(f"  {model['selector']}  {model['name']}")
            if provider.get("usage"):
                print("  Usage: " + json.dumps(provider["usage"], sort_keys=True))
            if provider.get("reason") == "needs_sign_in":
                print(f"  Sign in: run `omp`, then /login and pick {provider['name']}")
            elif not provider["usable"]:
                print("  No available models in OMP")
    return 0


def _run_key_command(argv: list[str]) -> int:
    if argv != ["set", "openrouter"]:
        print("usage: astrid agent key set openrouter (key on stdin)", file=sys.stderr)
        return 2
    key = sys.stdin.read(515).rstrip("\r\n")
    if not 20 <= len(key) <= 512 or any(character.isspace() for character in key):
        print(json.dumps({"error": "invalid_key"}))
        return 3
    base = os.environ.get("ASTRID_OPENROUTER_API_BASE", "https://openrouter.ai")
    try:
        parsed_base = urllib.parse.urlsplit(base)
        allowed_base = parsed_base.scheme == "https" and parsed_base.hostname is not None or (
            parsed_base.scheme == "http" and parsed_base.hostname in {"127.0.0.1", "localhost"}
        )
    except ValueError:
        allowed_base = False
    if not allowed_base:
        base = "https://openrouter.ai"
    try:
        request = urllib.request.Request(base.rstrip("/") + "/api/v1/auth/key", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(request, timeout=10):
            pass
    except urllib.error.HTTPError as exc:
        rejected = exc.code in (401, 403)
        exc.close()
        print(json.dumps({"error": "invalid_key" if rejected else "provider_unreachable"}))
        return 3 if rejected else 4
    except (OSError, ValueError):
        print(json.dumps({"error": "provider_unreachable"}))
        return 4
    try:
        omp_bin = _resolve_omp_bin()
        if omp_bin is None:
            raise ValueError("OMP is unavailable")
        profile = _service_profile()
        with tempfile.TemporaryDirectory(prefix="astrid-agent-key-") as directory:
            credential_file = Path(directory) / "credentials.json"
            with open(credential_file, "x", encoding="utf-8", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
                json.dump({"version": 1, "credentials": [{"provider": "openrouter", "credential": {
                    "type": "api_key", "key": key, "source": "login",
                }}]}, stream)
            command = [omp_bin, "auth", "import", str(credential_file)]
            if profile:
                command[1:1] = ["--profile", profile]
            result = subprocess.run(command, capture_output=True, timeout=15, check=False)
            if result.returncode:
                raise ValueError("OMP auth import failed")
        if ASTRID_OPENROUTER_DEFAULT_MODEL is not None:
            _save_default_model(omp_bin, ASTRID_OPENROUTER_DEFAULT_MODEL, profile, only_if_unset=True)
        snapshot = _omp_model_snapshot(omp_bin, profile)
        current_model = snapshot.get("currentModel")
        if current_model is not None and (not isinstance(current_model, str) or key in current_model):
            raise ValueError("Invalid current model")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Neither provider responses nor OMP diagnostics may expose credentials.
        print(json.dumps({"error": "acp_bridge_error"}))
        return 1
    print(json.dumps({"provider": "openrouter", "signedIn": True, "currentModel": current_model}))
    return 0


def _service_profile() -> str | None:
    """Read the chat profile retained by the installer service record."""
    try:
        from .installer.service import install_selection

        record_path = install_selection(Path.home(), sys.platform)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        settings = record.get("chat_settings", {}) if isinstance(record, dict) else {}
        value = settings.get("ASTRID_ACP_PROFILE") if isinstance(settings, dict) else None
        return value if isinstance(value, str) and value else None
    except (OSError, ValueError, ImportError):
        return None


def _omp_supports_onboard(omp_bin: str) -> bool:
    """Return whether this OMP build has the native onboarding command."""
    try:
        proc = subprocess.run(
            [omp_bin, "onboard", "--help"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and _ONBOARD_HELP_MARKER.encode() in proc.stdout + proc.stderr


def _split_flags(rest: list[str]) -> tuple[list[str], list[str]]:
    """Partition leading OMP flags from the message words."""
    flags: list[str] = []
    index = 0
    while index < len(rest) and rest[index].startswith("-"):
        token = rest[index]
        flags.append(token)
        index += 1
        if token in _VALUE_FLAGS and index < len(rest):
            flags.append(rest[index])
            index += 1
    return flags, rest[index:]


def _identity_label(agent: str) -> str:
    return "Astrid Creative Resident" if agent == DEFAULT_AGENT else f"agent · {agent}"


def _one_shot_header(agent: str) -> str:
    return f"astrid · {_identity_label(agent)}"


def _print_one_shot_header(agent: str, stream=None) -> None:
    """Brand a one-shot run without polluting piped stdout."""
    stream = sys.stderr if stream is None else stream
    if not stream.isatty():
        return
    header = _one_shot_header(agent)
    print(f"\x1b]0;{header}\x07", end="", file=stream, flush=True)
    print(f"\x1b[1m{header}\x1b[0m", file=stream, flush=True)


def _prompt_args(
    flags: list[str], *, agent: str = DEFAULT_AGENT, env=None, preferences_context: str = ""
) -> list[str]:
    """Return only an explicit Astrid prompt override.

    This contains only the optional appended notice/override; the shipped
    profile body is selected separately as OMP's base system prompt when no
    project or user named profile exists. Alternate agents never inherit it.
    """
    if any(flag in flags for flag in ("--system-prompt", "--append-system-prompt")):
        return []
    if agent != DEFAULT_AGENT:
        return []
    target = os.environ if env is None else env
    prompt = target.get("ASTRID_SYSTEM_PROMPT")
    prompt_file = target.get("ASTRID_SYSTEM_PROMPT_FILE")
    if prompt_file:
        try:
            prompt = Path(prompt_file).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            # A bad optional override must not prevent the OMP session from
            # starting; fall back to the explicit text override if present.
            prompt = target.get("ASTRID_SYSTEM_PROMPT")
    auth_notice = contributor_auth_system_notice()
    prompt = f"{prompt}\n\n{auth_notice}" if prompt else auth_notice
    if target.get("ASTRID_HOST_EXECUTION_PROFILE") == "installed":
        package_root = Path(__file__).resolve().parent
        prompt += (
            "\n\nUse this selected installation's Astrid skill: "
            f"{package_root / 'packs' / '_core' / 'docs' / 'SKILL.md'}. "
            f"Installed pack guides are under {package_root / 'packs'}/<pack>/docs/SKILL.md. "
            "Prefer these files over ambient user-home skill links; "
            "a missing global skill link is not a reason to search retired checkouts."
        )
    if preferences_context:
        prompt = f"{prompt}\n\n{preferences_context}"
    return ["--append-system-prompt", prompt]


def _has_named_agent_profile(agent: str = DEFAULT_AGENT, *, env=None, cwd=None) -> bool:
    """Check OMP's existing project/user profile locations without unpacking or writing."""
    if agent != DEFAULT_AGENT:
        return True
    target = os.environ if env is None else env
    working_directory = Path.cwd() if cwd is None else Path(cwd)
    home = Path(target.get("HOME") or Path.home()).expanduser()
    return any(
        path.is_file()
        for path in (
            working_directory / ".omp" / "agents" / f"{agent}.md",
            home / ".omp" / "agent" / "agents" / f"{agent}.md",
        )
    )


def _exec_omp_with_packaged_profile(
    omp_bin: str, flags: list[str], message: list[str], *, preferences_context: str = ""
) -> int:
    """Run OMP directly with the packaged profile, avoiding user-profile/cache writes."""
    exec_argv = [omp_bin]
    if "--system-prompt" not in flags:
        if not ASTRID_SYSTEM_PROMPT:
            print("astrid: installed Astrid agent profile is unavailable", file=sys.stderr)
            return 1
        exec_argv += ["--system-prompt", ASTRID_SYSTEM_PROMPT]
    exec_argv += flags
    if not any(flag in flags for flag in ("--system-prompt", "--append-system-prompt")):
        exec_argv += _prompt_args(flags, preferences_context=preferences_context)
    if message:
        exec_argv += ["--print", *message]
        _print_one_shot_header(DEFAULT_AGENT)
    replace_process(exec_argv)
    return 1  # pragma: no cover - hand-off exits or replaces the process


def main(argv: list[str] | None = None) -> int:
    rest = list(sys.argv[1:] if argv is None else argv)

    if rest and rest[0] == "install":
        return _dispatch_toolkit(rest)

    # The console script is now the unified Astrid surface.  ``--help`` is
    # intentionally launcher help (not OMP help), while ``help`` remains the
    # gateway's full product-family help.  Explicit ``agent`` is an alias for
    # the default conversational route and consumes no prompt text itself.
    if rest and rest[0] in _HELP_FLAGS:
        _print_unified_help()
        return 0
    if rest and rest[0] == "--agent-profile-file":
        if len(rest) != 1:
            print("astrid: --agent-profile-file takes no other arguments", file=sys.stderr)
            return 2
        return _print_agent_profile_file()
    if rest and rest[0] == "--agent-context":
        if len(rest) != 1:
            return 0
        try:
            from astrid.agent_context import current_context

            block = current_context()
            if block:
                print(block)
        except Exception:  # noqa: BLE001 - this launcher contract is best effort and always succeeds
            pass
        return 0
    if len(rest) > 1 and rest[0] == "agent" and rest[1] in {"models", "use"}:
        return _run_model_command(rest[1:])
    if len(rest) > 1 and rest[0] == "agent" and rest[1] == "key":
        return _run_key_command(rest[2:])
    if rest and rest[0] == "help":
        return _dispatch_toolkit(rest)
    if rest and rest[0] == "auth":
        return run_auth(rest[1:])
    if rest and rest[0] in _AUTH_ALIASES:
        return run_auth(rest)
    if rest and (
        rest[0] in _PRODUCT_TOOLKIT_FAMILIES
        or rest[0] in _installed_pack_families()
    ):
        return _dispatch_toolkit(rest)
    if rest and rest[0] == "agent":
        rest.pop(0)

    agent = DEFAULT_AGENT
    while "--agent" in rest:
        index = rest.index("--agent")
        rest.pop(index)
        if index >= len(rest):
            print('astrid: --agent requires a value (e.g. astrid --agent scout "hi")', file=sys.stderr)
            return 1
        agent = rest.pop(index)

    # The wrapper owns Astrid's product contract.  Do not expose OMP's
    # compatibility/runtime version here (or spend a provider request).
    if len(rest) == 1 and rest[0] in _VERSION_FLAGS:
        _print_version()
        return 0

    # The command name selects the user-visible OMP identity.  Assignment is
    # intentional: a shell that previously launched Arnold must not leak its
    # identity into a new Astrid process (including the onboarding surface).
    os.environ["OMP_AGENT_IDENTITY"] = agent
    os.environ["ASTRID_AGENT_IDENTITY"] = agent
    # Keep the OMP runtime's compatibility version separate from the version
    # shown in Astrid's interactive banner.  Assignment also prevents a
    # previously launched branded profile from leaking into this process.
    os.environ["OMP_DISPLAY_VERSION"] = ASTRID_VERSION

    if "--onboard" in rest:
        rest.remove("--onboard")
        if rest and rest not in (["--help"], ["-h"]):
            print("astrid: --onboard takes no other arguments", file=sys.stderr)
            return 1
        omp_bin = _resolve_omp_bin()
        if omp_bin is None or not _omp_supports_onboard(omp_bin):
            print("astrid: this OMP build does not support native onboarding", file=sys.stderr)
            return 1
        profile = _service_profile()
        replace_process([omp_bin, *(["--profile", profile] if profile else []), "onboard", *rest])
        return 1  # pragma: no cover - hand-off exits or replaces the process

    flags, message = _split_flags(rest)
    if not any(flag == "--profile" or flag.startswith("--profile=") for flag in flags):
        profile = _service_profile()
        if profile:
            flags += ["--profile", profile]
    if not message and _HELP_FLAGS.intersection(flags):
        print(_USAGE, end="")
        return 0
    if any(token.startswith("-") for token in message):
        print("astrid: flags must precede the message, e.g. astrid -c \"follow-up\"", file=sys.stderr)
        return 1

    preferences_context = ""
    if agent == DEFAULT_AGENT and not any(
        flag in flags for flag in ("--system-prompt", "--append-system-prompt")
    ):
        try:
            from astrid.agent_context import current_context

            preferences_context = current_context()
        except Exception:  # noqa: BLE001 - saved preferences are optional prompt context
            preferences_context = ""

    _select_omp_bin()
    if agent == DEFAULT_AGENT:
        os.environ["ASTRID_OMP_PYTHON"] = sys.executable
        # The named-agent shim only consumes values for its own narrow flag
        # allowlist. Use OMP's equals syntax so it never treats this resource
        # path as a one-shot prompt.
        flags += ["--extension=" + str(Path(__file__).resolve().parent / "omp_extensions" / "astrid-invoke.ts")]
    # Installed app/host execution is bound to this wheel's named profile.
    # A source user's customized named profile keeps its existing launcher
    # route; installed execution must not read or materialize its prompt cache.
    installed_profile = os.environ.get("ASTRID_HOST_EXECUTION_PROFILE") == "installed"
    if agent == DEFAULT_AGENT and (installed_profile or not _has_named_agent_profile(agent)):
        omp_bin = _resolve_omp_bin()
        if omp_bin is None:
            print("astrid: could not locate the OMP binary; set OMP_BIN or install oh-my-pi", file=sys.stderr)
            return 1
        return _exec_omp_with_packaged_profile(
            omp_bin, flags, message, preferences_context=preferences_context
        )

    launcher = _find_launcher()
    if launcher is None:
        print(_missing_launcher_message(), file=sys.stderr)
        return 1

    exec_argv = [str(launcher), "run", agent, *flags]
    if not any(flag in flags for flag in ("--system-prompt", "--append-system-prompt")):
        exec_argv += _prompt_args(flags, agent=agent, preferences_context=preferences_context)
    if message:
        exec_argv += ["--print", *message]
        _print_one_shot_header(agent)
    replace_process(exec_argv)
    return 1  # pragma: no cover - hand-off exits or replaces the process


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
