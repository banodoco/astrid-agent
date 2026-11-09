from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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
