"""Installed packs' relative element_contracts import resolves to the built-in pack."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "remotion" / "webpack-alias.mjs"
REQUEST = "../../../../rendering/shared/element_contracts"


def rule(remotion_root, issuer, request, packs):
    program = (
        "const m = await import(process.argv[1]);"
        "try { console.log(JSON.stringify({r: m.installedPackElementContracts("
        "{remotionRoot: process.argv[2], issuer: process.argv[3], request: process.argv[4],"
        " env: {ASTRID_PACKS_PATH: process.argv[5]}}) ?? null})); }"
        "catch (e) { console.log(JSON.stringify({error: e.message})); }"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", program, MODULE.as_uri(), str(remotion_root),
         str(issuer), request, str(packs)],
        capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def fake_host(tmp_path, with_target=True):
    host = tmp_path / "agent"
    (host / "remotion").mkdir(parents=True)
    shared = host / "astrid/packs/rendering/shared"
    shared.mkdir(parents=True)
    if with_target:
        (shared / "element_contracts.ts").write_text("export {};\n")
    packs = tmp_path / "packs"
    issuer = packs / "astrid_motion/rendering/elements/_shared/x.tsx"
    issuer.parent.mkdir(parents=True)
    issuer.write_text(f"import '{REQUEST}';\n")
    return host, packs, issuer


def test_installed_pack_request_maps_to_builtin(tmp_path):
    host, packs, issuer = fake_host(tmp_path)
    result = rule(host / "remotion", issuer, REQUEST, packs)
    assert result == {"r": str(host / "astrid/packs/rendering/shared/element_contracts")}
    assert rule(host / "remotion", issuer, REQUEST + ".ts", packs)["r"] is not None


def test_other_requests_and_issuers_untouched(tmp_path):
    host, packs, issuer = fake_host(tmp_path)
    assert rule(host / "remotion", issuer, "../../../../rendering/shared/other", packs) == {"r": None}
    assert rule(host / "remotion", issuer, "./element_contracts", packs) == {"r": None}
    outside = tmp_path / "elsewhere/x.tsx"
    assert rule(host / "remotion", outside, REQUEST, packs) == {"r": None}
    inside = host / "astrid/packs/rendering/elements/_shared/x.tsx"
    assert rule(host / "remotion", inside, REQUEST, host / "astrid/packs") == {"r": None}


def test_already_resolving_request_untouched(tmp_path):
    host, packs, issuer = fake_host(tmp_path)
    present = packs / "rendering/shared"
    present.mkdir(parents=True)
    (present / "element_contracts.ts").write_text("export {};\n")
    assert rule(host / "remotion", issuer, REQUEST, packs) == {"r": None}


def test_missing_builtin_target_is_a_clear_error(tmp_path):
    host, packs, issuer = fake_host(tmp_path, with_target=False)
    result = rule(host / "remotion", issuer, REQUEST, packs)
    assert "element_contracts module is missing" in result["error"]
