#!/usr/bin/env bash
# Warm-runtime A/B: deliver 10 preserved source frames plus a configurable
# number of generated frames (100 by default),
# with the destination used only as MiniMax H3 reference conditioning (no
# terminal keyframe or end blend). H3's joint 24-fps/40-Hz AV latent contract
# requires 39 conditioning frames; the delivery begins at frame 29 so only the
# final 10 preserved frames appear in the review video. The default candidate
# uses H3's native 1344x768 canvas, 30-step full-quality sampling, and the
# 141-frame (17k+5) working grid for 100 generated frames.
set -Eeuo pipefail
IFS=$'\n\t'

POC_ROOT="${ASTRID_H3_POC_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)}"
SAMPLING_PRESET="${H3_SAMPLING_PRESET:-native}"
[[ "$SAMPLING_PRESET" == "native" || "$SAMPLING_PRESET" == "pdd-ref2va-8plus2" ]] || { echo "[h3-ref10] ERROR: H3_SAMPLING_PRESET must be native or pdd-ref2va-8plus2" >&2; exit 1; }
if [[ "$SAMPLING_PRESET" == "pdd-ref2va-8plus2" ]]; then
  if [[ -n "${H3_SAMPLING_STEPS:-}" && "${H3_SAMPLING_STEPS}" != "10" ]]; then
    echo "[h3-ref10] ERROR: pdd-ref2va-8plus2 requires H3_SAMPLING_STEPS=10" >&2
    exit 1
  fi
  STEPS=10
else
  STEPS="${H3_SAMPLING_STEPS:-30}"
