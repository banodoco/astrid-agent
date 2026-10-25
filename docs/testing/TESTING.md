# Testing Astrid on your computer

You're testing the path a new person takes: one line installs Astrid on this computer, and the page at
https://astrid.haus connects to it. Work through the journeys below in order; together they take 30–45 minutes per
OS. When a step fails, write down what you did and what you saw (the exact words, or a screenshot), and attach a
doctor bundle (see [Reporting](#reporting)). The bundle is a small zip with Astrid's status, its versions and the
last part of its installer and Runtime logs. Passwords, tokens, keys, credentials, home paths and user names are redacted.
Chat and other service logs (`service-*.log`) are excluded from the bundle.

## Before you start

- Use Chrome or Edge. Safari cannot reach Astrid (WebKit blocks an https page from calling `http://127.0.0.1`; checked 21 Oct with Playwright WebKit 26.4, real Safari not yet confirmed). Firefox reached the health endpoint from astrid.haus (Playwright Firefox 150: 200, CORS-readable) but is not otherwise tested.
- macOS and Linux: use Terminal. Windows: use **Windows PowerShell** (not Command Prompt).
- You don't need administrator rights. Don't run the line with `sudo` or "Run as administrator".
- Use a computer where Astrid holds nothing you need. The uninstall journey deletes Astrid's data at the end.

### Where things live

| | macOS | Linux | Windows |
|---|---|---|---|
| Program files | `~/.local/share/astrid` | `${XDG_DATA_HOME:-$HOME/.local/share}/astrid` | `%LOCALAPPDATA%\Astrid` |
| Your workspace (projects, media) | `~/Library/Application Support/Astrid Runtime` | `${XDG_DATA_HOME:-$HOME/.local/share}/astrid-runtime` | `%LOCALAPPDATA%\Astrid Runtime` |
| Installer log | `~/Library/Logs/Astrid Runtime/install.log` | `${XDG_STATE_HOME:-$HOME/.local/state}/astrid-runtime/logs/install.log` | `%LOCALAPPDATA%\Astrid Runtime\logs\install.log` |
| Runtime log | `~/Library/Logs/Astrid Runtime/runtime.log` | `${XDG_STATE_HOME:-$HOME/.local/state}/astrid-runtime/logs/runtime.log` | `%LOCALAPPDATA%\Astrid Runtime\logs\runtime.log` |
| Runtime service log | `~/Library/Logs/Astrid Runtime/runtime-service.log` | `${XDG_STATE_HOME:-$HOME/.local/state}/astrid-runtime/logs/runtime-service.log` | `%LOCALAPPDATA%\Astrid Runtime\logs\runtime-service.log` |
| What keeps Astrid running | LaunchAgent `haus.astrid.runtime` | systemd user unit `astrid.service` | Task Scheduler task `Astrid Runtime` |
| Optional service settings | `~/.local/share/astrid/service-env.json` | `${XDG_DATA_HOME:-$HOME/.local/share}/astrid/service-env.json` | `%LOCALAPPDATA%\Astrid\service-env.json` |

On Linux, `XDG_DATA_HOME` sets the program and workspace roots, and `XDG_STATE_HOME` sets the log root.

The optional service settings file is private JSON. It starts as `{}`. To change the engine idle timeout or allow a GPU
pack that is not qualified yet, edit it with string values, for example:

```json
{
  "ASTRID_WARM_IDLE_SECONDS": "600",
  "ASTRID_ALLOW_UNQUALIFIED_GPU": "1"
}
```

Only those two keys are accepted. After saving, run the install line again to restart Astrid; the
service reads the file when it starts. Shell profile changes do not reach the login service.

### Two checks you'll use often

**Is Astrid answering?**

macOS and Linux:

```sh
curl -s -H "Origin: https://astrid.haus" http://127.0.0.1:17340/api/runtime/v1/health
```

Windows:

```powershell
curl.exe -s -H "Origin: https://astrid.haus" http://127.0.0.1:17340/api/runtime/v1/health
```

Good: a line of JSON where `status` is `ok`. Nothing at all means Astrid isn't answering.

**Is the login service running?**

macOS:

```sh
launchctl print "gui/$(id -u)/haus.astrid.runtime" | grep -E 'state =|pid ='
```

Good: `state = running` and a `pid`.

Linux:

```sh
systemctl --user status astrid.service --no-pager
```

Good: `active (running)`.

Windows:

```powershell
Get-ScheduledTask -TaskName 'Astrid Runtime' | Select-Object TaskName, State
```

Good: `State` is `Running`.

## 1. Install with the one line

**Steps**

1. Open https://astrid.haus in Chrome or Edge. Click **Set up Astrid**, then **Copy install command**.
2. Paste it into Terminal (macOS, Linux) or PowerShell (Windows) and press Enter. These are the lines:

   macOS and Linux:

   ```sh
   curl -fsSL https://astrid.haus/install | sh
   ```

   Windows:

   ```powershell
   irm https://astrid.haus/install.ps1 | iex
   ```

3. Wait. The first install prepares Python, Bun, Node and Astrid itself, so it can take 10 minutes or more. This build
   pins uv 0.11.32, Bun 1.3.14 and Node 20.19.4. It checks for FFmpeg and FFprobe on `PATH` first. If that build
   passes Astrid’s required encoder and filter probe, it uses it; otherwise it downloads the pinned FFmpeg build.
4. When the browser opens, choose **Allow** when the browser asks about devices on your network.
5. Run the same line a second time.

**You should see**

- The line first prints `Getting Astrid ready. The first time takes a few minutes.`, then
  `Using your workspace at …`, `Installing Astrid.`,
  `Starting Astrid at sign-in.`, `Open https://astrid.haus`.
- Your default browser opens astrid.haus. If it isn't Chrome or Edge, open https://astrid.haus in Chrome or Edge yourself.
- The page says **Choose Allow in Chrome** (or Edge). After you allow, it opens the editor by itself. A new workspace
  shows **No projects yet**.
- macOS may show a notice that a background item was added. That's the login service. Note the name it shows.
- The second run keeps the healthy Runtime running and prints `Using your workspace at …` and
  `Open https://astrid.haus`. The page connects again without reinstalling tools.
- The two checks above both pass.
- The install record and doctor bundle identify the FFmpeg source as `path` or `download`, along with its version.

**It's a failure if**

- The line prints `Astrid could not get ready.` or `Astrid could not finish; …`. Copy the whole message, including
  the `Details:` line.
- On Windows, the PowerShell window closes by itself. Open the log from the table above and attach it.
- The page stays on a connection screen after you chose Allow.
- The second run installs a new version or changes your workspace.
- Linux only: the line prints `There is no systemd user service; …`. Note it. Astrid runs, but it won't come back after
  a reboot or a crash (see Known gaps).

## 2. A cleared browser or a new browser profile

**Steps**

1. With Astrid running, open a new Chrome profile or an incognito window (Edge: InPrivate). Go directly to
   https://astrid.haus/tools/video-editor.
2. Before clicking anything, confirm the page says **Set up Astrid on this computer** and shows the install line.
   There should be no local-network permission prompt and the page should not try to connect yet.
3. In that profile, open `https://astrid.haus/?connect=1#continue`. When the browser asks about devices on your
   network, choose **Block**. The page should show **Allow Astrid in Chrome** with two Site settings steps. Follow
   them to set Local network access to Allow, then return to the tab; it should open the editor once Astrid is found.
4. Stop Astrid:

   macOS:

   ```sh
   launchctl bootout "gui/$(id -u)/haus.astrid.runtime"
   ```

   Linux:

   ```sh
   systemctl --user stop astrid.service
   ```

   Windows:

   ```powershell
   Stop-ScheduledTask -TaskName 'Astrid Runtime'
   ```

5. While Astrid is stopped, open another fresh profile or incognito window and go directly to
   https://astrid.haus/tools/video-editor. Confirm it shows setup before any permission instructions. Then open
   `https://astrid.haus/?connect=1#continue` and choose **Allow** when the browser asks.
6. Look at your first browser (the one from journey 1) too. Reload it if it hasn't changed. Click **Copy** on that
   page and run the line it gives you.

**You should see**

- Step 2: **Set up Astrid on this computer** appears before any permission instructions.
- Step 3: after you block the browser prompt, **Allow Astrid in Chrome** shows the two Site settings steps. After
  Local network access is allowed, the editor opens with your workspace.
- Step 5: the fresh profile shows setup before the prompt. After you allow it while Astrid is stopped, it still says
  **Set up Astrid on this computer**; this profile has not connected to Astrid before.
- Step 6: the browser that connected before says **Astrid isn't running**, with the same line.
- Step 6: the line prints `Restarting Astrid with the installed version.` Both pages open the editor by themselves.
- Windows: while Astrid is healthy and running, run the copied repair line again. Confirm the page opens and the
  Runtime process keeps the same PID; the repair should not interrupt active work. Then stop the scheduled task and
  run the line once more. Confirm Astrid starts and the page reconnects.

**It's a failure if**

- A page asks for the browser's permission before you clicked anything (except right after the install line opened it).
- A browser that has connected before says "Set up" instead of "isn't running", or the other way round.
- The line on the screen is for the wrong OS.
- A page doesn't open by itself once Astrid is back (wait 30 seconds before you count it).

## 3. Chat with each agent

**Steps**

1. In the editor, click **New project**, then open its timeline. Open the chat panel.
2. Click the small arrow next to **New** ("New chat with…"). Note what each agent says: Astrid, Claude Code, Codex.
3. For each agent marked Ready (or with a model name), start a chat and send: `Reply with the word hello.`
4. If you have Claude Code or Codex signed in, sign out of one:

   ```sh
   claude auth logout
   ```

   ```sh
   codex logout
   ```

   Then close and reopen the menu (reload the page if it hasn't changed). Click the agent you signed out of.
5. Sign back in, then reopen the menu:

   ```sh
   claude auth login
   ```

   ```sh
   codex login
   ```

**You should see**

- Each agent shows one state: Ready (or its model name), **Sign in** or **Not installed**, with one line underneath
  when it isn't ready.
- A ready agent answers within about 30 seconds.
- A signed-out Claude Code or Codex shows **Sign in** and its matching `claude auth login` or `codex login` command.
  A signed-out Astrid agent shows the absolute path of its launcher inside the program folder: `run-astrid` on
  macOS/Linux or `run-astrid.cmd` on Windows. Run that copied invocation in a fresh terminal, then enter `/login`
  inside Astrid. You should not need to change PATH or a shell profile. Clicking a sign-in line copies it and shows
  **Copied**; it doesn't start a chat.
- An agent you haven't installed shows **Not installed** and an install line.
- After you sign back in, the agent shows Ready again.

**It's a failure if**

- An agent you've installed and signed in shows **Not installed** or **Sign in**. Write down how you installed it
  (for example npm, Homebrew or the official installer).
- The chat panel says `Chat isn't running on this computer right now.` while the page is connected.
- You see a raw error, an error code or "404" anywhere in chat.
- An agent you're signed in to shows **Sign in**, or one you're signed out of shows **Ready**.

## 4. A GPU task, then idle

Do this only if the person who sent you this kit set up a generation engine (Wan2GP or ComfyUI) on your computer.
The install line doesn't set one up (see Known gaps). Skip it otherwise. Keep the engine checkout and its environment
outside Astrid's program folder; uninstall removes that folder. On Windows, installer-managed GPU environments live
under `%LOCALAPPDATA%\Astrid GPU`.

Astrid keeps an engine loaded for **10 minutes** after its last task, so the next task starts fast. Then it stops
the engine, which frees the GPU memory. The window defaults to 600 seconds; set `ASTRID_WARM_IDLE_SECONDS` in the
service settings file under [Where things live](#where-things-live) if you need to test another value, then run the
installer line again to restart the service.

**Steps**

1. Note the GPU memory in use before you start (NVIDIA, on Linux or Windows):

   ```sh
   nvidia-smi
   ```

   On macOS, open Activity Monitor and find the engine's `python` process instead. On Windows, untested NVIDIA
   combinations run by default; the `ASTRID_ALLOW_UNQUALIFIED_GPU` setting is no longer needed.

2. Run a short generation the way the kit's sender told you, for example by asking Astrid in chat.
3. Run `nvidia-smi` again while it runs, and again right after it finishes.
4. Leave Astrid idle. Don't start other tasks, and don't restart Astrid. After 12 minutes, run `nvidia-smi` again.

**You should see**

- While it runs, and right after it finishes: the engine process is listed and holds GPU memory. That's the warm hold.
- After 12 minutes: the engine process is gone, and the memory in use is back near where it started.

**It's a failure if**

- The memory is still held after 15 minutes of idle.
- The engine stops while a task is still running.
- Windows NVIDIA: the task runs, but the combination is untested. Report how it went, including the GPU model and
  driver version from `nvidia-smi`. Wan2GP still requires a detected NVIDIA GPU and working driver.

## 5. Kill the Runtime, and it comes back

**Steps**

1. Have the editor open on a project.
2. Kill Astrid's Runtime:

   macOS:

   ```sh
   launchctl kill SIGKILL "gui/$(id -u)/haus.astrid.runtime"
   ```

   Linux:

   ```sh
   systemctl --user kill --signal=SIGKILL astrid.service
   ```

   Windows:

   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.CommandLine -like '*astrid_runtime*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
   ```

3. Watch the page. Run the two checks.
4. Once it's back, send a chat message to a ready agent.

**You should see**

- The page shows **Astrid stopped**, then **Astrid restarted** with a **Reload** button. After Reload, your project is there.
- macOS restarts it within about 20 seconds (launchd's KeepAlive). Linux restarts it after about 5 seconds
  (systemd's Restart=always). Windows retries after 1 minute (Task Scheduler's repeating sign-in trigger).
- Chat answers again.

**It's a failure if**

- Astrid isn't back after 1 minute (macOS, Linux) or 3 minutes (Windows). Then run the install line: it should print
  `Restarting Astrid with the installed version.` and bring it back. Report both.
- Your project or recent changes are missing after Reload.
- Chat doesn't answer after the restart.

## 6. Reboot, or sign out and back in

**Steps**

1. Sign out of your computer's account and sign back in. Don't run anything.
2. Wait 1 minute, then open astrid.haus in the browser you used in journey 1.
3. Restart the computer, and do step 2 again.

**You should see**

- The two checks pass without you running anything.
- The page opens the editor by itself, with your project. It doesn't ask for permission again.

**It's a failure if**

- Astrid isn't running after sign-in. Note it, then run the install line to bring it back.
- Windows: the Task Scheduler task exists but isn't `Running`, or a console window shows up at sign-in.
- Linux: systemd user services start only once you sign in. Not running before sign-in isn't a failure.

## 7. Update

Before updating, leave a recognizable conversation in each agent you configured in journey 3.
Record its title and a sentence you can find again.

**Steps**

1. Run the update line:

   macOS and Linux:

   ```sh
   curl -fsSL https://astrid.haus/install | sh -s -- --update
   ```

   Windows:

   ```powershell
   & ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --update
   ```

   Windows qualification: Astrid must still be running when you run this line. Confirm the update succeeds while its
   service is writing `runtime-service.log`, then check that `install.log` and `runtime-service.log` both remain in
   the log folder. The doctor bundle should include the installer and Runtime logs, including `runtime-service.log`,
   and exclude other service logs (`service-*.log`).

2. Watch the page while it runs, and reload it if it asks.
3. List the program versions and backups:

   macOS:

   ```sh
   ls ~/.local/share/astrid/versions ~/.local/share/astrid/backups
   ```

   Linux:

   ```sh
   ls "${XDG_DATA_HOME:-$HOME/.local/share}/astrid/versions" "${XDG_DATA_HOME:-$HOME/.local/share}/astrid/backups"
   ```

   Windows:

   ```powershell
   Get-ChildItem "$env:LOCALAPPDATA\Astrid\versions", "$env:LOCALAPPDATA\Astrid\backups"
   ```

**You should see**

- The line prints, in order: `Stopping Astrid before the update.`, `Backing up your workspace.`,
  `Installing Astrid.`, `Updating the workspace with Astrid Runtime.`, `Starting Astrid at sign-in.`,
  `Open https://astrid.haus`. After a successful update, it prints `A backup of your workspace is kept at …`.
- The page shows **Astrid stopped**, then **Astrid restarted**. After Reload, the same projects are there.
- Two folders under `versions` (the new one and the previous one) and one under `backups`.
- Your earlier conversations still appear once in the session list and reopen with their messages.
  If you selected a custom chat folder or session directory, that selection is retained.
  The non-manifest npm fallback OMP 18.12.6 has a known session-folder gap described under
  [Known gaps](#known-gaps); record what happens there without treating it as workspace loss.

**The "Update Astrid" screen.** The page shows **Update Astrid** only when your Astrid is older than the page. You
can't bring it up on purpose. If you see it, click **Copy**, run that line, and check the same things as above.
Note which OS line it gave you.

**Rollback (optional).** This checks that a failed update puts everything back.

1. Run the update line again. Progress appears as it happens. When it prints
   `Installing Astrid.`, press Ctrl+C once.
2. Keep the terminal open and wait for recovery to finish and the failure message with its `Details:` line.
   Do not press Ctrl+C again while Astrid is restoring the previous version.
3. Run the two checks and open the page.
4. Native Windows qualification: run the update line once more and let it finish. Confirm the rollback restarted
   the previous version, the retry succeeds, and the page still shows the same workspace and projects. Check the
   installer log for permission/DACL errors opening credentials or source profiles; none should appear.

You should see `Astrid couldn't finish. Your work is kept.` followed by a
`Details:` line. Astrid then runs the previous version, on the same workspace, with the same projects. It's a failure if Astrid
isn't running afterwards, a project is missing, or the log says `recovery also failed`. If it isn't running, run the
plain install line and report what it prints.

## 8. Uninstall

Make a doctor bundle first (see [Reporting](#reporting)); there is no installed version for doctor after uninstall.

**Steps**

1. Run the uninstall line:

   macOS and Linux:

   ```sh
   curl -fsSL https://astrid.haus/install | sh -s -- --uninstall
   ```

   Windows:

   ```powershell
   & ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --uninstall
   ```

2. Check that the login service is gone:

   macOS:

   ```sh
   launchctl print "gui/$(id -u)/haus.astrid.runtime"; ls ~/Library/LaunchAgents/haus.astrid.runtime.plist
   ```

   Linux:

   ```sh
   systemctl --user status astrid.service --no-pager
   ```

   Windows:

   ```powershell
   Get-ScheduledTask -TaskName 'Astrid Runtime'
   ```

3. Check that the managed program folder is gone. On Windows, wait briefly for the installer to remove its external
   bootstrap after the line returns, then check both folders:

   macOS:

   ```sh
   ls ~/.local/share/astrid
   ```

   Linux:

   ```sh
   ls "${XDG_DATA_HOME:-$HOME/.local/share}/astrid"
   ```

   Windows:

   ```powershell
   Test-Path "$env:LOCALAPPDATA\Astrid"
   Test-Path "$env:LOCALAPPDATA\Astrid Installer"
   ```

4. Run the plain install line once more (journey 1) to reinstall. The installer should adopt the kept workspace; check
   that `Using your workspace at …` names the same path, then open astrid.haus and confirm your projects and earlier
   conversations are still there.
5. Run the uninstall line a second time. It should complete safely.
6. Remove the workspace at the end of testing with the delete-data line below. Run it twice; both runs should complete
   safely, and the workspace should be gone.

**You should see**

- The line prints `Astrid was uninstalled.` and `Workspace kept at …`.
- Step 2: the service isn't found (macOS: "Could not find service" and no such file; Linux: the unit "could not be
  found"; Windows: no scheduled task found).
- Step 3: no such folder (Windows: `False` for both paths, without a manual cleanup command).
- After the first uninstall, the page says **Astrid isn't running**. After reinstalling in step 4, the page opens the same workspace.

**What's left behind, on purpose:**

- your workspace folder (projects and media), from the table above;
- the log folder on macOS and Linux (on Windows it sits inside the workspace folder);
- on macOS and Linux, the per-user `astrid-install.lock` next to the program folder; Windows removes its installer
  lock and bootstrap folder after the uninstall exits;
- the `astrid-install.json` selection record next to the program folder, supporting repeat uninstall and reinstall;
- installer-managed Windows GPU environments under `%LOCALAPPDATA%\Astrid GPU`, if you have any;
- uv's download cache (macOS: `~/Library/Caches/uv`; Linux: `${XDG_CACHE_HOME:-$HOME/.cache}/uv`;
  Windows: `%LOCALAPPDATA%\uv\cache`);
- your sign-ins for Claude Code, Codex and Astrid's model provider;
- the browser's permission for astrid.haus.

**It's a failure if**

- Astrid still runs, or comes back after a reboot.
- The workspace folder is gone (you didn't ask for that).
- The line prints `Astrid was unregistered; some files remain.` Copy each `Remaining:` line. On Windows, run the
  uninstall line again and say whether the list got shorter.

**Delete the workspace after testing.** Before continuing, copy the doctor bundle and install log to a folder
outside Astrid's workspace and program folders, such as a folder on your Desktop. Use the paths printed by doctor
and listed under [Where things live](#where-things-live). On Windows, the install log lives inside the
workspace and will be deleted with it; the default doctor bundle is already outside it. Confirm the copied files
open before proceeding.

This removes Astrid's workspace and projects. Run the uninstall line with `--delete-data` twice to check that the
command is safe to repeat.

macOS and Linux:

```sh
curl -fsSL https://astrid.haus/install | sh -s -- --uninstall --delete-data
```

Windows PowerShell:

```powershell
& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --uninstall --delete-data
```

After the first run, confirm the workspace path from the table above is gone. Keep the copies of the doctor bundle
and install log outside that path for your report.

## Reporting

For each failure, send:

- the journey and step number;
- your OS and version, CPU (Intel, Apple silicon, AMD, ARM), and GPU with its driver version if you have one;
- the browser and its version;
- what you did and what you saw: exact words or a screenshot, and the time;
- a doctor bundle, made right after the failure.

Make a bundle at the end of a run too, even if everything passed.

**Make a doctor bundle.** The installer line runs the doctor from the installed version, supplies the selected workspace
root, and saves the bundle on your Desktop (or in Downloads if there is no Desktop folder), then reveals its
location. It works while Astrid is down.

macOS and Linux:

```sh
curl -fsSL https://astrid.haus/install | sh -s -- --doctor
```

Windows PowerShell:

```powershell
& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --doctor
```

Look for `Astrid doctor bundle: <path>` and attach that ZIP using the
[Asking for help instructions](../setup/troubleshooting.md#asking-for-help). By default the file is called `astrid-doctor.zip`; the line can also
take an explicit bundle path after `--doctor`. An unhealthy Runtime still produces a bundle, and the
installer line succeeds when the bundle is written. If no installed version exists (for example, the install never
finished or Astrid was uninstalled), attach the install log from the table above instead.

**What the bundle holds:** `status.json` (Astrid's health check, with secrets and most paths removed), `versions.json`,
`install-manifest.json` (version numbers, source revisions, and selected FFmpeg/FFprobe tools and provenance), and
`logs/` (the last 256 KB of each installer and Runtime log, 1 MB at most). Passwords, tokens, keys, credentials, home paths and
user names are redacted. Chat and other service logs (`service-*.log`) are excluded.

## Known gaps

These are known. Report what you see, but don't stop testing for them.

**Chat history with the published OMP producer**

- Real installs select the reviewed `peteromallet/oh-my-pi` fork through
  `tools.omp.source`; the npm 18.12.6 fallback is for non-manifest installs and
  lacks the fork's session-store fix and `omp onboard`.
- The complete fork has been rebased onto `v18.12.6` and is delivered as
  `omp-fork-18.12.6.bundle` (`refs/heads/astrid/18.12.6`). Local ACP tests and a
  live process restart cover listing/loading the explicit store and legacy
  history. An unavailable saved model produces "Start a new chat" and leaves
  the old transcript intact.
- Deployment still requires the bundled fork head to be pushed unchanged to
  GitHub and selected by full SHA in the published app manifest. Local proofs
  do not qualify a deployed manifest or native macOS/Windows builds. See the
  [OMP producer landing guide](../install/OMP-LANDING.md).

**Windows**

- Native Windows has never run end to end. Windows CI remains advisory until its full lane has run green on a
  real Windows runner. Linux tests exercise mocked Windows branches; they do not qualify the install line, the
  sign-in task, Chrome on Windows or GPUs.
- Windows NVIDIA combinations are untested and run by default; please report results with the GPU model and driver
  from `nvidia-smi`. `ASTRID_ALLOW_UNQUALIFIED_GPU` is no longer needed (existing service settings may keep it).
  Wan2GP still needs a detected NVIDIA GPU; VibeComfy's local server uses the user's own ComfyUI GPU.
- The installer has not been run natively on Windows. Windows PowerShell 5.1 in particular remains unverified; if
  the line fails or its window closes, report the exact message and attach the install log.
- Coming back after the Runtime is killed uses Task Scheduler's repeating sign-in trigger. It needs native confirmation.
- Uninstall removes the external `%LOCALAPPDATA%\Astrid Installer` folder after exit. Confirm that the program and
  installer folders both disappear without a manual cleanup command.
- Windows on ARM cannot install: the pinned downloads have no Bun or FFmpeg build for it. The install line
  refuses before downloading the bootstrap environment.
- A PC whose execution policy is set by the organization stops with a message asking the administrator to allow the installer.
- OneDrive: Astrid's own folders are in `AppData\Local`, which OneDrive doesn't sync. Media you add from a
  OneDrive-redirected Desktop or Documents, especially online-only files, is untested.
- Browser capture and the managed ComfyUI engine profile are not available on Windows yet. They refuse before
  launching or preparing an engine. ComfyUI's separate existing-server route is unaffected; native managed-engine
  custody and Chromium protocol-handle transport are deferred.
- Try the literal install line and the scriptblock form with arguments in both Windows PowerShell 5.1 and PowerShell 7.
  Check execution policy, Ctrl+C, error output and exit status. Bootstrap behavior tests here use PowerShell on Linux;
  a real Windows executable fixture and required native bootstrap jobs are still deferred.
- Test sign-in, reboot, crash/restart and stop with Task Scheduler. Confirm there is no console window and no surviving
  child process. Exercise update, rollback, repair and uninstall while logs, SQLite files or binaries are held open.
- Test profiles with spaces and non-ASCII names, long paths and junctions; qualify OMP foreground shell commands after
  provider sign-in. Port 17340 exclusions under Hyper-V/WSL, firewall and browser local-network prompts need native checks.
- Real NVIDIA provisioning, inference, cancellation, repeated warm runs and GPU-memory release require a native tester.

**Every OS**

- Chrome's local-network permission is the browser's own prompt, and its wording varies by Chrome version. If you
  block it, only the two steps in site settings undo that. Chrome and Edge are expected to work. Safari is blocked
  (see Before you start); Firefox reached the health endpoint but is otherwise untested.
- The install line doesn't set up a GPU engine.
- The engine idle window defaults to 10 minutes. It can be changed in the service settings file under [Where things
  live](#where-things-live).
- Reinstalling after an uninstall that kept the workspace is part of journey 8: the installer should adopt the kept
  workspace and start Astrid on it. Repeating uninstall, including `--uninstall --delete-data`, should be safe.
- If the installer never completed, or Astrid has already been uninstalled, the installer’s `--doctor` command is
  unavailable; attach the install log instead.
- "Making new images isn't on this page yet." next to the compose controls is expected.
- macOS service and installer behavior has not been tested natively; report what happens with the LaunchAgent and
  include the install log and doctor bundle.

**Linux**

- Without a systemd user session (some minimal or container setups), nothing restarts Astrid after a crash or at
  sign-in. The line is the restart.
- With a systemd user session, Astrid restarts after a crash or an ordinary process kill. Stopping the unit with
  `systemctl --user stop astrid.service` keeps it stopped until the install line runs again.
