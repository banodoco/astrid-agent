#!/usr/bin/env python3
"""Operator entry point for ``astrid.packs.runpod.claim_capacity``.

Not the generation path: ``astrid generate <image|video|audio> ... --placement runpod``
claims and releases its own machine. This wrapper only keeps the operator command
``python scripts/claim_runpod_5090_backup.py`` working; all logic lives in the pack module.
"""

from __future__ import annotations

from astrid.packs.runpod.claim_capacity import *  # noqa: F401,F403
from astrid.packs.runpod.claim_capacity import main

if __name__ == "__main__":
    raise SystemExit(main())
