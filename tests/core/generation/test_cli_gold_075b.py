"""CLI-GOLD: the public ``python -m astrid generate`` command resolves one default per slot.

Plan 02 (consistent generation), task D2. Every case is a real subprocess of
``python -m astrid generate ...`` with an empty working directory and an
environment of only PATH, HOME/TMPDIR/XDG_CACHE_HOME under ``tmp_path``,
PYTHONNOUSERSITE=1, plus the test process's PYTHONPATH and
ASTRID_RUNTIME_CHECKOUT. No RunPod key is ever present. This module never skips:
a missing prerequisite is a failure with a message naming it.

Source citations use ``file:rev:line``. Revs: DEFAULTS donor ``9d3bb248``
(9d3bb24831676cbfc2a0d3dd1c7769f1937569c3), PINS ``1608dbbf``
(1608dbbf7036ad3c3b2b5e10956a726921bd4f9f), Agent ``origin/pre-main`` as read on
2026-11-04 (94cf0a42ba51f6f51b9466c6a701f1718b9888dc). The test runs against the
merged candidate (pre-main + Plan 01 + PINS + DEFAULTS + D1b). Every value marked
``RE-VERIFY`` must be checked on that tree (see drafts/NOTES.md).

Interpretation of the D2 table's last column: both the ``--placement auto`` cell and
the ``--placement runpod`` cell are dry runs with ``--json``. With no engine installed,
``--placement auto`` can only resolve through an attached ComfyUI, so the auto cells
attach a stand-in ComfyUI server (it answers ``/system_stats`` and
``/object_info`` only, never ``/prompt``) by saving it as the selected target with
the public ``generate targets --placement URL --save`` command. Auto then reads that
saved target from ``$HOME/.vibecomfy/config.toml``
(astrid/core/generation/targets.py:9d3bb248:66, resolve_target at :374-400).
The fail-closed cases use a fresh HOME with no saved target.
"""

from __future__ import annotations

import copy
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import zlib
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

# ---------------------------------------------------------------------------
# The six slots (tasks.md D2 table). RE-VERIFY after D1/D1b on the merged tree.
# ---------------------------------------------------------------------------


# Placeholders substituted with per-test input files (outside the empty cwd).
IMAGE = "{IMAGE}"
VIDEO = "{VIDEO}"
H3_INPUTS = "{H3_INPUTS}"


@dataclass(frozen=True)
class Slot:
    name: str
    entry: str               # selected entry id expected in the JSON
    workflow: str            # VibeComfy workflow id, or the action for H3
    command: str             # generate subcommand
    argv: tuple[str, ...]    # flags/positionals, without --placement/--dry-run/--json
    template: str | None     # ready template the stand-in ComfyUI must describe
    catalog: bool            # entry is a models.yaml catalog row (model_roles apply)


