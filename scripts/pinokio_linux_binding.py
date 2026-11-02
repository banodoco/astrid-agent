"""Capture the immutable portion of a Linux M4-4a runner binding.

This deliberately does not produce a dispatchable executor config. Runtime
sessions, an action engine and real refusal controls must first be observed on
the runner by their respective providers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from . import ci_journey, pinokio_backend_executor as backend
from .pinokio_journey_adapter import CONFIG_SHA256, TUPLE

BOOTSTRAP_SHA256 = {
    "public/install": "100bab1deb15d620abd910c8fb0c9a6d77f4945428a3229dac6b506705e3fd0d",
    "public/install.sh": "591c4c51b148fccef2a7d1c45782d9ff949d5fd03d46cd82b5591e3a23d1b285",
    "public/install.ps1": "bba3f2edc41c047cb51475ceeac4489a04610af73a3af44bdc05287964d28258",
}


def _replace_token(value: Any, path: str) -> Any:
    if isinstance(value, str):
        return value.replace("__M4_4A_PINOKIO_CHECKOUT__", path)
    if isinstance(value, list):
        return [_replace_token(item, path) for item in value]
    if isinstance(value, dict):
        return {key: _replace_token(item, path) for key, item in value.items()}
    return value


def capture(template_path: Path, checkouts: Mapping[str, Path]) -> dict[str, Any]:
    """Verify frozen sources and produce an explicitly incomplete checkpoint."""
    if not template_path.is_absolute() or template_path.is_symlink() or not template_path.is_file():
        raise ValueError("template must be an absolute non-symlink file")
    template_bytes = template_path.read_bytes()
    if hashlib.sha256(template_bytes).hexdigest() != CONFIG_SHA256:
        raise ValueError("template SHA-256 mismatch")
    config = json.loads(template_bytes)["linux"]
    if (config.get("immutable_tuple") != TUPLE or config.get("source_override") is not False
            or config.get("source_override_test_mode") is not False):
        raise ValueError("tuple or source override policy differs")
    if set(checkouts) != set(TUPLE):
        raise ValueError("exactly four candidate checkouts are required")
    inventory = config["candidate_checkouts"]
    observed = {}
    for name, ref in TUPLE.items():
        path = checkouts[name]
        if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
            raise ValueError(f"{name} checkout must be absolute and non-symlink")
        entry = inventory[name]
        if entry["ref"] != ref:
            raise ValueError(f"{name} template ref differs")
        observed[name] = backend._validate_checkout(name, {**entry, "path": str(path)})
    if len({item["path"] for item in observed.values()}) != 4:
        raise ValueError("candidate checkout paths must be distinct")
    app = Path(observed["app"]["path"])
    bootstraps = {}
    for relative, expected_hash in BOOTSTRAP_SHA256.items():
        path = app / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"bootstrap {relative} is absent or symlinked")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected_hash:
            raise ValueError(f"bootstrap {relative} SHA-256 mismatch")
        bootstraps[relative] = digest
    pinokio = Path(observed["pinokio"]["path"])
    old = ci_journey.validate_old_launcher_provenance(pinokio, config["old_launcher"]["ref"])
    actions = {}
    for name, ref in (("first", old["ref"]), ("retry", TUPLE["pinokio"])):
        actual = ci_journey.build_pinokio_action(pinokio, ref, app_directory=pinokio)["run"]
        expected = _replace_token(config["actions"][name]["steps"], str(pinokio))
        if actual != expected:
            raise ValueError(f"{name} action differs from the immutable builder")
        actions[name] = actual
    return {
        "kind": "m4-4a-linux-binding-checkpoint.v1",
        "dispatchable": False,
        "source_override": False,
        "source_override_test_mode": False,
        "template_sha256": CONFIG_SHA256,
        "candidate_checkouts": observed,
        "bootstrap_sha256": bootstraps,
        "old_launcher": old,
        "actions": actions,
        "missing": ["runner-owned provider executable and spec", "observed runtime sessions",
                    "real action settlement", "busy and unknown refusal and recovery"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    for name in TUPLE:
        parser.add_argument(f"--{name}-checkout", type=Path, required=True)
    args = parser.parse_args()
    result = capture(args.template, {name: getattr(args, f"{name}_checkout") for name in TUPLE})
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
