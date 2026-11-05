"""Windows Pinokio row preflight for the pinned launcher candidate.

The required launcher can force-terminate processes during the requested
lifecycle. Refuse before creating an installation that cannot be exercised
and stopped under the row's no-force constraint.
"""


def run(config):
    """Return the first failure without touching the installation or host."""
    return [{
        "name": "install",
        "status": "fail",
        "error": (
            "Preflight refused install: pinned Pinokio session.mjs:273-278 "
            "can run taskkill.exe /T /F for an open child; lifecycle.mjs:24-26 "
            "also uses SIGKILL on command timeout. The required start/stop "
            "sequence cannot guarantee the row's no-force rule."
        ),
    }]
