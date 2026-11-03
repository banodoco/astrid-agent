# R1 Astra review — install and onboarding consent

**Verdict: BLOCKED.** The frozen combined candidate has **12 blocking fix tasks: 10 Normal, 2 X-hard, no Super-hard**. The editor has a parse regression; setup choices and destinations are lost across onboarding; several command/lifecycle paths bypass the agreed policy; and raw diagnostics and foreign skill handling violate safety requirements. The compatible manifest alone does not make this release ready.

This is review only. No product files, acceptance, published heads or refs were changed. No installer, dependency installation, build, pytest/Vitest suite, deployment, post, credential access or native Windows run was performed. One read-only helper covered App composition; helper count **1**. Disk preflight reported 23 GB available and STOP-DISPATCH was absent. Only lightweight source/parser probes ran; task-created probe scratch was deleted.

## Frozen provenance and evidence

- **G** = Agent `12f401eb8f707234697672317c8995a4637dc54d`, object database `/home/user/astrid/ws-I/work/05`; immediate task-4 base `71e08cc8a4f1039757224f166602d5d22525ff22`; cumulative Release-1 base `fb222b3a54e9ffe2fa4eb2c382bc25033e773949`.
- **A** = App `20c4c35dcf8c124fbb99c252b446aac6c95dc1e7`, `/home/user/astrid/ws-I/work/07-r2`; immediate task-6 base `0a0b14ed8a545a242634266202af878d5ab3d65f`; cumulative Release-1 base `8948f0dad225cb8588ede98ec0ca240ac7a547c9`.
- Agent work/05 is clean at `20966e50ba3dd972ac049fe92b16d017082bbfe3`, **not** the frozen head. `git diff 20966e50... 12f401...` changes only `tests/test_skills.py`; all production files cited below have identical bytes/line numbers at the frozen head. The frozen test and complete cumulative/immediate diffs were read with Git, without moving the checkout. App is clean at its exact frozen head.
- All `G:path:line` / `A:path:line` citations mean the exact commits above. **W** = `/home/user/astrid/ws-I`. Reviewed the five requested project documents, STATUS, all seven accepted reports, task-5 r1 context, the cumulative implementation/test diffs, relevant retained preflight/failure/final logs, and the actual production callers beyond changed files.
- New evidence: [App parser probe](logs/app-parser-repro.log), [Agent source-function probes](logs/agent-source-repros.log), with reproducible scripts in the same logs directory. App parser comparison reads exact base/head TSX blobs. Agent probes execute named functions extracted verbatim from exact Git blobs with stdlib dependencies and task-local fixtures; they are **not** installed-artifact or lifecycle qualification. Other repros below are explicitly source-derived, not claims of execution.
- Retained evidence includes task-1 base/head lifecycle parity; task-2 13 shared failures; task-3 final 72/72 and proof 157/157; task-4 203/205 base and 213/215 head; task-5 r2 47/55 on both round base/head; task-7 26/26 Setup and 21/22 manifest on both base/head. Task-6 reports 194 selected passes, but its accepted folder has no standalone `logs/` test packet. None of these proves the whole composition. In particular, a task-2 regression remains a Release-1 regression even when task 4/5 calls it “base-identical.”

## Blocking fix tasks

### R1-N1 — Normal — Repair the production editor parse error

**Finding / acceptance:** New Release-1 regression breaking the dependent editor/onboarding row. A:`src/shared/components/RuntimeConnection/EmptyWorkspaceGuidance.tsx:25` places unescaped backticks around `astrid` inside a template literal. A:`src/tools/video-editor/pages/VideoEditorPage.tsx:3` imports this module, so even users who never see the empty-workspace card encounter the invalid module.

**Evidence/repro:** `node logs/app-parser-repro.cjs` (absolute task path also works). TypeScript 5.9.3 parse-only result: cumulative base has zero diagnostics; task-6 base and exact candidate both report `TS1005: ',' expected` at **25:245**. Task-6's focused test/lint list omitted this changed component.

