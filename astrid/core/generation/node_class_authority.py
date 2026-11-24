"""Read-only proof of ComfyUI core class identity.

A cached schema is not evidence that an arbitrary custom node is a core
node. Both deferred target admission and attached-target advice share this
exact shipped authority; neither assumes an unknown class is core.
"""
from __future__ import annotations

from pathlib import Path
from collections.abc import Iterable


def core_node_classes(names: Iterable[str]) -> set[str]:
    """Return classes proven core by the shipped registry/index only."""
    import vibecomfy
    from vibecomfy.node_packs import CORE_COMFY_CLASSES
    from vibecomfy.schema.provider import ObjectInfoIndexSchemaProvider

    requested = set(names)
    proven = requested.intersection(CORE_COMFY_CLASSES)
    unresolved = requested - proven
    if not unresolved:
        return proven
    root = Path(vibecomfy.__file__).resolve().parent / 'porting' / 'cache' / 'object_info'
    provider = ObjectInfoIndexSchemaProvider(root)
    for name in sorted(unresolved):
        schema = provider.get_schema(name)
        if schema is None or schema.source_provider != 'object_info_index' or not schema.source_cache_path:
            continue
        pack_file = Path(schema.source_cache_path)
        if (pack_file.resolve().is_relative_to(root.resolve())
                and pack_file.name.startswith(('comfy_core@', 'comfy_extras@'))):
            proven.add(name)
    return proven
