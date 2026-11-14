import json
import subprocess
from pathlib import Path


def _probe(video: Path) -> dict:
    done = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
         "stream=nb_read_frames,r_frame_rate", "-of", "json", str(video)],
        capture_output=True, text=True, check=True, timeout=120)
    return json.loads(done.stdout)["streams"][0]


def _last_json_document(text: str) -> dict:
    """The orchestrator child prints its own result document to the shared stdout before the CLI's summary;
    the summary is the last top-level JSON document."""
    decoder, index, last = json.JSONDecoder(), 0, None
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        last, index = decoder.raw_decode(text, index)
    assert isinstance(last, dict), text[-2000:]
    return last
