"""Child of the installed rendering composition test; no internal test doubles."""
import json
import os
from pathlib import Path
import sys

root, site = map(Path, sys.argv[1:3])
mode = sys.argv[3]
import astrid
from astrid.core.element import catalog
from astrid.core.element.registry import load_default_registry
from astrid.core.pack.discovery import discover_pack_metadata
from astrid.core.rendering import remotion_runtime
from astrid.core.rendering.registry import load_default_registries
from astrid.core.pack.entrypoint import canonical_runtime_entrypoint
# The facade module refuses direct import outside the canonical runner; this
# probe stands in for the in-process runtime, the sanctioned caller.
with canonical_runtime_entrypoint("rendering.render"):
    from astrid.packs.rendering.actions.render import run as facade

assert sys.flags.isolated
assert Path(astrid.__file__).is_relative_to(site)
assert not any((site / name).exists() for name in (".git", "pyproject.toml", "scripts", "tests"))
# Real installed discovery and projection are shared by CLI and backend children.
packs = discover_pack_metadata(project_root=site)
assert {"rendering", "astrid_motion"} <= {pack.id for pack in packs}
registry = load_default_registry(project_root=site)
catalog_ids = catalog.list_effect_ids()
assert "am-snap-plate" in catalog_ids
renderers, _, _ = load_default_registries(site)
candidate = renderers.get("rendering.remotion")
assert candidate.pack_root == site / "astrid/packs/rendering"
assert candidate.eligibility.eligible
assert candidate.manifest.command == ("python3", "run.py")
# The fake external Node implements Composition's membership query. Definitions,
# selected element resolution, generation and validation still run in Astrid.
membership = {kind: [item.id for item in registry.list(kind=kind)
                     if item.source == "pack:rendering" and not item.component.is_file()]
              for kind in ("effects", "animations", "transitions")}
(root / "public-membership.json").write_text(json.dumps(membership))
reference = remotion_runtime.prepare_remotion_validation(
    artifact_directory=root / f"readiness-{mode}", host_environment=dict(os.environ),
    python_executable=sys.executable, launch_id="b" * 32,
    contexts={"standalone": dict(os.environ)},
)
os.environ.update(reference)
assert remotion_runtime.remotion_runtime_status().available
request = root / mode
request.mkdir()
timeline = request / "timeline.json"
timeline.write_text(json.dumps({
    "theme": "banodoco-default",
    "theme_overrides": {"visual": {"canvas": {"width": 1280, "height": 720, "fps": 24}}},
    "tracks": [{"id": "v", "kind": "visual", "label": "Video"}],
    "clips": [{"id": "motion", "clipType": "absent-motion" if mode == "missing-element" else "am-snap-plate",
               "track": "v", "at": 0, "hold": 1}],
}))
assets = request / "assets.json"
assets.write_text('{"assets":{}}')
output = request / "hype.mp4"
code = facade.main(["--timeline", str(timeline), "--assets", str(assets), "--out", str(output),
                    "--selector", "rendering.absent" if mode == "missing-renderer" else "rendering.remotion"])
if mode == "positive":
    assert code == 0 and output.read_bytes()[4:8] == b"ftyp"
    provenance = json.loads(Path(str(output) + ".provenance.json").read_text())
    assert provenance["segments_v2"][0]["renderer"]["id"] == "rendering.remotion", provenance
    props = json.loads((root / "rendered-props.json").read_text())
    assert props["timeline"]["clips"][0]["clipType"] == "am-snap-plate"
    assert "am-snap-plate" in (root / "remotion/src/effects.generated.ts").read_text()
else:
    assert code == 1 and not output.exists()
    assert not Path(str(output) + ".provenance.json").exists()
assert not list(request.glob(".astrid-render-*"))
(root / f"{mode}.proof.json").write_text(json.dumps({
    "isolated": bool(sys.flags.isolated), "origin": astrid.__file__,
    "selected_backend": candidate.id, "catalog_ids": catalog_ids, "published": output.exists(),
}))
