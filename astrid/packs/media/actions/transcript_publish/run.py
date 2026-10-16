"""Non-executable runner entrypoint for Host-owned transcript publication."""
from __future__ import annotations

from typing import NoReturn


def run(_request: object) -> NoReturn:
    """Fail closed when the regular Python action runner reaches this entrypoint."""
    raise RuntimeError(
        "media.transcript_publish requires the caller-bound Host document adapter; "
        "use the authenticated Astrid SDK Transcripts route"
    )
