"""macOS Pinokio row: refuse before installation when start is unsafe.

The pinned launcher cannot complete even its initial serve status probe without
issuing a process-group SIGKILL.  Do not install into a row that cannot proceed
under the no-force contract; leave the install and all subsequent steps alone.
"""


def run(config):
    """Return the first failure for the pinned launcher, without side effects."""
    return [{
        "name": "install",
        "status": "fail",
        "error": (
            "Install not attempted: required start is unsafe. Pinned launcher "
            "pinokio/session.mjs:370 runs runtimeHealthy; :255 calls terminate, "
            "which sends process-group SIGKILL at :290 even after the status "
            "probe exits. The row forbids SIGKILL."
        ),
    }]