fi
[[ "$STEPS" =~ ^[1-9][0-9]*$ ]] || { echo "[h3-ref10] ERROR: H3_SAMPLING_STEPS must be a positive integer" >&2; exit 1; }
ATTENTION_BACKEND="${H3_ATTENTION_BACKEND:-native}"
[[ "$ATTENTION_BACKEND" == "native" || "$ATTENTION_BACKEND" == "sage2" ]] || { echo "[h3-ref10] ERROR: H3_ATTENTION_BACKEND must be native or sage2" >&2; exit 1; }
PROMPT_VARIANT="${H3_PROMPT_VARIANT:-timeline-v1-no-end}"
[[ "$PROMPT_VARIANT" =~ ^[a-zA-Z0-9_-]+$ ]] || { echo "[h3-ref10] ERROR: H3_PROMPT_VARIANT must contain only letters, digits, underscores, or hyphens" >&2; exit 1; }
NEW_FRAMES="${H3_DELIVERED_NEW_FRAMES:-100}"
[[ "$NEW_FRAMES" =~ ^[1-9][0-9]*$ ]] || { echo "[h3-ref10] ERROR: H3_DELIVERED_NEW_FRAMES must be a positive integer" >&2; exit 1; }
WIDTH="${H3_WIDTH:-1344}"
HEIGHT="${H3_HEIGHT:-768}"
[[ "$WIDTH" =~ ^[1-9][0-9]*$ && "$HEIGHT" =~ ^[1-9][0-9]*$ ]] || { echo "[h3-ref10] ERROR: H3_WIDTH and H3_HEIGHT must be positive integers" >&2; exit 1; }
(( WIDTH % 32 == 0 && HEIGHT % 32 == 0 )) || { echo "[h3-ref10] ERROR: H3_WIDTH and H3_HEIGHT must be multiples of 32" >&2; exit 1; }
CONTEXT_FRAMES=39
VISIBLE_SOURCE_FRAMES=10
DELIVERY_START=$((CONTEXT_FRAMES - VISIBLE_SOURCE_FRAMES))
DELIVERY_END=$((CONTEXT_FRAMES + NEW_FRAMES))
DELIVERY_FRAMES=$((VISIBLE_SOURCE_FRAMES + NEW_FRAMES))
# MiniMax H3 accepts lengths congruent to 5 modulo 17. The workflow's length
# expression rounds PREGRID_FRAMES up to that grid, so record the same resolved
# working length in the run receipt and retain any surplus as temporal runway.
WORKING_FRAMES=$((DELIVERY_END + (5 - (DELIVERY_END % 17) + 17) % 17))
(( WORKING_FRAMES >= DELIVERY_END )) || { echo "[h3-ref10] ERROR: H3 working grid is shorter than delivery" >&2; exit 1; }
AUDIO_START="$(awk -v frames="$DELIVERY_START" 'BEGIN { printf "%.12f", frames / 24 }')"
AUDIO_END="$(awk -v frames="$DELIVERY_END" 'BEGIN { printf "%.12f", frames / 24 }')"
PY="$POC_ROOT/runtime/venv/bin/python"
COMFY_URL="${ASTRID_H3_COMFY_URL:-http://127.0.0.1:8189}"
MODELS_ROOT="${VIBECOMFY_SHARED_MODELS_ROOT:-/workspace/vibecomfy-models}"
PDD_FILE="minimax_h3_ref2va_pdd_acc_8step_comfyui.safetensors"
SESSION_ID="${ASTRID_H3_SESSION_ID:-astrid-h3-poc}"
RUN_TAG="$(date -u +%Y%m%dT%H%M%SZ)-$$-ref10-new${NEW_FRAMES}-refs2-${PROMPT_VARIANT}-${SAMPLING_PRESET}-${STEPS}steps-noturbo-${ATTENTION_BACKEND}"
RUN_ROOT="$POC_ROOT/runs/$RUN_TAG"
OUTPUT_NAMESPACE="h3_poc_ref10_new${NEW_FRAMES}_refs2_${PROMPT_VARIANT}_${SAMPLING_PRESET}_noturbo_s${STEPS}_${ATTENTION_BACKEND}"
RUN_COMFY_OUTPUT="$POC_ROOT/comfy-output/$OUTPUT_NAMESPACE/$RUN_TAG"
SOURCE_39="$POC_ROOT/inputs/shot-04-v4-borderless-last-39-frames-av.mp4"
SOURCE_FRAME="$POC_ROOT/inputs/shot-04-v4-borderless-last-frame.png"
DESTINATION="$POC_ROOT/inputs/shot-05-borderless-v1.png"
PROMPT_FILE="${H3_PROMPT_FILE_OVERRIDE:-$POC_ROOT/inputs/h3-sequential-shot-05-v1-no-end-frame.txt}"
IR_RECIPE="$POC_ROOT/h3_ir_recipe.py"
BASE_WORKFLOW="$POC_ROOT/workflow/base.py"

die() { echo "[h3-ref10] ERROR: $*" >&2; exit 1; }
[[ -x "$PY" ]] || die "missing prepared VibeComfy runtime: $PY"
[[ -d "$MODELS_ROOT" ]] || die "missing persistent model root: $MODELS_ROOT"
for path in "$SOURCE_39" "$SOURCE_FRAME" "$DESTINATION" "$PROMPT_FILE" "$IR_RECIPE" "$BASE_WORKFLOW"; do
  [[ -f "$path" ]] || die "missing required input: $path"
done
mkdir -p "$RUN_ROOT"
exec > >(tee -a "$RUN_ROOT/run.log") 2>&1

# Preserve compatibility for the already-running warm server, whose startup
# config predates the durable model-root migration. This link contains no data
# and may be replaced safely with any future project deployment.
if [[ ! -e "$POC_ROOT/models" ]]; then
  ln -s "$MODELS_ROOT" "$POC_ROOT/models"
fi

