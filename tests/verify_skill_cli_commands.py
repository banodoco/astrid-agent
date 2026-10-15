#!/usr/bin/env python3
"""Verify CLI examples in authored skills against the current gateway parser."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
SKILL_PATHS = [
    ROOT / "astrid/packs/timeline/docs/SKILL.md",
    *sorted((ROOT / "astrid/packs/_core/docs").rglob("SKILL.md")),
    ROOT / "astrid/packs/rendering/docs/SKILL.md",
]
FAMILIES = {"backup", "doctor", "media", "projects", "runs", "tasks", "timelines"}
NESTED_MOUNTS = {("media", "references"), ("timelines", "shots")}
COMMAND = re.compile(r"python3\s+-m\s+astrid\b")
OPTION = re.compile(r"^--?[A-Za-z][A-Za-z0-9_-]*$")


def fenced_cli_commands(path: Path) -> list[tuple[int, list[str]]]:
    commands: list[tuple[int, list[str]]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    in_fence = False
    for line_no, line in enumerate(lines, start=1):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        match = COMMAND.search(line)
        if not match:
            continue

        text = line[match.start() :].strip()
        while text.endswith("\\"):
            text = text[:-1].rstrip()
            # A continued command is required to stay inside its fenced block.
            next_line_no = line_no + 1
            while next_line_no <= len(lines) and not lines[next_line_no - 1].strip():
                next_line_no += 1
            if next_line_no > len(lines) or lines[next_line_no - 1].startswith("```"):
                raise ValueError(f"{path}:{line_no}: unfinished CLI continuation")
            text += " " + lines[next_line_no - 1].strip().rstrip("\\").strip()
            line_no = next_line_no
        try:
            tokens = shlex.split(text, comments=True)
        except ValueError as exc:
            raise ValueError(f"{path}:{line_no}: cannot parse CLI example: {exc}") from exc
        if tokens[:3] != ["python3", "-m", "astrid"]:
            raise ValueError(f"{path}:{line_no}: expected a python3 -m astrid command")
        commands.append((line_no, tokens[3:]))
    return commands


def command_path(tokens: list[str]) -> list[str]:
    if not tokens:
        return []
    first = tokens[0]
    if first in {"-h", "--help"}:
        return [first]
    if first not in FAMILIES:
        raise ValueError(f"unknown top-level family or help flag: {first!r}")

    path = [first]
    index = 1
    if len(tokens) > 1 and (first, tokens[1]) in NESTED_MOUNTS:
        path.append(tokens[1])
        index = 2
    if index < len(tokens) and not tokens[index].startswith("-") and not tokens[index].startswith("<"):
        path.append(tokens[index])
    return path


def help_text(path: list[str]) -> str:
    command = [sys.executable, "-m", "astrid", *path]
    if not path or path == ["-h"]:
        command = [sys.executable, "-m", "astrid", "--help"]
    elif path == ["--help"]:
        command = [sys.executable, "-m", "astrid", "--help"]
    else:
        command.append("--help")
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"help command failed ({result.returncode}): {' '.join(command)}\n{result.stdout}")
    return result.stdout


def flag_is_listed(flag: str, text: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", text) is not None


def check_frontmatter(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---" or not any(line == "---" for line in lines[1:]):
        raise ValueError(f"{path.relative_to(ROOT)}: missing YAML frontmatter")
    header = "\n".join(lines[1 : lines.index("---", 1)])
    if not re.search(r"(?m)^name:\s*\S", header) or not re.search(r"(?m)^description:\s*\S", header):
        raise ValueError(f"{path.relative_to(ROOT)}: frontmatter needs name and description")


def check_timeline_scenarios(path: Path, commands: list[tuple[int, list[str]]]) -> None:
    text = path.read_text(encoding="utf-8")
    edit_lines = [
        line_no
        for line_no, tokens in commands
        if tokens[:2] == ["timelines", "edit"]
    ]
    linked_paths = re.findall(r"\[[^]]+\]\(([^)]+\.ya?ml)\)", text, re.IGNORECASE)
    scenario_paths = [
        (path.parent / unquote(link.split("#", 1)[0])).resolve()
        for link in linked_paths
        if "tests/editing_scenarios/" in link
    ]
    if len(scenario_paths) != len(edit_lines):
        raise ValueError(
            f"{path.relative_to(ROOT)}: found {len(edit_lines)} edit recipes and "
            f"{len(scenario_paths)} linked editing-scenario YAML files"
        )
    for scenario in scenario_paths:
        if not scenario.is_file() or ROOT / "tests/editing_scenarios" not in scenario.parents:
            raise ValueError(f"{path.relative_to(ROOT)}: invalid editing-scenario link {scenario}")


def main() -> int:
    errors: list[str] = []
    total = 0
    for skill in SKILL_PATHS:
        if not skill.is_file():
            errors.append(f"missing skill: {skill.relative_to(ROOT)}")
            continue
        try:
            check_frontmatter(skill)
            commands = fenced_cli_commands(skill)
            if skill == ROOT / "astrid/packs/timeline/docs/SKILL.md":
                check_timeline_scenarios(skill, commands)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
            continue

        for line_no, tokens in commands:
            total += 1
            try:
                path = command_path(tokens)
                help_output = help_text(path)
                for flag in (token for token in tokens if OPTION.fullmatch(token)):
                    if flag in {"-h", "--help"}:
                        continue
                    if not flag_is_listed(flag, help_output):
                        raise ValueError(f"flag {flag} is not listed by {' '.join(path) or 'astrid'} --help")
                print(f"OK {skill.relative_to(ROOT)}:{line_no}: python3 -m astrid {' '.join(path)}")
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                errors.append(f"{skill.relative_to(ROOT)}:{line_no}: {exc}")

    if total == 0:
        errors.append("found no fenced python3 -m astrid examples in the selected skills")
    if errors:
        for error in errors:
            print(f"FAILED {error}", file=sys.stderr)
        return 1
    print(f"Verified {total} fenced CLI examples across {len(SKILL_PATHS)} skills.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
