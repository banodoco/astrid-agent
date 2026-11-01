"""Explicit Pinokio journey adapter; host preflight and driver remain caller-owned.

The fully-local executor is a provisional local-process smoke, not a product
lifecycle or a receipt for the supplied candidate tuple. See its raw receipt.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from . import fully_local_executor, pinokio_backend_executor
from .shared_journey_driver import JourneyDriver

TUPLE = {
    "runtime": "1e6d899efa77a2d6ec247c38250f9748330bb587",
    "agent": "dd6b48192c720c5346e6f140184e313b2cf84f31",
    "app": "99144531aedab3ac8042b5d06f72bd5aaecf523f",
    "pinokio": "89ed7aacae72bb48eb97ff9ff95c222bdd7e71e4",
}
CONFIG_SHA256 = "37c2c7f06712eb8e7fad9c9a0ca7371a301ee4ee7da5995d326e1f42ee9b87f4"
BINDING_SHA256 = "1f075dc352b2ac8fce4c3ed3a2820be9c35cfbc218222295517f796ece01c9b5"
APP_MANIFEST_PATH = "public/install-manifest.json"
APP_MANIFEST_SHA256 = "15ea3a2e0a4067773b727635e4eaa4b894eceb10f73aaa88cd29896a2364bd77"
MODES = {"pinokio-backend", "pinokio-fully-local"}


class AdapterError(ValueError):
    """An explicit binding is missing, inconsistent, or unmaterialized."""


def _read_pinned(path: Path, digest: str, label: str) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise AdapterError(f"{label} must be an absolute, non-symlink file")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise AdapterError(f"{label} SHA-256 mismatch")
    return data


def _absolute(value: Any, label: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise AdapterError(f"{label} must be an explicit absolute path")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise AdapterError(f"{label} must be an absolute non-symlink path")
    return path.resolve()


def _no_tokens(value: Any) -> None:
    if isinstance(value, str) and "__M4_4A_" in value:
        raise AdapterError("backend config contains unmaterialized runner/provider tokens")
    if isinstance(value, Mapping):
        for child in value.values():
            _no_tokens(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _no_tokens(child)


def _verify_actions(config: Mapping[str, Any], pinokio: Path) -> None:
    # The caller still owns runner preflight; authenticate the action arrays at
    # the same source boundary used by the canonical driver before execution.
    from . import ci_journey

    old = config.get("old_launcher")
    if (not isinstance(old, Mapping)
            or old.get("ref") != pinokio_backend_executor.APPROVED_OLD_LAUNCHER_REF
            or old.get("sha256") != pinokio_backend_executor.APPROVED_OLD_UPDATE_SHA256
            or old.get("file") != "pinokio/update.js"
            or _absolute(old.get("checkout"), "old launcher checkout") != pinokio):
        raise AdapterError("old launcher is not bound to the approved source")
    ci_journey.validate_old_launcher_provenance(pinokio, old["ref"])
    actions = config.get("actions")
    if not isinstance(actions, Mapping):
        raise AdapterError("explicit first and retry actions are required")
    for name, ref in (("first", old["ref"]), ("retry", TUPLE["pinokio"])):
        action = actions.get(name)
        steps = ci_journey.build_pinokio_action(pinokio, ref, app_directory=pinokio)["run"]
        if not isinstance(action, Mapping) or action.get("steps") != steps:
            raise AdapterError(f"{name} action differs from immutable Pinokio action builder")


def _bind(config: Mapping[str, Any], template_path: Path, binding_path: Path,
          os_name: str, checkouts: Mapping[str, Any]) -> dict[str, Path]:
    if not isinstance(config, Mapping) or not config:
        raise AdapterError("explicit non-empty executor config is required")
    if os_name not in {"linux", "macos", "windows"}:
        raise AdapterError("explicit supported OS is required")
    template_bytes = _read_pinned(template_path, CONFIG_SHA256, "journey config template")
    _read_pinned(binding_path, BINDING_SHA256, "config binding")
    try:
        template = json.loads(template_bytes)[os_name]
    except (ValueError, KeyError, TypeError) as exc:
        raise AdapterError("invalid pinned journey config template") from exc
    if config.get("immutable_tuple") != TUPLE or template.get("immutable_tuple") != TUPLE:
        raise AdapterError("immutable tuple differs from the published tuple")
    try:
        manifest_digest = template["candidate_checkouts"]["app"]["files"][APP_MANIFEST_PATH]
    except (KeyError, TypeError) as exc:
        raise AdapterError("pinned App manifest inventory missing") from exc
    if manifest_digest != APP_MANIFEST_SHA256:
        raise AdapterError("pinned App manifest SHA-256 differs from published manifest")
    supplied = config.get("candidate_checkouts")
    if not isinstance(checkouts, Mapping) or set(checkouts) != set(TUPLE):
        raise AdapterError("explicit four-checkout path binding is required")
    if not isinstance(supplied, Mapping) or set(supplied) != set(TUPLE):
        raise AdapterError("config requires exactly four candidate checkouts")
    result = {}
    for name in TUPLE:
        item = supplied[name]
        expected = template["candidate_checkouts"][name]
        if not isinstance(item, Mapping) or item.get("ref") != TUPLE[name] or item.get("files") != expected["files"]:
            raise AdapterError(f"candidate {name} differs from pinned tuple/file inventory")
        actual = _absolute(item.get("path"), f"candidate {name}.path")
        if actual != _absolute(checkouts[name], f"checkout binding {name}"):
            raise AdapterError(f"candidate {name} path differs from caller binding")
        pinokio_backend_executor._validate_checkout(name, item)
        result[name] = actual
    if len(set(result.values())) != 4:
        raise AdapterError("candidate checkouts must have distinct paths")
    return result


def execute(mode: str, *, config: Mapping[str, Any], template_path: Path,
            binding_path: Path, os_name: str, checkouts: Mapping[str, Any],
            output: Path, local_checkout: Path | None = None,
            state_root: Path | None = None) -> dict[str, Any]:
    """Run one route with caller-bound inputs; return the unpromoted evidence.

    Backend output is the executor directory. Local output is its raw JSON
    receipt path. Local checkout/state are separately marker-attested disposable
    paths and never any of the frozen candidate checkouts.
    """
    if mode not in MODES:
        raise AdapterError("unsupported Pinokio mode")
    candidates = _bind(config, template_path, binding_path, os_name, checkouts)
    out = _absolute(output, "output")
    for frozen in candidates.values():
        if out == frozen or out in frozen.parents or frozen in out.parents:
            raise AdapterError("output must be disjoint from frozen checkouts")
    if mode == "pinokio-backend":
        if local_checkout is not None or state_root is not None:
            raise AdapterError("local paths are only valid for the fully-local route")
        if config.get("source_override_test_mode") is not False:
            raise AdapterError("backend must explicitly disable source overrides")
        platform = config.get("platform")
        if not isinstance(platform, Mapping) or platform.get("os") != os_name:
            raise AdapterError("backend platform OS differs from selected OS")
        # path_binding describes template tokens; it is metadata, not an executable input.
        _no_tokens({k: v for k, v in config.items() if k != "path_binding"})
        _verify_actions(config, candidates["pinokio"])
        composed = JourneyDriver.compose_pinokio_backend_config(config)
        receipt = JourneyDriver.execute_pinokio_backend(composed, out, pinokio_backend_executor)
        if platform.get("hosted") is True:
            pinokio_backend_executor.admit_hosted_receipt(receipt)
        elif receipt.get("evidence_class") == "hosted-lifecycle":
            raise AdapterError("non-hosted route returned hosted evidence")
        return receipt

    checkout = _absolute(local_checkout, "local checkout")
    state = _absolute(state_root, "local state root")
    for mutable in (checkout, state, out):
        for frozen in candidates.values():
            if mutable == frozen or mutable in frozen.parents or frozen in mutable.parents:
                raise AdapterError("local mutable paths must be disjoint from frozen checkouts")
    receipt = fully_local_executor.run_execution(checkout=checkout, state_root=state, output=out)
    fully_local_executor.validate_execution_receipt(receipt)
    if receipt.get("hosted_linux_receipt") is True or receipt.get("evidence_class") != "local_process":
        raise AdapterError("fully-local smoke cannot produce hosted evidence")
    return {"mode": mode, "evidence_class": "local_process", "bound_input_tuple": dict(TUPLE),
            "tuple_execution_proven": False, "hosted_lifecycle": False,
            "executor_receipt": receipt}
