# X-UPDATE-DOOR activation-lock diagnostic

## Result and evidence

The removable diagnostic is pushed to `lane/M-R1FIX/update-lock-diagnostic` at **`f16894651635e2388a75e2ed9c3029782b1da51d`**, based on `40a6fd7ecf9138c6d9861631096a79820dc4c9b1`. `git ls-remote` returned that exact SHA. It instruments the direct installer CLI's actual nonblocking lock and the `activation_lock_held` function embedded in the launchd prelude; it does not alter their decisions, the update transaction, stop, rollback or data logic. This is diagnostic-only and should not be landed as a product change.

MAC-UPDATE-02 (41957390265) observed repeated `Choose Update to finish` from candidate admission. Its records do not establish the two HOME values, lock-file identity, or lock lifetime. The audited source indicates the direct updater normally holds the installer lock across activation, but that is not native-run proof. No cause is established. No macOS update was run in this task and this instrumentation has not yet reproduced that failure. The test changes the Agent pin to the diagnostic SHA, so any result must be qualified as an instrumented reproduction.

Each `ASTRID_LOCK_DIAG` JSON line includes `time_ns` (UTC Unix epoch nanoseconds), `pid`, `platform`, `role`, `event`, HOME directory `[device,inode]`, lock parent `[device,inode]`, opened lock file `[device,inode]` from `fstat`, and `home_relative_default`. These are path-free comparable identities, following symlinks as `stat` does; null means a stat failed. The boolean reports the lexical default relationship, not resolved pathname equality. The events bracket the nonblocking acquire and explicit unlock: `acquire_begin`, `acquire_success`, `acquire_contended`, `acquire_error`, `release_begin`, `release_success`, `release_error`. A contended probe does not acquire a lock and has no unlock. The timestamp immediately precedes or follows the OS call, not the exact instant inside it. Descriptor close and crash-driven release are not separately timed; a missing release record is inconclusive. Logger errors are suppressed to avoid affecting behavior, so missing records are also inconclusive. Trace lines go to the updater's installer stderr log and launchd's runtime-service stderr log respectively. Raw logs and full launchd commands can contain private paths; share only the extracted JSON below.

On Linux, four standalone diagnostic unit tests passed: actual CLI acquisition and unlock on failure, CLI contention without unlock, real separate-process generated-prelude contention and successful probe/unlock with matching identities, and logging failure isolation. In addition, the existing generated admission table and CLI recovery tests passed: **20 passed**. The unit tests used the existing `/usr/bin/python3`, existing pytest installation and the shared runtime checkout; no venv, clone, wheel, or build output was created. These tests do not establish Darwin/launchd behavior.

## MAC-TASK handoff

Use `astrid-runner`, and repeat MAC-UPDATE-02's **task-owned** fresh-install setup and isolation, including the same sandbox HOME, service label ownership preflight, manifest, root/data, readiness, seeded task, and guarded cleanup. Do not touch a foreign service, process, selection or data. Ensure there are no concurrent updater invocations. The baseline tuple used by that run was Agent `57cabe57f8294eded12c555d8eb3d5e1f1d20368`, Runtime `2776ba973b6c8f3555b5629b0840d9691a98d3c2`, App `878c28df7a6c80fa9aaa6540c5fccad95473e7c1`. Keep the same app bootstrap and launchd environment and preserve the pre-cleanup service log. The original run's exact generated stand-in manifest hash was `582741c3378d1a652c0b9615d53351b4e6ab527f816621a99fd33b2655b9a197`; the diagnostic manifest necessarily has a different hash.

At the point immediately before its one public `--update`, run the following exact replacement. Set the four variables to that already prepared task's local files/directories; these assignments and paths are private and must not be posted. `BASE_MANIFEST` is the release-1 candidate manifest used for the original setup, `INSTALL` is the pinned App `public/install`, `ROOT` and `DATA` are that task's selected roots, and `OUT` is a new private task-owned output directory. Keep `HOME` exactly as in the MAC-UPDATE-02 updater environment. The Python block uses only the standard library. It verifies the installed tuple, creates the next-release stand-in with the instrumented Agent pin, runs the actual public bootstrap once, then extracts records appended during that attempt from the two task-owned logs before cleanup. Do not run the original update as well.

