"""Optional real OMP loader/ABI test; no model request or Runtime admission."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from tests.lanes import unavailable


@pytest.mark.omp
def test_real_omp_registers_and_executes_packaged_admission_tool(tmp_path):
    omp_root = os.environ.get("ASTRID_OMP_TEST_PACKAGE_ROOT")
    bun = shutil.which("bun")
    if not omp_root or not bun or os.environ.get("ASTRID_OMP_TEST_RUNTIME") != "1":
        unavailable("Provide OMP, Bun and ASTRID_OMP_TEST_RUNTIME=1 with a scoped Runtime for integration")
    root = Path(__file__).parents[1]
    package = Path(omp_root)
    if (package / "packages/coding-agent").is_dir():
        package /= "packages/coding-agent"
    loader = package / "src/extensibility/extensions/loader.ts"
    extension = root / "astrid/omp_extensions/astrid-invoke.ts"
    probe = tmp_path / "probe.ts"
    probe.write_text(
        f"import {{ loadExtensions }} from {json.dumps(str(loader))};\n"
        f"const loaded = await loadExtensions([{json.dumps(str(extension))}], {json.dumps(str(root))});\n"
        "if (loaded.errors.length) throw Error(JSON.stringify(loaded.errors));\n"
        "const definition = loaded.extensions[0].tools.get('astrid_invoke')?.definition;\n"
        "if (!definition || definition.loadMode !== 'essential') throw Error('Admission tool is not top-level');\n"
        "const valid = {capability_id:'missing.test_capability',kind:'executor',"
        "project:'no-admission-project',inputs:{}};\n"
        "if (!definition.parameters.safeParse(valid).success) throw Error('Valid admission schema rejected');\n"
        "for (const invalid of [{...valid,capability_id:''},{...valid,kind:'other'},"
        "{...valid,project:''},{...valid,inputs:[]},{...valid,execution_request:[]}]) {\n"
        "if (definition.parameters.safeParse(invalid).success) throw Error('Invalid admission schema accepted');\n"
        "}\n"
        "const result = await definition.execute('rejection-probe', {capability_id:'missing.test_capability',"
        "kind:'executor',project:'no-admission-project',inputs:{}}, new AbortController().signal, undefined,"
        f"{{cwd:{json.dumps(str(root))}}});\n"
        "const content = JSON.parse(result.content[0].text);\n"
        "if(content.ok!==false||result.details.astridTaskRefs.length) throw Error('Rejection created a ref');\n"
        "console.log(JSON.stringify({registered:definition.name,loadMode:definition.loadMode,ok:content.ok,refs:result.details.astridTaskRefs}));\n"
        "process.exit(0);\n"
    )
    env = {**os.environ, "ASTRID_OMP_PYTHON": sys.executable,
           "PYTHONPATH": str(root), "ASTRID_EXTRA_PACK_ROOTS": ""}
    result = subprocess.run([bun, str(probe)], cwd=root, env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "registered": "astrid_invoke", "loadMode": "essential", "ok": False, "refs": [],
    }


@pytest.mark.omp
def test_real_omp_model_snapshot_respects_disabled_providers(tmp_path):
    omp_root = os.environ.get("ASTRID_OMP_TEST_PACKAGE_ROOT")
    bun = shutil.which("bun")
    if not omp_root or not bun:
        unavailable("Provide an OMP source checkout and Bun for the model snapshot probe")
    root = Path(__file__).parents[1]
    package = Path(omp_root)
    if (package / "packages/coding-agent").is_dir():
        package /= "packages/coding-agent"
    config = tmp_path / ".omp/config.yml"
    config.parent.mkdir()
    config.write_text("disabledProviders:\n  - hidden-fixture\n")
    snapshot = tmp_path / "snapshot.json"
    probe = tmp_path / "models.ts"
    probe.write_text(
        f"import {{ loadExtensions }} from {json.dumps(str(package / 'src/extensibility/extensions/loader.ts'))};\n"
        f"const loaded = await loadExtensions([{json.dumps(str(root / 'astrid/omp_extensions/astrid-models.ts'))}], {json.dumps(str(tmp_path))});\n"
        "if (loaded.errors.length) throw Error(JSON.stringify(loaded.errors));\n"
        "const registry = {getAll:()=>[{provider:'hidden-fixture',id:'model'},{provider:'visible-fixture',id:'model'}],getAvailable:()=>[],"
        "hasConfiguredAuth:()=>false,getDiscoverableProviders:()=>[],authStorage:{keys:{source:(id)=>id==='visible-fixture'?{kind:'api_key'}:undefined}}};\n"
        "const handlers = loaded.extensions[0].handlers.get('session_shutdown');\n"
        f"await handlers[0]({{}}, {{cwd:{json.dumps(str(tmp_path))},modelRegistry:registry}});\n"
        "process.exit(0);\n"
    )
    result = subprocess.run([bun, str(probe)], cwd=root,
                            env={**os.environ, "ASTRID_OMP_MODELS_RESULT": str(snapshot)},
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    value = json.loads(snapshot.read_text())
    assert "hidden-fixture" in value["_hiddenProviderIds"]
    assert "hidden-fixture" not in {row["id"] for row in value["providers"]}
    assert value["currentModel"] is None
    assert next(row for row in value["providers"] if row["id"] == "visible-fixture")["signedIn"] is True
