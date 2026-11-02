#!/usr/bin/env python3
"""Compatibility alias for the common OpenAI generation provider executor."""

from __future__ import annotations

from astrid.core.pack.entrypoint import (
    guard_canonical_entrypoint,
    run_pack_main,
    warn_if_unledgered,
)

guard_canonical_entrypoint("generation.generate_image_openai")
from astrid.core.generation.backends.openai import (
    API_URL as API_URL,
    DEFAULT_FORMAT as DEFAULT_FORMAT,
    DEFAULT_MODEL as DEFAULT_MODEL,
    DEFAULT_QUALITY as DEFAULT_QUALITY,
    DEFAULT_SIZE as DEFAULT_SIZE,
    GPT_IMAGE_2_MAX_EDGE as GPT_IMAGE_2_MAX_EDGE,
    GPT_IMAGE_2_MAX_PIXELS as GPT_IMAGE_2_MAX_PIXELS,
    GPT_IMAGE_2_MAX_RATIO as GPT_IMAGE_2_MAX_RATIO,
    GPT_IMAGE_2_MIN_PIXELS as GPT_IMAGE_2_MIN_PIXELS,
    PRESETS as PRESETS,
    _build_openai_manifest as _build_openai_manifest,
    _call_image_api,
    _die as _die,
    _resolve_key,
    _validate_payload as _validate_payload,
    build_parser,
    execute_legacy,
    write_json_atomic,
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
