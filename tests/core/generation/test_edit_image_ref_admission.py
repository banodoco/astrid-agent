"""An edit entry's reference image, given as --edit, reaches the entry's own input port."""

import json

from astrid import console


def test_edit_dry_run_binds_the_reference_image(tmp_path, capsys):
    image = tmp_path / "source.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    rc = console.main(["generate", "image", "Change the background to a clear blue sky",
                       "--entry", "edit/qwen_image_2_1_turbo_edit", "--mode", "edit",
                       "--edit", str(image), "--seed", "7", "--dry-run", "--json"])
    out = capsys.readouterr().out
    assert "requires: image_ref" not in out
    payload = json.loads(out)
    assert rc == 0, out
    assert payload["entry"]["bindings"]["image"] == str(image)
