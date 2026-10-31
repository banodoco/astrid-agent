# Install Astrid

Astrid Runtime and the agent run on your computer. Open the app at
https://astrid.haus.

On macOS or Linux:

```sh
curl -fsSL https://astrid.haus/install | sh
```

On Windows, open PowerShell and run:

```powershell
irm https://astrid.haus/install.ps1 | iex
```

Run the same line to repair or restart an existing installation. It keeps the
installed version and your workspace. To update, pass `--update` to the script
(`sh -s -- --update` after the pipe on macOS/Linux). Updates back up the selected
workspace and keep the previous program version until the new door is verified.

The scripts get pinned uv and Python, then invoke the shared `astrid install`
core. It installs user-local tools and registers a login service. No administrator
access is needed. Without a systemd user service on Linux, run the line to get
Astrid running again or to apply changed service settings.
Installation ends with `Open https://astrid.haus` and opens the connection page.
On failure, report the sentence and the `Details:` path to `install.log`. The
Runtime writes `runtime.log` and the login service writes `runtime-service.log`
in the same log folder. A doctor bundle includes redacted tails of all three logs.

The installer reuses FFmpeg and FFprobe from `PATH` when both run and FFmpeg has
all required encoders and filters. Otherwise it downloads the pinned build.
The installed version records the selected executable paths, actual versions,
and whether they came from `PATH` or a download; doctor bundles include those
fields. Tools inside an older managed version are downloaded again so updates
can remove that older version safely.

To remove an installed copy, run the same install line with `--uninstall`. It
keeps data unless you explicitly add `--delete-data`.

Run the line with `--doctor` to write a redacted support bundle using the installed
version and its recorded workspace. An optional path after `--doctor` selects the
ZIP destination. You can reinstall onto a kept workspace with the ordinary line;
it reattaches the existing realm. Repeating uninstall is safe, including adding
`--delete-data` after an earlier uninstall kept the workspace.

Linux uses `${XDG_DATA_HOME:-$HOME/.local/share}/astrid` for program files.
Windows GPU environments live separately at `%LOCALAPPDATA%\Astrid GPU` and
survive uninstall. On Windows, the line removes its installer files after the
shared installer finishes. If another program keeps those files open, the output
gives their path so you can remove them after closing the window.

To configure the background pack host, edit `service-env.json` in the program
directory, then run the ordinary line to restart. This JSON file supports these
string settings and is preserved during repair and updates:

```json
{
  "ASTRID_WARM_IDLE_SECONDS": "600"
}
```

The login service reads the file before starting Runtime and its declared
services. Unknown settings or non-string values fail with a logged error.
`ASTRID_ALLOW_UNQUALIFIED_GPU` is still accepted for existing settings files,
but is no longer needed to run an untested Windows NVIDIA combination.

