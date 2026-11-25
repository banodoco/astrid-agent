"""Resolver contract for the pinned installed motion pack's relative import."""

import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "remotion" / "webpack-alias.mjs"
REQUEST = "../../../../rendering/shared/element_contracts"


def resolve(tmp_path: Path, issuer: Path, request: str = REQUEST, packs_path: str = "") -> dict:
    script = """
    import {pathToFileURL} from 'node:url';
    const mod = await import(pathToFileURL(process.argv[1]).href);
    try {
      const result = mod.selectedInstalledPackContract({
        remotionRoot: process.argv[2], issuer: process.argv[3], request: process.argv[4],
        env: {HOME: process.argv[5], ASTRID_PACKS_PATH: process.argv[6]},
      });
      console.log(JSON.stringify({result: result ?? null}));
    } catch (error) {
      console.log(JSON.stringify({error: error.message}));
    }
    """
    result = subprocess.run(
        [os.environ.get("ASTRID_NODE_EXECUTABLE", "node"), "--input-type=module", "-e", script,
         str(MODULE), str(tmp_path / "project" / "remotion"), str(issuer), request,
         str(tmp_path / "home"), packs_path],
        cwd=ROOT / "remotion", check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


@pytest.fixture
def setup_pack(tmp_path: Path) -> tuple[Path, Path]:
    pack = tmp_path / "home" / "Astrid" / "packs" / "astrid_motion"
    issuer = pack / "rendering" / "elements" / "_shared" / "am.tsx"
    issuer.parent.mkdir(parents=True)
    issuer.write_text("", encoding="utf-8")
    (pack / "pack.yaml").write_text("id: astrid_motion\n", encoding="utf-8")
    target = tmp_path / "project" / "astrid" / "packs" / "rendering" / "shared" / "element_contracts.ts"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    return issuer, target


def test_installed_pack_contract_redirects(tmp_path: Path, setup_pack: tuple[Path, Path]) -> None:
    issuer, target = setup_pack
    assert resolve(tmp_path, issuer) == {"result": str(target.with_suffix(""))}


def test_outside_pack_and_other_requests_are_untouched(tmp_path: Path, setup_pack: tuple[Path, Path]) -> None:
    issuer, _ = setup_pack
    assert resolve(tmp_path, tmp_path / "outside" / "am.tsx") == {"result": None}
    assert resolve(tmp_path, issuer, "../../../../rendering/shared/other") == {"result": None}
    assert resolve(tmp_path, issuer, REQUEST + ".ts") == {"result": None}
    assert resolve(tmp_path, issuer, "./" + REQUEST) == {"result": None}


def test_existing_relative_file_is_untouched(tmp_path: Path, setup_pack: tuple[Path, Path]) -> None:
    issuer, _ = setup_pack
    existing = issuer.parent.joinpath(REQUEST + ".ts").resolve()
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("", encoding="utf-8")
    assert resolve(tmp_path, issuer) == {"result": None}


def test_missing_builtin_contract_fails_clearly(tmp_path: Path, setup_pack: tuple[Path, Path]) -> None:
    issuer, target = setup_pack
    target.unlink()
    assert resolve(tmp_path, issuer) == {"error": f"built-in rendering element_contracts missing: {target}"}


def test_configured_pack_root_is_supported(tmp_path: Path, setup_pack: tuple[Path, Path]) -> None:
    issuer, target = setup_pack
    external = tmp_path / "external"
    external.mkdir()
    pack = external / "motion"
    pack.mkdir()
    (pack / "pack.yaml").write_text("id: motion\n", encoding="utf-8")
    external_issuer = pack / "rendering" / "elements" / "_shared" / "am.tsx"
    external_issuer.parent.mkdir(parents=True)
    external_issuer.write_text("", encoding="utf-8")
    assert resolve(tmp_path, external_issuer, packs_path=str(external)) == {"result": str(target.with_suffix(""))}
