"""Generate or check cli-surface.json: the `astrid-tools` command tree, derived from the real argparse parsers.

    python scripts/gen_cli_surface.py          # rewrite cli-surface.json
    python scripts/gen_cli_surface.py --check  # exit 1, naming each command whose surface changed

Coverage: the gateway's top-level command names (astrid.core.gateway.dispatch) and, for the seven product families
(projects, documents, preferences, media, tasks, runs, timelines incl. the nested shots/references mounts), every
verb with its flags and positionals, walked from the parsers `FAMILY_PARSER_MODULES` builds. The operational
commands (dev, doctor, backup, status) build their parsers inline; they are listed by name only.
The exported file is versioned together with diagnostics and behavioral fixtures by schemas/public-boundary/bundle.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUTPUT = "cli-surface.json"


def _walk(parser: argparse.ArgumentParser) -> dict:
    node: dict = {"flags": {}, "positionals": [], "verbs": {}}
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if isinstance(action, argparse._SubParsersAction):
            node["verbs"].update({name: _walk(sub) for name, sub in sorted(action.choices.items())})
        elif action.option_strings:
            flag: dict = {"required": bool(action.required)}
            if action.choices is not None:
                flag["choices"] = sorted(str(c) for c in action.choices)
            for name in action.option_strings:
                node["flags"][name] = flag
        else:
            node["positionals"].append(action.dest)
    node["flags"] = dict(sorted(node["flags"].items()))
    return node


def build() -> dict:
    from astrid.core.cli.domain_product import PRODUCT_FAMILIES, family_parsers
    from astrid.core.gateway.dispatch import _top_level_commands

    families = {family: _walk(parser) for family, parser in family_parsers().items()}
    operational = sorted(_top_level_commands() - set(PRODUCT_FAMILIES))
    return {"binary": "astrid-tools", "families": families, "operational_commands": operational}


def render(surface: dict) -> str:
    return json.dumps(surface, indent=2, sort_keys=True) + "\n"


def _flatten(node: dict, path: str = "") -> dict[str, str]:
    out = {path or "(root)": json.dumps({k: v for k, v in node.items() if k != "verbs"}, sort_keys=True)}
    for name, sub in node.get("verbs", {}).items():
        out.update(_flatten(sub, f"{path} {name}".strip()))
    return out


def check(root: Path = ROOT) -> list[str]:
    """Return one line per command path whose flags/positionals changed, appeared or disappeared."""
    current = build()
    try:
        committed = json.loads((root / OUTPUT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [f"{OUTPUT}: missing or unreadable"]
    old = _flatten({"verbs": committed.get("families", {})})
    new = _flatten({"verbs": current["families"]})
    problems = [f"{path}: " + ("added" if path not in old else "removed" if path not in new else "changed")
                for path in sorted(old.keys() | new.keys()) if old.get(path) != new.get(path)]
    if not problems and (root / OUTPUT).read_text(encoding="utf-8") != render(current):
        problems.append(f"{OUTPUT}: differs from the generated surface")
    return problems


def main(argv: list[str]) -> int:
    if "--check" in argv:
        problems = check()
        if problems:
            print(f"{OUTPUT} is stale; run `python scripts/gen_cli_surface.py`:\n  " + "\n  ".join(problems))
            return 1
        print(f"{OUTPUT} is current")
        return 0
    (ROOT / OUTPUT).write_text(render(build()), encoding="utf-8")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
