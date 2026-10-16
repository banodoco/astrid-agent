"""Launch the named Astrid agent through oh-my-pi (OMP).

The installed ``astrid`` command is the conversational surface.  The product
gateway remains available as ``astrid-tools`` and through ``python -m astrid``.
Keeping this wrapper small and stdlib-only means ``astrid --help`` remains
usable even when the optional Astrid runtime dependencies are not installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core.execution.generic_host import _ConfinementPolicy

from .core.auth import AUTH_COMMANDS, contributor_auth_system_notice, run_auth
from .version import ASTRID_VERSION

DEFAULT_AGENT = "astrid"
_AGENT_PROFILE_FILE = Path(__file__).resolve().parent / "agent_profiles" / "astrid.md"

# ``astrid`` is the single user-facing command. Product families are stable
# core routes. Pack routes are discovered from installed manifests at dispatch
# time; a pack does not need a second launcher allowlist entry. Reserved
# conversational/auth words stay blocked so they cannot be claimed by a pack.
_PRODUCT_TOOLKIT_FAMILIES = frozenset(
    {"projects", "timelines", "media", "tasks", "runs", "documents", "preferences", "doctor", "backup"}
)
_PACK_ROUTE_BLOCKLIST = frozenset(
    {"agent", "auth", "help", "login", "status", "logout", "revoke"}
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
    except Exception:  # pragma: no cover - minimal launcher must still work
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
  astrid <toolkit> ...           product toolkit gateway
  astrid status [--json]         workspace and Runtime status
  astrid dev ...                 developer operations
  astrid auth login|status|logout|revoke  Hivemind contributor login lifecycle
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


def _omp_supports_onboard(omp_bin: str, *, confinement_policy=None) -> bool:
    """Return whether this OMP build has the native onboarding command."""
    try:
        from .core.execution.generic_host import _confined_launch_plan

        plan = _confined_launch_plan(
            [omp_bin, "onboard", "--help"], policy=confinement_policy,
            parent_environment=os.environ,
        )
        proc = subprocess.run(
            plan.argv, **plan.subprocess_options(),
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


def _prompt_args(flags: list[str], *, agent: str = DEFAULT_AGENT, env=None) -> list[str]:
    """Return only an explicit Astrid prompt override.

    This contains only the optional appended notice/override; the shipped
    profile body is selected separately as OMP's base system prompt when no
    project or user named profile exists. Alternate agents never inherit it.
    """
    if "--system-prompt" in flags:
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


def _run_confined_omp(argv: list[str], *, confinement_policy=None) -> int:
    """The sole OMP workload seam; no exec/PATH or unrestricted fallback.

    Authority is injected in-process by the trusted host, never by CLI flags or
    environment. Apply every plan option, including stdio and descriptor closure.
    """
    from .core.execution.generic_host import _confined_launch_plan

    plan = _confined_launch_plan(
        argv, policy=confinement_policy, parent_environment=os.environ,
    )
    completed = subprocess.run(plan.argv, **plan.subprocess_options(), text=True, check=False)
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return completed.returncode


def _exec_omp_with_packaged_profile(
    omp_bin: str, flags: list[str], message: list[str], *, confinement_policy=None,
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
        # Caller prompt-file/environment overrides cannot make the trusted
        # parent read outside the selected grant before the sandbox starts.
        exec_argv += _prompt_args(flags, env={})
    if message:
        exec_argv += ["--print", *message]
        _print_one_shot_header(DEFAULT_AGENT)
    return _run_confined_omp(exec_argv, confinement_policy=confinement_policy)


def main(argv: list[str] | None = None, *, confinement_policy: _ConfinementPolicy | None = None) -> int:
    rest = list(sys.argv[1:] if argv is None else argv)

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
    if rest and rest[0] == "help":
        return _dispatch_toolkit(rest)
    if rest and rest[0] in {"status", "dev"}:
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
        if rest:
            print("astrid: --onboard takes no other arguments", file=sys.stderr)
            return 1
        omp_bin = _resolve_omp_bin()
        if omp_bin is None or not _omp_supports_onboard(omp_bin, confinement_policy=confinement_policy):
            print("astrid: this OMP build does not support native onboarding", file=sys.stderr)
            return 1
        return _run_confined_omp([omp_bin, "onboard"], confinement_policy=confinement_policy)

    flags, message = _split_flags(rest)
    if not message and _HELP_FLAGS.intersection(flags):
        print(_USAGE, end="")
        return 0
    if any(token.startswith("-") for token in message):
        print("astrid: flags must precede the message, e.g. astrid -c \"follow-up\"", file=sys.stderr)
        return 1

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
        return _exec_omp_with_packaged_profile(omp_bin, flags, message, confinement_policy=confinement_policy)

    launcher = _find_launcher()
    if launcher is None:
        print(_missing_launcher_message(), file=sys.stderr)
        return 1

    exec_argv = [str(launcher), "run", agent, *flags]
    if not any(flag in flags for flag in ("--system-prompt", "--append-system-prompt")):
        exec_argv += _prompt_args(flags, agent=agent, env={})
    if message:
        exec_argv += ["--print", *message]
        _print_one_shot_header(agent)
    return _run_confined_omp(exec_argv, confinement_policy=confinement_policy)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
