"""Repository source-topology allowlist for concrete renderer dependencies.

The ``rendering.render`` executor is a backend-neutral facade: concrete
renderers (``astrid.packs.rendering.rendering.renderers.*``) are pluggable
implementations that production code must reach only through the rendering
registry / ``RenderService``.  This gate greps the checked-in ``astrid/``
source tree (site-packages and vendored code are never scanned) and fails
if any production module:

* imports ``astrid.packs.rendering.rendering.renderers.*``,
* references the facade runtime module
  (``astrid.packs.rendering.actions.render.run``) in argv / imports —
  i.e. spawns ``python -m astrid.packs.rendering.actions.render.run``
  or imports the facade entrypoint directly.

Exemptions are explicit and listed below with their reasons: executor
manifests (data, not code), the backend implementations themselves, the
facade package, the legacy engine module it preserves, and the pack
raw-command launcher that backend renderer manifests execute.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ASTRID_DIR = REPO_ROOT / "astrid"

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# ``import astrid.packs.rendering.rendering.renderers...`` or
# ``from astrid.packs.rendering.rendering.renderers... import ...`` (any depth).
_BACKEND_IMPORT = r"(?:import|from)\s+astrid\.packs\.rendering\.rendering\.renderers(?:\.[A-Za-z_]\w*)*"

# The retired legacy engine import pattern, retained only as a negative scan.
# (``import astrid.packs.rendering.actions.render.legacy_engine`` /
# ``from astrid.packs.rendering.actions.render.legacy_engine import ...``)
# or as a name in a package import
# (``from astrid.packs.rendering.actions.render import legacy_engine``).
_LEGACY_ENGINE_IMPORT = (
    r"(?:"
    r"(?:import|from)\s+astrid\.packs\.rendering\.actions\.render\.legacy_engine"
    r"|from\s+astrid\.packs\.rendering\.actions\.render\s+import\s+[^\n]*legacy_engine"
    r")"
)

# Any literal reference to the facade runtime module: ``-m
# astrid.packs.rendering.actions.render.run`` argv spawns, argv-list
# strings, and direct imports.  Executor manifests declare this module in
# their argv and ``metadata.runtime_module`` and are exempt as data.
_RENDER_RUN_REF = r"astrid\.packs\.rendering\.actions\.render\.run"

_PY_GLOBS = ("--glob", "*.py")
_SPAWN_GLOBS = ("--glob", "*.py", "--glob", "*.yaml", "--glob", "*.yml", "--glob", "*.json")

# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

# Backend implementations: any module inside the rendering/renderers package is the
# implementation of a pluggable renderer and may import sibling renderers
# (e.g. the FFmpeg backend reuses the Remotion transport lock).
_BACKEND_IMPL_PREFIX = "astrid/packs/rendering/rendering/renderers/"

# Explicitly allowlisted production files, each with its reason.  The facade
# owns all dispatch; the legacy engine module is the characterized
# pre-facade implementation preserved verbatim for parity fixtures; the pack
# launcher is the raw-command entrypoint that backend renderer manifests
# execute (it dispatches by the transport-selected qualified backend id).
_ALLOWED_FILES: dict[str, str] = {
    "astrid/core/rendering/remotion_runtime.py": (
        "Remotion runtime owner: lazily imports the concrete remotion "
        "renderer's lock and run modules because it IS the server-owned "
        "Remotion runtime (project discovery, render lock, frame capture), "
        "not a backend-neutral caller"
    ),
    "astrid/core/rendering/_effect_registry.py": (
        "product-owned registry generator: settles outputs under the shared Remotion lock "
        "and returns the inherited lock receipt"
    ),
    "astrid/packs/rendering/run.py": (
        "pack raw-command launcher: entrypoint that backend renderer "
        "manifests execute; dispatches to the transport-selected backend"
    ),
    "astrid/packs/rendering/actions/render/run.py": (
        "render facade: neutral adapter that delegates all dispatch to "
        "RenderService"
    ),
    "astrid/packs/video_editing/orchestrators/hype/steps.py": (
        "hype render step: orchestrator child invocation of the neutral "
        "render facade (the gateway `executors run` family was retired with "
        "the task-mode runtime; pack orchestrators invoke capability run "
        "modules directly under ASTRID_INTERNAL_INVOCATION, same as the "
        "editorial children). Reaches only the facade, never a backend."
    ),
    "astrid/sdk/host_bootstrap.py": (
        "host bootstrap probe: names the render action module as a string in "
        "the importlib.util.find_spec probe table; it only locates the module "
        "to bind the installed product root and never imports or spawns it"
    ),
    "astrid/packs/rendering/actions/timeline_visualize/composed_frame.py": (
        "timeline_visualize composed-frame capture: lazily imports the "
        "remotion renderer's frame-capture entrypoints "
        "(capture_remotion_frames, PersistentRemotionFrameSession); "
        "there is no registry-level frame-capture API, and this is the only "
        "action that needs per-frame Remotion stills"
    ),
    "astrid/packs/rendering/actions/render/__init__.py": (
        "render facade package marker"
    ),
    "astrid/packs/rendering/actions/render/audio_reactive_colour.py": (
        "render facade compatibility alias: keeps the historical module path "
        "for the FFmpeg backend specialization"
    ),
}

# Manifests (executor / renderer declarations) are data, not code: their argv
# and metadata declare the facade runtime module and neutral selectors.
_MANIFEST_SUFFIXES = {".yaml", ".yml", ".json"}


# ---------------------------------------------------------------------------
# Machinery
# ---------------------------------------------------------------------------


def _run_grep(pattern: str, globs: tuple[str, ...]) -> list[str]:
    """Run ripgrep over ``astrid/`` and return matching ``path:line:...`` lines.

    Falls back to a Python regex scan when ripgrep is unavailable. An exit
    code of 1 (no matches) is not an error.
    """
    try:
        result = subprocess.run(
            ["rg", "--no-heading", "-n", *globs, pattern, str(ASTRID_DIR)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        # macOS ships BSD grep, which does not implement ``-P``. Keep this
        # source-topology gate working on clean hosts without ripgrep by
        # applying the same regex to the requested file suffixes in Python.
        suffixes = {glob[1:] for glob in globs if glob.startswith("*")}
        expression = re.compile(pattern)
        matches = []
        for path in sorted(ASTRID_DIR.rglob("*")):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            matches.extend(
                f"{path}:{line_number}:{line}"
                for line_number, line in enumerate(lines, start=1)
                if expression.search(line)
            )
        return matches
    if result.returncode not in (0, 1):
        raise RuntimeError(f"source scan failed: {result.stderr}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def _rel_path(match: str) -> str:
    """Return the repo-relative source path for a grep match line."""
    raw = match.split(":", 1)[0]
    path = Path(raw).resolve()
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _allowed(match: str, *, manifests_ok: bool) -> bool:
    """Whether a match line is covered by the allowlist."""
    rel = _rel_path(match)
    if rel.startswith(_BACKEND_IMPL_PREFIX):
        return True
    if rel in _ALLOWED_FILES:
        return True
    if manifests_ok and Path(rel).suffix in _MANIFEST_SUFFIXES:
        return True
    return False


def _assert_no_violations(matches: list[str], *, manifests_ok: bool, label: str) -> None:
    violations = [m for m in matches if not _allowed(m, manifests_ok=manifests_ok)]
    assert not violations, (
        f"{label} found outside the allowlist ({len(violations)}):\n"
        + "\n".join(violations)
        + "\n\nAllowlisted files:\n"
        + "\n".join(f"  {path} — {reason}" for path, reason in _ALLOWED_FILES.items())
        + f"\n  {_BACKEND_IMPL_PREFIX}* — backend implementations"
        + ("\n  *.yaml/*.yml/*.json — manifests" if manifests_ok else "")
    )


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_backend_import_pattern_still_observes_known_importers() -> None:
    """Sanity: the backend-import grep must observe the known importer set.

    Guards against regex rot that would make the allowlist pass vacuously.
    """
    matches = _run_grep(_BACKEND_IMPORT, _PY_GLOBS)
    paths = {_rel_path(m) for m in matches}

    assert "astrid/packs/rendering/actions/render/audio_reactive_colour.py" in paths
    assert "astrid/packs/rendering/run.py" in paths
    assert any(p.startswith(_BACKEND_IMPL_PREFIX) for p in paths)


def test_grep_fallback_scans_source_when_ripgrep_is_unavailable(monkeypatch) -> None:
    """The clean-host fallback must not depend on GNU grep's unsupported -P flag."""

    def missing_ripgrep(*args, **kwargs):
        raise FileNotFoundError("rg")

    monkeypatch.setattr(subprocess, "run", missing_ripgrep)
    matches = _run_grep(_BACKEND_IMPORT, _PY_GLOBS)
    paths = {_rel_path(match) for match in matches}

    assert "astrid/packs/rendering/actions/render/audio_reactive_colour.py" in paths
    assert "astrid/packs/rendering/run.py" in paths


def test_no_production_backend_imports_outside_allowlist() -> None:
    """No production module imports ``astrid.packs.rendering.rendering.renderers.*``."""
    matches = _run_grep(_BACKEND_IMPORT, _PY_GLOBS)
    assert matches, "backend-import grep found nothing; pattern may be broken"
    _assert_no_violations(matches, manifests_ok=False, label="backend imports")


def test_no_production_legacy_engine_imports_outside_allowlist() -> None:
    """No production module imports the legacy engine module."""
    matches = _run_grep(_LEGACY_ENGINE_IMPORT, _PY_GLOBS)
    _assert_no_violations(
        matches, manifests_ok=False, label="legacy engine imports"
    )


def test_no_production_render_run_references_outside_allowlist() -> None:
    """The facade runtime module is referenced only by manifests and itself.

    Production code must not spawn ``python -m
    astrid.packs.rendering.actions.render.run`` or import the facade
    entrypoint directly: dispatch goes through the executor registry /
    ``RenderService``.
    """
    matches = _run_grep(_RENDER_RUN_REF, _SPAWN_GLOBS)
    assert matches, "render-run grep found nothing; pattern may be broken"
    _assert_no_violations(matches, manifests_ok=True, label="render.run references")
