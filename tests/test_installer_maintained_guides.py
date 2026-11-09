import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = "docs/setup/SKILL.md"
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
    expected = ["## 0. ", "## 1. ", "## 2. ", "## 3. ", "## 4. ", "## Set up generation"]
    positions = [next((i for i, h in enumerate(headings) if h.startswith(prefix)), -1) for prefix in expected]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions), positions


def test_skill_command_steps_have_one_block_per_os_success_line_and_doctor_fallback():
    steps = _steps(_skill())
    command_steps = {number for number, body in steps.items() if FENCE.search(body)}
    assert command_steps == {2, 4}, command_steps
    for number in sorted(command_steps):
        body = steps[number]
        covered = _block_os(body)
        assert None not in covered, (number, "a fenced block has no OS label")
        flat = sorted(os for group in covered for os in group)
        assert flat == ["Linux", "Windows", "macOS"], (number, flat)
        assert "Succeeds when" in body, number
        assert "--doctor" in body, number


def test_skill_never_names_a_bare_astrid_command():
    text = _skill()
    for block in FENCE.findall(text):
        for line in block.splitlines():
            assert not re.match(r"\s*(\$ )?astrid(\s|$)", line), line
    assert not re.search(r"`astrid\s[^`]*`", text)


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


def test_managed_engine_table_equals_local_engine_index_entries():
    rows = set()
    for line in _steps(_skill()).get(4, "").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip()[1:-1].split("|")]
        if cells[0] in ("Platform", "") or set(cells[0]) <= set("-: "):
            continue
        rows.add((cells[1], cells[0]))
    index = json.loads((ROOT / "astrid/packs/engine-index.json").read_text(encoding="utf-8"))
    local = {(entry["engine"], entry["platform"]) for entry in index["entries"]
             if not entry["recipe"]["path"].startswith("packs/runpod/")}
    assert rows == local, (rows, local)


def test_skill_has_set_up_generation_section_routing_to_generation_skills():
    text = _skill()
    assert "## Set up generation" in text.splitlines()
    body = _sections(text)["## Set up generation"]
    assert "](../../astrid/packs/vibecomfy/docs/SKILL.md#find-their-comfyui)" in body
    assert "](../../astrid/packs/runpod/docs/SKILL.md)" in body
    assert "coming" in body
    assert "not covered here yet" not in text
    vibecomfy = (ROOT / "astrid/packs/vibecomfy/docs/SKILL.md").read_text(encoding="utf-8")
    assert "### Find their ComfyUI" in vibecomfy
