"""The core/pack boundary ratchet is enforced by the suite, not only by a manual run."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_core_pack_boundary_ratchet_holds() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_core_pack_boundary.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_core_vocabulary_is_exactly_the_declared_union() -> None:
    from astrid.core.generation.placement import BUILTIN_KINDS
    from astrid.omp_agent import _PACK_ROUTE_BLOCKLIST
    from scripts.check_core_pack_boundary import _core_vocabulary

    assert _core_vocabulary() == set(BUILTIN_KINDS) | set(_PACK_ROUTE_BLOCKLIST)
    assert "unrelated_pack" not in _core_vocabulary()


def test_only_core_vocabulary_pack_literals_are_exempt() -> None:
    from scripts.check_core_pack_boundary import _core_vocabulary, scan_source

    findings = scan_source(
        "astrid/core/example.py", 'a = "local"\nb = "setup"\nc = "unrelated_pack"',
        {"local", "setup", "unrelated_pack"}, set(), _core_vocabulary(),
    )
    assert findings == {("astrid/core/example.py", "pack_name_literal", "unrelated_pack"): 1}


def test_vocabulary_does_not_exempt_other_finding_kinds() -> None:
    from scripts.check_core_pack_boundary import _core_vocabulary, scan_source

    findings = scan_source(
        "astrid/core/example.py",
        'import astrid.packs.local\na = "astrid.packs.local.module"\n'
        'b = "astrid/packs/local/file"\nc = "local.action"',
        {"local"}, {"local.action"}, _core_vocabulary(),
    )
    assert findings == {
        ("astrid/core/example.py", "pack_import", "astrid.packs.local"): 1,
        ("astrid/core/example.py", "dynamic_pack_import", "astrid.packs.local.module"): 1,
        ("astrid/core/example.py", "repo_pack_path", "astrid/packs/local/file"): 1,
        ("astrid/core/example.py", "capability_id", "local.action"): 1,
    }


def test_core_vocabulary_declarations_are_pinned() -> None:
    """Each declaration the exemption reads is pinned on its own, so widening either one is a visible change.

    A word added to ``BUILTIN_KINDS`` or ``_PACK_ROUTE_BLOCKLIST`` stops the boundary scanner from reporting
    that word as a pack dependency. Changing either set must therefore be deliberate: update the fingerprint
    here in the same change, and say in its review that the boundary exemption widens or narrows.
    """
    import hashlib

    from astrid.core.generation.placement import BUILTIN_KINDS
    from astrid.omp_agent import _PACK_ROUTE_BLOCKLIST

    def fingerprint(words) -> str:
        return hashlib.sha256("\n".join(sorted(words)).encode()).hexdigest()

    assert fingerprint(BUILTIN_KINDS) == "65e5abdff97f7c7a4bcfdd216d9fcfd7a46acc50adf8d016e0bce7aa42279199"
    assert fingerprint(_PACK_ROUTE_BLOCKLIST) == "1ffba8a7de096d6b59e16647ed6a95f71c3f3df514e54ea03c534e76640ba894"
