"""Fail closed for the R1.1 Windows Pinokio row.

The frozen candidate's session supervisor can force-terminate a Windows
process tree. Until that launcher path is safe under the row's no-force rule,
the row cannot run even its install stage. This module deliberately does not
inspect or mutate the supplied paths, data, or processes.
"""


def run(config):
    """Return the first failed stage without invoking the launcher."""
    return [{
        "name": "install",
        "status": "fail",
        "error": (
            "R1.1 Windows Pinokio row refused before install: pinned candidate "
            "pinokio/session.mjs:277-278 can invoke taskkill.exe /T /F for "
            "an open child; the row forbids force termination."
        ),
    }]
