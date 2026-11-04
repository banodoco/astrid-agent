# Release 1 notes

## Supported operating systems

Release 1 supports the documented install and update paths on:

- macOS;
- Linux desktop; and
- Windows through Windows PowerShell (including the Windows 11 path used by
  the release qualification matrix).

Windows ARM64 is unsupported.

Windows consumer profiles and the Pinokio desktop app have not been exercised
as part of the release gate. See [Optional native checks](../setup/release-recipe.md#optional-native-checks-windows-consumer-machines-and-the-pinokio-desktop-app)
for the separate procedure and its scope.

## Update and recovery

Astrid keeps the hosted editor at [astrid.haus](https://astrid.haus) as the
default. Use the channel that matches how Astrid is installed.

### Install line

Run the official install line again to install or repair an existing
installation. To request an update explicitly:

macOS or Linux:

```sh
curl -fsSL https://astrid.haus/install | sh -s -- --update
```

Windows PowerShell:

```powershell
& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --update
```

If an update was interrupted or the installer reports that recovery is needed,
run the same update command again. The installer owns the update transaction,
including recovery and rollback. Keep the printed diagnostics if the retry
does not complete.

### In-app Update

Use Update in the hosted editor. The editor saves a guarded draft before
requesting the update; if the Runtime reports that it is busy, wait and retry.
If the update needs recovery, choose Update again so the pending operation is
reconciled before the new request is accepted.

### Pinokio Update

Use Pinokio's existing Update action. If Astrid is running and the update is
refused, use Stop in Pinokio and then click Update. The update must not proceed
after a refused or unknown stop. The existing Pinokio local route remains the
route for fully local mode.

### Fully local mode

The hosted editor remains the default; fully local users keep the existing
local editor route. Update the editor source from its clone, then reinstall
its dependencies and restart it:

```sh
git pull --ff-only origin main
npx -y npm@11 ci
npm run dev:local
```

Update the Runtime and agent through the official installer with `--update`,
then restart the local editor. Preserve local changes before pulling and
resolve any Git conflict without discarding them.

## Known risks and boundaries

- A Pinokio Quit may leave an Astrid process alive. The Quit-process change is
  deferred; this is a known risk for a later release.
- Real consumer Windows profiles (including redirected or non-ASCII profiles)
  have not been tested in the release gate. Use the optional native procedure
  when such a machine is available.
- Windows ARM64 is unsupported.

The optional native checks are not a release-1 publication gate. A failure
found there should be recorded as a next-release bug unless the release owner
decides otherwise.
