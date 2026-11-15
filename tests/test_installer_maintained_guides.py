import importlib.util
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrid.installer.outcome import SUCCESS, Outcome


ROOT = Path(__file__).resolve().parents[1]
SKILL = "docs/setup/SKILL.md"
PLAIN_UNIX_LINE = "curl -fsSL https://astrid.haus/install | sh"
FULL_UNIX_LINE = "curl -fsSL https://astrid.haus/install | sh -s -- --yes --autostart on --command on --skills on --skill-copies off"
FRESH_LABEL = "for a fresh install, after step 1"
BANNER_ENDING = "Confirm that astrid.haus shows Connected, then help me begin at https://astrid.haus."
FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)
ISO_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z")
DEADLINE_LITERAL = re.compile(r"--deadline[= ]+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z)")
OS_LABEL = re.compile(r"\*\*(macOS or Linux|macOS|Linux|Windows)[^*]*:\*\*")
OS_OF_LABEL = {"macOS or Linux": ["macOS", "Linux"], "macOS": ["macOS"], "Linux": ["Linux"], "Windows": ["Windows"]}
DEFAULTS = (
    "--autostart on --command on --skills on --skill-copies off"
)
FLAGS = (
    "--autostart on|off",
    "--command on|off",
    "--skills on|off",
    "--skill-copies on|off",
)


def test_maintained_setup_guides_explain_approved_default_execution():
    # README is main-owned onboarding (#291 removed its install commands); it points at the skill and keeps the consent text.
    for relative in ("docs/setup/SKILL.md", "docs/getting-started.md"):
        content = (ROOT / relative).read_text(encoding="utf-8")
        for flag in FLAGS:
            assert flag in content, (relative, flag)
        assert DEFAULTS in content, relative
        assert "--yes" in content, relative
        assert "consent" in content.lower(), relative
        assert "approved" in content.lower(), relative

    setup_skill = (ROOT / "docs/setup/SKILL.md").read_text(encoding="utf-8")
    getting_started = (ROOT / "docs/getting-started.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "npm run dev:local" in setup_skill and "npm run dev:local" in getting_started
    assert "without discarding the user's work" in setup_skill and "keep your changes" in getting_started
    for name, content in (("README.md", readme), ("docs/getting-started.md", getting_started)):
        assert "without a prompt" not in content, name
        assert "docs/setup/SKILL.md" in content, name
    assert "consent" in readme.lower()
    assert "approved" in readme.lower()
    assert "needs no second consent question" in readme
    assert ("powershell -NoProfile -ExecutionPolicy Bypass -Command \"& ([scriptblock]::Create((irm "
            "https://astrid.haus/install.ps1))) --yes " + DEFAULTS + "\"") in setup_skill


def test_setup_guides_have_one_approved_installer_command_per_platform():
    expected = (
        "curl -fsSL https://astrid.haus/install | sh -s -- --yes " + DEFAULTS,
        "& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --yes " + DEFAULTS,
    )
    for relative in ("docs/setup/SKILL.md", "docs/getting-started.md"):
        content = (ROOT / relative).read_text(encoding="utf-8")
        assert all(command in content for command in expected), relative


def _skill() -> str:
    return (ROOT / SKILL).read_text(encoding="utf-8")


def _sections(text: str) -> dict:
    # Map each "## " heading line to its section, in document order.
    return {part.splitlines()[0]: part for part in re.split(r"^(?=## )", text, flags=re.M)
            if part.startswith("## ")}


def _steps(text: str) -> dict:
    # Map each numbered journey step ("## 0. ...") to its section body.
    steps = {}
    for heading, body in _sections(text).items():
        match = re.match(r"## (\d+)\. ", heading)
        if match:
            steps[int(match.group(1))] = body
    return steps


def _block_os(body: str) -> list:
    # For each fenced block, the OS labels of the nearest bold label above it.
    labels_for_blocks = []
    position = 0
    for block in FENCE.finditer(body):
        labels = OS_LABEL.findall(body[position:block.start()])
        labels_for_blocks.append(OS_OF_LABEL[labels[-1]] if labels else None)
        position = block.end()
    return labels_for_blocks


def test_skill_journey_steps_run_in_order():
    headings = list(_sections(_skill()))
    expected = ["## 0. ", "## 1. ", "## 2. ", "## 3. "]
    positions = [next((i for i, h in enumerate(headings) if h.startswith(prefix)), -1) for prefix in expected]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions), positions


def test_install_journey_hands_over_to_generation():
    text = _skill()
    assert sorted(_steps(text)) == [0, 1, 2, 3, 4], sorted(_steps(text))
    assert "## 4. Set up generation" in text
    assert "## 5. " not in text
    assert "astrid/packs/generation/docs/SKILL.md#set-up-my-generation" in text
    assert "coming" not in text.lower()


