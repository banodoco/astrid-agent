"""Pack-manifest loading/parsing, discovery, and the packs root."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata as importlib_metadata
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from astrid.core.env_vars import ASTRID_PACKS_PATH
from astrid.core.pack._common import (
    PackValidationError,
    _optional_string,
)
from astrid.core.pack.definition import PackDefinition
from astrid.core.pack.canonical import (
    CanonicalPackValidationError,
    LEGACY_MANIFEST_NAMES,
    canonical_manifest_path,
    validate_canonical_pack,
)
from astrid.core.pack.permissions import (
    _normalize_pack_permissions,
    _optional_pack_extensions,
)
from astrid import pack_api
from astrid.pack_api import VERSION as PACK_API_VERSION
from astrid.pack_imports import ImportContract, check_pack_imports
from astrid.pack_validation import validate_pack as validate_pack_compatibility

_LOGGER = logging.getLogger(__name__)

PACK_VALIDATOR_COMMAND = "python3 -m astrid.core.pack.cli validate"

# Source admission remains strict.  These constants describe the small
# diagnostic scan used to report optional invalid packs; they are not an
# alternate admission path and do not let a required capability through.
FAIL_CLOSED_PACK_FOLDERS: frozenset[str] = frozenset({"_core"})
REQUIRED_PACK_IDS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class QuarantinedPack:
    """One optional pack omitted because canonical admission failed."""

    pack_id: str
    pack_dir: Path
    manifest_path: Path | None
    error: str

    @property
    def fix(self) -> str:
        return f"run the pack validator: {PACK_VALIDATOR_COMMAND} {self.pack_dir}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "state": "quarantined",
            "pack_dir": str(self.pack_dir),
            "manifest_path": str(self.manifest_path) if self.manifest_path is not None else None,
            "error": self.error,
            "fix": self.fix,
        }


@dataclass(frozen=True)
class PackScan:
    """An observational pack scan, including optional invalid-pack records."""

    packs: tuple[PackDefinition, ...]
    quarantined: tuple[QuarantinedPack, ...]


_LOGGED_QUARANTINES: set[tuple[str, str]] = set()


def _fail_closed(folder_name: str, required_pack_ids: frozenset[str]) -> bool:
    return folder_name in FAIL_CLOSED_PACK_FOLDERS or folder_name in required_pack_ids


def _quarantine_warning(record: QuarantinedPack) -> None:
    key = (str(record.pack_dir), record.error)
    if key in _LOGGED_QUARANTINES:
        return
    _LOGGED_QUARANTINES.add(key)
    _LOGGER.warning(
        "quarantined optional pack %r: %s; capabilities are unavailable until it is fixed (%s)",
        record.pack_id,
        record.error,
        record.fix,
    )


def packs_root() -> Path:
    return Path(__file__).resolve().parents[2] / "packs"


DEFAULT_PACKS_ROOT = packs_root()


def scan_packs(
    root: str | Path | None = None,
    *,
    include_hidden: bool = False,
    strict: bool = True,
    required_pack_ids: frozenset[str] = REQUIRED_PACK_IDS,
    _validate: bool = True,
) -> PackScan:
    """Scan a pack root without importing pack code.

    Strict scans are the only source-admission path and raise on every invalid
    manifest.  A non-strict scan is observational: invalid optional packs are
    quarantined individually so doctor/discovery reporting can retain valid
    neighbours.  Required and runtime-owned folders remain fail-closed in both
    modes.
    """
    if root is None:
        from astrid.core.pack.source_setup import active_source_inventory
        inventory = active_source_inventory()
        sources = inventory.sources if inventory is not None else ()
        roots = (*_default_pack_roots(), *(source.pack_root for source in sources))
        return scan_pack_roots(roots, include_hidden=include_hidden, strict=strict,
                               required_pack_ids=required_pack_ids, _validate=_validate,
                               source_records={source.pack_id: source for source in sources})
    source_root = Path(root)
    if not source_root.is_dir():
        return PackScan((), ())
    packs: list[PackDefinition] = []
    quarantined: list[QuarantinedPack] = []
    seen: dict[str, Path] = {}
    children = ((source_root,) if (source_root / "pack.yaml").is_file()
                else sorted(source_root.iterdir(), key=lambda path: path.name))
    for child in children:
        if not child.is_dir() or child.name.startswith(".") or child.name == "__pycache__":
            continue
        manifest_path: Path | None = None
        try:
            manifest_path = pack_manifest_path(child)
            if manifest_path is None:
                continue
            pack = load_pack_manifest(manifest_path)
        except Exception as exc:  # noqa: BLE001 - optional scan boundary
            legacy_version = _legacy_schema_refusal(manifest_path or (child / "pack.yaml"))
            if legacy_version is not None and not _fail_closed(child.name, required_pack_ids):
                reason = (
                    "Pack manifest needs an explicit schema upgrade. Migrate to v3 and add it again."
                    if legacy_version in {1, 2} else
                    "Pack manifest schema is not supported. Use a supported pack schema version."
                )
                record = QuarantinedPack(
                    pack_id=child.name, pack_dir=child,
                    manifest_path=(child / "pack.yaml") if (child / "pack.yaml").is_file() else None,
                    error=reason,
                )
                quarantined.append(record)
                _quarantine_warning(record)
                continue
            if strict or _fail_closed(child.name, required_pack_ids):
                if isinstance(exc, PackValidationError):
                    raise
                raise PackValidationError(f"{child}: {exc}") from exc
            record = QuarantinedPack(
                pack_id=child.name,
                pack_dir=child,
                manifest_path=(child / "pack.yaml") if (child / "pack.yaml").is_file() else None,
                error=" ".join(str(exc).split()),
            )
            quarantined.append(record)
            _quarantine_warning(record)
            continue
        if pack.visibility == "hidden" and not include_hidden:
            continue
        if pack.id in seen:
            raise PackValidationError(f"duplicate pack id {pack.id!r}: {seen[pack.id]} and {manifest_path}")
        seen[pack.id] = manifest_path
        packs.append(pack)
    if _validate:
        packs, compatibility_quarantine = _isolate_incompatible_packs(packs, required_pack_ids=required_pack_ids)
        quarantined.extend(compatibility_quarantine)
    return PackScan(tuple(packs), tuple(quarantined))



def scan_pack_roots(
    roots: tuple[str | Path, ...], *, include_hidden: bool = False,
    strict: bool = False, required_pack_ids: frozenset[str] = REQUIRED_PACK_IDS,
    _validate: bool = True, source_records: dict[str, Any] | None = None,
) -> PackScan:
    """Collect and collision-check all roots before resolving dependencies."""
    if source_records is None:
        from astrid.core.pack.source_setup import active_source_inventory
        inventory = active_source_inventory()
        source_records = {source.pack_id: source for source in inventory.sources} if inventory else {}
    scans = [scan_packs(path, include_hidden=include_hidden, strict=strict,
                        required_pack_ids=required_pack_ids, _validate=False)
             for path in dict.fromkeys(Path(root).expanduser().resolve() for root in roots)]
    packs = list({pack.root.resolve(): pack for scan in scans for pack in scan.packs}.values())
    quarantined = [record for scan in scans for record in scan.quarantined]
    if _validate:
        packs, rejected = _isolate_incompatible_packs(
            packs, required_pack_ids=required_pack_ids, source_records=source_records)
        quarantined.extend(rejected)
    return PackScan(tuple(packs), tuple(quarantined))

def _legacy_schema_refusal(manifest_path: Path | None) -> int | None:
    if manifest_path is None:
        return None
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    version = raw.get("schema_version")
    if type(version) is int and version != 3:
        return version
    return None


def _isolate_incompatible_packs(
    packs: list[PackDefinition],
    *,
    required_pack_ids: frozenset[str] = REQUIRED_PACK_IDS,
    source_records: dict[str, Any] | None = None,
) -> tuple[list[PackDefinition], list[QuarantinedPack]]:
    """Apply the shared pure compatibility validator to external pack inventory."""
    seen: dict[str, Path] = {}
    for pack in packs:
        root = pack.root.resolve()
        if pack.id in seen and seen[pack.id] != root:
            raise PackValidationError(f"duplicate pack id {pack.id!r}: {seen[pack.id]} and {root}")
        seen[pack.id] = root
    executable: list[PackDefinition] = []
    quarantined: list[QuarantinedPack] = []
    for pack in packs:
        if pack.schema_version != "3":
            record = QuarantinedPack(
                pack_id=pack.id, pack_dir=pack.root, manifest_path=pack.manifest_path,
                error="Pack manifest needs an explicit schema upgrade. Migrate to v3 and add it again.",
            )
            if _fail_closed(pack.id, required_pack_ids):
                raise PackValidationError(record.error)
            quarantined.append(record)
            _quarantine_warning(record)
        else:
            executable.append(pack)
    packs = executable
    bundled_root = packs_root().resolve()
    external = [pack for pack in packs if not pack.root.resolve().is_relative_to(bundled_root)]
    external_ids = {pack.id for pack in external}
    if not external:
        return packs, quarantined
    python_packages = {
        dist.metadata.get("Name", "").lower().replace("_", "-").replace(".", "-"): dist.version
        for dist in importlib_metadata.distributions()
        if dist.metadata.get("Name")
    }
    # The existing managed-source policy is the only default pin authority on
    # this base; the release-manifest packs mechanism is a later integration.
    from astrid.core.pack.source_setup import declarations_from_json
    release_pins = {item.pack_id: {"commit": item.revision, "digest": item.tree_sha256 or ""}
                    for item in declarations_from_json()}
    from astrid import sdk
    contract = ImportContract(
        facade_exports=frozenset(pack_api.__all__), facade_version=PACK_API_VERSION,
        sdk_names=frozenset(sdk.__all__),
        core_pack_apis={pack.id: f"astrid.packs.{pack.id}.api" for pack in packs
                        if pack.root.resolve().is_relative_to(bundled_root)
                        and (pack.root / "api.py").is_file()},
    )
    source_records = source_records or {}
    active = packs
    while True:
        selected = {
            pack.id: {
                "id": pack.id,
                "canonical_root": str(pack.root.resolve()),
                "schema_version": int(pack.schema_version),
                "pack_api": pack.pack_api or "",
                "dependencies": pack.dependencies,
                "commit": source_records[pack.id].revision if pack.id in source_records and source_records[pack.id].pack_root.resolve() == pack.root.resolve() else "",
                "installed_digest": source_records[pack.id].tree_sha256 if pack.id in source_records and source_records[pack.id].pack_root.resolve() == pack.root.resolve() else "",
            }
            for pack in active
        }
        rejected: dict[str, QuarantinedPack] = {}
        for pack in active:
            if pack.id not in external_ids:
                continue
            manifest = pack.to_dict()
            manifest["schema_version"] = int(pack.schema_version)
            result = validate_pack_compatibility(
                mode="inventory", manifest=manifest, candidate=selected[pack.id],
                selected=selected, core_api=PACK_API_VERSION, release_pins=release_pins,
                python_packages=python_packages,
                import_violations=check_pack_imports(pack.root, contract=contract, manifest=manifest),
            )
            if not result["ok"]:
                reason = f"{result['reason']} {result['fix']}"
                rejected[pack.id] = QuarantinedPack(
                    pack_id=pack.id, pack_dir=pack.root, manifest_path=pack.manifest_path,
                    error=" ".join(reason.split()),
                )
        if not rejected:
            return active, quarantined
        for record in rejected.values():
            if _fail_closed(record.pack_id, required_pack_ids):
                raise PackValidationError(record.error)
            quarantined.append(record)
            _quarantine_warning(record)
        active = [pack for pack in active if pack.id not in rejected]


def discover_packs(
    root: str | Path | None = None,
    *,
    include_hidden: bool = False,
) -> tuple[PackDefinition, ...]:
    """Return packs admitted by the canonical v3 source parser.

    Optional schema/compatibility failures are quarantined individually by
    :func:`scan_packs`; malformed required manifests remain fail-closed.
    """
    return scan_packs(root, include_hidden=include_hidden, strict=False).packs


def _raw_declared_capabilities(manifest_path: Path | None) -> tuple[str, ...]:
    """Read only declared labels from a failed manifest for diagnostics."""
    if manifest_path is None:
        return ()
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ()
    labels = raw.get("capabilities") if isinstance(raw, dict) else None
    if not isinstance(labels, list):
        return ()
    return tuple(str(label) for label in labels if isinstance(label, str) and label.strip())


def quarantined_pack_for_capability(
    capability_id: str,
    roots: tuple[str | Path, ...] | None = None,
) -> QuarantinedPack | None:
    """Find an optional quarantined pack that declared *capability_id*.

    This is a not-found diagnostic only.  It never admits a broken manifest
    and never changes the strict registry result.
    """
    scan_roots = roots if roots is not None else _default_pack_roots()
    for record in scan_pack_roots(scan_roots).quarantined:
        if capability_id.startswith(f"{record.pack_id}."):
            return record
        labels = _raw_declared_capabilities(record.manifest_path)
        if capability_id in labels or any(
            capability_id == f"{record.pack_id}.{label}" for label in labels
        ):
            return record
    return None


def _default_pack_roots() -> tuple[Path, ...]:
    env_roots = tuple(
        Path(item).expanduser()
        for item in os.environ.get(ASTRID_PACKS_PATH, "").split(os.pathsep)
        if item
    )
    return tuple(dict.fromkeys(path.resolve() for path in (packs_root(), Path.home() / "Astrid" / "packs", *env_roots)))


def pack_quarantine_report(
    roots: tuple[str | Path, ...] | None = None,
) -> list[dict[str, Any]]:
    """Return read-only JSON-shaped optional-pack quarantine records."""
    scan_roots = roots if roots is not None else _default_pack_roots()
    return [record.to_dict() for record in scan_pack_roots(scan_roots).quarantined]


def pack_quarantine_section() -> dict[str, Any]:
    """Return a doctor-ready quarantine section without raising on bad options."""
    try:
        records = pack_quarantine_report()
    except PackValidationError as exc:
        return {"count": 0, "quarantined": [], "fail_closed_error": str(exc)}
    return {"count": len(records), "quarantined": records, "fail_closed_error": None}


def load_pack_manifest(path: str | Path, *, expected_pack_id: str | None = None) -> PackDefinition:
    """Load one admitted v2/v3 pack through the canonical parser."""
    manifest_path = Path(path).expanduser().resolve()
    if manifest_path.name != "pack.yaml" or not manifest_path.is_file():
        raise PackValidationError(
            f"canonical pack admission requires a regular pack.yaml, got {manifest_path}"
        )
    try:
        entry = validate_canonical_pack(
            manifest_path.parent, expected_pack_id=expected_pack_id
        )
    except CanonicalPackValidationError as exc:
        raise PackValidationError(str(exc)) from exc
    definition = entry.definition
    data = definition.to_dict()
    taxonomy = pack_taxonomy_from_manifest(data, status=definition.status)
    return PackDefinition(
        id=definition.id,
        name=definition.name,
        version=definition.version,
        root=entry.root,
        manifest_path=manifest_path,
        metadata={},
        description=definition.description,
        content=dict(definition.content),
        agent=dict(data["agent"]),
        status=definition.status,
        visibility=definition.visibility,
        schema_version=str(definition.schema_version),
        aliases=tuple(dict(alias) for alias in data["aliases"]),
        permissions=_normalize_pack_permissions(data["permissions"]),
        extensions=_optional_pack_extensions(data.get("extensions", {}), path="pack.extensions"),
        actions=dict(data.get("actions", {})),
        ui=dict(data.get("ui", {})),
        rendering=dict(data.get("rendering", {})),
        documents=dict(data.get("documents", {})),
        documentation=dict(data.get("documentation", {})),
        pack_api=data.get("pack_api"),
        dependencies=dict(data.get("dependencies", {})),
        cli=dict(data.get("cli", {})),
        engines=dict(data.get("engines", {})),
        **taxonomy,
    )


def pack_taxonomy_from_manifest(data: dict[str, Any], *, status: str) -> dict[str, str]:
    """Return the deterministic taxonomy projection for a pack manifest.

    These defaults are the taxonomy baseline for manifests that do not yet
    declare an explicit taxonomy block.
    """
    return {
        "origin": _optional_string(data, "origin", "pack.origin", default="unknown"),
        "install_tier": _optional_string(data, "install_tier", "pack.install_tier", default="default"),
        "pack_type": _optional_string(data, "pack_type", "pack.pack_type", default="capability"),
        "domain": _optional_string(data, "domain", "pack.domain", default="general"),
        "stability": _optional_string(
            data,
            "stability",
            "pack.stability",
            default=_default_stability_for_status(status),
        ),
        "support": _optional_string(data, "support", "pack.support", default="project"),
    }


def _default_stability_for_status(status: str) -> str:
    if status == "experimental":
        return "experimental"
    if status == "deprecated":
        return "deprecated"
    return "stable"


def pack_manifest_path(root: str | Path) -> Path | None:
    pack_root = Path(root)
    legacy = sorted(name for name in LEGACY_MANIFEST_NAMES if (pack_root / name).exists())
    if legacy and not (pack_root / "pack.yaml").exists():
        raise PackValidationError(
            f"canonical pack admission requires pack.yaml; found alternate manifest(s): {', '.join(legacy)}"
        )
    try:
        return canonical_manifest_path(pack_root)
    except CanonicalPackValidationError as exc:
        raise PackValidationError(str(exc)) from exc


def _load_manifest_payload(path: Path) -> dict[str, Any]:
    """Read a YAML mapping for internal static-inspection helpers.

    Pack admission remains strict in :func:`load_pack_manifest`; this helper
    also reads non-pack catalogs such as ``models.yaml`` and renderer
    manifests while building the capability ledger.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PackValidationError(f"manifest not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise PackValidationError(f"invalid YAML manifest {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PackValidationError(f"manifest must contain a YAML object: {path}")
    return data
