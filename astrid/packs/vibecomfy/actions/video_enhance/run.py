"""Bound command entrypoint for ``vibecomfy.video_enhance``."""
from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint("vibecomfy.video_enhance")

from astrid.packs.vibecomfy.media import runtime  # noqa: E402
from astrid.packs.vibecomfy.media.compiler import VideoEnhanceRequest  # noqa: E402

CAPABILITY_ID = "vibecomfy.video_enhance"


def build_parser():
    return runtime.build_parser(CAPABILITY_ID)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request = VideoEnhanceRequest(
        video_ref=args.video_ref or "",
        scale=args.scale,
        enable_upscale=bool(args.enable_upscale),
        color_fix=args.color_fix,
        output_quality=args.output_quality,
    )
    runtime._run(
        capability=CAPABILITY_ID,
        request=request,
        task_identity=args.task_identity,
        profile_id=args.profile,
        readiness_profile_path=args.readiness_profile_path,
        readiness_profile_hash=args.readiness_profile_hash,
        out=args.out.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
