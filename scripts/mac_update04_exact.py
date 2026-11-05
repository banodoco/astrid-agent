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