export VIBECOMFY_HEADLESS=1
export VIBECOMFY_MODELS_ROOT="$MODELS_ROOT"
export VIBECOMFY_CUSTOM_NODES_DIR="$POC_ROOT/runtime/ComfyUI/custom_nodes"
export VIBECOMFY_SESSION_ID="$SESSION_ID"
export VIBECOMFY_SESSION_ROOT="$POC_ROOT/runtime/ComfyUI/out/sessions"
export VIBECOMFY_ON_DEMAND_SCHEMAS=0
export H3_BASE_WORKFLOW="$BASE_WORKFLOW"
export H3_PROMPT_FILE="$PROMPT_FILE"
export H3_IR_RECEIPT="$RUN_ROOT/ir-proof.json"
export H3_RUN_TAG="$RUN_TAG"
export H3_SOURCE_VIDEO_NAME="$(basename "$SOURCE_39")"
export H3_SOURCE_START_TIME="0"
export H3_SOURCE_FRAME_NAME="$(basename "$SOURCE_FRAME")"
export H3_DESTINATION_NAME="$(basename "$DESTINATION")"
export H3_DESTINATION_MODE="reference_only"
export H3_DISABLE_TURBO=1
export H3_ATTENTION_BACKEND="$ATTENTION_BACKEND"
export H3_SAMPLING_PRESET="$SAMPLING_PRESET"
export H3_SAMPLING_STEPS="$STEPS"
export H3_CONTEXT_FRAMES="$CONTEXT_FRAMES"
export H3_WIDTH="$WIDTH"
export H3_HEIGHT="$HEIGHT"
export H3_WORKING_FRAMES="$WORKING_FRAMES"
export H3_PREGRID_FRAMES="$DELIVERY_END"
export H3_DELIVERY_START="$DELIVERY_START"
export H3_DELIVERY_END="$DELIVERY_END"
export H3_EXPECTED_DELIVERY_FRAMES="$DELIVERY_FRAMES"
export H3_OUTPUT_NAMESPACE="$OUTPUT_NAMESPACE"

run_cli() {
  local output="$1"
  shift
  if ! (cd "$POC_ROOT" && "$PY" -m vibecomfy.cli "$@") > "$output" 2> "$output.stderr"; then
    sed -n '1,200p' "$output.stderr" >&2 || true
    sed -n '1,200p' "$output" >&2 || true
    die "VibeComfy CLI failed: $*"
  fi
}

curl --fail --silent "$COMFY_URL/system_stats" > "$RUN_ROOT/system-stats.json"
if [[ "$ATTENTION_BACKEND" == "sage2" || "$SAMPLING_PRESET" == "pdd-ref2va-8plus2" ]]; then
  curl --fail --silent "$COMFY_URL/object_info" > "$RUN_ROOT/object-info.json"
fi
if [[ "$SAMPLING_PRESET" == "pdd-ref2va-8plus2" ]]; then
  [[ -f "$MODELS_ROOT/pdd_acc/$PDD_FILE" ]] || die "PDD preset requires $MODELS_ROOT/pdd_acc/$PDD_FILE"
  "$PY" - "$RUN_ROOT/object-info.json" <<'PY'
import json
import sys
from pathlib import Path

info = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
def required(class_name):
    node = info.get(class_name)
    if not node:
        raise SystemExit(f"PDD preset requested but {class_name} is not installed")
    return node.get("input", {}).get("required", {})

apply_required = required("MiniMaxH3PDDAccApply")
for name in ("model", "pdd_file", "nfe", "lora_strength", "head_strength", "on_off_grid"):
    if name not in apply_required:
        raise SystemExit(f"PDD Apply schema is missing required input {name}")
if "8" not in apply_required["nfe"][0]:
    raise SystemExit("PDD Apply schema does not expose nfe=8")
if "error" not in apply_required["on_off_grid"][0]:
    raise SystemExit("PDD Apply schema does not expose on_off_grid=error")
warm_required = required("MiniMaxH3PDDAccWarmupScheduler")
if "warmup_steps" not in warm_required or "handoff_sigma" not in warm_required:
    raise SystemExit("PDD warmup scheduler schema is missing warmup_steps or handoff_sigma")
