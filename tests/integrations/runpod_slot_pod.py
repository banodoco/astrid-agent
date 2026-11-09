"""A stand-in ComfyUI for the RunPod slot-path CPU test.

Unlike the journey's stand-in, it behaves like a pod: ``/object_info`` is built
from VibeComfy's packaged real node schemas (only the classes the workflows
use), ``/prompt`` validates against that object_info, loader combos are filled
from the stand-in pod's install-receipt rows, and ``/history`` returns the real
output key for each output type.
"""
from __future__ import annotations

import glob
import io
import json
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

SAVE_KEYS = {"SaveImage": "images", "PreviewImage": "images", "SaveAudio": "audio", "SaveAudioMP3": "audio",
             "SaveAudioOpus": "audio", "PreviewAudio": "audio", "VHS_VideoCombine": "gifs", "SaveVideo": "images"}
MODEL_EXT = (".safetensors", ".gguf", ".pth", ".ckpt", ".pt", ".bin", ".sft")
# loader field -> models folder the stand-in pod keeps those files in
FOLDERS = {"unet_name": "diffusion_models", "vae_name": "vae", "clip_name": "text_encoders",
           "clip_name1": "text_encoders", "clip_name2": "text_encoders", "lora_name": "loras",
           "ckpt_name": "checkpoints", "model_name": None}
# the order real snapshots are preferred in when a class is in several
PREFERRED = ("runpod-snapshot", "source-", "local", "zz-official", "object_info_comfyui", "stub")


def cache_dir() -> Path:
    import vibecomfy
    return Path(vibecomfy.__file__).resolve().parent / "porting" / "cache" / "object_info"


@lru_cache(maxsize=1)
def _snapshots():
    rows = {}
    for path in sorted(glob.glob(str(cache_dir() / "*.json"))):
        try:
            data = json.loads(Path(path).read_text())
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        rank = next((i for i, tag in enumerate(PREFERRED) if tag in Path(path).name), len(PREFERRED))
        for name, row in data.items():
            if isinstance(row, dict) and isinstance(row.get("inputs"), dict):
                if name not in rows or rank < rows[name][0]:
                    rows[name] = (rank, row)
    return {name: row for name, (_, row) in rows.items()}


def _combo(spec):
    """The menu of a combo input spec, or None."""
    if isinstance(spec, list) and spec:
        head = spec[0]
        if isinstance(head, list):
            return list(head)
        if head == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
            return list(spec[1].get("options", []))
    return None


def _set_menu(spec, names):
    """Replace a combo spec's menu, keeping its format (a list head, or COMBO with options)."""
    if isinstance(spec[0], list):
        spec[0] = list(names)
    else:
        spec[1]["options"] = list(names)


def real_class(name: str, extra: dict | None = None) -> dict:
    """One class as ComfyUI's /object_info serves it, from a packaged real schema."""
    if extra and name in extra:
        row = extra[name]
        if "input" in row:
            return row
    row = (extra or {}).get(name) or _snapshots().get(name)
    if row is None:
        raise KeyError(f"no packaged object_info for {name}")
    inputs = row["inputs"]
    outputs = row.get("outputs") or []
    types = row.get("output") or [o["type"] if isinstance(o, dict) else o for o in outputs]
    names = row.get("output_name") or [o.get("name", o["type"]) if isinstance(o, dict) else o for o in outputs]
    return {"input": {k: v for k, v in inputs.items() if k in ("required", "optional", "hidden")},
            "input_order": row.get("input_order", {}), "output": types, "output_name": names,
            "output_is_list": [False] * len(types), "name": name, "display_name": row.get("display_name", name),
            "category": row.get("category", ""), "output_node": bool(row.get("output_node") or name in SAVE_KEYS)}


def pin_object_info() -> dict:
    """The h3 closure plus the CLIPLoader overlay captured at the pod's pinned ComfyUI (ee71d5c4).

    The snapshot cache's CLIPLoader predates the pin, so its type enum lacks 'minimax' (music) and
    'yue2'. The overlay fixture is the pin's own schema for that one class.
    """
    root = Path(__file__).resolve().parents[1] / "fixtures/h3_cpu"
    closure = json.loads((root / "object_info_h3_runpod_graph_closure.json").read_text())
    overlay = json.loads((root / "object_info_h3_runpod_overlay.json").read_text())
    return {**closure, **overlay}


def receipt_files(receipt: dict) -> list[str]:
    return [row["path"] for row in receipt["models"]]


def fill_loader_menus(info: dict, files: list[str]) -> dict:
    """Replace every model-file combo's menu with what the pod's install receipt lists."""
    for cls in info.values():
        for section in ("required", "optional"):
            for field, spec in (cls["input"].get(section) or {}).items():
                menu = _combo(spec)
                if menu is None or not any(str(v).lower().endswith(MODEL_EXT) for v in menu):
                    continue
                folder = FOLDERS.get(field)
                names = []
                for path in files:
                    parts = path.split("/models/", 1)[-1]
                    if folder is None or parts.startswith(folder + "/") or "/" + folder + "/" in "/" + parts:
                        names.append(parts.split("/", 1)[-1] if folder and parts.startswith(folder + "/") else parts)
                _set_menu(spec, sorted(set(names)))
    return info


