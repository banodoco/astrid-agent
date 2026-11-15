"""Pack IDs reserved by the product shipped in this installation."""

from functools import lru_cache
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from astrid.core.pack.canonical import ExternalPackSource
from astrid.core.pack.loader import packs_root


@lru_cache(maxsize=1)
def product_pack_ids() -> frozenset[str]:
    """Read shipped manifests without importing pack discovery."""
    core_manifest = Path(__file__).resolve().parents[1] / 'hivemind' / 'pack.yaml'
    manifests = [core_manifest, *packs_root().glob('*/pack.yaml')]
    ids = {ExternalPackSource.LOCAL.value}
    for manifest in manifests:
        if not manifest.is_file():
            continue
        data = yaml.safe_load(manifest.read_text(encoding='utf-8'))
        if isinstance(data, dict) and isinstance(data.get('id'), str):
            ids.add(data['id'])
    return frozenset(ids)
