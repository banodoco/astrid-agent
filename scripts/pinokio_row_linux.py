"""Fail closed for the R1.1 Linux Pinokio row.

The frozen candidate's start session uses SIGKILL in its normal status probe.
Until that launcher path can satisfy the row's no-force rule, even an install
would leave the requested lifecycle impossible to complete safely.
"""


def run(config):
    """Return the first failed stage without inspecting or changing host state."""
    return [{
        "name": "install",
        "status": "fail",
        "error": (
            "R1.1 Linux Pinokio limitation: candidate "
            "3a6a98ae251ca126505245286e2d3e760f4348e9 "
            "pinokio/session.mjs:290 sends SIGKILL to the status probe's "
            "process group, including after the probe closes; the row forbids "
            "force-kill. Refusing before install or session start."
        ),
    }]
