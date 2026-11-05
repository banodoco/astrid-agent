"""Project the published audio contracts through the real generation CLI."""
import json

import pytest

from astrid import console


@pytest.mark.parametrize("entry_id,mode,public_inputs", [
    ("audio/qwen3_tts_custom_voice", "tts", {"text"}),
    ("audio/minimax_music_3", "music", {"prompt"}),
    ("audio/hunyuan_video_foley", "sfx", {"prompt", "video_ref"}),
])
def test_published_audio_modes_reach_public_list_and_describe(
    entry_id, mode, public_inputs, monkeypatch, capsys,
):
    from astrid.packs.vibecomfy import local

    # Only the loopback server scan is replaced. Inventory, workflow loading,
    # mode resolution and input descriptions all use the published sources.
    monkeypatch.setattr(local, "scan", lambda *_args, **_kwargs: {"servers": []})
    assert console.main(["generate", "list", "--kind", "audio", "--json"]) == 0
    inventory = json.loads(capsys.readouterr().out)
    row, = [row for row in inventory if row["id"] == entry_id]
    assert row["kind"] == "audio" and row["mode"] == mode
    assert row["source"] == "ready-template" and row["backend"] == "local"
    assert not row["ready"]

    assert console.main([
        "generate", "describe", "--entry", row["id"], "--mode", mode, "--json",
    ]) == 0
    description = json.loads(capsys.readouterr().out)
    assert description["kind"] == "audio" and description["mode"] == mode
    assert description["modes"] == [mode]
    assert public_inputs <= {port["name"] for port in description["inputs"]}
