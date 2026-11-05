"""Fail closed before installing the unsafe frozen Linux launcher candidate.

This is a preflight refusal, not an implementation of the lifecycle.  The
candidate's ordinary startup status probe sends SIGKILL to a process group.
Installing first would leave state that the requested row cannot safely run to
completion.  Do not invoke the launcher or attempt cleanup in this situation.
"""


def run(config):
    """Return the first failed step in the shared driver's record format."""
    return [{
        "name": "install",
        "status": "fail",
        "error": (
            "Preflight refused before install: candidate pinokio/session.mjs:370 "
            "runs the status probe; :252-255 calls terminate and :290 sends "
            "SIGKILL to its process group even after the child exits. The "
            "required launcher start cannot run under the no-force-kill rule."
        ),
    }]
