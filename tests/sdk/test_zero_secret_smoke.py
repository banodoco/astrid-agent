"""Canonical secret-shaped environment variable sets for zero-secret smoke tests.

These tuples are the canonical list of environment variable names that must
never reach a child-process environment or a backup directory. The names are
matched by the kernel's secret-name filter (``astrid.core.subprocess_env``,
``astrid.core.backup.operations``) via the
``(^|_)(API[_-]?KEY|AUTH|CREDENTIAL|PASSWORD|SECRET|TOKEN)($|_)`` shape, and
the v10 secret-sink gate (``tests/v10/test_m6_secret_sink.py``) seeds each
name with a canary value and asserts the backup/subprocess env stays
disjoint.

``PROVIDER_ENV_VARS`` mirrors the provider credential table in
``astrid.core.util.credentials_scope`` (``_PROVIDER_ENV``). Keep the two
sources in sync.
"""

from __future__ import annotations

PROVIDER_ENV_VARS: tuple[str, ...] = (
    "FAL_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "FIREWORKS_API_KEY",
    "GEMINI_API_KEY",
    "GIPHY_API_KEY",
    "WAVESPEED_API_KEY",
)

ACCOUNT_CLOUD_ENV_VARS: tuple[str, ...] = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "HF_TOKEN",
    "HUGGINGFACE_HUB_TOKEN",
    "REPLICATE_API_TOKEN",
    "RUNPOD_API_KEY",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
)
