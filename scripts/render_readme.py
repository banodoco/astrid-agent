#!/usr/bin/env python3
"""Keep the agent-first README setup prompt aligned with its generated block."""
from __future__ import annotations

from pathlib import Path

START_MARKER = "<!-- BEGIN GENERATED AGENT BANNER -->"
END_MARKER = "<!-- END GENERATED AGENT BANNER -->"
BANNER = """
### Give this to your agent (recommended)

```text
Help me set up Astrid on this computer.

Read this setup guide and follow its steps in order:
https://github.com/banodoco/astrid-agent/blob/main/docs/setup/SKILL.md
Do not install the guide itself as a skill.

Before you run anything, show me the setup choices and let me change them.
Keep my existing work and credentials.
Confirm that astrid.haus shows Connected, then help me begin at https://astrid.haus.
```
"""


def main() -> None:
    path = Path(__file__).resolve().parents[1] / "README.md"
    current = path.read_text(encoding="utf-8")
    if current.count(START_MARKER) != 1 or current.count(END_MARKER) != 1:
        raise ValueError("README must contain exactly one generated prompt block")
    before, tail = current.split(START_MARKER, 1)
    _, after = tail.split(END_MARKER, 1)
    updated = before + START_MARKER + "\n" + BANNER + "\n" + END_MARKER + after
    if updated != current:
        path.write_text(updated, encoding="utf-8")
    print("README agent setup prompt is up to date")


if __name__ == "__main__":
    main()