if "0.800000" not in warm_required["handoff_sigma"][0]:
    raise SystemExit("PDD warmup scheduler schema does not expose handoff_sigma=0.800000")
for class_name in ("SplitSigmas", "DisableNoise"):
    if class_name not in info:
        raise SystemExit(f"PDD preset requested but core node {class_name} is unavailable")
print("PDD node and core topology schemas verified")
PY
fi
if [[ "$ATTENTION_BACKEND" == "sage2" ]]; then
  "$PY" - "$RUN_ROOT/object-info.json" <<'PY'
import json
import sys
from pathlib import Path

info = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
node = info.get("PathchSageAttentionKJ")
if not node:
    raise SystemExit("Sage 2 requested but ComfyUI-KJNodes PathchSageAttentionKJ is not installed")
required = node.get("input", {}).get("required", {})
mode_choices = required.get("sage_attention", [[]])[0]
if "sageattn_qk_int8_pv_fp16_cuda" not in mode_choices:
    raise SystemExit("Sage 2 requested but PathchSageAttentionKJ has no SageAttention 2 CUDA mode")
if required.get("model", [None])[0] != "MODEL":
    raise SystemExit("PathchSageAttentionKJ schema has an unexpected model input")
PY
  if ! "$PY" - > "$RUN_ROOT/sageattention-import.txt" 2>&1 <<'PY'
import sageattention
if not callable(getattr(sageattention, "sageattn", None)):
    raise SystemExit("sageattention import succeeded but sageattn is missing")
print("sageattention verified")
PY
  then
    sed -n '1,80p' "$RUN_ROOT/sageattention-import.txt" >&2
    die "Sage 2 requested but a usable sageattention package is not installed in the VibeComfy runtime venv"
  fi
fi
run_cli "$RUN_ROOT/vibecomfy-run.json" run "$IR_RECIPE" --yes --non-interactive \
  --runtime server --server-url "$COMFY_URL" --ensure-models \
  --shared-models-root "$MODELS_ROOT"

find_current() {
  local pattern="$1" found
  found="$(find "$RUN_COMFY_OUTPUT" -type f -name "$pattern" -print | sort | tail -n 1)"
  [[ -n "$found" ]] || die "no output matching $pattern under $RUN_COMFY_OUTPUT"
  printf '%s\n' "$found"
}
raw_source="$(find_current 'raw_extension*-audio.mp4')"
assembled_source="$(find_current 'assembled*-audio.mp4')"
latent_source="$(find_current 'extension_latent*.safetensors')"
cp "$raw_source" "$RUN_ROOT/raw-extension-full.mp4"
cp "$assembled_source" "$RUN_ROOT/assembled-full.mp4"
cp "$latent_source" "$RUN_ROOT/extension-1.safetensors"

"$PY" - "$RUN_ROOT/raw-extension-full.mp4" "$RUN_ROOT/assembled-full.mp4" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

expected_width = int(os.environ["H3_WIDTH"])
expected_height = int(os.environ["H3_HEIGHT"])
expected_end = int(os.environ["H3_DELIVERY_END"])
for filename in sys.argv[1:]:
    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-count_frames", "-show_streams", "-of", "json", filename,
    ], text=True))
    streams = {row.get("codec_type"): row for row in probe.get("streams", [])}
    video, audio = streams.get("video"), streams.get("audio")
    if not video or not audio:
        raise SystemExit(f"H3 output must contain video and audio: {filename}")
    frames = int(video.get("nb_read_frames") or video.get("nb_frames") or 0)
    actual = (
        frames, int(video.get("width", 0)), int(video.get("height", 0)),
        video.get("r_frame_rate"), int(audio.get("sample_rate", 0)), int(audio.get("channels", 0)),
    )
    expected = (expected_end, expected_width, expected_height, "24/1", 32000, 2)
    if frames < expected_end or actual[1:] != expected[1:]:
        raise SystemExit(f"H3 native output contract failed for {Path(filename).name}: expected at least {expected}, got {actual}")
    print(f"native output contract: {Path(filename).name} has {frames} frames at {expected_width}x{expected_height}/24fps with 32kHz stereo audio")
