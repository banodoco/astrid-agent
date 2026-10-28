import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pod_bootstrap.sh"


def test_syntax_ok():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_strict_mode_and_marker():
    text = SCRIPT.read_text()
    assert "set -euo pipefail" in text
    assert "/workspace/astrid/bootstrap/$digest/ok" in text
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_RUNPOD" in text
    assert "1.12.0+astrid.2" in text
    assert "3.12.10" in text


def test_no_secret_variables():
    text = SCRIPT.read_text()
    assert not re.search(r"(KEY|TOKEN|SECRET|PASSWORD|BEARER|CREDENTIAL)", text, re.I)
    assert not re.search(r"\b(sk|rpa|ghp|hf)_[A-Za-z0-9]{8,}", text)
