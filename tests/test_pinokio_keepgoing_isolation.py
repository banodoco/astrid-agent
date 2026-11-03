"""Pinokio diagnostic reports cannot enter the Agent hosted proof gate."""
from pathlib import Path
import pytest
from scripts.pinokio_backend_executor import ValidationError, admit_hosted_receipt, execute

@pytest.mark.parametrize("marker", [
    {"proof_mode": "keep-going-diagnostic"},
    {"diagnostic_keep_going": True},
    {"qualification_eligible": False},
])
@pytest.mark.parametrize("nested", [False, True])
def test_diagnostic_labels_rejected_before_hosted_claims(marker, nested):
    receipt = {"status": "pass", "evidence_class": "hosted-lifecycle",
               "source_override_test_mode": False,
               "pinokio_backend": {"hosted_lifecycle": {"status": "pass"}}}
    (receipt["pinokio_backend"] if nested else receipt).update(marker)
    with pytest.raises(ValidationError, match="diagnostic output cannot satisfy Pinokio proof"):
        admit_hosted_receipt(receipt)

@pytest.mark.parametrize("marker", [
    {"proof_mode": "keep-going-diagnostic"}, {"diagnostic_keep_going": True},
])
def test_diagnostic_config_refused_before_action_or_receipt(tmp_path: Path, marker):
    output = tmp_path / "must-not-be-created"
    with pytest.raises(ValidationError, match="diagnostic output cannot satisfy Pinokio proof"):
        execute(marker, output)
    assert not output.exists()

def test_missing_or_false_diagnostic_flag_retains_ordinary_gate():
    for marker in ({}, {"diagnostic_keep_going": False}):
        with pytest.raises(ValidationError, match="cannot satisfy hosted receipt gate"):
            admit_hosted_receipt(marker)
