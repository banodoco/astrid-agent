import importlib.util
import re
from pathlib import Path

from astrid.installer.outcome import SUCCESS, Outcome


ROOT = Path(__file__).resolve().parents[1]
SKILL = "docs/setup/SKILL.md"
PLAIN_UNIX_LINE = "curl -fsSL https://astrid.haus/install | sh"
FULL_UNIX_LINE = "curl -fsSL https://astrid.haus/install | sh -s -- --yes --autostart on --command on --skills on --skill-copies off"
FRESH_LABEL = "for a fresh install, or after the choices step only"
BANNER_ENDING = "Confirm that astrid.haus shows Connected, then help me begin at https://astrid.haus."
FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)
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


def test_skill_journey_ends_at_connected():
    text = _skill()
    assert sorted(_steps(text)) == [0, 1, 2, 3], sorted(_steps(text))
    assert "## Set up generation" not in text
    assert "## 4. " not in text and "## 5. " not in text
    assert "astrid/packs/generation/docs/SKILL.md" not in text
    assert "coming" not in text.lower()


def test_connected_step_says_local_engines_are_not_covered_yet():
    body = _steps(_skill())[3]
    assert "(Connecting local engines such as ComfyUI or Wan2GP is not covered here yet.)" in body, body


def test_skill_command_steps_have_one_block_per_os_success_line_and_doctor_fallback():
    steps = _steps(_skill())
    command_steps = {number for number, body in steps.items() if FENCE.search(body)}
    assert command_steps == {2}, command_steps
    for number in sorted(command_steps):
        body = steps[number]
        covered = _block_os(body)
        assert None not in covered, (number, "a fenced block has no OS label")
        flat = sorted(os for group in covered for os in group)
        assert flat == ["Linux", "Windows", "macOS"], (number, flat)
        assert "Succeeds when" in body, number
        assert "--doctor" in body, number


def _bare_astrid_hits(text: str) -> list:
    # Old detection only: fenced lines that start with astrid, and backticked astrid commands.
    hits = []
    for block in FENCE.findall(text):
        for line in block.splitlines():
            if re.match(r"\s*(\$ )?astrid(\s|$)", line):
                hits.append(line)
    hits += re.findall(r"`astrid\s[^`]*`", text)
    return hits


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
    assert "`--yes` is not needed" in route, route
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
    assert "recommended" not in route, route
    assert "Then follow step 2's success and failure rules." in route, route


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
    assert "brief's values" in body
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