**Smallest fix:** Escape/remove the embedded formatting backticks.

**Done when:** The exact candidate source parses with zero diagnostics and the narrow existing `src/shared/components/RuntimeConnection/EmptyWorkspaceGuidance.test.tsx` selector passes through the heavy gate. No broad build is needed to establish this fix.

### R1-N2 — Normal — Preserve onboarding choices, local mode and requested destination

**Finding / acceptance:** Violates user acceptance **1/3/9**, PROPOSAL Design lines 9/11, and `project/astra-review.md:27` (retain requested destination). Related losses arise at the Welcome handoff:

- A:`src/shared/components/RuntimeConnection/SetupBlock.tsx:30` and `:32` keep already-set-up and choices only in component state. A:`src/app/onboarding/WelcomeFlow.tsx:46`, `:53` and `:55` unmount it; `WelcomeConnection.tsx:15` and `RuntimeConnectionPanel.tsx:320` mount a new default block with none of those selections.
- A:`src/app/onboarding/WelcomeRunMode.tsx:46` enters generic `setup`, while `WelcomeFlow.tsx:25` restores local mode only for phase `local`. Reload loses the selected local route.
- A:`src/app/onboarding/WelcomeFlow.tsx:38` prioritizes `connect=1` over project/editor destinations at `:39`. `WelcomeConnection.tsx:16` then chooses a starter project through `StarterProjectHandoff.tsx:31` / `:52`, using stored preference rather than the requested URL. The setup-completion history-token branch at `welcomeProgress.ts:62` repeats the same connect-first precedence.

**Source-derived repro:** (a) Fresh Welcome, choose all three effects off and copies on, Continue before running the command, then use Not-running: its fresh-install command becomes bare `--yes`. (b) Choose fully local setup, reload before Continue or follow the installer handoff: generic setup restores hosted mode. (c) First visit `?project=P&timeline=T&connect=1#edit` with no storage and another newer project Q: completion can select Q and discard P/T/hash. Base work-link routing had precedence over connect.

**Smallest fix:** Give pending onboarding state one owner; carry choices and checkbox into receiving blocks, persist the local-mode discriminator, and preserve work/editor destination precedence through completion. Keep both query routes.

**Done when:** Focused real Welcome composition tests cover all-off/copies-on and checked-state handoffs, local reload/installer handoff, combined work/editor+connect query parameters and hash retention. Plain `?connect=1` still connects; local recovery still leads with `npm run dev:local`.

### R1-N3 — Normal — Remove Customize from the actual checked path

**Finding / acceptance:** Violates user acceptance **2**, `project/PROPOSAL.md:10`: checked means Start Astrid, no Customize, no consent question. The shared component's condition is overridden by real callers.

**Evidence/repro:** A:`src/shared/components/RuntimeConnection/SetupBlock.tsx:47` allows `forceCustomize` to defeat checked state. Not-running passes it at `RuntimeConnectionPanel.tsx:320`, the stopped pill at `RuntimeConnectionPill.tsx:88`, and Settings at `SettingsModal/sections/AstridStatusSection.tsx:130`. Source-derived repro: remembered connection true, or tick the checkbox in an actual panel; Start Astrid still has Customize. The bare-component test does not use these props.

**Smallest fix:** Make the visible checkbox authoritative for the checked/unchecked presentation. Retain Settings' explicit four-choice emission; unchecking can expose its proposed choices.

**Done when:** Actual panel/pill/Settings tests assert no Customize or consent question when checked, Customize and one question when unchecked, and identical command bytes when only the checkbox is toggled.

### R1-N4 — Normal — Route ordinary manual updates through the shared setup choices

**Finding / acceptance:** Violates user acceptance **3/9**, `project/PROPOSAL.md:9` and `:11`. Autostart-off refusal uses SetupBlock, but ordinary update alternatives still expose a separate command without choices.