Installation does not clone or build the editor. astrid.haus is the default;
to change the editor itself, follow the
[fully local route](../getting-started.md#run-everything-on-your-computer).
Creative providers still need their own credentials and qualified artifacts.

## Developer qualification with local Git sources

### Install an agent Git ref

Use the official installer for development refs too. This repository includes
the pinned tool assets and `scripts/local_source_manifest.py`, which combines
them with the selected agent ref and the Runtime commit in `runtime.lock.json`.
It does not contact `astrid.haus` to create the manifest:

```sh
python3 scripts/local_source_manifest.py \
  --agent-ref refs/heads/my-change \
  --runtime-repo /path/to/astrid-runtime \
  --output /tmp/astrid-local-install-manifest.json
python3 -m astrid install \
  --manifest /tmp/astrid-local-install-manifest.json \
  --uv "$(command -v uv)" \
  --root "$HOME/.local/share/astrid-dev" \
  --data-root "$HOME/.local/share/astrid-dev-workspace"
```

Run these commands from the Astrid checkout with its development environment
and `uv` on `PATH`. The generator records both local repository paths and full
commit SHAs; the installer archives those commits locally, so commit source
changes first. The Runtime checkout must contain the commit named by this
agent's `runtime.lock.json`; the generator rejects a checkout that cannot
resolve it. Passing `--runtime-repo` keeps the supported unreleased-source
route independent of the Runtime archive host. Run
`python3 -m astrid install --help` for repair, update, and uninstall options.

For a separate agent checkout, pass `--agent-repo PATH`. To select another ref
from that checkout, add `--agent-ref REF`. The generated manifest uses tool
assets checked into this repository and does not fetch the hosted manifest.
With this local-source route, Runtime and agent archives come from the pinned
checkouts; pinned tool assets are included in this repository.

The same manifest can be handed to CI or a pod. Keep its source paths valid in
that environment, or generate it there from the staged repository. Use the
official installer transaction and smoke/import checks before reusing a cached
installation.

### Optional host and RunPod packages

The VibeComfy host is optional. The managed installer provisions it from its
pinned source and installs it beside Astrid with `--no-deps`; no Astrid pip extra
is used for this host. A bring-your-own ComfyUI setup does not require it. To
run a host manually outside a managed Astrid installation, install VibeComfy
from its own documented source and follow the VibeComfy guide; do not add it to
Astrid's required package set.

RunPod worker commands are optional. Install their lifecycle CLI from the
`runpod` extra when needed:

```sh
python -m pip install 'astrid[runpod]'
```

Use a package index or local wheelhouse that provides `runpod-lifecycle`. This
extra is not needed for local generation or bring-your-own ComfyUI.

## Additional local Git sources

The generated manifest covers the agent ref and Runtime lock. For an additional
unreleased source such as VibeComfy, add a `repo` to that source entry. For
example:

```json
{
  "vibecomfy": {
    "sha": "<40-character VibeComfy commit>",
    "repo": "/absolute/path/to/scratch/VibeComfy"
  }
}
```

`repo` accepts an absolute local directory or a local `file://` URL, including
URL-encoded spaces. It does not accept remote Git URLs. Each `sha` must be the
full lowercase 40-character commit ID available in that repository; branch
names, abbreviated IDs, tree IDs, and annotated tag IDs fail. Omitting `repo`
keeps the pinned GitHub archive download for that source. The optional
`vibecomfy` entry follows the same validation and `git archive` rules as `agent`
and `runtime`. If it is absent, the installer uses the default VibeComfy commit
(`VIBECOMFY_ENGINE_REVISION` in
`astrid/core/generation/vibecomfy_dependency.py`) from
`peteromallet/VibeComfy`. The resolved manifest and selected revisions are
recorded with the installed version.

Use committed source changes. The installer reads the commit with `git archive`;
it leaves the repository untouched and ignores worktree edits and untracked
files. Git archive attributes apply. Source trees containing tracked symlinks
or submodules are rejected. Git must be on `PATH` for local source overrides;
hosted installs do not need it.

If you choose the hosted bootstrap, serve the local manifest on loopback and
set its URL. That bootstrap still downloads the installer script from
`astrid.haus`:

In one terminal, run:

```sh
python3 -m http.server 17605 --bind 127.0.0.1 --directory /tmp
```

In another terminal, run:

```sh
curl -fsSL https://astrid.haus/install | ASTRID_INSTALL_MANIFEST_URL=http://127.0.0.1:17605/astrid-local-install-manifest.json sh -s -- \
  --root /absolute/path/to/test/program \
  --data-root /absolute/path/to/test/data
```

When running from a source checkout, use `python3 -m astrid install --manifest`
above to avoid the hosted bootstrap as well. The transaction still installs the
manifest's pinned tools and Python packages, registers the platform service,
and verifies the installed Runtime. VibeComfy is installed with `--no-deps`
beside the Runtime and agent, so it uses the ordinary installed Python import
path; no `ASTRID_VIBECOMFY_CHECKOUT` setting is needed. For an existing
installation, add `--update` to qualify new source pins against its workspace,
including the normal backup and rollback.

Astrid's installer supplies VibeComfy. If an older installation reports that
the package is missing, run the installer line with `--update` in a terminal
outside chat. Repair keeps the installed program code, so repair alone will not
add a missing package. The agent must not install VibeComfy or restart Astrid
from inside a chat; a service restart ends the chat and can leave Astrid down.
