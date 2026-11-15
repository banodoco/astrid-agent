"""The hand-off sentence has one owner: the setup skill. Every door quotes it exactly."""

import importlib.util
import re
from pathlib import Path

from astrid.installer.outcome import Outcome

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "astrid/packs/setup/docs/SKILL.md"
MIRROR = ROOT / "docs/setup/SKILL.md"
README = ROOT / "README.md"
QUOTED = re.compile(r"^\*(Next: [^*\n]+)\*$", re.M)


def _quoted_sentences(path: Path) -> list[str]:
    return QUOTED.findall(path.read_text(encoding="utf-8"))


SENTENCE = _quoted_sentences(SKILL)[0]


def _banner() -> str:
    spec = importlib.util.spec_from_file_location("render_readme", ROOT / "scripts/render_readme.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BANNER


def test_setup_skill_quotes_the_sentence_exactly_once():
    assert _quoted_sentences(SKILL) == [SENTENCE]
    assert SKILL.read_text(encoding="utf-8").count("Next:") == 1


def test_public_mirror_quotes_the_same_sentence_once():
    assert _quoted_sentences(MIRROR) == [SENTENCE]
    assert MIRROR.read_text(encoding="utf-8").count("Next:") == 1


def test_installer_success_line_is_the_sentence_after_the_first_line():
    lines = Outcome("updated", 12, None, 12).headline()
    assert lines[0].startswith("Astrid installed: ")
    assert lines[1] == SENTENCE


def test_readme_banner_ends_with_the_sentence_and_the_readme_carries_it():
    assert SENTENCE in _banner().splitlines()
    assert SENTENCE in README.read_text(encoding="utf-8").splitlines()


def test_the_sentence_text_is_held_in_one_agent_code_place():
    hits = [path.relative_to(ROOT).as_posix()
            for folder in ("astrid", "scripts")
            for path in (ROOT / folder).rglob("*.py")
            if SENTENCE in path.read_text(encoding="utf-8")]
    assert hits == ["astrid/installer/outcome.py"], hits
