"""Runtime entrypoint for moirae.moirae."""


from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('moirae.moirae')
import argparse
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

from astrid.pack_api import build_manifest, write_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Moirae against a screenplay.")
    parser.add_argument("screenplay", type=Path)
    parser.add_argument("-o", "--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output)
    if output.suffix == "":
        output = output.with_suffix(".mp4")
    rc = subprocess.run([sys.executable, "-X", "utf8", "-m", "moirae", str(args.screenplay), "-o", str(output)]).returncode
    if rc == 0 and output != Path(args.output) and not Path(args.output).exists():
        Path(args.output).symlink_to(output.name)
    if rc == 0:
        manifest = build_manifest(
            kind="moirae",
            inputs={"screenplay": str(args.screenplay)},
            outputs=[{"name": "video", "path": output.name, "type": "file",
                      "media_type": "video/mp4", "is_primary": True}],
            created=datetime.now(timezone.utc).isoformat(),
        )
        write_manifest(output.parent / "manifest.json", manifest)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