**Evidence/repro:** A:`src/shared/components/RuntimeConnection/UpdateResolutionActions.tsx:42` renders `CopyInstallCommand mode="update"`; its `CopyInstallCommand.tsx:81`, `:82` and `:99` build/display the old command. A:`src/shared/components/RuntimeConnection/copy.ts:251` returns `--update` before processing selections; the later local agent handoff also embeds the old update aliases at `copy.ts:149`. Settings/chat paths also use it (`SettingsModal/sections/AstridStatusSection.tsx:145`, `tools/video-editor/components/AgentChat/ChatUpdateNeeded.tsx:22`, `NewChatMenu.tsx:28`). Source-derived repro: ordinary installer update-needed card → manual/agent alternative: no choices. Calling `installCommand('update', {...defaults, skills:false})` drops the off selection.

**Smallest fix:** Use the common choice-aware install/restart/update line and shared block for installer-owned alternatives. Preserve Update now, draft flushing, restart/reload behavior, and the distinct local npm-update/Pinokio/source routes.

**Done when:** Focused ordinary-update card and builder tests assert visible setup choices and manual/agent command equality with `--yes` plus changed flags; Update now remains available. Autostart-off refusal still offers a runnable command.

### R1-N5 — Normal — Make terminal consent visible and honor displayed defaults

**Finding / acceptance:** Violates the terminal Y/n/c row (`project/PROPOSAL.md:21`) and choice fidelity in Design lines 11/12.

**Evidence/repro:** G:`astrid/installer/cli.py:71` displays copies as `[y/N]`, but `:76`/`:77` interprets every empty Customize answer as True. Executed source probe `prompt_custom_enter`: `c`, Enter four times returns `skill_copies=True`, although the displayed default is off.

There is also a cross-repository Windows prompt defect: G:`astrid/installer/cli.py:55`/`:57` and `:73`/`:75` flush a **non-newline** prompt and then read input. A:`public/install.ps1:29`–`:33` redirects stdout while retaining console input; the actual invocation uses StreamOutput at `:274`. Its handler emits completed lines only (`:146`, `:152`, `:162`). Therefore the newly introduced question is buffered until input produces later output. This is a source-derived Windows composition finding, **not a Windows run**.

**Smallest fix:** Empty response selects `RECOMMENDED_CHOICES[name]`; make each prompt visible before input through the existing bootstrap (newline-terminated flushed prompts are the smallest change).

**Done when:** Extend `tests/test_installer_terminal_summary.py::test_prompt_customizes_each_supported_choice` with all empty Customize answers and explicit y/n. A focused process-adapter check observes each question before answering. Keep fresh unattended refusal and decline-before-effects unchanged; native Windows remains “not run” unless actually executed.

### R1-N6 — Normal — Keep raw verification diagnostics out of the public summary

**Finding / acceptance:** Safety regression introduced in Release 1. G:`astrid/installer/cli.py:163` prints the full exception; `safe()` at `:102` merely escapes non-ASCII. The safe-message boundary at `:576` is immediately undone by passing the original exception into the summary at `:580`.

**Evidence/repro:** G:`astrid/installer/backend.py:1215` and `:1248`–`:1251` deliberately separate raw log details from `safe_message`. Existing selector `tests/test_installer_lifecycle.py::test_cli_shows_safe_verification_context_but_not_raw_diagnostics` fails on the exact head in `queue/done/05_SKILLS_POLICY-r2/logs/head.log:31`, printing its synthetic private-token marker. Earlier task-4 logs show the same issue, not an exemption. The cumulative base has no raw-exception summary. The retained source probe independently confirms raw text reaches the summary; no real secret was read.

**Smallest fix:** Render the existing safe exception projection (or exception type plus log location), not arbitrary `str(failure)`.

