# Get help

Astrid is used at [astrid.haus](https://astrid.haus). If setup does not finish,
read the log at the installer's `Details:` path. Open Terminal on macOS or
Linux, or PowerShell on Windows, when you run an installer command below.

The supported setup commands are listed in [Getting Started](../getting-started.md).
For the default hosted route, cloning `astrid-app` is not a repair step.
For a source editor, follow the [fully local route](../getting-started.md#run-everything-on-your-computer).

## Asking for help

For help, open [Astrid issues](https://github.com/banodoco/astrid-agent/issues)
and attach the doctor bundle. By default, it is saved to your Desktop (or
Downloads if Desktop is unavailable) and its folder opens in Finder or File
Explorer. It contains installer logs and service states, with home paths, user
names, and secrets redacted. It excludes Runtime, chat, and service logs.

## Update Astrid

### macOS: retry an interrupted update

When an update replaces an older LaunchAgent plist, launchd may keep using the
action it already loaded. If the old Runtime crashes before C1 reaches
`launchctl bootout`, launchd can restart that still-selected Runtime from its
cached action. Replacing the file on disk does not prove launchd reloaded it.

This window is bounded: until C1 succeeds, the update has not selected the
candidate or backed up/migrated the workspace. If the old Runtime restarts,
wait for its work to finish, then retry through the normal Update path above.
Recovery republishes the admission action and routes the stop through Runtime's
C1 lifecycle fence before launchd bootout. If the fence reports busy, it leaves
the workspace and pending update intact so you can retry later. Astrid does not
bootout/bootstrap the service before C1 to refresh launchd's cached action.

The interruption scenario is covered with a modeled service-manager boundary.
Native launchd cache and restart behavior is **written, not run** until it has
been exercised on macOS.

**Copy for your agent:** “Update Astrid using the official command for my
operating system. If the previous update was interrupted, use the C1-fenced
recovery path above.”

macOS and Linux — **No coding agent? Run this in Terminal:**

```sh
curl -fsSL https://astrid.haus/install | sh -s -- --update
```

Windows — **Run it yourself in PowerShell:**

```powershell
& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --update
```

## Make a support bundle

macOS and Linux, in Terminal:

```sh
curl -fsSL https://astrid.haus/install | sh -s -- --doctor
```

Windows, in PowerShell:

```powershell
& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --doctor
```

The command prints the bundle path and opens its folder. If Astrid has not
finished installing or has already been removed, attach the install log
instead.

## Uninstall Astrid

macOS and Linux, in Terminal:

```sh
curl -fsSL https://astrid.haus/install | sh -s -- --uninstall
```

Windows, in PowerShell:

```powershell
& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --uninstall
```

Uninstall keeps your workspace. Add `--delete-data` only when you intend to delete it too.

Developers working on the Python toolkit or Runtime can use the developer
commands in [Getting Started](../getting-started.md#developer-reference).
