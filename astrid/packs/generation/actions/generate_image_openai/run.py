#!/usr/bin/env python3
"""Compatibility alias for the common OpenAI generation provider executor."""
from __future__ import annotations

from astrid.core.pack.entrypoint import guard_canonical_entrypoint, run_pack_main, warn_if_unledgered

guard_canonical_entrypoint("generation.generate_image_openai")
from astrid.core.generation.backends.openai import (
    API_URL, DEFAULT_MODEL, DEFAULT_SIZE, DEFAULT_QUALITY, DEFAULT_FORMAT,
    PRESETS, _build_openai_manifest, _call_image_api, _resolve_key, build_parser,
    GPT_IMAGE_2_MAX_EDGE, GPT_IMAGE_2_MAX_PIXELS, GPT_IMAGE_2_MAX_RATIO,
    GPT_IMAGE_2_MIN_PIXELS, _die, _validate_payload,
    execute_legacy, write_json_atomic,
)


def generate(args):
    return execute_legacy(args, transport=_call_image_api, key_resolver=_resolve_key,
                          manifest_writer=write_json_atomic)


def main(argv=None):
    warn_if_unledgered()
    return run_pack_main("generation.generate_image_openai",
                         lambda: generate(build_parser().parse_args(argv)), argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