```sh
# Set INSTALL, BASE_MANIFEST, ROOT, DATA, OUT to the private task-owned values.
mkdir -m 700 "$OUT"
python3 - "$INSTALL" "$BASE_MANIFEST" "$ROOT" "$DATA" "$OUT" <<'PY'
import hashlib, json, os, pathlib, subprocess, sys, time
install, source, root, data, out = map(pathlib.Path, sys.argv[1:])
assert sys.platform == "darwin"
assert all(p.is_absolute() for p in (install, source, root, data, out))
assert all(p.exists() for p in (install, source, root, data, out))
version_id = (root / "current").read_text().strip()
assert version_id and "/" not in version_id and "\\" not in version_id
installed = json.loads((root / "versions" / version_id / "manifest.json").read_text())
base = json.loads(source.read_text())
assert installed["agent"]["sha"] == "57cabe57f8294eded12c555d8eb3d5e1f1d20368"
assert installed["runtime"]["sha"] == base["runtime"]["sha"] == "2776ba973b6c8f3555b5629b0840d9691a98d3c2"
assert base["release"] == 1
manifest = dict(base)
manifest["agent"] = {"sha": "f16894651635e2388a75e2ed9c3029782b1da51d"}
manifest["release"] = 2
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
# The bootstrap's own stdout/stderr can contain private paths; keep it private.
with (out / "private-update-output.log").open("wb") as stream:
    result = subprocess.run(["sh", str(install), "--manifest", str(path),
                             "--root", str(root), "--data-root", str(data), "--update"],
                            stdout=stream, stderr=subprocess.STDOUT, check=False)
end = time.time_ns()
records = []
log_status = []
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
            if any(value is not None and (not isinstance(value, list) or len(value) != 2
                       or any(type(number) is not int for number in value))
                   for value in (item["home"], item["lock_parent"], item["lock_file"])):
                continue
            records.append(item)
receipt = {"diagnostic_agent_sha": manifest["agent"]["sha"],
           "diagnostic_manifest_sha256": hashlib.sha256(raw).hexdigest(),
           "update_exit": result.returncode, "start_ns": start, "end_ns": end,
           "log_status": dict(zip(("installer", "runtime_service"), log_status)),
           "records": sorted(records, key=lambda x: x["time_ns"])}
(out / "sanitized-lock-diagnostic.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps(receipt, indent=2))
PY
```

Share only `sanitized-lock-diagnostic.json`, whether the same refusal recurred, and the task-owned cleanup outcome. Do not share the diagnostic manifest if a local source repository/path was embedded by the harness, the private output, full native logs, launchd arguments, HOME or credentials. The file extraction assumes both logs are at the task updater HOME's standard macOS Astrid log directory and remain append-only; if missing, truncated, rotated, or logs were redirected, use the actual task-owned log locations and report the capture as incomplete rather than inferring absence. The pre-existing service and updater must be the task-owned ones verified by the harness. The command intentionally does not change HOME or launchd's environment and does not perform cleanup. The public bootstrap creates its usual transient bootstrap environment alongside the diagnostic manifest; the Mac task's guarded cleanup must remove that task-owned output after collecting the receipt.

Expected on a normal held-lock activation: updater `acquire_begin` then `acquire_success`; within that interval candidate `acquire_begin` then `acquire_contended`, same `lock_file` device/inode, followed later by updater `release_begin`/`release_success`. A candidate `acquire_success` followed by `release_success` is the refusal-producing observation; compare its timestamp to the updater interval and all three identities. Different HOME or parent/file identities demonstrate a difference in the observed files; equal file identities with candidate success between recorded updater acquisition and release require further native locking/lifetime investigation, not an automatic root-cause claim. Multiple candidate PIDs/attempts are possible. Compare only records between this receipt's `start_ns` and `end_ns` and identify the updater by PID and time. Failure before the CLI or before the candidate lock probe yields no corresponding lines and is not evidence of a free or held lock.

## Workspace

Used `git worktree add` against the shared repository; other lanes' checkout and untracked files were untouched. No clone, virtual environment, wheel or build output was made. Task directory size was checked under the 500 MB limit.
