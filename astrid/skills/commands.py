"""Bind commands in generated Markdown code to Astrid's own Python."""

from __future__ import annotations

import os
import re
import sys

_PYTHON = re.compile(r"(?<![\w./\\-])python3?\s+-m\s+(?=astrid(?:\.[A-Za-z_]\w*)*(?![\w.-]))")
_SPAN = re.compile(r"(?<![\\`])(?P<ticks>`+)(?!`)(?P<code>.*?)(?<!`)(?P=ticks)(?!`)", re.DOTALL)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def rewrite_commands(text: str) -> str:
    """Rewrite Python prefixes only inside code spans and fenced blocks."""
    if os.name == "nt":
        quoted = "& '" + sys.executable.replace("'", "''") + "'"
    else:
        quoted = "'" + sys.executable.replace("'", "'\"'\"'") + "'"

    def command(code: str) -> str:
        return _PYTHON.sub(lambda _match: quoted + " -m ", code)

    out: list[str] = []
    prose: list[str] = []
    fence = ""
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
                fence = ""
                out.append(line)
            else:
                out.append(command(line))
        elif match:
            out.append(_SPAN.sub(lambda m: m["ticks"] + command(m["code"]) + m["ticks"], "".join(prose)))
            prose.clear()
            fence = match[1]
            out.append(line)
        else:
            prose.append(line)
    out.append(_SPAN.sub(lambda m: m["ticks"] + command(m["code"]) + m["ticks"], "".join(prose)))
    return "".join(out)