SLOTS: tuple[Slot, ...] = (
    # Default ranking: ("image", "t2i") -> qwen-image-2.1-turbo first
    # (astrid/sdk/generation.py:9d3bb248:1014-1016; also origin/pre-main:1000-1003).
    # Catalog row and template: astrid/core/model_catalog/models.yaml:9d3bb248:52-65.
    # Positional prompt: astrid/core/generation/cli.py:9d3bb248:140.
    Slot("image", "qwen-image-2.1-turbo", "image/qwen_image_2_1_turbo", "image",
         ("A tiny blue lighthouse.",), "image/qwen_image_2_1_turbo", True),
    # ("image", "edit") -> qwen-image-2.1-turbo-edit (generation.py:9d3bb248:1016).
    # --mode edit is required: without it an image_ref infers i2i, whose ranking is
    # empty (generation.py:9d3bb248:321-331), which would let another entry win.
    # --mode: cli.py:9d3bb248:145; --edit (dest image_ref): cli.py:9d3bb248:157.
    # Catalog row: models.yaml:9d3bb248:67-79 (requires prompt, image_ref).
    Slot("edit", "qwen-image-2.1-turbo-edit", "edit/qwen_image_2_1_turbo_edit", "image",
         ("Make the lighthouse red.", "--mode", "edit", "--edit", IMAGE),
         "edit/qwen_image_2_1_turbo_edit", True),
    # H3 is an explicit action entry; argv copied from
    # tests/core/generation/test_h3_action_entry.py:9d3bb248:120-123
    # (test_public_h3_cli_and_sdk_preserve_the_same_producer_local_inputs).
    # --entry cli.py:9d3bb248:143, --inputs-json :144, --project :154.
    # Action id: test_h3_action_entry.py:9d3bb248:15 (entry["action"] == "h3_av.transform").
    Slot("video-references", "h3", "h3", "video",
         ("--entry", "h3", "--inputs-json", H3_INPUTS, "--project", "project"), None, False),
    # ("audio", "tts") -> audio/qwen3_tts_custom_voice (generation.py:9d3bb248:1017).
    # Public input "text": VibeComfy ready_templates/audio/qwen3_tts_custom_voice.py
    # PUBLIC_INPUT_METADATA at VibeComfy donor 1b2e242c. --input: cli.py:9d3bb248:49;
    # audio --mode is required with choices music/tts/sfx: cli.py:9d3bb248:145-147.
    Slot("voice", "audio/qwen3_tts_custom_voice", "audio/qwen3_tts_custom_voice", "audio",
         ("--mode", "tts", "--input", "text=Hello from the lighthouse."),
         "audio/qwen3_tts_custom_voice", False),
    # ("audio", "music") defaults to the live-proven ACE-Step entry.
    Slot("music", "audio/ace_step_1_5_t2a_song", "audio/ace_step_1_5_t2a_song", "audio",
         ("A calm piano piece about a lighthouse.", "--mode", "music"),
         "audio/ace_step_1_5_t2a_song", False),
    # ("audio", "sfx") -> audio/hunyuan_video_foley (generation.py:9d3bb248:1019; plan Decision 2).
    # The template's required public inputs are prompt and video_ref
    # (VibeComfy 1b2e242c ready_templates/audio/hunyuan_video_foley.py PUBLIC_INPUT_METADATA).
    # RE-VERIFY: "its VibeComfy workflow" is assumed to be the same id as the entry.
    Slot("sound-effects", "audio/hunyuan_video_foley", "audio/hunyuan_video_foley", "audio",
         ("Waves against a lighthouse.", "--mode", "sfx", "--input", f"video_ref={VIDEO}"),
         "audio/hunyuan_video_foley", False),
)
SLOT_IDS = [slot.name for slot in SLOTS]
DEFAULT_ENTRY_IDS = frozenset(slot.entry for slot in SLOTS)

# No-engine guidance (astrid/core/generation/preflight.py:9d3bb248:311-316).
GPU_GUIDANCE = "needs a local GPU or --placement runpod"
EDGE_TTS = "Edge TTS"

# Prompt-only video refusal (astrid/sdk/generation.py:9d3bb248:1560-1566).
PROMPT_ONLY_VIDEO = "Prompt-only video requires a reference"

# Not-selected families (tasks.md D2): Z-Image, LTX, Wan, and hosted (fal) rows.
# Ids from models.yaml:9d3bb248 (z-image :102, wan-2.2 :671, ltx-2.3 :768,
# ltx-2.3-pro :841) and the direct-image alias z-image-turbo
# (astrid/core/generation/listing.py:9d3bb248:40-44).
_BANNED_FAMILY = re.compile(r"(?i)(?:^|[/_.-])(?:z[-_]?image|ltx|wan)(?:$|[/_.-])")

TIMEOUT = 300


# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------