def _generation_placement() -> str:
    # The generation skill's "## Placement" section, which owns the no-GPU route (P4.7.1, c1).
    text = (ROOT / "astrid/packs/generation/docs/SKILL.md").read_text(encoding="utf-8")
    start = text.index("## Placement\n")
    return text[start:text.index("\n## ", start + 1)]


def test_generation_setup_routes_capability_and_consent():
    # P4.7.1: the capability check and engine offer moved to the generation skill's Placement
    # (c1); setup step 4 keeps a one-line link. The Wan2GP "planned (Plan 12)" line is gone (c9).
    body = " ".join(_steps(_skill())[4].split())
    for phrase in (
        "No local GPU or ComfyUI?", "astrid/packs/generation/docs/SKILL.md#placement",
        "managed engine", "RunPod", "Never", "--doctor",
        "runpod/docs/SKILL.md#runpod-generation-machines",
    ):
        assert phrase in body, phrase
    assert "nvidia-smi" not in body
    assert "Plan 12" not in _skill()
    placement = " ".join(_generation_placement().split())
    for phrase in (
        "No local GPU or ComfyUI?",
        "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader",
        "uname -m", "NVIDIA", "12 GB", "declared",
        "managed engine", "RunPod", "own yes", "never ask for a key in chat",
        "runpod/docs/SKILL.md#runpod-generation-machines",
    ):
        assert phrase in placement, phrase
    assert placement.count("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader") == 1
    generation = (ROOT / "astrid/packs/generation/docs/SKILL.md").read_text(encoding="utf-8")
    assert "Wan2GP" in generation[generation.index("## Set up my generation"):generation.index("## Generate")]
    core = (ROOT / "astrid/packs/_core/docs/SKILL.md").read_text(encoding="utf-8")
    row = next(line for line in core.splitlines() if line.startswith("| Set up my generation:"))
    assert "https://github.com/banodoco/astrid-agent/blob/main/docs/setup/SKILL.md#4-set-up-generation" in row
    assert "skill `astrid-setup`" in row


def test_generation_commands_include_platform_launchers_and_paid_bounds():
    # Rewritten: the RunPod claim walkthrough (its paid bounds, --offer-digest included)
    # moved to the RunPod skill. Setup keeps the launcher forms and links that skill.
    # cause f123e45c1, 889312e1f; ruling: machines 12:25 Sat
    # P4.R13: the claim now lives in the RunPod skill (Plan 20 phase 4); its paid-bounds rule follows it there.
    import shlex

    from astrid.core.machines_cli import _parser

    body = _steps(_skill())[4]
    blocks = FENCE.findall(body)
    assert blocks
    launchers = (
        '"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid"',
        '"$HOME/.local/share/astrid/run-astrid"',
        '& "$env:LOCALAPPDATA\\Astrid\\run-astrid.cmd"',
    )
    skill = _skill()
    for launcher in launchers:
        assert launcher in skill, launcher
    for command in ("credential set runpod", "machines claim", "--consent paid", "generate describe --placement runpod"):
        assert command not in body, command
    assert "engines install comfyui --dry-run" in _generation_placement()  # P4.7.1 (c1)
    assert "generate fetch-models" in body and "--apply PLAN_SHA256" in body

    # Every guide the installer ships or the MCP surface serves: build_prompts() serves each
    # pack SKILL.md (main and extras, e.g. astrid/packs/*/docs/**/SKILL.md) and docs/setup/ is the setup guide set.
    guides = sorted({*ROOT.glob("astrid/packs/**/SKILL.md"), *(ROOT / "docs/setup").rglob("*.md")})
    runpod_skill = ROOT / "astrid/packs/runpod/docs/SKILL.md"
    assert runpod_skill in guides and (ROOT / "docs/setup/SKILL.md") in guides

    def commands_of(path):
        text = path.read_text(encoding="utf-8")
        for block in FENCE.findall(text):
            for line in block.replace("\\\n", " ").splitlines():
                if "machines" in line or "generate" in line:
                    try:
                        yield shlex.split(line)
                    except ValueError:
                        yield line.split()

    paid_flags = {"--consent", "--max-usd", "--deadline", "--offer-digest", "--preparation-seconds",
                  "--execution-seconds", "--idle-seconds", "--allow-paid", "--until-release"}
    claims_by_guide, generates, runpod_generates = {}, [], 0
    for guide in guides:
        for command in commands_of(guide):
            if "machines" in command and command[command.index("machines") + 1:][:1] == ["claim"]:
                claims_by_guide.setdefault(guide, []).append(command[command.index("machines") + 1:])
            elif "generate" in command:
                generate = command[command.index("generate") + 1:]
                generates.append((guide, generate))
                if "--placement=runpod" in generate or any(
                        a == "--placement" and generate[i + 1:i + 2] == ["runpod"] for i, a in enumerate(generate)):
                    runpod_generates += 1
    all_claims = [(g, c) for g, claims in claims_by_guide.items() for c in claims]
    assert all_claims, "no `machines claim` line found in any guide"
    assert runpod_generates, "no `generate ... --placement runpod` line found in any guide"
    assert claims_by_guide.get(runpod_skill), "the RunPod skill shows no `machines claim` line"
    now = datetime.now(timezone.utc)
    deadline_now = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for guide, claim in all_claims:
        claim = [deadline_now if arg == "FUTURE_UTC_DEADLINE" else arg for arg in claim]
        flags = [arg.split("=", 1)[0] for arg in claim if arg.startswith("--")]
        assert {"--consent", "--max-usd", "--deadline", "--for"} <= set(flags), (guide.name, claim)
        parsed = _parser().parse_args(claim)
        assert parsed.consent == ["paid"], (guide.name, claim)
        assert parsed.max_usd > 0 and parsed.deadline and parsed.claim_for, (guide.name, claim)
    # A literal deadline a day or more out goes stale and agents copy it; only placeholders may sit that far ahead.
    horizon = now + timedelta(hours=24)
    for guide in guides:
        for block in FENCE.findall(guide.read_text(encoding="utf-8")):
            for line in block.replace("\\\n", " ").splitlines():
                stamps = DEADLINE_LITERAL.findall(line)
                if "machines" in line and " claim" in line:
                    stamps += ISO_UTC.findall(line)
                for stamp in stamps:
                    when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    assert when <= horizon, (guide.name, stamp, line)
    for guide, generate in generates:
        assert not paid_flags.intersection(arg.split("=", 1)[0] for arg in generate), (guide.name, generate)


