import json
from pathlib import Path

import pytest

from scripts import pinokio_linux_binding as binding


def test_rejects_unpinned_template_before_using_checkouts(tmp_path):
    template = tmp_path / "template.json"
    template.write_text(json.dumps({"linux": {}}))
    with pytest.raises(ValueError, match="template SHA-256 mismatch"):
        binding.capture(template, {})


def test_requires_absolute_non_symlink_template(tmp_path):
    template = tmp_path / "template.json"
    template.write_text("{}")
    link = tmp_path / "link"
    link.symlink_to(template)
    for path in (Path("relative.json"), link):
        with pytest.raises(ValueError, match="absolute non-symlink"):
            binding.capture(path, {})


def test_checkpoint_rejects_changed_action_and_never_claims_dispatchable(tmp_path, monkeypatch):
    paths = {name: tmp_path / name for name in binding.TUPLE}
    for path in paths.values():
        path.mkdir()
    config = {
        "immutable_tuple": binding.TUPLE,
        "source_override": False,
        "source_override_test_mode": False,
        "candidate_checkouts": {name: {"ref": ref, "files": {"f": "x"}}
                                for name, ref in binding.TUPLE.items()},
        "old_launcher": {"ref": "old"},
        "actions": {"first": {"steps": [{"message": "__M4_4A_PINOKIO_CHECKOUT__"}]},
                    "retry": {"steps": [{"message": "__M4_4A_PINOKIO_CHECKOUT__"}]}},
    }
    template = tmp_path / "template.json"
    template.write_text(json.dumps({"linux": config}))
    monkeypatch.setattr(binding, "CONFIG_SHA256", binding.hashlib.sha256(template.read_bytes()).hexdigest())
    monkeypatch.setattr(binding, "BOOTSTRAP_SHA256", {})
    monkeypatch.setattr(binding.backend, "_validate_checkout", lambda name, item: item)
    monkeypatch.setattr(binding.ci_journey, "validate_old_launcher_provenance", lambda *_: {"ref": "old"})
    monkeypatch.setattr(binding.ci_journey, "build_pinokio_action",
                        lambda *_args, **_kw: {"run": [{"message": str(paths["pinokio"])}]})
    result = binding.capture(template, paths)
    assert result["dispatchable"] is False
    assert result["source_override"] is False
    assert result["source_override_test_mode"] is False
    assert "observed runtime sessions" in result["missing"]
    monkeypatch.setattr(binding.ci_journey, "build_pinokio_action",
                        lambda *_args, **_kw: {"run": [{"message": "changed"}]})
    with pytest.raises(ValueError, match="first action differs"):
        binding.capture(template, paths)