def _link(value):
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int)) and isinstance(value[1], int)


def validate_prompt(graph: dict, info: dict) -> dict:
    """ComfyUI's /prompt validation: classes, required inputs, links and combo menus."""
    errors = {}
    for node_id, node in graph.items():
        cls = node.get("class_type")
        problems = []
        if cls not in info:
            errors[node_id] = {"errors": [{"type": "missing_node_type", "message": f"Node '{cls}' not found"}],
                               "class_type": cls}
            continue
        spec = info[cls]["input"]
        inputs = node.get("inputs", {})
        for field, field_spec in (spec.get("required") or {}).items():
            if field not in inputs:
                problems.append({"type": "required_input_missing", "message": f"Required input is missing: {field}"})
                continue
            value = inputs[field]
            if _link(value):
                if str(value[0]) not in graph:
                    problems.append({"type": "bad_linked_input", "message": f"{field} links to missing node {value[0]}"})
                continue
            menu = _combo(field_spec)
            if menu is not None and value not in menu:
                problems.append({"type": "value_not_in_list",
                                 "message": f"Value not in list: {field}: '{value}' not in {menu[:8]}"})
        if problems:
            errors[node_id] = {"errors": problems, "class_type": cls}
    return errors


@lru_cache(maxsize=None)
def _media(kind: str) -> tuple[bytes, str, str]:
    """(bytes, extension, mime) of a tiny real file of the output type."""
    import tempfile
    with tempfile.TemporaryDirectory() as scratch:
        if kind == "images":
            from PIL import Image
            buffer = io.BytesIO()
            Image.new("RGB", (16, 16), (12, 34, 56)).save(buffer, format="PNG")
            return buffer.getvalue(), "png", "image/png"
        if kind == "audio":
            out = Path(scratch) / "a.flac"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=1", str(out)], check=True, timeout=60)
            return out.read_bytes(), "flac", "audio/flac"
        out = Path(scratch) / "v.mp4"
        # video_h3: the 108-frame 24 fps 1024x576 continuation h3's after_outputs composes (request.v2 fixture).
        size, seconds = ("1024x576", "4.5") if kind == "video_h3" else ("64x64", "1")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=blue:s={size}:r=24:d={seconds}", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)],
                       check=True, timeout=60)
        return out.read_bytes(), "mp4", "video/mp4"


class PodComfy:
    """State and routes of the stand-in ComfyUI behind the pod's engine proxy."""

    def __init__(self, classes, installed_files, extra=None):
        """``installed_files()`` lists the model paths the pod's install receipts show right now."""
        self.classes, self.extra, self.installed_files = sorted(classes), extra, installed_files
        self.prompts, self.rejections, self.uploads, self.views = [], [], {}, []
        self.input_files = set()
        self.outputs = {}
        self.count = 0

    @property
    def info(self):
        # Loader menus follow whatever the pod has installed when ComfyUI is asked, as on a pod.
        info = fill_loader_menus({name: real_class(name, self.extra) for name in self.classes}, self.installed_files())
        for name in ("LoadImage", "LoadImageMask", "VHS_LoadVideo", "VHS_LoadVideoFFmpeg", "LoadAudio"):
            # These list the pod's input directory, which is whatever was uploaded.
            for field, spec in (info.get(name, {}).get("input", {}).get("required") or {}).items():
                if _combo(spec) is not None and field in ("image", "video", "audio"):
                    _set_menu(spec, sorted(self.input_files))
        return info

    def object_info(self):
        return self.info

    def submit(self, body: dict):
        graph = body["prompt"]
        info = self.info
        errors = validate_prompt(graph, info)
        if errors:
            self.rejections.append(errors)
            return 400, {"error": {"type": "prompt_outputs_failed_validation",
                                   "message": "Prompt outputs failed validation", "details": "", "extra_info": {}},
                         "node_errors": errors}
        self.prompts.append(body)
        self.count += 1
        prompt_id = f"slot-{self.count}"
        outputs = {}
        for node_id, node in graph.items():
            key = SAVE_KEYS.get(node["class_type"])
            if key is None:
                continue
            kind = {"images": "images", "audio": "audio", "gifs": "video"}[key]
            data, ext, mime = _media("video_h3" if kind == "video" and getattr(self, "h3_output", False) else kind)
            name = f"{prompt_id}-{node_id}.{ext}"
            self.outputs[name] = (data, mime)
            outputs[node_id] = {key: [{"filename": name, "subfolder": "", "type": "output"}]}
            if key == "gifs":
                outputs[node_id][key][0].update(format="video/h264-mp4", frame_rate=24)
        self.history = getattr(self, "history", {})
        self.history[prompt_id] = {"outputs": outputs, "prompt": [self.count, prompt_id, graph, {}, list(outputs)],
                                   "status": {"status_str": "success", "completed": True, "messages": []}}
        return 200, {"prompt_id": prompt_id, "number": self.count, "node_errors": {}}
