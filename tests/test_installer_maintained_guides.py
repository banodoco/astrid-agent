import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = (
    "--autostart on --command on --skills on --skill-copies off"
)
PLAIN_UNIX_LINE = "curl -fsSL https://astrid.haus/install | sh"
FRESH_LABEL = "for a fresh install, or after the choices step only"
BANNER_ENDING = "Confirm that astrid.haus shows Connected, then help me begin at https://astrid.haus."
FLAGS = (
    "--autostart on|off",
    "--command on|off",
    "--skills on|off",
    "--skill-copies on|off",
)


def test_maintained_setup_guides_explain_approved_default_execution():
    # README is main-owned onboarding (#291 removed its install commands); it keeps the consent text only.
    for relative in ("docs/setup/SKILL.md", "docs/getting-started.md", "README.md"):
        content = (ROOT / relative).read_text(encoding="utf-8")
        if relative != "README.md":
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
    assert "docs/setup/SKILL.md" in readme
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


def test_setup_skill_labels_full_flag_command_for_fresh_install_only():
    # R1.3: the full-flag command must not read as safe for an existing install.
    skill = (ROOT / "docs/setup/SKILL.md").read_text(encoding="utf-8")
    full_flag = "curl -fsSL https://astrid.haus/install | sh -s -- --yes " + DEFAULTS
    assert FRESH_LABEL in skill
    assert skill.index(FRESH_LABEL) < skill.index(full_flag)


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
    assert "#" not in banner  # the banner links the skill file only, no heading anchor
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert BANNER_ENDING in readme
