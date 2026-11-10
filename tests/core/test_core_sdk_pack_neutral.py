"""Core and sdk name no audiovisual pack; the only allowed hits are vendor ids and HTML."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("astrid/core", "astrid/sdk")
ALLOWED = {
    ("astrid/core/hivemind/executors/ingest_article/run.py",
     '"div", "p", "br", "h1", "h2", "h3", "h4", "h5", "h6",'),
    ("astrid/core/hivemind/docs/SKILL.md",
     "GET /rest/v1/message_filters?channel_name=eq.minimax_h3_resources  # by channel "
     "(note: channel_name is NOT unique — use channel_id for exactly one)"),
    ("astrid/core/model_catalog/models.yaml", "- id: minimax-h3"),
    ("astrid/core/model_catalog/models.yaml", "endpoint: minimax/h3/text-to-video"),
    ("astrid/core/model_catalog/models.yaml", "endpoint: minimax/h3/reference-to-video"),
}


def _hits() -> set[tuple[str, str]]:
    hits: set[tuple[str, str]] = set()
    for rel in SCANNED:
        for path in sorted((ROOT / rel).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for line in text.splitlines():
                if "h3" in line.lower():
                    hits.add((path.relative_to(ROOT).as_posix(), line.strip()))
    return hits


def test_core_and_sdk_hits_are_exactly_the_allowlist() -> None:
    assert _hits() == ALLOWED
