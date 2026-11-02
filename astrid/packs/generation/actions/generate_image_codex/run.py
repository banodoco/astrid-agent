"""Bounded managed Codex profile for the canonical image generator."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from runtime_protocol import platform

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint("generation.generate_image_codex")


def main() -> int:
    from astrid.packs.generation.actions.generate_image.run import build_parser
    from astrid.packs.generation.actions.generate_image.run import main as generate

    args = build_parser().parse_args()
    if args.execution != "codex" or not 1 <= args.count <= 4:
        raise ValueError("managed Codex requires execution=codex and count between 1 and 4")
    auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    if not auth.is_file():
        raise ValueError("Codex ChatGPT authentication is unavailable; run codex login")
    attempt_home = Path(args.out).resolve().parent / "codex-home"
    attempt_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_auth = attempt_home / "auth.json"
    shutil.copyfile(auth, private_auth)
    private_auth.chmod(0o600)
    from astrid.core.generation.backends.codex import managed_attempt_home

    def interrupted(signum, _frame):
        private_auth.unlink(missing_ok=True)
        raise SystemExit(128 + signum)
    restore = platform.install_stop_handlers(interrupted)
    try:
        # In-process, never os.environ: the host's command custody binds the launch environment.
        with managed_attempt_home(attempt_home):
            return generate(sys.argv[1:])
    finally:
        private_auth.unlink(missing_ok=True)
        restore()


if __name__ == "__main__":
    raise SystemExit(main())
