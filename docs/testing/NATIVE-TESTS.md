# Hands-on native checks (optional; not part of the release-1 plan or gate)
> **Status:** release 1 is qualified entirely in GitHub Actions. This sheet documents how to check the three things CI can't reproduce: the Pinokio desktop app's own Stop and Quit, a Windows 11 consumer setup, and logout and reboot. Run it when a Windows consumer machine or the Pinokio desktop app is available, before advertising broader Windows or Pinokio support, or when a user report points here. It's referenced from `docs/setup/release-recipe.md` (section text: `Optional native checks`). This file is the canonical copy in `docs/testing/`.

*About 1–2 hours per OS. Anyone careful can run this. Use a spare machine or account, never one with real projects. Record the result of each step (pass / fail plus a note) in the release-candidate issue, and attach `Copy diagnostics` output for any failure.*

**You'll need (from Q):**
- the **frozen** release-0 bootstrap and its local manifest server, from `tests/fixtures/release0/`;
- the candidate bootstrap served from an **isolated test origin**;
- for Pinokio, a **test fork or branch** of the launcher that you advance yourself.

**Never publish to production, and never touch the real tracked launcher branch.** Each run uses a fresh account or VM. On Linux, use systemd 254 or newer for the in-app button (older shows the install command; that's expected).

## A. Every OS (macOS, Windows 11, Linux desktop)
1. **Install release 0** with the current public line (direct install).
2. **Create a project:** add a clip or image, make an edit, change a setting. Note the project name.
3. **Update to release 1 with the release-candidate install line.**
   - Expect: it completes, Astrid opens, and the project and setting are intact.
4. **Log out and back in** (Windows: sign out, or reboot).
   - Expect: Astrid starts by itself and the project is still there.
5. **In-app Update** to the stand-in release 2 (point the isolated origin at the stand-in manifest; reset it afterwards).
   - While a draft is unsaved, click Update. Expect: the draft is saved first, then the update runs and the page reconnects.
6. **Busy refusal:** start a long task, then click Update.
   - Expect: "Astrid is busy…", and nothing changes.
7. **Uninstall, keeping data,** then reinstall.
   - Expect: the same projects are back.

## B. Pinokio (each OS, real Pinokio desktop app)
1. **Install Astrid in Pinokio from launcher `main`** (release 0). Create a project as in A2.
2. **With Astrid running, advance your test launcher branch to the candidate** (open Pinokio's Update action first, so the old steps are already built), then **click Update**.
   - Expect: either it succeeds, or "Stop Astrid in Pinokio, then click Update" with nothing changed.
3. **If refused: click Stop, then Update.**
   - Expect: it succeeds, "Open" appears only when ready, and the project is intact.
4. **Stop while a task is running.**
   - Expect: either a clean stop, or the task fails cleanly on the next start. Nothing hangs; there's no "running" for ever.
5. **Update, then Quit Pinokio.** Check the process list (Activity Monitor / Task Manager / `ps`).
   - Expect: **no Astrid process left running.**
6. **Fully local mode** (one OS is enough): repeat 1–3 in fully-local mode.
   - Expect: the editor is rebuilt and local edits are kept.
7. **Reset with an update pending** (kill Pinokio mid-update, reopen, choose Reset).
   - Expect: Reset refuses until Update has recovered.

## C. Windows 11 consumer setup (extra)
1. **A standard (non-admin) user whose name has a space and an accent** (e.g. `José Ana`): install, then update.
   - Expect: success, with correct paths in Settings → diagnostics.
2. **A OneDrive-backed profile** (Documents and Desktop redirected): install, then update.
   - Expect: success. If it's refused, report the exact message; it goes in the release notes as a known risk and is filed for the next release.
3. **Defender on** (default): install and update. Expect: no quarantine, no locked-file failure.
4. **Windows PowerShell 5.1** (`irm … | iex` in "Windows PowerShell", not PowerShell 7): install.
   - Expect: success.

**Report:** each step pass or fail. Any failure goes to Q as an `r1/` bug, with the diagnostics attached.
