"""Each onboarding concept has one home; the other onboarding files carry a line and a link.

Checks run over the onboarding files in scope for card P4.12 only.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SETUP_SOURCE = "astrid/packs/setup/docs/SKILL.md"
SETUP_MIRROR = "docs/setup/SKILL.md"
GENERATION = "astrid/packs/generation/docs/SKILL.md"
VIBECOMFY = "astrid/packs/vibecomfy/docs/SKILL.md"
RUNPOD = "astrid/packs/runpod/docs/SKILL.md"
CORE = "astrid/packs/_core/docs/SKILL.md"

IN_SCOPE = (
    "README.md",
    "scripts/render_readme.py",
    "docs/getting-started.md",
    "docs/setup/README.md",
    SETUP_MIRROR,
    SETUP_SOURCE,
    CORE,
    GENERATION,
    VIBECOMFY,
    RUNPOD,
)

# One home per concept: the heading that holds its full section, and the one file that owns it.
CONCEPT_HOMES = {
    "### Find their ComfyUI": VIBECOMFY,
    "### Claim, inspect, keep and release": RUNPOD,
    "## Placement": GENERATION,
    "## Defaults": GENERATION,
    "## Set up my generation": GENERATION,
}

# The RunPod claim and key commands live only in the RunPod skill among the in-scope files.
RUNPOD_COMMANDS = ("machines claim", "credential set runpod")

_FENCE = re.compile(r"```[^\n]*\n(.*?)\n```", re.S)
_LAUNCHER = re.compile(r'run-astrid"? generate (?P<rest>[^`\n]*)')


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    start = text.index(heading + "\n")
    end = text.find("\n## ", start + len(heading))
    return text[start:end if end > 0 else len(text)]


def _headings(text: str) -> set[str]:
    return {line.rstrip() for line in text.splitlines() if line.startswith("#")}


_BOOLEAN_FLAGS = {"--json", "--dry-run", "--no-run", "--replace"}


def _generate_argv_key(argv: list[str]) -> tuple:
    """Subcommand words and flags of a `generate` command, placeholders and values ignored.

    A token is a placeholder when it has an upper-case letter (ENTRY, URL, INPUTS.json),
    is a path (contains `/` or `$`), or a URL (contains `://`). The value after a flag
    that takes one is skipped. What remains is the subcommand, its literal positionals
    (t2i, placement) and its flags.
    """
    words, flags = [], []
    skip_value = False
    for token in argv:
        if token.startswith("#"):
            break
        if skip_value:
            skip_value = False
            continue
        if token.startswith("--"):
            flags.append(token)
            skip_value = token not in _BOOLEAN_FLAGS
        elif any(c.isupper() for c in token) or "/" in token or "$" in token or "://" in token:
            continue
        else:
            words.append(token)
    return tuple(words), frozenset(flags)


def _setup_generate_keys() -> list[tuple]:
    keys = []
    for block in _FENCE.findall(_text(SETUP_MIRROR)):
        for line in block.splitlines():
            match = _LAUNCHER.search(line)
            if match:
                keys.append(_generate_argv_key(shlex.split(match.group("rest"))))
    return keys


def _generation_generate_keys() -> set[tuple]:
    keys = set()
    for match in _LAUNCHER.finditer(_text(GENERATION)):
        keys.add(_generate_argv_key(shlex.split(match.group("rest"))))
    return keys


def test_runpod_machine_commands_live_only_in_the_runpod_skill():
    for relative in IN_SCOPE:
        if relative == RUNPOD:
            continue
        text = _text(relative)
        for command in RUNPOD_COMMANDS:
            assert command not in text, (relative, command)


def test_setup_generate_commands_have_the_same_form_in_the_generation_skill():
    setup_keys = _setup_generate_keys()
    assert setup_keys, "the setup skill has no generate commands"
    generation_keys = _generation_generate_keys()
    missing = [key for key in setup_keys if key not in generation_keys]
    assert missing == [], missing


def test_each_concept_section_has_one_home_and_the_others_only_link_to_it():
    for heading, home in CONCEPT_HOMES.items():
        owners = [relative for relative in IN_SCOPE if heading in _headings(_text(relative))]
        assert owners == [home], (heading, owners)


def test_runpod_section_in_the_generation_skill_is_one_line_and_a_link():
    body = _section(_text(GENERATION), "## RunPod")
    assert "RunPod machine skill" in body
    assert "astrid machines" not in body
    assert len([line for line in body.splitlines() if line.strip()]) <= 4, body


def test_skills_reach_the_scan_through_the_launcher_and_never_the_pack_module():
    launcher_scan = 'run-astrid" generate scan'
    for relative in (SETUP_SOURCE, SETUP_MIRROR, GENERATION, VIBECOMFY):
        text = _text(relative)
        assert "vibecomfy.local scan" not in text, relative
        assert "outside the launcher" not in text, relative
        assert "runs without the launcher" not in text, relative
        assert launcher_scan in text, relative
    for relative in (SETUP_SOURCE, SETUP_MIRROR, GENERATION):
        assert 'run-astrid" generate scan --json' in _text(relative), relative
    assert 'run-astrid" generate scan --ports 8188 --json' in _text(VIBECOMFY)


def test_user_facing_scan_strings_name_the_generate_scan_command():
    from astrid.core.generation import gpu_environment

    guidance = gpu_environment._unmanaged_guidance("vibecomfy", "linux")
    assert "astrid generate scan" in guidance, guidance
    assert "vibecomfy.local scan" not in guidance, guidance
    run_action = _text("astrid/packs/vibecomfy/actions/run/run.py")
    assert "astrid generate scan" in run_action
    assert "vibecomfy.local scan" not in run_action
    mcp_server = _text("astrid/mcp_server.py")
    assert "(astrid generate scan)" in mcp_server
    assert "vibecomfy local scan" not in mcp_server
