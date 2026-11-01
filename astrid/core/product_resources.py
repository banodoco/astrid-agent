"""Locate installer-provided and packaged product resources."""
from pathlib import Path

_PACKAGE_RESOURCES = Path(__file__).resolve().parents[1] / "resources"


def product_resource(name: str, *, installed: Path | None = None, source: Path | None = None) -> Path:
    """Prefer the installer's copy, then wheel data, then a source checkout."""
    candidates = [installed, _PACKAGE_RESOURCES / name, source]
    for path in candidates:
        if path is not None and path.is_file():
            return path
    raise FileNotFoundError(f"Astrid resource {name!r} is missing; reinstall or update Astrid")