**Done when:** The named selector passes without weakening its negative assertion; generic subprocess/service-log exceptions also cannot leak raw diagnostics in the summary. Retain useful safe context and the log pointer.

### R1-N7 — Normal — Make plain skills-off commands report and persist successfully

**Finding / acceptance:** Violates user acceptance **7**, the saved-policy/apply requirement at `project/PROPOSAL.md:12`, and truthful result reporting.

**Evidence/repro:** G:`astrid/skills/__init__.py:186` creates off actions with `removed`/`preserved`, no `steps`. G:`astrid/skills/cli.py:391` indexes `action['steps']`. Both off entry points call this renderer **before** `_save_explicit_skill_policy` (`:175`/`:176`, `:207`/`:208`). The source probe reproduces `KeyError('steps')` for the exact report shape.

Source-derived end-to-end repro: with saved skills on and installed skills, run `python -m astrid.skills sync --skills off` without `--json`. Cleanup runs, human rendering crashes, and off is not persisted; later plain sync recreates skills. The existing new tests use `--json`, bypassing the failing renderer. `install --skills off` has the same defect, including an empty home because actions are produced for all three adapters.

**Smallest fix:** Make the shared human renderer handle removal/preservation/conflict actions and complete the existing persistence path.

**Done when:** Focused CLI tests run both off commands without JSON, assert readable results and edited-copy reports, successful exit, saved off, and no re-add on subsequent plain sync. Repeat with JSON and dry-run to protect their semantics.

### R1-X1 — X-hard — Prove skill ownership at every replacement/removal boundary

**Finding / acceptance:** Violates user acceptance **7**, PROPOSAL task 5 (`:24`), and `project/astra-review.md:29`/`:30`: only owned items may be changed; refresh/off/uninstall must preserve and report edited copies. This remains a safety/acceptance failure even where the unsafe behavior predates task 5.

**Evidence/repro:**

- G:`astrid/skills/harnesses/base.py:257`, `:285` and `:173` treat a path component named `astrid`, or the recorded destination alone, as sufficient ownership. G:`astrid/skills/__init__.py:220`/`:274` use similar substring heuristics for off cleanup. Create a foreign `~/.claude/skills/astrid` link to an unrelated existing `.../projects/astrid/notes`, then sync/install. The candidate replaces it. The task4/head source probes confirm this; they also confirm that the target directory itself is not deleted, but the user's link is lost.
- G:`astrid/skills/harnesses/base.py:318` still unconditionally unlinks any symlink. Real uninstall callers reach it at `harnesses/claude.py:71`, `codex.py:86`, `hermes.py:108`. Repoint an installed skill link at a foreign directory and run the corresponding skills uninstall: the foreign link is deleted.
- For an edited marked copy, that remover returns False at `:325`; the adapter reports “absent” (`claude.py:74`) and `astrid/skills/__init__.py:420` forgets the record, rather than reporting the retained edit.
- Off's Codex cleanup changes bytes **outside** its fence via `before.rstrip()` / `after.lstrip()` at G:`astrid/skills/__init__.py:308`. A user's blank lines or Markdown trailing spaces adjoining the block are removed too.

**Smallest fix:** Reuse exact known descriptor sources, owned state views and installation records to establish the current target's ownership; remove path-substring guesses and unconditional unlinking. Preserve ambiguous/retargeted links and edited copies with explicit conflicts; preserve bytes outside the Codex fence. Do not build a new universal ownership inventory or expand into deferred full safe-copy staging.

**Done when:** Focused adapter/CLI tests cover foreign paths with and without an `astrid` component, stale records after retargeting, broken owned versus unknown links, ordinary and edited marked copies, and uninstall conflict reporting across all three adapters. Every protected file/link and Codex outside-fence byte stays unchanged; proven owned links/copies still reconcile idempotently.

### R1-X2 — X-hard — Complete skill policy application in uninstall and strict app update

