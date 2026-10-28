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

## Linux without systemd

Without a working systemd user manager, the installer starts one detached
Runtime process. It can survive closing the terminal; it has no crash restart
or startup after reboot. Logout cleanup and container shutdown may stop it.
Run the install line again for a manual start. `python -m astrid doctor` and
the installer's `--doctor` bundle warn when no systemd service owns the observed
Runtime PID. An external supervisor's restart policy must be checked separately.

For a supported foreground launch, use `astrid-runtime run --profile astrid`
with the selected data root and source manifest, plus the installed version's
environment. For a managed installation the following launcher preserves those
settings and `service-env.json` (adjust `program_root` for a custom install):

```sh
program_root="${XDG_DATA_HOME:-$HOME/.local/share}/astrid"
version=$(cat "$program_root/current")
exec "$program_root/versions/$version/py/bin/python" -X utf8 -I - "$program_root" <<'PY'
import os
import sys
from pathlib import Path
from astrid.installer.core import current, read_json
from astrid.installer.service import command

root = Path(sys.argv[1])
version = current(root)
data = Path(read_json(root / "install.json")["data_root"])
env = {k: v for k, v in os.environ.items() if k not in {
    "PYTHONPATH", "PYTHONHOME", "ASTRID_WORKSPACE_ENDPOINT",
    "ASTRID_WORKSPACE_CREDENTIAL", "ASTRID_RUNTIME_SOURCE_MANIFEST",
    "ASTRID_RUNTIME_DATA_ROOT", "ASTRID_PACKS_PATH"}}
env.update(version.env)
argv = command(version, data, env_file=root / "service-env.json")
os.chdir(version.root)
os.execvpe(argv[0], argv, env)
PY
```

Stop the existing detached Runtime with its selected version's
`python -I -m astrid_runtime down --profile astrid --data-root DATA_ROOT`
before handing ownership to this launcher. In a terminal it runs until stopped;
it does not restart itself. For containers, use the foreground launch as the
container workload with the container platform's restart policy enabled. Other
service managers may run the same command and restart it on exit. Keep program
and workspace storage persistent, and allow at least 90 seconds for graceful
shutdown. Stop the external supervisor before installer repair/update/uninstall,
then start it again after the transaction completes so it selects the new version.


## Developer qualification with local Git sources

Installer developers can exercise unpublished agent, Runtime, or VibeComfy
commits with the same installer transaction. Copy the release install manifest,
keep its pinned tool versions and assets, and add an optional `repo` to a source
entry. For example:

```json
{
  "agent": {
    "sha": "0123456789abcdef0123456789abcdef01234567",
    "repo": "/absolute/path/to/scratch/astrid-agent"
  },
  "runtime": {
    "sha": "89abcdef0123456789abcdef0123456789abcdef",
    "repo": "file:///absolute/path/to/scratch/astrid-runtime"
  },
  "vibecomfy": {
    "sha": "<40-character VibeComfy commit>",
    "repo": "/absolute/path/to/scratch/VibeComfy"
  }
}
```

This is an excerpt: preserve `schema_version` and the complete `tools` section
from the release manifest. `repo` accepts an absolute local directory or a local
`file://` URL, including URL-encoded spaces. It does not accept remote Git URLs.
Each `sha` must be the full lowercase 40-character commit ID available in that
repository; branch names, abbreviated IDs, tree IDs, and annotated tag IDs fail.
Omitting `repo` keeps the pinned GitHub archive download for that source. The
optional `vibecomfy` entry follows the same validation and `git archive` rules
as `agent` and `runtime`. If it is absent, the installer uses the default
VibeComfy commit (`VIBECOMFY_ENGINE_REVISION` in
`astrid/core/generation/vibecomfy_dependency.py`) from
`peteromallet/VibeComfy`. The resolved manifest, including the selected source
revision, is recorded with the installed version.

Use scratch clones for candidate changes, commit those changes, and set each
entry's `sha` from `git -C /absolute/path/to/scratch/repo rev-parse HEAD`. The
installer reads the commit with `git archive`; it leaves the original repository
untouched and ignores worktree edits and untracked files. Git archive attributes
apply. Source trees containing tracked symlinks or submodules are rejected.
Git must be on `PATH` for local source overrides; hosted installs do not need it.

With a disposable test home and separate empty program and data directories,
serve the directory containing the local manifest on localhost. In that
directory, start:

```sh
python -m http.server 17605 --bind 127.0.0.1
```

In another terminal, set the manifest URL in the install process and run the
normal install line:

```sh
curl -fsSL https://astrid.haus/install | ASTRID_INSTALL_MANIFEST_URL=http://127.0.0.1:17605/local-install-manifest.json sh -s -- \
  --root /absolute/path/to/test/program \
  --data-root /absolute/path/to/test/data
```

On Windows, pass the same arguments in PowerShell:

```powershell
$env:ASTRID_INSTALL_MANIFEST_URL = 'http://127.0.0.1:17605/local-install-manifest.json'
& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) `
  --root C:\path\to\test\program `
  --data-root C:\path\to\test\data
```

Stop the local HTTP server after installation with Ctrl+C.

This still downloads and installs the manifest's real tools and Python packages,
registers the platform service, and verifies the installed Runtime. VibeComfy
is installed with `--no-deps` beside the Runtime and agent, so it uses the
normal installed Python package import path; no `ASTRID_VIBECOMFY_CHECKOUT`
setting is needed. The selected manifest is recorded in the managed version
directory. For an existing installation, add `--update` to qualify new source
pins against an existing workspace, including the normal backup and rollback.

Astrid's installer supplies VibeComfy. If an older installation reports that
the package is missing, run the installer line with `--update` in a terminal
outside chat. Repair keeps the installed program code, so repair alone will not
add a missing package. The agent must not install VibeComfy or restart Astrid
from inside a chat; a service restart ends the chat and can leave Astrid down.
