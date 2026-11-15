"""Ratchets for hand-maintained RunPod contracts and provider boundaries."""

import ast
import builtins
import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
RUNPOD = ROOT / "astrid/packs/runpod"

# Existing imports are tracked by source file. Adding a file or increasing a
# count requires an explicit architecture review. provider.py is the sole
# allowed import boundary and is intentionally absent here.
PROVIDER_IMPORT_BASELINE = {
    "adapter.py": 1,
    "h3_release.py": 3,
    "shared/common.py": 2,
    "worker_owner.py": 1,
    "worker_preparation.py": 6,
}


def provider_import_counts(pack_root):
    forbidden = {"runpod_lifecycle.api", "httpx", "requests", "runpod"}
    counts = {}
    for path in sorted(pack_root.rglob("*.py")):
        if path.name == "provider.py":
            continue
        relative = path.relative_to(pack_root).as_posix()
        count = 0
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                count += sum(alias.name in forbidden for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                count += module in forbidden or any(
                    f"{module}.{alias.name}" in forbidden for alias in node.names
                )
        if count:
            counts[relative] = count
    return counts


def test_provider_imports_do_not_grow_past_committed_per_file_baseline():
    observed = provider_import_counts(RUNPOD)
    assert set(observed) <= set(PROVIDER_IMPORT_BASELINE), (
        f"new provider import files: {sorted(set(observed) - set(PROVIDER_IMPORT_BASELINE))}"
    )
    assert all(count <= PROVIDER_IMPORT_BASELINE[path] for path, count in observed.items()), (
        f"provider import counts exceed baseline: {observed}"
    )


def test_provider_import_ratchet_detects_a_new_violation_file(tmp_path):
    (tmp_path / "provider.py").write_text("import runpod\n", encoding="utf-8")
    (tmp_path / "new_module.py").write_text("import requests\n", encoding="utf-8")
    observed = provider_import_counts(tmp_path)
    assert "new_module.py" in observed
    assert set(observed) - set(PROVIDER_IMPORT_BASELINE) == {"new_module.py"}


def exception_subclass_names(pack_root):
    """Resolve Exception subclasses from source without importing provider code."""
    classes = {}
    exception_bases = set()
    for path in pack_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = {base.id if isinstance(base, ast.Name) else base.attr
                         for base in node.bases if isinstance(base, (ast.Name, ast.Attribute))}
                classes[node.name] = bases
                exception_bases.update(
                    base for base in bases
                    if base.endswith(("Error", "Exception", "Cancelled", "Refused"))
                )
    exception_names = exception_bases | {
        name for name in dir(builtins)
        if isinstance(getattr(builtins, name), type)
        and issubclass(getattr(builtins, name), BaseException)
    }
    changed = True
    while changed:
        changed = False
        for name, bases in classes.items():
            if name not in exception_names and bases & exception_names:
                exception_names.add(name)
                changed = True
    return {name for name in classes if name in exception_names}


# Deliberately excluded: these are exceptions raised only by infrastructure
# paths and are represented with the generic failure label in user receipts.
# Keep this empty unless a concrete, reviewed exception requires exclusion.
EXCEPTION_NAME_EXCLUSIONS = {
    # Pod-side control flow in engine_snapshot.py: caught inside its own CLI and reported
    # only as a fixed-vocabulary reason; never raised on the host or into a receipt.
    "SnapshotSkip": "engine_snapshot pod-side skip signal",
}


def test_every_runpod_exception_name_is_receipt_safe_or_explicitly_excluded():
    from astrid.packs.runpod.failure_diagnostics import EXCEPTIONS

    defined = exception_subclass_names(RUNPOD)
    assert not defined - set(EXCEPTIONS) - set(EXCEPTION_NAME_EXCLUSIONS), (
        f"unclassified RunPod exception class names: "
        f"{sorted(defined - set(EXCEPTIONS) - set(EXCEPTION_NAME_EXCLUSIONS))}"
    )
    assert not set(EXCEPTIONS) & set(EXCEPTION_NAME_EXCLUSIONS)


def test_known_receipt_exception_names_are_preserved():
    from astrid.packs.runpod.failure_diagnostics import host_summary
    from astrid.packs.runpod.machine import InstallationUnverified
    from astrid.packs.runpod.managed_sources import ManagedSourcesError
    from astrid.packs.runpod.pod_watchdog import PodWatchdogError, PodWatchdogTransportError
    from astrid.packs.runpod.provider import ProviderNotStarted
    from astrid.packs.runpod.remote_placement import InstallationReceiptChanged
    from astrid.packs.runpod.target_selection import CapacityWaitCancelled
    from astrid.packs.runpod.worker_staging import PreparationJournalError, WorkerPreparationError

    for error_type in (ManagedSourcesError, WorkerPreparationError,
                       PreparationJournalError, CapacityWaitCancelled,
                       InstallationReceiptChanged, InstallationUnverified,
                       PodWatchdogError, PodWatchdogTransportError, ProviderNotStarted):
        summary = host_summary(error_type("SENTINEL_TOKEN private exception detail"))
        assert summary["exception"] == error_type.__name__
        assert "SENTINEL_TOKEN" not in json.dumps(summary)


def test_runpod_host_target_operations_match_manifest_metadata():
    manifest = yaml.safe_load((RUNPOD / "pack.yaml").read_text(encoding="utf-8"))
    declared = {
        name for name, action in manifest["actions"].items()
        if action.get("metadata", {}).get("host_target_operation")
    }
    source = (RUNPOD / "adapter.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    operations = set()
    for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name == "operate"):
        for node in ast.walk(function):
            if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                    and node.left.id == "operation" and isinstance(node.ops[0], ast.NotIn)
                    and isinstance(node.comparators[0], (ast.Set, ast.Tuple, ast.List))):
                operations.update(item.value for item in node.comparators[0].elts if isinstance(item, ast.Constant))
    assert declared == operations