**Finding / acceptance:** Violates user acceptance **7**, PROPOSAL Design lines 12/14, and the explicit uninstall/edited-copy requirement in `project/astra-review.md:30`. Two production lifecycle routes remain disconnected from the apply policy.

**Evidence/repro:**

- G:`astrid/installer/core.py:576`–`:587` performs stop/unregister/program removal without any skill cleanup. `backend.py:1416` cleans command artifacts, then `:1422` deletes programs; no skill caller exists. Source-derived repro: install skills into a detected harness, then run installer `--uninstall` (with workspace retained or deleted). External skill copies/links and the Codex block survive; program-backed links can become broken. No edited-copy report is emitted.
- G:`astrid/installer/cli.py:294` accepts/resolves skill flags for `--update-app`. `app_update.py:503`/`:505` selects `no_migration=True`; `:549`/`:551` persists those choices, but `:568`/`:570` skips sync for exactly that route. Source-derived repro: Pinokio strict app update with `--skills off` succeeds and saves off while existing owned skills remain; copy-mode changes likewise do not apply. It also returns before the normal summary (`cli.py:331`).

**Smallest fix:** Wire the existing safe skill apply/cleanup into these lifecycle entry points, before destroying the selected interpreter/source needed for cleanup. Reuse X1's ownership checks; preserve workspace, manager custody, no-migration guarantees and rollback responsibilities. Report retained conflicts and failures. This task depends on X1 and N7.

**Done when:** Focused real-composition tests cover normal and Pinokio uninstall with owned/edited/foreign skills and workspace retention; strict `--update-app` applies skills off and copy changes without migration. Update the four stale uninstall-test monkeypatches of removed `backend.remove_shim` (`tests/test_installer_pinokio_lifecycle.py:629`, `:664`, `:719`, two parameterizations) so their intended checks actually execute. Their retained task-5 failures are test breakage from task 4, not evidence that uninstall cleanup was tested. Ordinary update, local update and rollback retain saved policy and lifecycle checks.

### R1-N8 — Normal — Make Hermes skills-off dry-run read-only

**Finding / acceptance:** Safety/data-loss problem: a preview mutates external configuration, contrary to its advertised dry-run behavior and user acceptance **7**'s controlled apply policy.

**Evidence/repro:** G:`astrid/skills/__init__.py:197`–`:200` calls Hermes `_rewrite_config('uninstall', ...)` without checking `dry_run`; G:`astrid/skills/harnesses/hermes.py:178`/`:183` removes and rewrites the external_dirs entry. Source-derived repro: create a valid recorded Hermes `external-dir` installation, save config bytes, run `python -m astrid.skills sync --skills off --dry-run --json`: the registered directory is removed despite dry-run. JSON avoids N7 and isolates this failure. The existing dry-run test uses an empty home, so it never reaches this branch.

**Smallest fix:** Plan the external-dir removal without calling the writer in dry-run; use the same actual apply only for non-preview execution.

**Done when:** A focused existing-state test asserts byte-identical Hermes config and skills/install records under both off dry-run entry points; actual off removes only the recorded Astrid entry and reports it.

### R1-N9 — Normal — Retire provably owned legacy POSIX command shims

**Finding / acceptance:** Violates “Off = absent” (`project/PROPOSAL.md:14`) and regresses uninstall cleanup versus the cumulative base. Legacy installs legitimately have a managed shim with a null receipt.

**Evidence/repro:** Cumulative Agent base `astrid/installer/backend.py:1439`/`:1440` records `command_shim=None` whenever `~/.local/bin` is outside PATH, although `install_shim` created the file. Candidate G:`astrid/installer/launcher.py:148` requires a nonempty receipt, so `Backend.open` off (`backend.py:1492`) and uninstall (`:1416`) leave that exact owned shim behind. Executed source probe compares removal at cumulative base, task4 and frozen head: base removes the exact managed shim; task4/head retain it when given the legitimate null receipt.

