"""Fail closed for the R1.1 macOS Pinokio row under the no-force rule.

The pinned launcher cannot safely execute the required row: see
docs/r1.1-pinokio-macos-limitation.md. This refusal is unconditional, including
for missing or ambiguous configuration, and must happen before installation.
"""


def run(config):
    """Return the shared row driver's first failed stage without side effects."""
    return [{
        "name": "install",
        "status": "fail",
        "error": (
            "R1.1 macOS Pinokio no-force limitation: pinned candidate "
            "pinokio/session.mjs:255 calls terminate after a status probe; "
            "pinokio/session.mjs:290 sends SIGKILL to its process group. "
            "Preflight refuses before install or session start."
        ),
    }]
