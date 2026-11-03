#!/usr/bin/env bash
# Run a command with credentials removed from the environment, so Codex managers and workers never see them.
# Proxy and CA variables are kept (the network needs them). Usage: codex/env-safe.sh <command...>
exec env -u RUNPOD_API_KEY -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
  -u GH_TOKEN -u GITHUB_TOKEN -u CLOUDSDK_AUTH_ACCESS_TOKEN -u CLAUDE_CODE_MESSAGING_TOKEN \
  -u CLAUDE_SESSION_INGRESS_TOKEN_FILE -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u FAL_KEY -u HF_TOKEN \
  -u RAILWAY_TOKEN -u RAILWAY_API_TOKEN "$@"