**Smallest fix:** Migrate/recognize this exact legacy shim using its canonical path, marker and full contents bound to this owned install. Preserve foreign/edited shims. Do **not** infer ownership of legacy Windows PATH entries or restore removed entries.

**Done when:** Narrow shim/lifecycle tests create the legacy off-PATH POSIX state and assert that command-off and uninstall remove the proven owned shim, while foreign/edited files and pre-existing PATH bytes remain intact. Summaries accurately state removals or preservation.

### R1-N10 — Normal — Complete the maintained agent consent guide and off-choice recovery text

**Finding / acceptance:** Violates user acceptance **9**, PROPOSAL Design lines 9/14, `project/astra-review.md:13`, and `project/offpath-and-local.md:40`/`:42`. The App guide link still leads to unchanged instructions, and the terminal's generic replay is not the accepted off-choice way back.

**Evidence/repro:** G:`docs/setup/SKILL.md:13`–`:22` instructs agents to run bare install lines, without setup choices, one consent question or `--yes`. `docs/getting-started.md:18`/`:24` and `README.md:54`/`:60` retain bare entry examples. A:`src/shared/components/RuntimeConnection/copy.ts:258`/`:296` explicitly sends users/agents to this guide. Source-derived repro: a fresh noninteractive agent following the maintained guide runs the bare line and exits at G:`astrid/installer/cli.py:464` rather than completing agreed setup. Simply adding unconditional --yes would bypass the missing consent step.

G:`astrid/installer/cli.py:85`–`:97` prints a “Way back” containing the current off flags, and `:110` only says running/ready. An all-off result does not tell the user it will not start at sign-in, how to re-enable skills/command, or how to repair/update/uninstall without the command, as the explicit off-choice acceptance requires.

**Smallest fix:** Align maintained guides with the accepted App/terminal policy: disclose defaults and four flags, ask once only when consent is missing, preserve an approved handoff's selections, then run the exact --yes line. Add the specific off-row recovery instructions, using existing absolute launch/install paths, not a new start command. Preserve fully local guidance.

**Done when:** Focused documentation/copy checks and terminal summary tests cover fresh agent consent, already-approved/restart handoffs with no second question, custom flags and all-off recovery; raw unattended no-yes continues to refuse. Each off row explains its actual effect and the specific way to start/re-enable/maintain it.

## Production composition and acceptance coverage

| Acceptance / route | Reviewed path and conclusion |
| --- | --- |
| 1: shared Welcome/Not-running/Settings; first query visits | Common component exists; initial entry routing includes Setup. N1/N2 block production and handoff correctness. Settings visibility limitation is deferred separately. |
| 2: remembered checked/unchecked wording | Initialization and handoff variants exist; real callers override no-Customize (N3). |
| 3: same command and selections | Fresh setup builder uses frozen flags and --yes; state transitions and ordinary update branch fail (N2/N4). |
| 4: update availability/refusal/restart | Update now and guarded restart/reload remain in `UpdateResolutionActions.tsx:31`/`:38`/`:49`; off refusal has Setup at `:45`. Stale preference authority is recorded in DEFERRED, not misreported as a proved newly unsafe start. |
| 5: fresh unattended refusal | G:`cli.py:434`–`:472` resolves before effectful setup; raw no-yes stops with plan. Allowed bootstrap/log/lock preparation is not a zero-write violation. Windows interactive composition fails N5. |
| 6: defaults and PATH | Resolver defaults existing records to all-on; App spellings match. New Windows added-entry receipt and maintenance no-restore logic are present (`launcher.py:49`, `backend.py:1518`). Legacy POSIX cleanup fails N9. |
| 7: skills on/off/copies and every caller | Fresh/core update forwards explicit policy through `backend.py:1276`; healthy repair applies changed/off/copy policy (`core.py:385`). Standalone non-JSON off fails N7; ownership fails X1; uninstall/strict app update fail X2; Hermes preview fails N8. |
| Local / npm update | A:`scripts/local-update-backend.mjs:52` selects installed Python → G:`local_update.py:200` saved choices → `:374` registration → `:467` skill sync. Candidate activation and previous activation `:471` share service policy. Retained tests establish limited local composition, not a real npm/Pinokio desktop journey. |
| Ordinary click update / recovery | `update_job.py:1951` forwards bound manifest/root into bootstrap; existing CLI resolves saved choices before `admit_worker` (`cli.py:509`). Recovery uses `core.py:357` registration; service resolves saved autostart even on a fresh Backend (`service.py:423`). No new transaction restart logic was removed. |
| Pinokio | Manager registration/start stays separate (`backend.py:1111`, `:1143`). Fresh no-yes obeys the global refusal; actual external launcher argv was unavailable. Ordinary `update_pinokio` syncs; strict app update does not (X2). |
| 8 / 10: exclusions | No new Start Menu shortcut, OS astrid:// registration, start command or generation-default change in the reviewed cumulative diffs. Per-agent skills, full safe-copy framework and saved-settings API remain deferred as ordered. |
| 9: maintained command surfaces | Setup uses shared choices, but ordinary update and Agent guide remain stale (N4/N10). |

