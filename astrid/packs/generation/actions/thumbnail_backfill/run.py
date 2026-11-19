"""Extract a preview inside the ordinary task's input/output custody."""
from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint("generation.thumbnail_backfill")

import argparse
from datetime import datetime, timezone
from pathlib import Path

from astrid.core.execution.thumbnails import extract_thumbnail
from astrid.pack_api import build_manifest, write_manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--media-type", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    extract_thumbnail(args.source, args.out / "thumbnail.jpg", args.media_type)
    manifest = build_manifest(kind="generation-thumbnail", inputs={"source": str(args.source)},
        outputs=[{"name": "thumbnail", "path": "thumbnail.jpg", "ordinal": 0,
                  "role": "result", "is_primary": False, "media_type": "image/jpeg"}],
        created=datetime.now(timezone.utc).isoformat())
    write_manifest(args.out / "manifest.json", manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
