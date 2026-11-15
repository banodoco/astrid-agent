"""Astrid intro: endpoint-guided starter and direct AV-latent continuations.

Append segment specifications without changing earlier ones to retain the
server's sampler cache. Generated context is never reconstructed from MP4.
"""
from __future__ import annotations

import json
import os
import runpy
from pathlib import Path

from vibecomfy.cli_loader import load_bundle
from vibecomfy.handles import Handle
from vibecomfy.security.provenance import Provenance
from vibecomfy.templates import ReadyMetadata, OutputSpec, new_workflow

ROOT = Path(__file__).resolve().parents[2]
CANONICAL = Path(os.environ.get("ASTRID_H3_CANONICAL_WORKFLOW", str(ROOT / "workflows/seitanism_h3_av_extension_verified")))
SOURCE = runpy.run_path(str(CANONICAL / "workflow.py"))
MODELS = SOURCE["MODEL_ASSETS"]
READY_METADATA = ReadyMetadata.build(
    capability="video", template_id="astrid_intro_h3_anchor_chain", models=MODELS,
    requirements={"runtime": {
        # The fresh RTX 5090 provider host exposes a CUDA 12.8-capable driver.
        # comfy-kitchen 0.2.34's CUDA-13 attention kernel cannot launch there,
        # so this revision uses torch cu128 plus core PyTorch attention.
        "comfy_commit": "6bfaacc67c2103481e5f0c84d75257cd0581d86a",
        "comfy_version": "==0.36.0",
        "packages": {"torch": "==2.10.0+cu128", "comfy-kitchen": "==0.2.35", "comfy-aimdo": "==0.5.5"},
        "launch_flags": ["--lowvram", "--use-pytorch-cross-attention", "--disable-comfy-compiler"],
    }, "custom_node_refs": [
        {"slug": "ComfyUI-H3-Motion-Context-MultiRef", "source": "git", "url": "https://github.com/seitanism/ComfyUI-H3-Motion-Context-MultiRef.git", "commit": "361624fb406b63eb6694442eac6c895fc1533a70", "classes": ["MiniMaxH3ReferenceToVideo", "MiniMaxH3SigmaShift", "MiniMaxH3AudioVAECompatibility", "MiniMaxH3CustomKeyframes", "MiniMaxH3GeneratedAVMaskedContext"]},
        {"slug": "ComfyUI-VideoHelperSuite", "source": "git", "url": "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git", "commit": "4ee72c065db22c9d96c2427954dc69e7b908444b", "classes": ["VHS_VideoCombine"]},
    ]},
)

FIRST_PROMPT = "Flat 2D orange pixel-art animation on a pure black background; crisp square pixel edges, limited orange and amber palette, no photorealism, 3D, fur texture, gradients, or live-action scenery. One friendly orange mink begins upright beneath the framed title AN AGENT FOR THE OPEN-SOURCE AI ART ECOSYSTEM with both paws raised. It lowers its paws, turns and runs playfully toward the right in a clear rhythmic pixel-sprite run cycle, tail bouncing. The orange sign breaks into orderly small square pixels which travel overhead and rebuild into the TWO IDEAS heading and the two framed labels shown in the destination image. The mink curves back into the lower center, catches a wrench and a small panel as the tools pop into place, arriving at the destination pose. Keep the opening title readable early; do the scene change in the second half. Maintain the same orange mink identity, flat black background, full-frame graphic composition, and clean readable destination typography. Continuous playful motion, no camera shake or scene cut."


