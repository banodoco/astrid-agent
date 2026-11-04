#!/usr/bin/env python3
"""Compatibility CLI for the product-owned Remotion registry generator."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from astrid.core.rendering import _effect_registry as _generator  # noqa: E402

del TOOLS_DIR


class _GeneratorCLI(ModuleType):
    # Preserve imported script callers (including patchable private helpers).
    def __getattr__(self, name):
        return getattr(_generator, name)

    def __setattr__(self, name, value):
        if not name.startswith('__'):
            setattr(_generator, name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name):
        if not name.startswith('__'):
            delattr(_generator, name)
        else:
            super().__delattr__(name)


sys.modules[__name__].__class__ = _GeneratorCLI
if __name__ == '__main__':
    raise SystemExit(_generator.main())
