#!/usr/bin/env python3
"""SDK v1 command backend for the ``sdk`` conformance pack (T6.4).

Thin wrapper: delegates the ENTIRE rendering protocol to the public SDK
entrypoint ``astrid.sdk.rendering.renderer_main`` (T6.2 shared contract):

    python3 sdk_render.py render|support --request <abs.json> --result <abs.json>

Per the shared contract, ``renderer_main`` reads ``--request <path> --result
<path>`` exactly like the raw backends and writes the same
``RenderResult``/``SupportReport``/``RendererError`` JSON, so the SDK twin
must emit semantically identical wire fields to ``render.py`` for the same
request.

Environment bootstrap (test-workspace only):

* The editable ``astrid`` install on this machine points at the *main*
  checkout, which predates the rendering subsystem; this script prepends its
  own repository root to ``sys.path`` so the subprocess imports the worktree's
  ``astrid`` (the same package the pytest harness runs against).
* ``renderer_main`` discovers the owning fixture pack from the command's
  working directory as an explicit extra root. No mutable user pack store or
  installation side effect is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PACK_ROOT = Path(__file__).resolve().parent
# _PACK_ROOT = .../tests/fixtures/renderer_packs/sdk, so:
#   parents[0] = .../renderer_packs (fixture pack roots)
#   parents[3] = repository root
_REPO_ROOT = _PACK_ROOT.parents[3]


def _bootstrap() -> None:
    repo = str(_REPO_ROOT)
    if repo not in sys.path:
        sys.path.insert(0, repo)


def main(argv: list[str]) -> int:
    _bootstrap()
    # The frozen public SDK name (external packs may not deep-import astrid.sdk.*).
    from astrid.sdk import renderer_main

    return renderer_main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
