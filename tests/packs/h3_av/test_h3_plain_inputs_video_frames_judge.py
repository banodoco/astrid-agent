"""Video frame overrides cannot be accepted while using the full source."""
import pytest
from types import SimpleNamespace
from astrid.core.generation import entries
from astrid.core.generation.preflight import GenerationReadinessError
from astrid.sdk.generation import generate

@pytest.fixture(autouse=True)
def _h3_candidate_only(monkeypatch):
    """Keep unrelated catalog/ready discovery outside these admission regressions.

    Real default discovery is exercised separately through the CPU CLI proofs.
    """
    import astrid.sdk
    from astrid.core.generation import defaults
    from astrid.core.model_catalog.registry import ModelRegistry

    entry = entries.find_entry("h3")
    monkeypatch.setattr(astrid.sdk, "_load_model_registry", lambda **kwargs: ModelRegistry([]))
    monkeypatch.setattr(entries, "user_entries", lambda: [entry])
    monkeypatch.setattr(entries, "ready_entries", lambda: [])
    monkeypatch.setattr(defaults, "ordered_ids", lambda *a, **k: ("h3",))



def test_h3_plain_video_rejects_an_ignored_frame_override():
    namespace = entries.entry_hooks(entries.find_entry("h3"))["build"].__globals__

    class Facts:
        @staticmethod
        def measure_source_av(_path):
            pytest.fail("unsupported frames reached source inspection")

    with pytest.raises(ValueError, match="frames.*image_ref|frames.*source"):
        namespace["_plain_request"]({"prompt": "a kite", "frames": 6},
                                    {"video_ref": "/x/clip.mp4"}, Facts)


@pytest.mark.parametrize('selector', [{}, {'entry': 'h3'}], ids=['automatic', 'explicit'])
def test_h3_video_frame_override_has_typed_public_input_error(tmp_path, monkeypatch, selector):
    namespace = entries.entry_hooks(entries.find_entry("h3"))["build"].__globals__
    facts = namespace["_shared"]("engine_facts")
    monkeypatch.setattr(facts, "facts", lambda: SimpleNamespace(
        measure_source_av=lambda *a, **k: pytest.fail("unsupported frames reached source inspection")))
    with pytest.raises(GenerationReadinessError, match="frames.*image_ref|frames.*source") as caught:
        generate.video(placement="runpod", dry_run=True, mode="v2v",
                       prompt="a kite", video_ref=str(tmp_path / "clip.mp4"), frames=6, **selector)
    assert caught.value.code == "input_rejected"
    assert caught.value.to_envelope()["entry"]["id"] == "h3"
