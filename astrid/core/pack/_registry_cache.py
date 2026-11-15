"""Content identity for process-local registry projections.

Reuse discovery's complete admission fingerprint, including managed-source
verification, rather than inventing a weaker filesystem freshness contract.
No inventory or fingerprint is itself cached. Custom discovery seams cannot
be fingerprinted and must continue through their ordinary loader.
"""
from pathlib import Path


def discovery_key(project_root, extra_pack_roots, scan):
    from astrid.core.pack.discovery import _discovery_fingerprint
    from astrid.core.pack.personal import resolve_personal_packs_root
    from astrid.core.pack.source_setup import active_source_inventory

    personal = resolve_personal_packs_root()
    if personal is not None:
        personal = Path(personal).expanduser().resolve(strict=False)
    return _discovery_fingerprint(
        project_root, extra_pack_roots, active_source_inventory(), scan,
        personal_packs_root=personal,
    )
