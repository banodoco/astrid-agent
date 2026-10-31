# Credentials

Configure only what your work needs.

## Your workspace

The launcher creates the workspace connection credentials automatically. Keep them separate from creative-provider API keys and let the launcher manage them.

The installer keeps your workspace at `~/Library/Application Support/Astrid Runtime` on macOS, `${XDG_DATA_HOME:-$HOME/.local/share}/astrid-runtime` on Linux, or `%LOCALAPPDATA%\Astrid Runtime` on Windows. If you chose `--data-root` when installing, Astrid uses that folder instead. This support root contains connection credentials and the Runtime realm, including projects and media. See [Getting Started](../getting-started.md) for the install line.

`ASTRID_RUNTIME_SOURCE_MANIFEST` selects an explicit source-profile file when needed. `ASTRID_RUNTIME_CREDENTIAL` identifies a Runtime credential file for connection paths that use that override. Normal launcher setup supplies its connection information automatically.

## Creative providers

The selected pack tells you which service it needs and where its executing process reads the key. Common names include:

| Service | Environment variable |
| --- | --- |
| OpenAI | `OPENAI_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY` |
| Gemini | `GEMINI_API_KEY` |
| fal | `FAL_KEY` |
| WaveSpeed | `WAVESPEED_API_KEY` |
| RunPod | `RUNPOD_API_KEY` |
| Replicate | `REPLICATE_API_TOKEN` |
| Hugging Face | `HF_TOKEN` |

Enter secrets locally through the selected integration’s configuration. Keep them out of chat and source control. A key in your interactive shell is not proof that an already-running worker can see it; follow the pack’s execution setup.

## Store a provider key once

Use the hidden local prompt for the provider you need. Run the absolute launcher
path shown in the missing-key message, followed by `credential set` and the
provider name. For example, the default macOS/Linux launcher is
`$HOME/.local/share/astrid/run-astrid` and the default Windows launcher is
`%LOCALAPPDATA%\Astrid\run-astrid.cmd`:

```sh
"$HOME/.local/share/astrid/run-astrid" credential set runpod
"$HOME/.local/share/astrid/run-astrid" credential set openai
```

In PowerShell, use the command form:

```powershell
& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd" credential set openai
```

These are the default install paths. If you chose a different program folder,
run the absolute launcher command printed in the missing-key message.

The command updates that key in `~/.astrid/astrid.env`, preserves other settings, and applies owner-only permissions. This file belongs to your login on this computer and is shared across local Astrid projects and checkouts. Do not commit it or copy it to another computer.

Local credential resolution checks an explicit value, then the shared Astrid credential file, then the process environment, and finally an explicitly named fallback environment file. It does not search arbitrary folders for `.env` files. See the maintained [Credential strategy](../reference/credentials.md) for supported providers, `ASTRID_ENV_FILE` and `ASTRID_HOME` overrides, and other execution environments.

[Back to setup](README.md)
