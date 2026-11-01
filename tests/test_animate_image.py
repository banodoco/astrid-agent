from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from astrid.packs.video_editing.actions.animate_image import run as animate_image


class AnimateImageDryRunTest(unittest.TestCase):
    def make_tempdir(self) -> Path:
        path = Path(tempfile.mkdtemp(prefix="animate-image-test-"))
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def _patch_runtime(self) -> None:
        patchers = (
            mock.patch.object(
                animate_image,
                "_load_env_var",
                side_effect=AssertionError("env lookup in dry-run"),
            ),
            mock.patch.object(
                animate_image,
                "_submit_fal",
                side_effect=AssertionError("network call in dry-run"),
            ),
            mock.patch.object(
                animate_image,
                "_poll_fal_result",
                side_effect=AssertionError("network call in dry-run"),
            ),
            mock.patch.object(
                animate_image,
                "_probe_video_dimensions",
                return_value=(1280, 720),
            ),
            mock.patch.object(
                animate_image,
                "_extract_first_frame",
                side_effect=lambda _video, dest: dest.write_bytes(b"frame"),
            ),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_dry_run_writes_plan_manifest_and_placeholder_image_without_network(self) -> None:
        root = self.make_tempdir()
        style_image = root / "style.png"
        ref_video = root / "ref.mp4"
        out_dir = root / "out"
        style_image.write_bytes(b"style")
        ref_video.write_bytes(b"video")
        self._patch_runtime()

        rc = animate_image.main(
            [
                "--style-image",
                str(style_image),
                "--ref-video",
                str(ref_video),
                "--out",
                str(out_dir),
                "--dry-run",
            ]
        )
        self.assertEqual(rc, 0)

        resolved_out = out_dir.expanduser().resolve()
        plan_path = resolved_out / "plan.json"
        first_frame_path = resolved_out / "first_frame.png"
        generated_path = resolved_out / "generated.png"
        manifest_path = resolved_out / "manifest.json"

        for path in (plan_path, first_frame_path, generated_path, manifest_path):
            self.assertTrue(path.is_file(), f"missing {path.name}")
        self.assertFalse((resolved_out / "animation.mp4").exists())

        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["mode"], "dry-run")
        self.assertEqual(plan["style_image"], str(style_image.resolve()))
        self.assertEqual(plan["ref_video"], str(ref_video.resolve()))
        self.assertEqual(plan["first_frame"], str(first_frame_path))
        self.assertEqual(plan["video_dimensions"], {"width": 1280, "height": 720})

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["mode"], "dry-run")
        self.assertEqual(manifest["first_frame"], str(first_frame_path))
        self.assertEqual(manifest["stage1"]["fal_model_id"], animate_image.FAL_EDIT_MODEL_ID)
        self.assertTrue(manifest["stage1"]["image"]["placeholder"])
        self.assertEqual(manifest["stage1"]["image"]["path"], str(generated_path))
        self.assertEqual(
            manifest["stage2"]["fal_model_id"],
            animate_image.FAL_ANIMATE_MODEL_ID,
        )
        self.assertEqual(
            manifest["stage2"]["animation"],
            {
                "path": None,
                "placeholder": True,
                "reason": "dry-run",
            },
        )


if __name__ == "__main__":
    unittest.main()
