# MAC-UPDATE-04 handoff: X-UPDATE-DOOR after Agent #250

## Corrected candidate and tuple

MAC-UPDATE-02 (run `41957390265`) generated the baseline candidate manifest with SHA-256 `582741c3378d1a652c0b9615d53351b4e6ab527f816621a99fd33b2655b9a197` and `release: 2`. MAC-UPDATE-03's handoff at `cee5da686552cb7e469b45bdaca9d64abe813f35` wrongly asserted baseline release 1 and set its diagnostic manifest to release 2. MAC-UPDATE-03 did not dispatch or execute an update. MAC-UPDATE-04 must preserve that release-2 baseline and use a release-3 diagnostic manifest.

The diagnostic candidate pins Agent `39a3ed6da488b32196df3148cc3bd4da19f82472` (`int/r1`, after Agent #250) and Runtime `9fc53b771134940aaac901e4cfc5d03c1db1a20a` (`int/r1`). Its Runtime schema digest is `sha256:9628bb5fd6e1dbbec392dd39f2e01031622dcf52d7ccff7cd1f0ad10743dcb1c`.

**Pairing note:** Agent `39a3…`'s `runtime.lock.json` declares Runtime `2776ba973b6c8f3555b5629b0840d9691a98d3c2`, while this handoff explicitly requests the current int/r1 Runtime `9fc…` integration pin. The MAC task should preserve and report the explicitly requested pair. Do not run App `pin.mjs` to produce the diagnostic manifest: its behavior would select the Agent lock's `2776…` Runtime and increment the App source manifest from release 1 to release 2. Instead copy the verified MAC-UPDATE-02 release-2 manifest into a private task-owned file, set both requested SHAs and the Runtime schema digest below, and set release to 3. This is diagnostic pairing evidence, not a claim that the pair matches Agent's declared runtime lock.

## MAC runner procedure

Use the `astrid-runner` macOS runner for one isolated fresh install/update. MAC-UPDATE-02's public run and sanitized artifact are [run 41957390265](https://github.com/banodoco/astrid-agent/actions/runs/41957390265) and [artifact 13384425856](https://github.com/banodoco/astrid-agent/actions/runs/41957390265/artifacts/13384425856). The artifact's `capture-upload/candidate-manifest.json` is the exact installed release-2 baseline; verify SHA-256 `582741c3378d1a652c0b9615d53351b4e6ab527f816621a99fd33b2655b9a197` and `release == 2` before installation. Its baseline pins are App `878c28df7a6c80fa9aaa6540c5fccad95473e7c1`, Agent `57cabe57f8294eded12c555d8eb3d5e1f1d20368`, and Runtime `2776ba973b6c8f3555b5629b0840d9691a98d3c2`. The supplied diagnostic pins are Agent `39a3ed6da488b32196df3148cc3bd4da19f82472` and Runtime `9fc53b771134940aaac901e4cfc5d03c1db1a20a`; the latter's schema digest is `sha256:9628bb5fd6e1dbbec392dd39f2e01031622dcf52d7ccff7cd1f0ad10743dcb1c`.

### Exact setup and safety gates

1. Use the runner's existing Python 3.11.18; create no virtual environment. Create a fresh runner-owned task directory with mode `0700`; place the task's HOME, temporary files, install root, and data root inside that directory. Set `HOME` to the task HOME and `TMPDIR`, `TMP`, `TEMP`, `RUNNER_TEMP`, `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, and `XDG_STATE_HOME` to task-owned locations. Set `ASTRID_RUNTIME_DATA_ROOT` to the task data root and `ASTRID_HOST_EXECUTION_PROFILE=installed`. Set `ASTRID_RUNTIME_DOOR_PORT` to an available loopback port other than the default `17340`. Keep this same updater HOME for the entire run; do not change the launchd service's environment.
2. Before creating/installing anything, fail closed unless the selected port is free, both launchd labels `haus.astrid.runtime` and `haus.astrid.runtime.r1` are absent, the corresponding `~/Library/LaunchAgents/haus.astrid.runtime*.plist` paths do not exist, there is no runner-owned Runtime process (a process running the Astrid Runtime CLI/server), and disk space is at least 10 GB. Record both labels' original enabled/disabled state. If any observation is missing or ambiguous, stop without installation. Never inspect or alter another user's services or processes.
3. Use the App source at the pinned App base and the exact release-2 baseline manifest from the artifact. The task-owned install command is `sh <task-owned-app>/public/install --manifest <task-owned-release-2-manifest> --root <task-owned-root> --data-root <task-owned-data>`. Do not use App `pin.mjs` to construct the diagnostic manifest. Run the install once, then wait for `/api/runtime/v1/health` on the selected loopback port and require `status == "ok"`, a non-empty `runtime_instance_id`, and equality with `runtime/discovery.json`'s `runtime_instance_id`. Require exactly one of the two task labels to be loaded; capture its launchd definition and verify its command points into this task's root before treating it as owned.
4. Seed project `mac-update-smoke` using the installed version's isolated Python: `python -I -m astrid projects create mac-update-smoke --name mac-update-smoke --json`. Invoke exactly this task: `python -I -m astrid.omp_invoke '{"capability_id":"chiptune.sfx","kind":"executor","project":"mac-update-smoke","inputs":{"kind":"blip","variant":1,"pitch":0,"duration_s":0.1}}'`. Follow the returned task ID with `python -I -m astrid tasks follow <TASK_ID> --project mac-update-smoke --poll-seconds 2 --timeout-seconds 900 --json`; require `succeeded` and non-empty `sfx` and `sfx_meta` outputs with SHA-256 digests. Preserve the task ID for the post-update check.
5. Capture the task-owned `runtime-service.log` before cleanup. At the update step, use the exact Python block below. It verifies both the source baseline manifest and the installed release-2 tuple before writing the private release-3 diagnostic manifest, invokes the public updater exactly once, and extracts only validated appended lock-diagnostic records. If either baseline assertion fails, do not invoke the updater.

Do not dispatch this handoff from a manager. This document is the MAC worker's task input.

At the point immediately before the single public update, set `BASE_MANIFEST`, `INSTALL`, `ROOT`, `DATA`, and `OUT` to the already prepared task's private files/directories. `BASE_MANIFEST` is the MAC-UPDATE-02 generated release-2 manifest (SHA-256 above); `INSTALL`, `ROOT`, and `DATA` are the task-owned bootstrap and selected roots; `OUT` is a new private task-owned directory. Keep the same updater HOME. Then run this exact replacement for MAC-UPDATE-03's bad release assertion/update block. It verifies the installed baseline, creates the release-3 manifest with both requested pins, invokes the public bootstrap exactly once, and extracts only validated diagnostic records appended during the update.

```sh
mkdir -m 700 "$OUT"
python3 - "$INSTALL" "$BASE_MANIFEST" "$ROOT" "$DATA" "$OUT" <<'PY'
import hashlib, json, os, pathlib, subprocess, sys, time
install, source, root, data, out = map(pathlib.Path, sys.argv[1:])
assert sys.platform == "darwin"
assert all(p.is_absolute() and p.exists() for p in (install, source, root, data, out))
base_raw = source.read_bytes()
assert hashlib.sha256(base_raw).hexdigest() == "582741c3378d1a652c0b9615d53351b4e6ab527f816621a99fd33b2655b9a197"
base = json.loads(base_raw)
assert base["release"] == 2
assert base["agent"]["sha"] == "57cabe57f8294eded12c555d8eb3d5e1f1d20368"
assert base["runtime"]["sha"] == "2776ba973b6c8f3555b5629b0840d9691a98d3c2"
version_id = (root / "current").read_text().strip()
assert version_id and "/" not in version_id and "\\" not in version_id
installed = json.loads((root / "versions" / version_id / "manifest.json").read_text())
assert installed["release"] == 2
assert installed["agent"]["sha"] == "57cabe57f8294eded12c555d8eb3d5e1f1d20368"
assert installed["runtime"]["sha"] == "2776ba973b6c8f3555b5629b0840d9691a98d3c2"
manifest = dict(base)
manifest["agent"] = dict(base["agent"], sha="39a3ed6da488b32196df3148cc3bd4da19f82472")
manifest["runtime"] = dict(base["runtime"], sha="9fc53b771134940aaac901e4cfc5d03c1db1a20a",
                           schema_digest="sha256:9628bb5fd6e1dbbec392dd39f2e01031622dcf52d7ccff7cd1f0ad10743dcb1c")
manifest["release"] = 3
manifest["qualification_marker"] = "fresh-install-update-stand-in"
if "vibecomfy" not in manifest:
    manifest["vibecomfy"] = installed["vibecomfy"]
raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
path = out / "diagnostic-manifest.json"
path.write_bytes(raw)
logs = [pathlib.Path.home() / "Library/Logs/Astrid Runtime/install.log",
        pathlib.Path.home() / "Library/Logs/Astrid Runtime/runtime-service.log"]
offsets = [p.stat().st_size if p.exists() else 0 for p in logs]
start = time.time_ns()
with (out / "private-update-output.log").open("wb") as stream:
    result = subprocess.run(["sh", str(install), "--manifest", str(path),
                             "--root", str(root), "--data-root", str(data), "--update"],
                            stdout=stream, stderr=subprocess.STDOUT, check=False)
end = time.time_ns()
records, log_status = [], []
keys = {"role", "event", "time_ns", "pid", "platform", "home", "lock_parent",
        "lock_file", "home_relative_default"}
for log, offset in zip(logs, offsets):
    if not log.exists():
        log_status.append("missing")
        continue
    with log.open("rb") as stream:
        if os.fstat(stream.fileno()).st_size < offset:
            log_status.append("truncated")
            continue
        log_status.append("read")
        stream.seek(offset)
        for line in stream:
            prefix = b"ASTRID_LOCK_DIAG "
            if not line.startswith(prefix):
                continue
            try:
                item = json.loads(line[len(prefix):])
            except (ValueError, UnicodeError):
                continue
            if not isinstance(item, dict) or set(item) != keys:
                continue
            if (item["role"] not in ("updater", "candidate") or item["platform"] != "darwin"
                    or item["event"] not in ("acquire_begin", "acquire_success", "acquire_contended",
                                              "acquire_error", "release_begin", "release_success", "release_error")
                    or type(item["pid"]) is not int or type(item["time_ns"]) is not int
                    or type(item["home_relative_default"]) is not bool):
                continue
            if any(v is not None and (not isinstance(v, list) or len(v) != 2
                       or any(type(n) is not int for n in v))
                   for v in (item["home"], item["lock_parent"], item["lock_file"])):
                continue
            records.append(item)
receipt = {"baseline_release": 2, "baseline_manifest_sha256": hashlib.sha256(base_raw).hexdigest(),
           "diagnostic_release": manifest["release"], "diagnostic_agent_sha": manifest["agent"]["sha"],
           "diagnostic_runtime_sha": manifest["runtime"]["sha"],
           "diagnostic_manifest_sha256": hashlib.sha256(raw).hexdigest(),
           "update_exit": result.returncode, "start_ns": start, "end_ns": end,
           "log_status": dict(zip(("installer", "runtime_service"), log_status)),
           "records": sorted(records, key=lambda x: x["time_ns"])}
(out / "sanitized-lock-diagnostic.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps(receipt, indent=2))
PY
```

If either baseline assertion fails, stop before invoking the updater and report the mismatch. Do not perform the old MAC-UPDATE-03 update as well. Preserve the task-owned pre-cleanup service log. Share only `sanitized-lock-diagnostic.json`, whether the launch-admission refusal recurred, relevant post-update checks, and the guarded cleanup outcome. Do not share the diagnostic manifest, private update output, full native logs, launchd arguments, HOME, credentials, or local paths. If logs are missing, truncated, rotated or redirected, mark capture incomplete; absence of records does not establish lock state. Compare only extracted records in `[start_ns, end_ns]`. Same lock-file device/inode plus candidate success during the updater-held interval is the key anomalous observation.

### Guarded cleanup

Capture the pre-cleanup service log and launchd state before cleanup. If the update succeeds, verify the previous task still succeeded, run the same `chiptune.sfx` task again on the new version, and verify the task-owned label and version retention. Then run the pinned public installer's `--uninstall` with the baseline manifest and the same task-owned root/data. If the update fails, do not run product uninstall: verify exactly one loaded label still matches the task-owned launchd command/root, disable and boot out only that verified label, and confirm task-owned Runtime processes are gone and the owner lock is free. If ownership or label state cannot be proven, stop cleanup and report the residue; never stop an unidentified process or service. In either outcome, verify both labels are absent and restore their original launchd enabled/disabled states. Remove the sandbox only after proving the task path is the exact runner-owned directory created for this run, has not changed identity, is not a symlink, and no process has an open file under it. Report cleanup as incomplete if any guard fails. Preserve the pre-cleanup service log and sanitized receipt; do not include raw logs or private paths in the handoff response.

## Exact public-safe task line (do not dispatch from this handoff)

> MAC: MAC-UPDATE-04 for X-UPDATE-DOOR on hannah-mac; branch int/r1; Agent SHA 39a3ed6da488b32196df3148cc3bd4da19f82472; Runtime SHA 9fc53b771134940aaac901e4cfc5d03c1db1a20a; command: repeat MAC-UPDATE-02 task-owned setup, then execute exactly one public `sh <task-owned-install> --manifest <task-owned-release-3-diagnostic-manifest> --root <task-owned-root> --data-root <task-owned-data> --update`; requested result: return the sanitized lock-diagnostic JSON, state whether the launch-admission refusal recurred, report post-update checks and guarded cleanup outcome; no private paths or raw logs.

## Preparation/cleanup record

Read MAC-UPDATE-02/03 evidence and the earlier diagnostic handoff in `ORCHESTRATOR.md`, plus the D2 repair evidence. Verified the supplied Agent and Runtime full SHAs exist in the shared repositories and checked Agent's runtime-lock discrepancy described above. No dispatch, clone, venv, build, or product/worktree mutation was performed. This task wrote only `REPORT.md`; no generated build output was created. Task folder size after writing is 1.4 MB, below the 500 MB limit.
