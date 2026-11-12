from copy import deepcopy

from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline_cuts import _clip_text, build_cut_table
from astrid.packs.rendering.actions.timeline_visualize.motion.model import Element
from astrid.packs.rendering.actions.timeline_visualize.model import build_model
from tests.packs.rendering.test_timeline_visualize_model import _prepared_snapshot, _with_assembly
from tests.packs.timeline.test_parent_clips import media_only
from tests.sdk.test_timeline_cuts import _bundle


def test_add_text_caption_reaches_readers():
    checkout = Checkout(media_only())
    clip = checkout.add_text("A real caption", at=0.5, hold=1, track="V2", id="caption")
    raw = deepcopy(clip.data)
    assert _clip_text(raw) == "A real caption"
    assert Element(id="caption", type=clip.element, track="V2", start=.5, end=1.5,
                   params=clip.params, clip=raw).label == 'text-card "A real caption"'

    bundle = _bundle()
    raw["track"] = "type"
    bundle["shots"]["intro"]["internal_timeline"]["clips"].append(raw)
    table = build_cut_table(bundle)
    assert any(layer.get("text") == "A real caption" for layer in table["rows"][0]["layers"])

    snapshot = _prepared_snapshot()
    assembly = deepcopy(snapshot.assembly)
    raw["track"] = assembly["clips"][0]["track"]
    assembly["clips"].append(raw)
    result = build_model(_with_assembly(snapshot, assembly))
    assert next(item for item in result.clips if item.clip_id == "caption").authored_text == "A real caption"

    raw["clipType"] = "am-type"
    raw["params"] = {"text": "Old motion caption"}
    assembly["clips"][-1] = raw
    result = build_model(_with_assembly(snapshot, assembly))
    assert next(item for item in result.clips if item.clip_id == "caption").authored_text is None
