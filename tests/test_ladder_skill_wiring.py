"""R93-D: maintained guides consume C's action and do not invent a second one."""
import importlib.util
import re
import shlex
from pathlib import Path

from astrid.core.generation import cli, preflight


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "astrid/packs/setup/docs/SKILL.md"
MIRROR = ROOT / "docs/setup/SKILL.md"
VIBE = ROOT / "astrid/packs/vibecomfy/docs/SKILL.md"


def normalized(text):
    return " ".join(text.split())


def setup_section():
    text = SETUP.read_text(encoding="utf-8")
    return text.split("## 4. Set up generation\n", 1)[1].split("\n## ", 1)[0]


def vibe_section():
    text = VIBE.read_text(encoding="utf-8")
    return text.split("### Find their ComfyUI\n", 1)[1].split("\n### ", 1)[0]


def test_both_guides_follow_one_target_action():
    for body in (setup_section(), vibe_section()):
        for token in ("generate targets", "ladder.through", "ladder.stopped_at",
                      "next_action", "then_argv", "choices", "attach_existing",
                      "available=false", "who=person", "who=agent"):
            assert token in body, token
        assert "through=compatible" in body
        assert "compute" in body and "delivery" in body
        assert "permission request" in body or "permission-request" in body
    assert "Never launch, restart or install" in setup_section()


def test_vibe_reasons_and_copy_agree_with_producer():
    body = normalized(vibe_section())
    reasons = re.findall(r"^- " + chr(96) + r"([a-z_]+)" + chr(96) + r":", vibe_section(), re.M)
    assert len(reasons) == len(set(reasons))
    assert set(reasons) == set(preflight.LADDER_REASONS)
    for reason in ("closed", "stale_registration", "several_installs"):
        for key in ("message", "step"):
            assert preflight.LADDER_TEXT[reason][key] in body
    assert "normal permission-request flow" in body
    assert "don't change permission defaults" in body
    assert "A denied request means report the access block" in body
    assert "without asking" not in VIBE.read_text(encoding="utf-8").split("### Remember the setup", 1)[1].split("\n## ", 1)[0]


def _generate_commands(text):
    for block in re.findall(r"^" + chr(96)*3 + r"[^\n]*\n(.*?)^" + chr(96)*3, text, re.M | re.S):
        for line in block.replace("\\\n", " ").splitlines():
            if " generate " not in line:
                continue
            prefix, rest = line.split(" generate ", 1)
            if "run-astrid" in prefix or prefix.strip() == "astrid":
                yield shlex.split(rest, comments=True)


def test_guide_commands_parse_and_placement_is_not_duplicated():
    for document in (SETUP, VIBE):
        commands = list(_generate_commands(document.read_text(encoding="utf-8")))
        assert commands
        for argv in commands:
            cli._parser().parse_args(argv)
            assert argv[0] != "launch"
            if "--apply" in argv:
                assert argv[0] == "fetch-models"
        assert sum(argv[:3] == ["settings", "set", "placement"] for argv in commands) == 1
        assert sum(argv == ["targets", "--json"] for argv in commands) == 1


def test_setup_mirror_is_generated():
    script = ROOT / "scripts/generate_setup_skill_mirror.py"
    spec = importlib.util.spec_from_file_location("setup_mirror", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert MIRROR.read_bytes() == module.render(SETUP.read_bytes())