def test_skill_command_steps_have_one_block_per_os_success_line_and_doctor_fallback():
    steps = _steps(_skill())
    command_steps = {number for number, body in steps.items() if FENCE.search(body)}
    assert command_steps == {2, 4}, command_steps
    for number in (2,):
        body = steps[number]
        covered = _block_os(body)
        assert None not in covered, (number, "a fenced block has no OS label")
        flat = sorted(os for group in covered for os in group)
        assert flat == ["Linux", "Windows", "macOS"], (number, flat)
        assert "Succeeds when" in body, number
        assert "--doctor" in body, number


BARE_ASTRID = re.compile(
    r"(?<![-/\w.])astrid\s+(?:doctor|backup|projects|documents|preferences|media|tasks|runs|timelines|-{1,2}[a-z])"
)


def _bare_astrid_hits(text: str) -> list:
    # astrid followed by a known subcommand or flag, anywhere: prose, code, or a && chain.
    # The launcher path (run-astrid) and "the `astrid` terminal command" are not commands.
    text = text.replace("the `astrid` terminal command", "")
    return [match.group(0) for match in BARE_ASTRID.finditer(text)]


def test_skill_never_names_a_bare_astrid_command():
    assert _bare_astrid_hits(_skill()) == []


def test_bare_astrid_check_catches_prose_chained_and_fenced_forms():
    # R1.7 proof: a copy of the skill that names a bare command must be caught.
    for planted in ("run astrid doctor", "cd astrid-app && astrid doctor", "$ astrid backup --help"):
        assert _bare_astrid_hits(_skill() + "\n" + planted + "\n"), planted
    assert _bare_astrid_hits(_skill() + "\nthe `astrid` terminal command on\n") == []


def test_skill_entry_routes_appear_once_each_in_order():
    body = _steps(_skill()).get(0, "")
    names = ("**App brief**", "**Fresh install**", "**Existing install**")
    assert all(body.count(name) == 1 for name in names), names
    positions = [body.find(name) for name in names]
    assert positions == sorted(positions), positions


def test_quoted_installer_first_lines_exist_in_installer_outcome():
    quoted = re.findall(r"`(Astrid [^`]*)`", _steps(_skill()).get(2, ""))
    outcome = (ROOT / "astrid/installer/outcome.py").read_text(encoding="utf-8")
    assert {"Astrid installed:", "Astrid is installed and running", "Astrid is already up to date",
            "Astrid was NOT installed"} <= set(quoted), quoted
    for line in quoted:
        assert line in outcome, line


CHOICE_FLAG_NAMES = ("--autostart", "--command", "--skills", "--skill-copies")
EXISTING_INSTALL_COMMANDS = (
    "curl -fsSL https://astrid.haus/install | sh",
    "& ([scriptblock]::Create((irm https://astrid.haus/install.ps1)))",
)