The manifest at A:`public/install-manifest.json:3`/`:5`/`:8` selects release 2, exact Agent 12f401 and Runtime 2776ba with unchanged schema digest. A:`public/install:111`–`:116` and `public/install.ps1:271`–`:274` preserve supplied arguments. This is compatible source composition, not a claim of a deployed or installed artifact.

## Ablejones draft audit

Checked `queue/done/07_MANIFEST_ROLLOUT-r2/REPORT.md:39`/`:41`/`:43` against accepted task reports, source and STATUS. **No independent claim of deployment, native Windows verification or completed signing is present.** Do not publish the draft during this review. The product implementation gaps above must be fixed or reflected before it is used.

| Draft claim | Evidence / qualification |
| --- | --- |
| Prior PATH denial was wrong; new added-entry ownership preserves pre-existing paths and removed entries | Task-4 report and Windows registry-boundary tests support the candidate implementation. No native Windows execution is established. N9 concerns legacy POSIX shim cleanup, not proof that the Windows PATH receipt claim is false. |
| Optional Windows task and manual start; repair/update retain autostart choice | Task-3 report, saved-policy service code and Linux detached-process evidence support design/limited tests. Draft explicitly says Windows beta/no real-runner evidence. Stale legacy authority remains D1. |
| Skills shown and on by default; optional off or copies; off does not add to ~/.claude | Resolver/builder and task-5 JSON/filesystem tests support these scoped claims. However “you can turn them off” must not imply all routes are complete: N7/X2 are counterexamples, and choices visibility is incomplete under N1/N4/N10. Treat that broad assurance as **not yet release-qualified**, covered by those blocking tasks rather than a separate duplicate task. |
| Candidate coming, not deployed; Windows beta; unsigned, signing pending certificate | Consistent with STATUS (candidate READY-TO-PUSH/review, not a deployment receipt) and task-7 limits. No real-runner/signing overclaim. Keep all these qualifications. |

The draft correctly says skills are on by default, correcting the earlier off-by-default promise. It does not renew the deferred per-agent, shortcut or signing promises. A final factual refresh should describe only the repaired and verified candidate; no reply was sent.

## Handoff

Fix the source safety/ownership tasks and App parse error first; land X1 before X2, and N7 before validating X2's human-facing cleanup. N2/N3/N4 share App surfaces and should be coordinated. Preserve immutable published heads; fixes need new candidates. Then review round 2 against a new frozen tuple with narrow selectors for these repros. Respect the two-fix-round rule: repeated failures require diagnosis, not another blind attempt.

All non-blocking observations and qualification gaps are in [DEFERRED.md](DEFERRED.md). No fixes were implemented.