def _minimal_env(root: Path) -> dict[str, str]:
    """PATH, HOME/TMPDIR/XDG_CACHE_HOME under root, PYTHONNOUSERSITE, and the two pass-throughs."""
    home, temporary, cache = root / "home", root / "tmp", root / "cache"
    for directory in (home, temporary, cache):
        directory.mkdir(parents=True, exist_ok=True)
    path = os.environ.get("PATH")
    if not path:
        pytest.fail("PATH is empty; the CLI-GOLD subprocesses need the test interpreter's PATH")
    env = {"PATH": path, "HOME": str(home), "TMPDIR": str(temporary),
           "XDG_CACHE_HOME": str(cache), "PYTHONNOUSERSITE": "1"}
    for name in ("PYTHONPATH", "ASTRID_RUNTIME_CHECKOUT"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    if "ASTRID_RUNTIME_CHECKOUT" not in env:
        # tests/lanes.py:9d3bb248 "integration" names this variable; agent-pytest.sh sets it.
        pytest.fail("ASTRID_RUNTIME_CHECKOUT is not set; run through plans/02-consistent-generation/env/agent-pytest.sh")
    assert not any(name.upper().startswith("RUNPOD") for name in env), sorted(env)
    return env


@dataclass
class Cli:
    env: dict[str, str]
    cwd: Path

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = [sys.executable, "-m", "astrid", "generate", *args]
        return subprocess.run(argv, cwd=self.cwd, env=self.env, capture_output=True,
                              text=True, timeout=TIMEOUT, check=False)


def _cli(root: Path) -> Cli:
    cwd = root / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    return Cli(_minimal_env(root), cwd)


def _describe(result: subprocess.CompletedProcess[str]) -> str:
    return (f"exit {result.returncode}\nargv {result.args}\n"
            f"--- stdout ---\n{result.stdout[-6000:]}\n--- stderr ---\n{result.stderr[-6000:]}")


def _parse_json(text: str) -> Any:
    text = text.strip()
    if not text:
        raise ValueError("no output")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
        if not starts:
            raise
        value, _ = json.JSONDecoder().raw_decode(text[min(starts):])
        return value


def _payload(result: subprocess.CompletedProcess[str]) -> Any:
    """--json success and generation errors print to stdout (cli.py:9d3bb248:468, :493-496);
    gateway-rendered errors print a JSON document to stderr (tests/core/generation/
    test_generate_align.py:9d3bb248:340-343)."""
    for stream in (result.stdout, result.stderr):
        try:
            return _parse_json(stream)
        except ValueError:
            continue
    raise AssertionError("no JSON document in the output\n" + _describe(result))


def _walk(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield None, item
            yield from _walk(item)


def _strings(payload: Any) -> set[str]:
    return {item for _key, item in _walk(payload) if isinstance(item, str)}


def _task_ids(payload: Any) -> list[Any]:
    return [item for key, item in _walk(payload) if key in {"task_id", "kernel_task_id"} and item is not None]


def _selected_ids(payload: Any) -> set[str]:
    """Values of the keys that name a selection (dry-run preview: generation.py:9d3bb248:1220-1226;
    H3 InvocationResult: capability_id)."""
    keys = {"id", "model", "entry", "entry_id", "capability_id", "template", "workflow", "workflow_id"}
    return {item for key, item in _walk(payload) if key in keys and isinstance(item, str)}


def _hosted_ids() -> set[str]:
    """Catalog rows that only run on a hosted backend (fal 'cloud' or another provider)."""
    from astrid.core.model_catalog.registry import ModelRegistry

    hosted = set()
    for entry in ModelRegistry.load_default().list_all(include_closed=True):
        backends = {name for spec in entry.modes.values() for name in spec.backends}
        if backends and "local" not in backends:
            hosted.add(entry.id)
    return hosted


def _assert_nothing_banned_selected(payload: Any, *, context: str) -> None:
    hosted = _hosted_ids()
    bad = sorted(item for item in _selected_ids(payload)
                 if _BANNED_FAMILY.search(item) or item in hosted)
    assert not bad, f"{context}: a Z-Image/LTX/Wan/hosted entry is named as selected: {bad}\n{json.dumps(payload)[:4000]}"


def _assert_entry_named(payload: Any, slot: Slot) -> None:
    """The selected entry id appears in the JSON. An action entry (H3) returns
    the action's InvocationResult, which names the action (capability_id
    h3_av.transform) and has no entry field; its entry id is asserted only
    when the JSON carries one (Plan 02 D2, recorded decision)."""
    has_entry_field = any(key in {"entry", "entry_id"} for key, _item in _walk(payload))
    if slot.template is None and not has_entry_field:
        assert payload.get("capability_id") == slot.workflow, payload
        return
    assert slot.entry in _strings(payload), f"entry {slot.entry!r} not in the JSON\n{json.dumps(payload)[:4000]}"


# Voice on an attached (human) ComfyUI: the Qwen3 TTS template's workflow-local
# model files (config.json, model.safetensors, ...) are verified on this machine
# before approval (vibecomfy workflow_bundle "workflow-local model is not present
# locally"); only a managed target with a model root declares target custody.
# Without model bytes (Decision 10: no download) the auto dry run therefore
# selects the default and refuses on its missing assets. Recorded as a STOP.
# video-references (h3) builds its graph even on a dry run; the stand-in target has no MiniMax H3 nodes.
AUTO_ASSET_REFUSALS = {"voice": "workflow-local model is not present locally",
                       "video-references": "target is missing node"}


def _workflow_named(payload: Any, workflow: str) -> bool:
    for value in _strings(payload):
        if value == workflow or value.endswith(f"/{workflow}.py") or value.endswith(f"/{workflow}/workflow.py"):
            return True
    return False


# ---------------------------------------------------------------------------
# Input files (kept outside the empty cwd)
# ---------------------------------------------------------------------------


def _png(width: int = 16, height: int = 16) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x18\x30\x60" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _inputs(root: Path) -> dict[str, str]:
    folder = root / "inputs"
    folder.mkdir(parents=True, exist_ok=True)
    image = folder / "reference.png"
    image.write_bytes(_png())
    # RE-VERIFY: a placeholder file. A dry run is not expected to decode the clip;
    # if the merged tree probes it, replace this with a tiny real MP4 fixture.
    video = folder / "foley-input.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
    # h3 builds its graph even on a dry run (that is how it plans), so its request and media are real.
    import subprocess
    request, assets = folder / "request.json", folder / "assets.json"
    template = Path(__file__).resolve().parents[2] / "fixtures" / "h3_cpu" / "request.v2.json"
    request.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    source = folder / "h3-source.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=blue:s=1024x576:r=24", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-frames:v", "57", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(source)], check=True, timeout=120)
    asset_id = json.loads(request.read_text(encoding="utf-8"))["media"][0]["asset"]["id"]
    assets.write_text(json.dumps({asset_id: str(source), "source": str(source)}), encoding="utf-8")
    h3 = folder / "h3-inputs.json"
    h3.write_text(json.dumps({"request": str(request), "asset_map": str(assets)}), encoding="utf-8")
    return {IMAGE: str(image), VIDEO: str(video), H3_INPUTS: str(h3)}


def slot_argv(slot: Slot, files: dict[str, str], *, placement: str, dry_run: bool = True) -> list[str]:
    """Full argv after ``generate`` for one slot and target. Shared with tests/installed."""
    args = [slot.command]
    for item in slot.argv:
        for placeholder, value in files.items():
            item = item.replace(placeholder, value)
        args.append(item)
    args += ["--placement", placement]
    if dry_run:
        args.append("--dry-run")
    args.append("--json")
    return args


# Every non-slot argv this module runs, so the help check covers them too.
def _extra_argvs(files: dict[str, str], url: str = "http://127.0.0.1:8188") -> list[list[str]]:
    return [
        ["list", "--json"],
        ["describe", "--entry", "h3", "--json"],
        ["targets", "--placement", url, "--save", "--json"],
        ["video", "A lighthouse in gentle rain.", "--placement", "auto", "--dry-run", "--json"],
        ["video", "A lighthouse in gentle rain.", "--placement", "runpod", "--dry-run", "--json"],
        ["image", "A tiny blue lighthouse.", "--placement", "auto", "--json"],
        ["audio", "--mode", "tts", "--input", "text=The exact authored words.", "--placement", "auto", "--json"],
    ]


def _flags(argv: list[str]) -> set[str]:
    return {item.split("=", 1)[0] for item in argv if item.startswith("--")}


# ---------------------------------------------------------------------------
# Stand-in ComfyUI (no /prompt): object_info for exactly the slot templates
# ---------------------------------------------------------------------------

_SCHEMA_CACHE: dict[str, Any] = {}


def _offer(info: dict[str, Any], offered: dict[tuple[str, str], list[str]], class_type: str,
           field: str, value: str) -> None:
    if class_type in info:
        values = offered.setdefault((class_type, field), [])
        if value not in values:
            values.append(value)


def _apply_offers(info: dict[str, Any], offered: dict[tuple[str, str], list[str]]) -> None:
    """Replace each loader choice list with exactly the offered names, as the existing
    attached fixture does (tests/core/generation/test_generate_target_journey.py:9d3bb248:367-368)."""
    for (class_type, field), values in offered.items():
        inputs = info[class_type]["input"]
        for section in ("required", "optional"):
            spec = inputs.get(section, {}).get(field)
            if spec is None:
                continue
            tail = [item for item in spec[1:] if isinstance(item, dict)] if isinstance(spec, list) else []
            inputs[section][field] = [list(values), *tail]
            break
        else:
            inputs.setdefault("required", {})[field] = [list(values)]


def _stand_in_object_info() -> dict[str, Any]:
    """object_info built like the existing fixture's _target_schema
    (test_generate_target_journey.py:9d3bb248:346-369) and target_info
    (tests/core/generation/backends/test_comfy_model_resolution.py:9d3bb248:29-51),
    generalised to every slot template. Model names come from the template's own
    references and, for catalog rows, from models.yaml model_roles."""
    if "info" in _SCHEMA_CACHE:
        return copy.deepcopy(_SCHEMA_CACHE["info"])
    from astrid.core.model_catalog.registry import ModelRegistry
    from vibecomfy.model_assets import _referenced_model_values
    from vibecomfy.porting.object_info import get_class
    from vibecomfy.registry.ready import workflow_from_ready

    registry = ModelRegistry.load_default()
    info: dict[str, Any] = {}
    offered: dict[tuple[str, str], list[str]] = {}
    missing: list[str] = []
    for slot in SLOTS:
        if slot.template is None:
            continue
        workflow = workflow_from_ready(slot.template)
        for node in workflow.runtime_nodes().values():
            if node.class_type in info:
                continue
            definition = get_class(node.class_type)
            if not definition or "inputs" not in definition or "input_order" not in definition:
                missing.append(f"{slot.template}: {node.class_type}")
                continue
            outputs = definition.get("outputs", [])
            info[node.class_type] = {
                "input": copy.deepcopy(definition["inputs"]),
                "input_order": copy.deepcopy(definition["input_order"]),
                "output": [item["type"] for item in outputs],
                "output_name": [item["name"] for item in outputs],
                "output_is_list": [item["is_list"] for item in outputs],
                "output_node": bool(definition.get("output_node",
                                                   node.class_type.startswith(("Save", "Preview")))),
            }
        for reference in _referenced_model_values(workflow):
            _offer(info, offered, reference["class_type"], reference["field"], reference["value"])
        for node in workflow.runtime_nodes().values():
            # The offline object_info cache can predate a choice the template
            # uses (e.g. CLIPLoader type 'minimax'); a ComfyUI that runs the
            # template offers it, so the stand-in offers it as well.
            if node.class_type not in info:
                continue
            for field, value in {**getattr(node, "widgets", {}), **getattr(node, "inputs", {})}.items():
                if isinstance(value, str) and _is_choice(info[node.class_type], field):
                    _offer_choice(info[node.class_type], field, value)
        if slot.catalog:
            spec = registry.get(slot.entry).modes[_catalog_mode(slot)].backends["local"]
            for role in spec.model_roles.values():
                for name in role.names:
                    _offer(info, offered, role.node_class, role.input, name)
    if missing:
        pytest.fail("The stand-in ComfyUI cannot describe these node classes from VibeComfy's "
                    "offline object_info cache (vibecomfy.porting.object_info.get_class): "
                    + "; ".join(missing) + ". Extend the fixture; this is not a product result.")
    _apply_offers(info, offered)
    _SCHEMA_CACHE["info"] = info
    return copy.deepcopy(info)


def _is_choice(schema: dict[str, Any], field: str) -> bool:
    for section in ("required", "optional"):
        spec = schema["input"].get(section, {}).get(field)
        if isinstance(spec, list) and spec and isinstance(spec[0], list):
            return True
    return False


def _offer_choice(schema: dict[str, Any], field: str, value: str) -> None:
    for section in ("required", "optional"):
        spec = schema["input"].get(section, {}).get(field)
        if isinstance(spec, list) and spec and isinstance(spec[0], list):
            if value not in spec[0]:
                spec[0] = [*spec[0], value]
            return


def _catalog_mode(slot: Slot) -> str:
    return {"image": "t2i", "edit": "edit"}[slot.name]


class StandInComfy:
    """Answers the read-only probes a dry run makes; records every request."""

    def __init__(self, object_info: dict[str, Any]) -> None:
        self.object_info = object_info
        self.requests: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def send(self, status: int, value: Any) -> None:
                data = json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                with state._lock:
                    state.requests.append(("GET", self.path))
                path = urlsplit(self.path).path
                if path == "/object_info":
                    self.send(200, state.object_info)
                elif path.startswith("/object_info/"):
                    name = path.split("/", 2)[2]
                    self.send(200, {name: state.object_info[name]} if name in state.object_info else {})
                elif path == "/system_stats":
                    # Shape required by preflight._select_generation_target
                    # (astrid/core/generation/preflight.py:9d3bb248:688-690).
                    self.send(200, {"system": {"comfyui_version": "0.36.0"}, "devices": []})
                elif path == "/queue":
                    self.send(200, {"queue_running": [], "queue_pending": []})
                else:
                    self.send(404, {"error": "stand-in ComfyUI: unexpected read"})

            def do_POST(self) -> None:
                with state._lock:
                    state.requests.append(("POST", self.path))
                self.send(400, {"error": "stand-in ComfyUI refuses writes: a dry run must not submit"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def writes(self) -> list[tuple[str, str]]:
        with self._lock:
            return [request for request in self.requests if request[0] != "GET"]

    def __enter__(self) -> "StandInComfy":
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()


@pytest.fixture
def attached_auto(tmp_path):
    """A CLI whose `--placement auto` binds the stand-in through the public save command."""
    cli = _cli(tmp_path)
    with StandInComfy(_stand_in_object_info()) as comfy:
        # generate targets: --placement cli.py:9d3bb248:207, --save :209, --json :211.
        # Saving does not probe (astrid/sdk/generation.py:9d3bb248:716-724).
        saved = cli.run("targets", "--placement", comfy.url, "--save", "--json")
        assert saved.returncode == 0, _describe(saved)
        config = Path(cli.env["HOME"]) / ".vibecomfy" / "config.toml"
        assert config.is_file(), f"generate targets --save did not write {config}\n" + _describe(saved)
        assert comfy.url.split("//", 1)[1] in config.read_text(encoding="utf-8")
        yield cli, comfy


def _assert_cwd_untouched(cli: Cli) -> None:
    assert not any(cli.cwd.iterdir()), f"the empty working directory was written: {sorted(cli.cwd.iterdir())}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_flag_used_here_is_in_the_subcommand_help(tmp_path):
    """Assert each flag this module passes appears in that subcommand's --help."""
    cli = _cli(tmp_path)
    files = _inputs(tmp_path)
    argvs = [slot_argv(slot, files, placement=target) for slot in SLOTS for target in ("auto", "runpod")]
    argvs += _extra_argvs(files)
    by_command: dict[str, set[str]] = {}
    for argv in argvs:
        by_command.setdefault(argv[0], set()).update(_flags(argv[1:]))
    problems = []
    for command, flags in sorted(by_command.items()):
        # list goes through listing.main (astrid/core/gateway/dispatch.py:9d3bb248:80-86,
        # listing.py:9d3bb248:237-245); the rest through cli._parser (cli.py:9d3bb248:127-224).
        result = cli.run(command, "--help")
        assert result.returncode == 0, _describe(result)
        for flag in sorted(flags):
            if re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", result.stdout) is None:
                problems.append(f"generate {command} --help does not list {flag}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("slot", SLOTS, ids=SLOT_IDS)
def test_auto_dry_run_resolves_the_slot_default(slot, tmp_path, attached_auto):
    cli, comfy = attached_auto
    result = cli.run(*slot_argv(slot, _inputs(tmp_path), placement="auto"))
    if slot.name in AUTO_ASSET_REFUSALS:
        assert result.returncode != 0, _describe(result)
        payload = _payload(result)
        assert AUTO_ASSET_REFUSALS[slot.name] in result.stdout + result.stderr, _describe(result)
        # The default itself is selected; nothing else is substituted.
        assert isinstance(payload.get("entry"), dict) and payload["entry"].get("id") == slot.entry, payload
        _assert_nothing_banned_selected(payload, context=f"{slot.name} auto")
        assert not _task_ids(payload), f"the refusal admitted a task: {_task_ids(payload)}"
        assert not comfy.writes(), f"a dry run wrote to the attached ComfyUI: {comfy.writes()}"
        _assert_cwd_untouched(cli)
        return
    assert result.returncode == 0, _describe(result)
    payload = _payload(result)
    _assert_entry_named(payload, slot)
    assert _workflow_named(payload, slot.workflow), (
        f"workflow {slot.workflow!r} not in the JSON\n{json.dumps(payload)[:4000]}")
    if slot.template is not None:
        # The dry-run preview names the selection (astrid/sdk/generation.py:9d3bb248:1220-1226).
        assert isinstance(payload, dict) and isinstance(payload.get("entry"), dict), payload
        assert payload["entry"].get("id") == slot.entry, payload["entry"]
    _assert_nothing_banned_selected(payload, context=f"{slot.name} auto")
    assert not _task_ids(payload), f"a dry run admitted a task: {_task_ids(payload)}"
    assert not comfy.writes(), f"a dry run wrote to the attached ComfyUI: {comfy.writes()}"
    _assert_cwd_untouched(cli)


@pytest.mark.parametrize("slot", SLOTS, ids=SLOT_IDS)
def test_runpod_dry_run_resolves_the_slot_default(slot, tmp_path):
    """A dry-run may inspect a claimed machine without renting or claiming one."""
    cli = _cli(tmp_path)
    result = cli.run(*slot_argv(slot, _inputs(tmp_path), placement="runpod"))
    assert result.returncode == 0, _describe(result)
    payload = _payload(result)
    _assert_entry_named(payload, slot)
    assert _workflow_named(payload, slot.workflow), (
        f"workflow {slot.workflow!r} not in the JSON\n{json.dumps(payload)[:4000]}")
    _assert_nothing_banned_selected(payload, context=f"{slot.name} runpod")
    assert not _task_ids(payload), f"a dry run admitted a task: {_task_ids(payload)}"
    _assert_cwd_untouched(cli)


def test_generate_help_excludes_paid_renting_and_job_override_flags(capsys):
    from astrid.core.generation.cli import main
    with pytest.raises(SystemExit) as stopped:
        main(["image", "--help"])
    assert stopped.value.code == 0
    output = capsys.readouterr().out
    for flag in ("--max-usd", "--deadline", "--gpu", "--datacenter"):
        assert flag not in output
    assert "--consent {user_env}" in output


def test_list_json_contains_all_six_default_entries(tmp_path):
    cli = _cli(tmp_path)
    # list --json prints a JSON array of rows with "id" (listing.py:9d3bb248:248-249).
    result = cli.run("list", "--json")
    assert result.returncode == 0, _describe(result)
    rows = _parse_json(result.stdout)
    assert isinstance(rows, list), rows
    ids = {row.get("id") for row in rows if isinstance(row, dict)}
    assert DEFAULT_ENTRY_IDS <= ids, f"missing from generate list: {sorted(DEFAULT_ENTRY_IDS - ids)}"
    _assert_cwd_untouched(cli)


@pytest.mark.parametrize("slot", SLOTS, ids=SLOT_IDS)
def test_describe_each_default_entry(slot, tmp_path):
    cli = _cli(tmp_path)
    # describe --entry/--json: cli.py:9d3bb248:170-176, output :378.
    result = cli.run("describe", "--entry", slot.entry, "--json")
    assert result.returncode == 0, _describe(result)
    description = _parse_json(result.stdout)
    assert isinstance(description, dict) and description.get("id") == slot.entry, description
    _assert_cwd_untouched(cli)


@pytest.mark.parametrize("target", ["auto", "runpod"])
def test_prompt_only_video_refuses_without_substitution(target, tmp_path):
    cli = _cli(tmp_path)
    result = cli.run("video", "A lighthouse in gentle rain.", "--placement", target, "--dry-run", "--json")
    assert result.returncode != 0, _describe(result)
    text = result.stdout + result.stderr
    assert PROMPT_ONLY_VIDEO in text, _describe(result)
    payload = _payload(result)
    # No entry is substituted: the refusal carries a null entry
    # (tests/core/generation/test_h3_action_entry.py:9d3bb248:164-188).
    entries = [item for key, item in _walk(payload) if key == "entry"]
    assert entries and all(item is None for item in entries), f"an entry was substituted: {entries}"
    _assert_nothing_banned_selected(payload, context=f"prompt-only video {target}")
    assert not _task_ids(payload), f"the refusal admitted a task: {_task_ids(payload)}"
    _assert_cwd_untouched(cli)


def _require_no_discovered_comfy() -> None:
    """Unbound auto discovery probes 127.0.0.1:8188 (targets.py:9d3bb248:400). A ComfyUI
    already listening there would bind, so the no-engine case could not be shown."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1", 8188)) == 0:
            pytest.fail("Something is listening on 127.0.0.1:8188; stop it so the no-engine "
                        "fail-closed case is not bound to it by auto discovery.")


def test_no_engine_image_fails_closed_with_gpu_guidance(tmp_path):
    """Exact command from tasks.md D2 (no --dry-run). RE-VERIFY: see NOTES (Runtime connection order)."""
    _require_no_discovered_comfy()
    cli = _cli(tmp_path)
    result = cli.run("image", "A tiny blue lighthouse.", "--placement", "auto", "--json")
    assert result.returncode != 0, _describe(result)
    text = result.stdout + result.stderr
    assert GPU_GUIDANCE in text, _describe(result)
    payload = _payload(result)
    _assert_nothing_banned_selected(payload, context="no-engine image")
    assert not _task_ids(payload), f"the refusal admitted a task: {_task_ids(payload)}"
    _assert_cwd_untouched(cli)


def test_no_engine_tts_fails_closed_and_names_edge_tts(tmp_path):
    """Exact command from tasks.md D2. Edge TTS is named for tts (preflight.py:9d3bb248:313-315)."""
    _require_no_discovered_comfy()
    cli = _cli(tmp_path)
    result = cli.run("audio", "--mode", "tts", "--input", "text=The exact authored words.",
                     "--placement", "auto", "--json")
    assert result.returncode != 0, _describe(result)
    text = result.stdout + result.stderr
    assert GPU_GUIDANCE in text, _describe(result)
    assert EDGE_TTS in text, _describe(result)
    payload = _payload(result)
    _assert_nothing_banned_selected(payload, context="no-engine tts")
    assert not _task_ids(payload), f"the refusal admitted a task: {_task_ids(payload)}"
    _assert_cwd_untouched(cli)


def _voice_slot() -> Slot:
    return next(slot for slot in SLOTS if slot.name == "voice")


def test_attached_voice_passes_with_align_trust(tmp_path, attached_auto):
    cli, comfy = attached_auto
    slot = _voice_slot()
    result = cli.run(*slot_argv(slot, _inputs(tmp_path), placement="auto"), "--align", "trust")
    assert result.returncode == 0, _describe(result)
    payload = _payload(result)
    assert payload["alignment"] == {"mode": "trust", "verified": False}, payload
    _assert_entry_named(payload, slot)
    assert not _task_ids(payload) and not comfy.writes()
    _assert_cwd_untouched(cli)


def test_attached_voice_fails_with_align_check_and_names_the_fixes(tmp_path, attached_auto):
    cli, comfy = attached_auto
    slot = _voice_slot()
    result = cli.run(*slot_argv(slot, _inputs(tmp_path), placement="auto"), "--align", "check")
    assert result.returncode != 0, _describe(result)
    text = result.stdout + result.stderr
    assert AUTO_ASSET_REFUSALS["voice"] in text, _describe(result)
    assert "--align trust" in text and "generate align" in text, _describe(result)
    assert not _task_ids(_payload(result)) and not comfy.writes()
