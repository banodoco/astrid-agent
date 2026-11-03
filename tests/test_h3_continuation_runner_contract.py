"""Static contract checks for the local H3 continuation runner.

The payload requires a 5090/RunPod environment, so these tests intentionally
check the safety-critical lifecycle wiring without starting ComfyUI or
installing models in CI.
"""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "projects/astrid-intro/build/h3/continuation-poc-v1/run-on-5090.sh"

# The runner exists only on the 5090 workstation checkout (never tracked in git), so
# every test here needs that local run artifact.
pytestmark = pytest.mark.integration


def test_h3_runner_reuses_only_a_compatible_named_session() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert 'SESSION_ID="${ASTRID_H3_SESSION_ID:-astrid-h3-poc}"' in text
    assert 'session status "$SESSION_ID"' in text
    assert "vibecomfy.cli session start" in text
    assert "--warm-policy auto" in text
    assert "--input-directory \"$POC_ROOT/inputs\"" in text
    assert "--output-directory \"$COMFY_OUTPUT\"" in text
    assert "validate_session_config" in text
    assert "refusing to stop or replace it" in text
    assert "COMFY_PID" not in text
    assert "main.py --listen" not in text
    assert 'curl --fail --silent "$COMFY_URL/object_info"' in text
    assert "--no-object-info-cache" in text
    assert "export VIBECOMFY_HEADLESS=1" in text


def test_h3_runner_skips_pip_only_after_fingerprint_and_still_repairs() -> None:
    text = RUNNER.read_text(encoding="utf-8")

    assert 'RUNTIME_SETUP_MARKER="$RUNTIME_ROOT/.h3-runtime-setup"' in text
    assert "runtime_setup_fingerprint()" in text
    assert '"${VIBECOMFY_WHEEL_SHA:-source}"' in text
    assert "setup fingerprint unchanged" in text
    assert "verify_runtime_install" in text
    assert "runtime_install_dependencies" in text
    assert '--comfyui-root "$COMFY_ROOT"' in text
    assert "comfyui==0.34.0" not in text
    assert 'importlib.metadata.version("comfyui")' not in text
    assert "--offline-normalizer" in text
    assert "installed VibeComfy lacks source-checkout session support" in text
    assert 'printf \'%s\\n\' "$RUNTIME_SETUP_FINGERPRINT" > "$RUNTIME_SETUP_MARKER"' in text
    # Model reconciliation remains an explicit per-run freshness gate.
    assert "models ensure" in text
    assert "--ensure-models" in text


def test_h3_runner_edits_and_runs_the_vibecomfy_ir_recipe() -> None:
    runner = RUNNER.read_text(encoding="utf-8")
    recipe_path = RUNNER.with_name("h3_ir_recipe.py")
    recipe = recipe_path.read_text(encoding="utf-8")

    assert 'port convert "$PATCHED_UI" --out "$WORKFLOW_ROOT/base.py"' in runner
    assert 'IR_RECIPE="$POC_ROOT/h3_ir_recipe.py"' in runner
    assert 'validate "$IR_RECIPE"' in runner
    assert 'models ensure "$IR_RECIPE"' in runner
    assert 'run "$IR_RECIPE"' in runner
    assert 'run "$WORKFLOW_ROOT/converted.py"' not in runner

    assert "WorkflowLens" in recipe
    assert '_source_node(workflow, sampler, "guider")' in recipe
    assert '_source_node(workflow, guider, "conditioning")' in recipe
    assert 'conditioning.inputs["prompt"] = prompt' in recipe
    assert 'workflow.compile("api")' in recipe
    assert 'compiled active conditioning prompt differs' in recipe
    assert '"workflow_authority": "VibeWorkflow"' in recipe
