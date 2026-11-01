#!/usr/bin/env bash
set -euo pipefail

# Installed-wheel smoke: the default-lane home for "the wheel we ship works when
# installed" (testing plan R-1).  It asserts, against the INSTALLED copy only:
#   - wheel identity: the venv holds exactly the wheel's files and version, and
#     Astrid imports from the venv, never from the checkout;
#   - every packaged pack manifest and the vendored workspace client are present
#     and byte-identical to the checkout's lock, imported from the venv;
#   - `--version`, `help` (family census) and `doctor --json` match the product
#     contract, read from the installed package rather than hard-coded here;
#   - two adversaries fail for the right reason (a missing resource, a lane that
#     leaks a checkout path), so a broken check cannot pass as a green one;
#   - the workspace is removed when the harness owns it, kept when you supply it.
#
# Two ways to run it:
#   bash scripts/smoke_wheel_install.sh
#       Builds one wheel from this checkout with the hashed build lock, installs
#       it with the hashed runtime lock into a private venv, then smokes it.
#       (`make wheel`; PYTHON_BIN picks the interpreter, for example python3.12.)
#   bash scripts/smoke_wheel_install.sh --wheel dist/astrid-X-py3-none-any.whl --venv /path/to/venv
#       Smokes a wheel and venv something else already built and installed (the
#       CI release job), without building a second wheel.  Both are verified,
#       not trusted: the installed files must match the wheel's RECORD hashes.
#
# SMOKE_WHEEL_WORKSPACE=/dir keeps the isolated workspace there (outside the
# checkout) instead of using and deleting a temporary one.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$REPO_ROOT"

exec "$PYTHON_BIN" - "$REPO_ROOT" "${SMOKE_WHEEL_WORKSPACE:-}" "$@" <<'PY'
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tomllib
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(repo_root))

from scripts.reshape.installed_artifact import InstalledArtifactHarness, build_once

parser = argparse.ArgumentParser(prog="smoke_wheel_install.sh")
parser.add_argument("--wheel", help="an already built astrid wheel (requires --venv)")
parser.add_argument("--venv", help="the venv that wheel is installed in (requires --wheel)")
options = parser.parse_args(sys.argv[3:])
if bool(options.wheel) != bool(options.venv):
    parser.error("--wheel and --venv go together: supply both or neither")
workspace = Path(sys.argv[2]).expanduser().resolve() if sys.argv[2] else None

print("=== installed Astrid wheel smoke ===")
print(f"repository: {repo_root}")
if workspace is not None:
    print(f"workspace: {workspace}")

supplied_wheel = Path(options.wheel).expanduser().resolve() if options.wheel else None
supplied_venv = Path(options.venv).expanduser().absolute() if options.venv else None
if supplied_wheel is not None:
    print(f"adopting wheel {supplied_wheel.name} and venv {supplied_venv}")
    harness = InstalledArtifactHarness.attach(
        repo_root, wheel=supplied_wheel, venv_dir=supplied_venv, workspace=workspace
    )
else:
    harness = build_once(
        repo_root,
        workspace=workspace,
        # A user-facing product check, so install the declared runtime
        # dependencies from the hashed lock.  The no-dependency harness use
        # remains the stricter dependency-leak proof.
        install_dependencies=True,
    )
owned_workspace = workspace is None
workspace_path = harness.workspace

