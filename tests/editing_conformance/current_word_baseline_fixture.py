"""Current-only alias for the approved native word seed."""
from __future__ import annotations

from tests.editing_conformance.native_word_anchor_fixture import DECLARATION, FPS, current_retained

__all__ = ["DECLARATION", "FPS", "retained"]
retained = current_retained