PY

# No destination blend: deliver only H3 pixels, with the review window sliced
# to the final protected context frames followed by the requested new frames.
ffmpeg -nostdin -hide_banner -loglevel error -y -i "$RUN_ROOT/assembled-full.mp4" \
  -filter_complex "[0:v]trim=start_frame=${DELIVERY_START}:end_frame=${DELIVERY_END},setpts=PTS-STARTPTS[v];[0:a]atrim=start=${AUDIO_START}:end=${AUDIO_END},asetpts=PTS-STARTPTS[a]" \
  -map "[v]" -map "[a]" -frames:v "$DELIVERY_FRAMES" -r 24 \
  -c:v libx264 -crf 12 -profile:v high -level:v 4.1 -pix_fmt yuv420p -movflags +faststart \
  -c:a aac -b:a 192k "$RUN_ROOT/assembled.mp4"

ffprobe -v error -count_frames -show_streams -show_format -of json \
  "$RUN_ROOT/assembled.mp4" > "$RUN_ROOT/assembled.ffprobe.json"
"$PY" - "$RUN_ROOT/assembled.ffprobe.json" <<'PY'
import json, os, sys
probe = json.load(open(sys.argv[1], encoding="utf-8"))
streams = {row["codec_type"]: row for row in probe["streams"]}
video, audio = streams.get("video"), streams.get("audio")
actual = (
    video and video.get("codec_name"), video and video.get("profile"),
    video and video.get("pix_fmt"), video and int(video.get("nb_read_frames") or 0),
    video and int(video["width"]), video and int(video["height"]),
    audio and audio.get("codec_name"), audio and int(audio["sample_rate"]),
    audio and int(audio["channels"]),
)
expected_frames = int(os.environ["H3_EXPECTED_DELIVERY_FRAMES"])
expected = ("h264", "High", "yuv420p", expected_frames, int(os.environ["H3_WIDTH"]), int(os.environ["H3_HEIGHT"]), "aac", 32000, 2)
if actual != expected:
    raise SystemExit(f"delivery contract failed: expected {expected}, got {actual}")
PY
ffmpeg -v error -i "$RUN_ROOT/assembled.mp4" -f null -
sha256sum "$RUN_ROOT/assembled.mp4" "$RUN_ROOT/assembled-full.mp4" \
  "$RUN_ROOT/extension-1.safetensors" > "$RUN_ROOT/output-sha256.txt"

# Publish review artifacts under the lifecycle fetcher's canonical output/
# directory. Keep the run-root files for backwards compatibility with earlier
# POC runs, but make every new run directly fetchable without repackaging.
PUBLISHED_OUTPUT="$RUN_ROOT/output"
PUBLISHED_VIDEO="$PUBLISHED_OUTPUT/shot-04-to-tools-${PROMPT_VARIANT}-${STEPS}steps-${NEW_FRAMES}new.mp4"
mkdir -p "$PUBLISHED_OUTPUT"
cp "$RUN_ROOT/assembled.mp4" "$PUBLISHED_VIDEO"
cp "$RUN_ROOT/assembled.ffprobe.json" "$PUBLISHED_OUTPUT/assembled.ffprobe.json"
cp "$RUN_ROOT/ir-proof.json" "$PUBLISHED_OUTPUT/ir-proof.json"
cp "$RUN_ROOT/output-sha256.txt" "$PUBLISHED_OUTPUT/output-sha256.txt"
cp "$PROMPT_FILE" "$PUBLISHED_OUTPUT/prompt.txt"
echo "[h3-ref10] completed: $PUBLISHED_VIDEO"
