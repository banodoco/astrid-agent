import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest

from astrid.packs.rendering.actions.render import run as render_executor
from astrid.core import timeline
from tests.packs.rendering._helpers import _execution_env


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.remotion
@pytest.mark.usefixtures("remotion_runtime_environment")
class AudioRenderTest(unittest.TestCase):
    # Hosted Darwin render exceeded 120s; R12's default timeout was 600s.
    @pytest.mark.timeout(600)
    def test_rendered_mp4_contains_audio_stream(self) -> None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="audio-render-"))
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        tone_path = tmp_dir / "tone.m4a"
        silent_path = tmp_dir / "silent.mp4"
        timeline_path = tmp_dir / "hype.timeline.json"
        assets_path = tmp_dir / "hype.assets.json"
        out_path = tmp_dir / "hype.mp4"

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-c:a",
                "aac",
                str(tone_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=640x360:d=2:r=30",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(silent_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        config: timeline.TimelineConfig = {
            "theme": "banodoco-default",
            "theme_overrides": {
                "visual": {"canvas": {"width": 640, "height": 360, "fps": 30}}
            },
            "tracks": [
                {"id": "v1", "kind": "visual", "label": "Video"},
                {"id": "a1", "kind": "audio", "label": "Audio"},
            ],
            "clips": [
                {
                    "id": "visual_001",
                    "at": 0.0,
                    "track": "v1",
                    "clipType": "media",
                    "asset": "visual",
                    "from_": 0.0,
                    "to": 2.0,
                },
                {
                    "id": "audio_001",
                    "at": 0.0,
                    "track": "a1",
                    "clipType": "media",
                    "asset": "tone",
                    "from_": 0.0,
                    "to": 2.0,
                },
            ],
        }
        registry: timeline.AssetRegistry = {
            "assets": {
                "visual": {
                    "media_id": "silent-object",
                    "content_sha256": hashlib.sha256(silent_path.read_bytes()).hexdigest(),
                    "type": "video/mp4",
                    "duration": 2.0,
                    "resolution": "640x360",
                    "fps": 30.0,
                },
                "tone": {
                    "media_id": "tone-object",
                    "content_sha256": hashlib.sha256(tone_path.read_bytes()).hexdigest(),
                    "type": "audio/mp4",
                    "duration": 2.0,
                },
            }
        }
        timeline.save_timeline(config, timeline_path)
        timeline.save_registry(registry, assets_path)

        with _execution_env():
            output = render_executor.render(
                timeline_path,
                assets_path,
                out_path,
                project_dir=ROOT / "remotion",
                materialized_root=tmp_dir,
                materialized_objects={
                    "silent-object": str(silent_path),
                    "tone-object": str(tone_path),
                },
            )

        self.assertTrue(output.exists())
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("audio", probe.stdout)


if __name__ == "__main__":
    unittest.main()