# --- Facts read from the checkout: what the wheel was built from. -------------
pyproject_version = tomllib.loads((repo_root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
pack_manifests = sorted(
    path.relative_to(repo_root / "astrid").as_posix()
    for path in (repo_root / "astrid" / "packs").glob("*/pack.yaml")
)
vendor_lock = json.loads((repo_root / "runtime.lock.json").read_text("utf-8"))
spec = importlib.util.spec_from_file_location(
    "checkout_contract_metadata", repo_root / "astrid_runtime_client" / "contract_metadata.py"
)
checkout_contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkout_contract)
assert pack_manifests, "checkout has no astrid/packs/*/pack.yaml to compare against"
assert vendor_lock["files"], "runtime.lock.json lists no files"

# --- Lane programs, run by the INSTALLED interpreter. -------------------------
REGISTRY_LANE = """
import json
from astrid.core.cli.domain_product import (
    EXCLUDED_FROM_PRODUCT_CENSUS, PRODUCT_FAMILIES, REQUIRED_RUNTIME_MOUNTS)
print(json.dumps({
    "product_families": list(PRODUCT_FAMILIES),
    "excluded": sorted(EXCLUDED_FROM_PRODUCT_CENSUS),
    "mounts": {k: list(v) for k, v in REQUIRED_RUNTIME_MOUNTS.items()},
}))
"""

RESOURCE_LANE = """
import json, sys
from importlib import resources
required = json.loads(sys.argv[1])
root = resources.files("astrid")
missing = [name for name in required if not root.joinpath(*name.split("/")).is_file()]
assert not missing, missing
print("package resources: OK", len(required))
"""

CLIENT_LANE = """
import hashlib, json, sys, sysconfig
from pathlib import Path
import astrid_runtime_client as client
from astrid_runtime_client import contract_metadata as meta
lock_files = json.loads(sys.argv[1])
purelib = Path(sysconfig.get_path("purelib")).resolve()
package = Path(client.__file__).resolve().parent
assert package.parent == purelib, f"client imported from outside the venv site-packages: {package}"
for relative, expected in lock_files.items():
    installed = purelib / relative
    digest = "sha256:" + hashlib.sha256(installed.read_bytes()).hexdigest()
    assert digest == expected, f"{relative}: installed {digest} != lock {expected}"
print(json.dumps({"protocol": meta.PROTOCOL, "schema_digest": meta.SCHEMA_DIGEST,
                  "operations": len(meta.OPERATIONS)}))
print("vendored workspace client: OK")
"""

DOCTOR_LANE = """
import json, sys
from astrid.core.gateway import diagnostics as d
payload = json.loads(sys.argv[1])
exit_code = int(sys.argv[2])
report = payload["diagnostic"]
errors = d.validate_diagnostic(report)
assert not errors, errors
# Doctor prints one of two shapes: the observed report (the Runtime's own health document plus the
# C2 diagnostic; "ok" appears only when the Runtime supplied it) or the fail-closed error payload
# (always "ok": false).  The contract is therefore carried by the exit code and the diagnostic.
healthy = report["problemCode"] is None
assert healthy is (exit_code == 0), (report["problemCode"], exit_code)
if "ok" in payload:
    assert payload["ok"] is healthy, (payload["ok"], report["problemCode"])
if not healthy:
    assert payload.get("problem_code", report["problemCode"]) == report["problemCode"] in d.PROBLEM_CODES, payload.get("problem_code")
    assert report["failureBoundary"] in d.FAILURE_BOUNDARIES, report["failureBoundary"]
    assert report["nextActions"], "a failed diagnostic must carry a typed next action"
    for action in report["nextActions"]:
        assert action["commandId"] in d.COMMAND_EFFECTS, action
        assert action["effects"] == d.COMMAND_EFFECTS[action["commandId"]], action
print("doctor contract: OK")
"""

try:
    # --- Identity -------------------------------------------------------------
    # The harness already proved this at build/attach time (installed version ==
    # wheel version, imported from the venv, RECORD hashes match when adopted).
    wheel_version = harness.artifact.version
    assert harness.installed_version == wheel_version, (harness.installed_version, wheel_version)
    assert wheel_version == pyproject_version, (
        f"wheel {wheel_version} was not built from this checkout ({pyproject_version})"
    )
    assert harness.artifact.path.is_file()

    version = harness.run_module("version", "astrid", ["--version"], check=True)
    assert version.stdout.strip() == f"astrid/{wheel_version}", version.stdout

    # The installed console script must dispatch through the packaged
    # ``astrid.console`` entry point, not merely import the package module.
    # ``generate list`` is deliberately read-only, so it proves the console
    # route without requiring a configured generation target.
    console_help = harness.run_console("console-help", ["--help"], check=True)
    assert console_help.returncode == 0
    console_generate = harness.run_console(
        "console-generate-list", ["generate", "list", "--json"], check=True
    )
    assert console_generate.returncode == 0
    assert isinstance(json.loads(console_generate.stdout), list)

    # --- Product contract: family census, from the installed registry ---------
    registry_record = harness.run_lane("registry", ["-c", REGISTRY_LANE], check=True)
    registry = json.loads(registry_record.stdout.strip().splitlines()[-1])
    product_families = registry["product_families"]
    assert len(product_families) == len(set(product_families)) > 0, product_families
    # The census adds the operational families (doctor, backup); serve is
    # excluded from the census and from help.
    operational = sorted(set(registry["excluded"]) - {"serve"})
    expected_census = set(product_families) | set(operational)

    help_record = harness.run_module("help", "astrid", ["help"], check=True)
    help_lines = help_record.stdout.splitlines()
    census_lines = [line for line in help_lines if line.startswith("Family census (exactly ")]
    assert len(census_lines) == 1, census_lines
    head, _, tail = census_lines[0].partition("):")
    census_names = tail.split()
    # The stated count must be the number of names listed, whatever that is.
    number_words = (
        "zero one two three four five six seven eight nine ten eleven twelve".split()
    )
    assert head == f"Family census (exactly {number_words[len(census_names)]} families", census_lines[0]
    assert len(census_names) == len(set(census_names)), census_names
    assert set(census_names) == expected_census, (sorted(census_names), sorted(expected_census))
    for family in product_families:
        assert any(line.lstrip().startswith(f"{family} ") for line in help_lines), family
    # Nested mounts are declared by the registry and must be advertised under
    # their parent with their pack tag.
    for child, path in registry["mounts"].items():
        if len(path) == 2:
            parent, name = path
            assert any(
                line.strip().startswith(f"{parent} {name}") and f"[pack: {name}]" in line
                for line in help_lines
            ), (parent, name)

    # --- doctor --json on a fresh isolated workspace --------------------------
    # A clean workspace has no Runtime, so doctor either reports healthy
    # (exit 0) or fails closed with its typed C2 diagnostic (exit 1).  Anything
    # else, or a payload that violates the diagnostic contract, is a failure.
    doctor = harness.run_module("doctor", "astrid", ["doctor", "--json"])
    if doctor.returncode not in (0, 1):
        raise RuntimeError(doctor.error or doctor.output)
    doctor_payload = json.loads(doctor.stdout)
    doctor_contract = harness.run_lane(
        "doctor-contract",
        ["-c", DOCTOR_LANE, json.dumps(doctor_payload), str(doctor.returncode)],
        check=True,
    )
    assert "doctor contract: OK" in doctor_contract.stdout

    # --- Packaged resources: every pack manifest in the checkout shipped -------
    resource_probe = harness.run_lane(
        "resources", ["-c", RESOURCE_LANE, json.dumps(pack_manifests)], check=True
    )
    assert "package resources: OK" in resource_probe.stdout

    # --- Vendored client: installed copy, byte-identical to the lock ----------
    lock_hashes = {
        name: entry["sha256"] for name, entry in vendor_lock["files"].items()
        if name.startswith("astrid_runtime_client/")
    }
    runtime_client = harness.run_lane(
        "runtime-client", ["-c", CLIENT_LANE, json.dumps(lock_hashes)], check=True
    )
    assert "vendored workspace client: OK" in runtime_client.stdout
    client_facts = json.loads(runtime_client.stdout.splitlines()[0])
    assert client_facts["protocol"] == checkout_contract.PROTOCOL, client_facts
    assert client_facts["schema_digest"] == checkout_contract.SCHEMA_DIGEST, client_facts
    assert client_facts["operations"] == len(checkout_contract.OPERATIONS), client_facts

    # --- Adversaries: each must fail, and for the intended reason --------------
    # A missing resource must fail the lane, and a lane that prints a checkout
    # path must be rejected even though it exits 0; otherwise the checks above
    # could be green without proving anything about the installed artifact.
    missing = harness.run_lane(
        "missing-resource",
        [
            "-c",
            "from importlib import resources; "
            "assert resources.files('astrid').joinpath('not-shipped.txt').is_file()",
        ],
    )
    if missing.returncode == 0 or missing.status != "failed" or "AssertionError" not in missing.stderr:
        raise RuntimeError(f"missing-resource adversary was not rejected as expected: {missing.as_dict()}")

    checkout = harness.run_lane(
        "checkout-import",
        ["-c", f"print({str(repo_root)!r})"],
    )
    if checkout.returncode != 0 or checkout.status != "failed":
        raise RuntimeError(f"checkout-import adversary was not rejected: {checkout.as_dict()}")
    if not checkout.error or "source-tree path" not in checkout.error:
        raise RuntimeError(f"checkout-import failure lacked source evidence: {checkout.as_dict()}")

    print(json.dumps({
        "schema": "astrid.installed_smoke.v1",
        "mode": "adopted" if supplied_wheel is not None else "built",
        "wheel_sha256": harness.artifact_digest,
        "version": harness.installed_version,
        "family_census": sorted(census_names),
        "console_help_lane": console_help.as_dict(),
        "console_generate_list_lane": console_generate.as_dict(),
        "doctor_ok": doctor.returncode == 0,
        "pack_manifests": len(pack_manifests),
        "resource_lane": resource_probe.as_dict(),
        "runtime_client_lane": runtime_client.as_dict(),
        "missing_resource_lane": missing.as_dict(),
        "checkout_import_lane": checkout.as_dict(),
    }, sort_keys=True))
finally:
    harness.close()

# --- Workspace cleanup ---------------------------------------------------------
if owned_workspace:
    if workspace_path.exists():
        raise RuntimeError(f"owned harness workspace was not cleaned: {workspace_path}")
else:
    if not workspace_path.is_dir():
        raise RuntimeError(f"explicit harness workspace was not preserved: {workspace_path}")
if supplied_wheel is not None:
    # Adopting must not consume what the caller supplied.
    if not supplied_wheel.is_file() or not (supplied_venv / "pyvenv.cfg").is_file():
        raise RuntimeError("the supplied wheel or venv was removed by the smoke")

print("=== installed Astrid wheel smoke PASSED ===")
PY