def build():
    source = load_bundle(CANONICAL, trust=Provenance.USER_CONFIRMED, allow_unresolved=True).workflow
    contracts = {n.class_type: n for n in source.nodes.values()}
    fields = ("native_input_names", "native_output_names", "native_input_types", "native_output_types", "native_input_optional", "native_input_asset_kinds", "native_output_slots")
    segments = [{"frames": 175, "start": "anchor_shot_v03.png", "end": "anchor_shot_v04.png", "prompt": FIRST_PROMPT, "seed": 2026091701}]
    prompt_path = Path(os.environ.get("ASTRID_H3_PROMPTS", str(ROOT / "workflows/astrid_intro_h3_prompts.json")))
    if prompt_path.is_file():
        authored = json.loads(prompt_path.read_text())["segments"]
        frame_counts = [175, 260, 175, 175, 294]
        segments = [{"frames": frame_counts[i], "start": s["start_anchor"] + ".png", "end": s["end_anchor"] + ".png", "prompt": s["prompt"], "seed": 2026091701+i} for i, s in enumerate(authored[:int(os.environ.get("ASTRID_H3_SEGMENT_COUNT", "1"))])]
    if os.environ.get("ASTRID_H3_SEGMENTS"):
        segments = json.loads(Path(os.environ["ASTRID_H3_SEGMENTS"]).read_text())
        # Explicit revision specifications can contain an exit stage after the
        # original five. Admit only the reviewed prefix while keeping earlier
        # node IDs, seeds and prompts identical for native latent cache reuse.
        if os.environ.get("ASTRID_H3_SEGMENT_COUNT"):
            limit = int(os.environ["ASTRID_H3_SEGMENT_COUNT"])
            if not 1 <= limit <= len(segments):
                raise ValueError("Segment count must select a non-empty prefix of the explicit specification")
            segments = segments[:limit]
    with new_workflow(READY_METADATA, source_path=__file__) as wf:
        def node(kind, ident, **kwargs):
            contract = contracts[kind]
            ports = {f: getattr(contract, f) for f in fields}
            if kind == "MiniMaxH3CustomKeyframes" and "keyframe_image_2" in kwargs:
                # Dynamic keyframe sockets are documented by the installed
                # node's kwargs contract, absent from its static object_info.
                ports["native_input_names"] = list(ports["native_input_names"]) + ["keyframe_image_2"]
                ports["native_input_types"] = list(ports["native_input_types"]) + ["IMAGE"]
                ports["native_input_optional"] = list(ports["native_input_optional"]) + [False]
            return wf.node(kind, _id=ident, _native_ports=ports, pass_raw=True, **kwargs)
        def out(n, index=0):
            return Handle(node_id=n.id, output_slot=index)

        model = node("UNETLoader", "model", unet_name=SOURCE["UNET_NAME"], weight_dtype="default")
        clip = node("CLIPLoader", "text", clip_name=SOURCE["CLIP_NAME"], type="minimax", device="default")
        vae = node("VAELoader", "video_vae", vae_name=SOURCE["VIDEO_VAE_NAME"])
        audio = node("VAELoader", "audio_vae", vae_name=SOURCE["AUDIO_VAE_NAME"])
        audio = node("MiniMaxH3AudioVAECompatibility", "audio_compat", audio_vae=out(audio))
        attention_backend = os.environ.get("ASTRID_H3_ATTENTION_BACKEND", "pytorch attention")
        if attention_backend not in {"pytorch attention", "comfy kitchen attention"}:
            raise ValueError("ASTRID_H3_ATTENTION_BACKEND must select a supported ModelAttentionBackend value")
        attention = node("ModelAttentionBackend", "attention", model=out(model), attention=attention_backend)
        lora = node("LoraLoaderModelOnly", "lora", model=out(attention), lora_name=SOURCE["LORA_NAME"], strength_model=0.95)
        model = node("MiniMaxH3SigmaShift", "shift", model=out(lora), shift_audio=3, shift_video=12)
        scheduler = node("BasicScheduler", "schedule", model=out(model), scheduler="simple", steps=8, denoise=1.0)
        sampler = node("KSamplerSelect", "sampler", sampler_name="res_multistep")
        previous = None
        outputs = []
        for i, segment in enumerate(segments):
            prefix = f"s{i+1:02}"
            count = int(segment["frames"])
            if count % 17 != 5:
                raise ValueError("H3 frame count must be 5 + 17*k")
            start = node("LoadImage", prefix + "_start", image=segment["start"])
            end = node("LoadImage", prefix + "_end", image=segment["end"])
            # Continuations already inherit identity through generated AV
            # context. Omit the separate static appearance reference after
            # two low-motion takes; pose attraction is the working diagnosis.
            references = {"ref_images.ref_image_0": out(start)} if previous is None else {}
            if i == 1 and os.environ.get("ASTRID_H3_SECOND_REFERENCE", "none") == "endpoint":
                references = {"ref_images.ref_image_0": out(end)}
            if i > 1 and os.environ.get("ASTRID_H3_FUTURE_REFERENCE", "source") == "source":
                references = {"ref_images.ref_image_0": out(start)}
            fresh = node("MiniMaxH3ReferenceToVideo", prefix + "_target", prompt=segment["prompt"], width=int(os.environ.get("ASTRID_H3_WIDTH", "1920")), height=int(os.environ.get("ASTRID_H3_HEIGHT", "1088")), length=count, ref_image_size="match", audio_vae=out(audio), clip=out(clip), vae=out(vae), **references)
            latent = out(fresh, 1)
            if previous is not None:
                context = node("MiniMaxH3GeneratedAVMaskedContext", prefix + "_context", latent=latent, source_latent=out(previous), context_length=39, audio_feather_ticks=8)
                latent = out(context)
                positions = [count]
                images = {"keyframe_image_1": out(end)}
            else:
                positions = [1, count]
                images = {"keyframe_image_1": out(start), "keyframe_image_2": out(end)}
            # A CustomKeyframes call replaces minimax_keyframes: both initial
            # anchors must therefore be supplied in one call, not chained.
            keyframes = node("MiniMaxH3CustomKeyframes", prefix + "_keyframes", conditioning=out(fresh), latent=out(fresh, 1), vae=out(vae), keyframe_state=json.dumps({"count": len(positions), "positions": positions}), indexing="1-based", crop="disabled", **images)
            guider = node("BasicGuider", prefix + "_guider", model=out(model), conditioning=out(keyframes))
            noise = node("RandomNoise", prefix + "_noise", noise_seed=int(segment["seed"]))
            previous = node("SamplerCustomAdvanced", prefix + "_sample", guider=out(guider), latent_image=latent, noise=out(noise), sampler=out(sampler), sigmas=out(scheduler))
            decoded = node("VAEDecode", prefix + "_decode", samples=out(previous), vae=out(vae))
            movie = node("VHS_VideoCombine", prefix + "_movie", images=out(decoded), frame_rate=24, loop_count=0, filename_prefix=f"astrid_intro_native/{prefix}", format="video/h264-mp4", pingpong=False, save_output=True, crf=16, pix_fmt="yuv420p", save_metadata=True, trim_to_audio=False)
            outputs.append(OutputSpec(node=movie, output_type="VHS_VideoCombine", name=prefix, artifact_kind="video", mime_type="video/mp4", filename_prefix=f"astrid_intro_native/{prefix}", expected_cardinality="one"))
        wf = wf.finalize({}, outputs=outputs)
        wf.strict_types = False
        wf.metadata["continuation"] = {"mode": "direct_generated_av_latent", "context_frames": 39, "segments": segments, "audio_policy": "generated audio latent retained for context; picture outputs silent"}
        return wf
