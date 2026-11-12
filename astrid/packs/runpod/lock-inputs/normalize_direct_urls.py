"""Keep only each direct-URL lock entry's own archive hash (run after `uv pip compile`).

uv can add the hashes of a version's other registry files to a direct-URL entry
when a registry requirement for the same package is merged into it. `uv pip sync
--require-hashes` then refuses the environment ("Conflicting archive URL hashes").
The policy pins the exact archive, so its URL fragment's hash is the only valid one.

    python astrid/packs/runpod/lock-inputs/normalize_direct_urls.py <lock>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def normalize(text: str) -> str:
    out = []
    for block in re.split(r"(?<=\n)(?=[A-Za-z0-9])", text):
        lines = block.split("\n")
        fragment = re.search(r" @ \S+#sha256=([0-9a-f]{64})", lines[0]) if lines else None
        if fragment:
            own = fragment.group(1)
            hashes = [line for line in lines if line.strip().startswith("--hash=sha256:")]
            kept = [line for line in hashes if own in line]
            if len(kept) != 1:
                raise SystemExit(f"{lines[0].split(' @ ')[0]}: its own archive hash is not listed exactly once")
            body = [line for line in lines if line not in hashes]
            # The kept hash line ends the requirement; drop its continuation backslash.
            lines = [body[0].rstrip(" \\") + " \\", kept[0].rstrip(" \\"), *body[1:]]
        out.append("\n".join(lines))
    return "".join(out)


if __name__ == "__main__":
    path = Path(sys.argv[1])
    path.write_text(normalize(path.read_text(encoding="utf-8")), encoding="utf-8")