def _routes() -> dict:
    # Map each entry route's bold name ("App brief", ...) to its text.
    body = _steps(_skill()).get(0, "")
    return {match.group(1): " ".join(match.group(0).split()) for match in re.finditer(
        r"^\d\. \*\*([^*]+)\*\*:.*?(?=^\d\. \*\*|\Z)", body, re.M | re.S)}


def test_existing_install_route_keeps_saved_choices_and_passes_no_choice_flags():
    route = _routes()["Existing install"]
    assert "Your saved setup choices stay as they are. Tell me if you want to change one." in route
    assert "step 1" not in route, route
    assert "no `--yes`" in route, route
    for command in EXISTING_INSTALL_COMMANDS:
        assert f"`{command}`" in route, command
    for flag in CHOICE_FLAG_NAMES:
        assert flag not in route, flag


def test_app_brief_route_shows_the_brief_values_and_runs_its_command_as_given():
    route = _routes()["App brief"]
    assert "lists the setup choices and a command" in route, route
    assert "Show those values back" in route, route
    assert "Install with these choices, or change one?" in route, route
    assert "run the brief's command exactly as given" in route, route
    assert "Then check the result as in step 2." in route, route


def test_success_first_lines_start_with_a_quoted_prefix_in_step_2():
    # R1.1: the success list is read from installer outcomes, so it cannot drift.
    quoted = re.findall(r"`(Astrid [^`]*)`", _steps(_skill())[2])
    for kind in SUCCESS:
        first = Outcome(kind=kind, still=1, start=1, target=2).headline()[0]
        assert any(first.startswith(prefix) for prefix in quoted), (kind, first, quoted)


def test_step_2_labels_the_full_flag_command_for_fresh_install_only():
    # R1.3: the full-flag command must not read as safe for an existing install.
    body = _steps(_skill())[2]
    assert FRESH_LABEL in body
    assert body.index(FRESH_LABEL) < body.index(FULL_UNIX_LINE)


def test_step_1_keeps_the_brief_values_and_applies_a_changed_choice_alone():
    # R1.5
    body = _steps(_skill())[1]
    assert "keep the brief's other values" in body
    assert "unchanged" not in body


def test_getting_started_tells_existing_installs_to_run_the_plain_line():
    # R1.4: an existing install runs the plain line with no choice flags; saved choices stay.
    guide = (ROOT / "docs/getting-started.md").read_text(encoding="utf-8")
    assert "saved choices stay" in guide
    assert "\n" + PLAIN_UNIX_LINE + "\n" in guide
    assert guide.index("saved choices stay") < guide.index("\n" + PLAIN_UNIX_LINE + "\n")


def test_readme_banner_ends_with_connected_check_and_names_no_missing_heading():
    # R1.2: the banner ends with the Connected check and names no skill section.
    spec = importlib.util.spec_from_file_location("render_readme", ROOT / "scripts/render_readme.py")
    render_readme = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render_readme)
    banner = render_readme.BANNER
    prompt = banner.split("```text\n", 1)[1].split("\n```", 1)[0]  # the prompt the person copies
    assert prompt.rstrip().endswith(BANNER_ENDING)
    assert "Set up generation" not in banner
    assert "#" not in prompt  # the prompt links the skill file only, no heading anchor
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert BANNER_ENDING in readme


def test_readme_banner_keeps_saved_choices_for_an_existing_install():
    # Route 3 in docs/setup/SKILL.md: an existing install keeps its saved choices.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "If Astrid is already installed, keep my saved choices unless I ask to change one." in readme
    assert "Before you run anything, show me the setup choices" not in readme


def test_setup_choices_recommend_current_settings():
    routes = _routes()
    app = routes["App brief"]
    assert 'as "your choices from the App"' in app
    assert "Recommend keeping them." in app
    assert "saved" not in app
    assert "defaults" not in app
    assert "Recommend Astrid defaults." in routes["Fresh install"]
    existing = routes["Existing install"]
    assert 'Recommend "keep your current settings".' in existing
    # P4.7.1 (c3): "Do not guess" and "Do not show" restated this promise; it now says it once.
    assert "Keep the saved choices and say so:" in existing
    choices = " ".join(_steps(_skill())[1].split())
    # P4.7.1 (c4): step 1 no longer repeats the routes' recommendations, which are asserted above.
    assert "recommend" not in choices.lower(), choices
    assert "Astrid defaults are:" in choices
    install = " ".join(_steps(_skill())[2].split())
    assert "After consent, run the line for the user's OS with the values from step 1." in install
    banner_line = (
        "Before you install, show me the setup choices, recommending my current "
        "settings, and let me change them."
    )
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert banner_line in readme
