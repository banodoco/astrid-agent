from __future__ import annotations

import re
from pathlib import Path


CORE = Path(__file__).resolve().parents[1] / "astrid" / "core"
# Owner ruling, 12 Nov 2026: core neutrality is an aspiration; no early
# generalisation while there is one provider and one engine. M11: a neutral
# machine interface when a second provider arrives.
BASELINE = {"runpod": 306, "comfy": 1206}


def test_provider_and_engine_mentions_in_core_do_not_grow():
    counts = {term: 0 for term in BASELINE}
    for path in CORE.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for term in counts:
            counts[term] += len(re.findall(re.escape(term), source, flags=re.IGNORECASE))
    print(f"core provider/engine mention counts: {counts}")
    assert all(counts[term] <= baseline for term, baseline in BASELINE.items())
