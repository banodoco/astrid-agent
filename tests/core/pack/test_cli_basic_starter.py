from __future__ import annotations

import contextlib
import io
import tempfile
from pathlib import Path

import yaml

from astrid.core.pack.cli_basic import cmd_new
from astrid.core.pack.validate import validate_pack


def test_packs_new_emits_authored_v3_fields_and_validates() -> None:
    with tempfile.TemporaryDirectory(prefix="pack-starter-") as temporary:
        destination = Path(temporary) / "demo_pack"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            assert cmd_new(["demo_pack", "--destination", str(destination)]) == 0

        manifest = yaml.safe_load((destination / "pack.yaml").read_text(encoding="utf-8"))
        assert manifest["schema_version"] == 3
        assert manifest["description"].strip()
        assert manifest["agent"]["purpose"].strip()
        assert manifest["agent"]["normal_entrypoints"] == ["demo_pack.echo"]
        assert manifest["actions"]["echo"]["inputs"] == [
            {
                "name": "value",
                "type": "string",
                "required": True,
                "description": "Value returned unchanged by this starter action.",
            }
        ]
        assert manifest["documentation"] == {
            "kind": "skill",
            "path": "docs/SKILL.md",
        }
        errors, warnings = validate_pack(destination)
        assert errors == []
        assert warnings == []
        assert "created and validated" in stdout.getvalue()
        assert stderr.getvalue() == ""


def test_ui_rendering_and_support_only_starters_do_not_force_action_entrypoints() -> None:
    with tempfile.TemporaryDirectory(prefix="pack-starter-roles-") as temporary:
        root = Path(temporary)
        for pack_id, roles in (
            ("ui_pack", ["--role", "ui"]),
            ("render_pack", ["--role", "rendering"]),
            ("support_pack", ["--role", "shared"]),
        ):
            destination = root / pack_id
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                assert cmd_new([pack_id, "--destination", str(destination), *roles]) == 0
            manifest = yaml.safe_load((destination / "pack.yaml").read_text(encoding="utf-8"))
            assert "normal_entrypoints" not in manifest["agent"]
            errors, _warnings = validate_pack(destination)
            assert errors == []
