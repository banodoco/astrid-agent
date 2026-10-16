"""Joined command proof on a disposable Runtime, with explicit offline speech/evaluation fixtures."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import math
import os
import shutil
import struct
import sys
import uuid
import wave
from pathlib import Path

import pytest

if not os.environ.get("ASTRID_NARRATION_RUNTIME_TESTS"):
    pytest.skip("requires admitted Runtime fixtures", allow_module_level=True)

sys.path.append(os.environ["ASTRID_NARRATION_RUNTIME_TESTS"])

from test_generic_output_publication import _settle_body  # noqa: E402
from test_timeline_narration_runtime import (  # noqa: E402,F401
    commit,
    managed_take,
    runtime,
    synthetic_scene_answers,
    wire,
)

from astrid.core.execution import generic_host as host  # noqa: E402
from astrid.sdk import timeline_intent as intent  # noqa: E402
from astrid.sdk.media_handles import resolve_media_handle  # noqa: E402
from astrid.sdk.timeline import commands, narration, store  # noqa: E402
from astrid.sdk.timeline.checkout import Checkout  # noqa: E402
from astrid.sdk.timeline.tools import operation_call, resolve_operation  # noqa: E402

pytestmark = pytest.mark.usefixtures("synthetic_scene_answers")


def _pcm():
    out = io.BytesIO()
    with wave.open(out, "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"".join(struct.pack("<h", int(10000 * math.sin(2 * math.pi * (220 + 55 * (i // 8000 % 5)) * i / 16000)
            * (.15 + .85 * math.exp(-((i % 8000) / 900))))) for i in range(18 * 16000)))
    return out.getvalue()


def _media_client(s):
    from types import SimpleNamespace
    def show(project, digest):
        return next(row for row in s.objects(project) if row["digest"] == digest.removeprefix("sha256:"))
    return SimpleNamespace(
        tasks=SimpleNamespace(list_managed_outputs=s.managed_outputs),
        media=SimpleNamespace(show=show, read_bytes=lambda digest: s.object(digest)[1]))


def _music_action(s, p, root, capability, inputs, audio):
    """Real V3 admission, Runtime-issued claim, confined command, and settlement."""
    h = host.GenericPackHost(pack_roots=[Path(host.__file__).resolve().parents[2] / "packs/music"])
    h.discover()
    record = h.preflight(capability)[0]
    assert record.ready, record.preflight
    s.register_capability(dict(capability_id=capability, definition_digest=record.capability_digest))
    executor = capability.replace(".", "-") + "-worker"
    s.register_executor(dict(executor_id=executor, capabilities=[capability]), idempotency_key=executor)
    s.create_task(dict(capability_id=capability, capability_digest=record.capability_digest,
        project=p, idempotency_key=capability), _host_owned=True)
    claim = s.claim_next(dict(executor_id=executor, capability_ids=[capability],
        runtime_epoch=s.health()["runtime_epoch"]), idempotency_key=capability + "-claim")
    authority = claim["claim_authority"]
    client = object.__new__(host.RuntimeProtocolClient)
    client._authenticated_claims = {claim["attempt_id"]: copy.deepcopy(authority)}
    client.heartbeat = lambda task_id, lease_id, **kw: s.heartbeat_attempt(kw["attempt_id"], dict(
        lease_id=lease_id, fence=kw["fence"], runtime_epoch=claim["runtime_epoch"]),
        idempotency_key=str(uuid.uuid4()), identity=dict(actor=executor, scopes=["worker:execute"]))["data"]
    h.client = client
    admitted_authority = client.confirm_claim_authority(**{k: authority[k] for k in (
        "task_id", "attempt_id", "lease_id", "fence", "runtime_epoch", "project_id", "operation", "executor_id")})
    attempt = root / capability
    attempt.mkdir()
    policy = host._AttemptPolicyFactory(h._executable_admission).build(admitted_authority, attempt=attempt, record=record)
    h._confinement_policy, h._attempt_claim_authority = policy, admitted_authority
    source = policy.project.project_root / "score.wav"
    source.write_bytes(audio)
    values = {k: str(source) if v == "AUDIO" else v for k, v in inputs.items()}
    _, admission = h.admit("executor", capability)
    result = h._run_command_definition(record, values, policy.private_write_root, attempt, admission=admission)
    assert result.returncode == 0, result
    manifest = json.loads((policy.private_write_root / "manifest.json").read_text())
    # Preserve the exact produced JSON bytes and result identity, rather than rebuilding a grid.
    outputs = []
    port = {"music.align": "alignment", "music.level": "level", "music.beats": "beats"}[capability]
    data = (policy.private_write_root / (port + ".json")).read_bytes()
    outputs.append(dict(name=port, kind="object", filename=port + ".json", output_port=port,
        digest="sha256:" + hashlib.sha256(data).hexdigest(), media_type="application/json", size=len(data),
        role="result", is_primary=True, durability="durable", data_base64=base64.b64encode(data).decode(),
        producer=dict(capability_id=capability), provenance=dict(source_digest=record.source_digest)))
    s.settle_attempt(claim["attempt_id"], _settle_body(claim, outputs), idempotency_key=capability + "-settle",
        identity=dict(actor=executor, scopes=["objects:write", "worker:execute"]))
    managed = s.managed_outputs(claim["task_id"])
    assert len(managed) == 1 and managed[0]["object_id"] == outputs[0]["digest"]
    assert s.cas.path_for(outputs[0]["digest"][7:]).read_bytes() == data
    handle = f"task:{claim['task_id']}/{port}"
    descriptor, lineage = resolve_media_handle(_media_client(s), p, handle)
    assert descriptor["digest"] == managed[0]["object_id"]
    return dict(descriptor=descriptor, lineage=lineage, input_audio_sha256=hashlib.sha256(audio).hexdigest(), result_type=type(result).__name__,
        returncode=result.returncode, capability_id=capability, source_digest=record.source_digest,
        capability_digest=record.capability_digest, task_id=claim["task_id"], claim=claim,
        output=managed[0], handle=handle, manifest=manifest,
        result=json.loads(data), result_sha256=hashlib.sha256(data).hexdigest(), process_id=result.process_id)


@pytest.mark.skipif(__import__('sys').platform != "darwin", reason="real confined actions require macOS")
def test_music_actions_opcall_reflow_runtime_publication_and_reserved_undo(runtime, tmp_path, monkeypatch):  # noqa: F811
    from astrid.core.timeline.authoring_bundle import open_authoring_bundle
    from astrid.sdk.timeline._timing_evaluators import _TimingEvaluators
    from astrid.sdk.timeline.commit import _digest

    s, p = runtime
    audio = _pcm()
    actions = [_music_action(s, p, tmp_path, cap, inputs, audio) for cap, inputs in (
        ("music.align", dict(reference="AUDIO", target="AUDIO", ref_s=8, window_s=8)),
        ("music.level", dict(audio="AUDIO", from_s=0, to_s=9)),
        ("music.beats", dict(audio="AUDIO")),
    )]
    # Existing offline speech producer fixture, with unique ordinary Runtime tasks.
    create = s.create_task
    serial = []
    def unique(body, **kwargs):
        serial.append(1)
        return create({**body, "idempotency_key": "speech-fixture-" + str(len(serial))}, **kwargs)
    monkeypatch.setattr(s, "create_task", unique)
    claim_next = s.claim_next
    def unique_claim(body, **kwargs):
        kwargs["idempotency_key"] = "speech-claim-" + str(len(serial))
        return claim_next(body, **kwargs)
    monkeypatch.setattr(s, "claim_next", unique_claim)
    old_refs, _, old_words = managed_take(s, p)
    initial = s._commit_revision_closure(p, "main", "parent-1")
    bundle = open_authoring_bundle(initial["parent_revision"], shot_revisions=initial["shot_revisions"],
                                   internal_timeline_revisions=initial["internal_timeline_revisions"])
    tl = Checkout(bundle)
    internal = next(iter(tl.bundle["shots"].values()))["internal_timeline"]
    internal["tracks"] += [dict(id="vo", kind="audio"), dict(id="music", kind="audio"), dict(id="overlay", kind="visual"), dict(id="plate", kind="visual")]
    internal["clips"][0].update(hold=9, clipType="am-snap-plate", asset="picture", track="plate", params={},
        app={"astrid": {"cut": "c1", "layer": "field"}})
    for i in range(3):
        row = dict(id=f"vo-{i}", clipType="media", track="vo", asset="speech", at=i * 3,
                   **{"from": 0, "to": 1.25})
        intent.set_line(row, "repeated", [[w["start_s"], w["end_s"], w["word"], f"w{i}-{j}"] for j, w in enumerate(old_words["words"])])
        intent.set_line_text(row, "Hello world!")
        intent.set_line_take(row, old_refs)
        intent.set_gap_after(row, 1.8)
        internal["clips"].append(row)
    for name in ("rocket", "label"):
        internal["clips"].append(dict(id=name, clipType="am-type", track="overlay", at=.5, hold=1,
            params={"text": name}, app={"astrid": {"cut": "c1", "layer": name}}))
    music_object = s.ingest(p, audio, media_type="audio/wav", original_name="score.wav", idempotency_key="score")["data"]["object_id"]
    image = io.BytesIO()
    from PIL import Image
    Image.new("RGB", (4, 4), "blue").save(image, format="PNG")
    picture = s.ingest(p, image.getvalue(), media_type="image/png", original_name="plate.png", idempotency_key="plate")["data"]["object_id"]
    internal["registry"] = {"assets": {"picture": dict(object_id=picture, type="image"), "speech": dict(object_id=old_refs["speech"]["object_id"], type="audio", duration=1.25),
                                       "score": dict(object_id=music_object, type="audio", duration=18)}}
    internal["clips"].append(dict(id="music", clipType="media", track="music", asset="score", at=0, **{"from":0,"to":9}))
    tl.bundle["placements"][0]["duration_ms"] = 9000
    # Seed attachment is the exact action output plus its stable managed handle label.
    grid, source_label = store.load_beats(p, actions[2]["handle"], client=_media_client(s))
    assert source_label == actions[2]["handle"]
    tl.clip("music").set_beats({**grid, "source": actions[2]["handle"]})
    publication = __import__('astrid.core.timeline.authoring_bundle', fromlist=['compile_authoring_candidate']).compile_authoring_candidate(tl.document()).publication
    publication.update(project_id=p, timeline_id="main", expected_head="parent-1")
    seeded = s.publish_parent_composition(p, "main", publication, idempotency_key="seed-current-bed")["data"]
    rid = seeded["revision_id"]
    closure = s._commit_revision_closure(p, "main", rid)
    base = open_authoring_bundle(closure["parent_revision"], shot_revisions=closure["shot_revisions"],
                                internal_timeline_revisions=closure["internal_timeline_revisions"])
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(store, "resolve_ids", lambda *a, **k: (p, "main", rid))
    monkeypatch.setattr(store, "fetch_bundle", lambda *a, **k: copy.deepcopy(base))
    draft = Checkout.draft(p, "main")
    before = draft.document()
    source_before = copy.deepcopy(closure)
    # Resolve through normal V3 project-local discovery, without a parallel operation registry.
    local = tmp_path / "project" / "astrid/packs/beat_tools"
    shutil.copytree(Path(__file__).parents[2] / "examples/packs/beat_tools", local)
    from astrid.sdk.timeline.tools import operation_discovery_scope
    discovery = dict(project_root=tmp_path / "project", extra_pack_roots=(str(local),))
    operation = resolve_operation("timeline.operation/beat_tools.fit_to_beats", **discovery)
    macro = operation_call(operation, dict(clips=["c1.rocket", "c1.label"],
        beats=["beat 1 after c1", "beat 2 after c1", "beat 3 after c1"]))
    syntheses = []
    def synthesize(capability, **kwargs):
        assert capability == "generation.generate_speech"
        text = kwargs["inputs"]["text"]
        def words(manifest, doc):
            doc["words"] = [dict(word=w, start_s=j*.4, end_s=j*.4+.3) for j,w in enumerate(text.split())]
        refs, _, _ = managed_take(s, p, text=text, bad=words)
        syntheses.append(dict(capability=capability, inputs=kwargs["inputs"], refs=refs))
        return __import__('types').SimpleNamespace(ok=True, outputs={"managed_outputs": [dict(ref, output_port=port) for port,ref in refs.items()]})
    monkeypatch.setattr("astrid.sdk.invocation.invoke", synthesize)
    port_body = wire(p)
    port_body["base_revision_id"] = rid
    managed_client = __import__('types').SimpleNamespace(
        get_managed_output=s.managed_output,
        get_object=lambda ref: {"data": s.object(ref)[1]})
    spec = dict(version=1, remove=[], lines=[dict(occurrence_id="occurrence-1", clip_id=f"vo-{i}",
        segment="repeated", text=text) for i,text in enumerate(("Changed first.", "Changed second.", "Hello world!"))])
    prepared, takes = narration.prepare_script(draft, spec, client=managed_client, preparation_id="joined-reflow")
    assert len(syntheses) == 2
    assert prepared["args"]["spec"]["lines"][2]["take"] == old_refs
    draft._narration_takes = takes
    fits = []
    apply_bed = _TimingEvaluators._apply_bed_cuts
    def observe(self, records):
        fits.append(len(records))
        return apply_bed(self, records)
    monkeypatch.setattr(_TimingEvaluators, "_apply_bed_cuts", observe)
    with operation_discovery_scope(**discovery), draft.edit(operation.identity + "@" + operation.implementation_digest, quiet=True) as tx:
        commands.run(draft, macro)
        commands.execute(draft, prepared)
    assert len(fits) == 1 and fits[0] > 0, fits
    assert len(tx.plan.calls) == 5
    assert len(list(store._history_dir(Path(draft.path)).glob("*.json"))) == 1
    local_fits = list(fits)
    fits.clear()
    body = wire(p)
    body.update(base_revision_id=rid, request_id="integrated-reflow", base_publication_id=port_body["base_publication_id"])
    body["change"]["commands"] = [c.to_json() for c in tx.plan.calls]
    def count_publications():
        return s.store.conn.execute("SELECT count(*) FROM parent_composition_publications").fetchone()[0]
    publications_before = count_publications()
    result = commit(s, p, body)
    assert len(fits) == 1 and fits[0] > 0, fits
    assert count_publications() == publications_before + 1
    assert result["status"] == "accepted", result
    assert s._commit_revision_closure(p, "main", rid) == source_before
    assert commit(s, p, body) == result
    assert count_publications() == publications_before + 1 and len(fits) == 1
    accepted = s._commit_revision_closure(p, "main", result["accepted_revision_id"])
    from runtime_protocol.narration import narration_projection
    rows = narration_projection(accepted)["lines"]
    assert [r["segment"] for r in rows] == ["repeated"] * 3
    assert rows[2]["take"] == old_refs
    after = draft.document()
    draft.undo()
    restored = draft.document()
    from astrid.core.timeline import astrid_ns as ns
    from astrid.sdk.timeline.identity import cut_reservations
    expected = copy.deepcopy(before)
    ns.film_ns(expected["parent"].setdefault("config", {}))["cut_id_high_water"] = cut_reservations(after)
    assert restored == expected
    event = dict(actions=actions, syntheses=syntheses, operation=operation.to_json(), command=macro.to_json(),
        prepared=prepared, expanded_commands=body["change"]["commands"], local_fits=local_fits, runtime_fits=fits,
        result=result, reflow_publications=count_publications()-publications_before, source_invariant=True, source_digest=_digest(source_before),
        before_digest=_digest(before), after_digest=_digest(after), undo_digest=_digest(restored),
        undo_authored_equal=True, reservation_before=ns.film_intent(before["parent"]["config"]).get("cut_id_high_water"),
        reservation_after=ns.film_intent(restored["parent"]["config"]).get("cut_id_high_water"))
    target = os.environ.get("ASTRID_MUSIC_SEGMENTS_WITNESS")
    if target:
        Path(target).write_text(json.dumps(event, indent=2, default=str))
    print("MUSIC_SEGMENTS_WITNESS", json.dumps(dict(syntheses=len(syntheses), fits=fits, accepted=result["accepted_revision_id"], undo_authored_equal=True)))
