"""Bound command entrypoint for ``vibecomfy.character_animation``."""
from __future__ import annotations

from astrid.core.pack.entrypoint import guard_canonical_entrypoint

guard_canonical_entrypoint("vibecomfy.character_animation")

from astrid.packs.vibecomfy.media import runtime  # noqa: E402
from astrid.packs.vibecomfy.media.compiler import CharacterAnimationRequest  # noqa: E402

CAPABILITY_ID = "vibecomfy.character_animation"


def build_parser():
    return runtime.build_parser(CAPABILITY_ID)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request = CharacterAnimationRequest(
        reference_image_ref=args.reference_image_ref or "",
        driving_video_ref=args.driving_video_ref or "",
        mode=args.mode or "",
        resolution=args.resolution or "",
        prompt=args.prompt,
        seed=args.seed,
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
