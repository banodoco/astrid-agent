#!/bin/bash
set -euo pipefail
case_dir="$RUNNER_TEMP/astrid-modern-qualification"
evidence="$GITHUB_WORKSPACE/modern-updater-evidence"
scripts="$GITHUB_WORKSPACE/scripts/qualification/modern-updater"
mkdir -p "$case_dir" "$evidence"
chmod 755 "$case_dir"
cp "$scripts/manifest.json" "$case_dir/manifest.json"
[ "$(shasum -a 256 "$case_dir/manifest.json" | awk '{print $1}')" = ed02a738def5db4552e2860e42ad60914fbb72e36f8a721f1673d718aa29baf0 ]
curl --fail --silent --show-error --location 'https://raw.githubusercontent.com/banodoco/astrid-app/6fab4ceea6abbf93e906a31825719ee640b71717/public/install' -o "$case_dir/install"
[ "$(shasum -a 256 "$case_dir/install" | awk '{print $1}')" = 100bab1deb15d620abd910c8fb0c9a6d77f4945428a3229dac6b506705e3fd0d ]
python3 - "$case_dir" <<'PY'
import json, pathlib, sys
case = pathlib.Path(sys.argv[1]); manifest = json.loads((case/'manifest.json').read_text())
assert manifest['release'] == 1
assert manifest['agent']['sha'] == '9e7032e41ddf3d270fe9cff5d164fd84f30ac0b8'
assert manifest['runtime']['sha'] == 'ebd412199a9c1e53c3a984d27d335de8b40654a1'
assert 'repo' not in manifest['agent'] and 'repo' not in manifest['runtime']
manifest['release'] = 0
manifest['agent']['sha'] = '50e3dc5480ebf8fbb860ac3d37bf5d98ab58b3fc'
(case/'baseline.json').write_text(json.dumps(manifest, indent=2) + '\n')
PY
shasum -a 256 "$case_dir/install" "$case_dir/manifest.json" "$case_dir/baseline.json" > "$evidence/input-sha256.txt"
python3 - "$case_dir/manifest.json" > "$evidence/first-party-archive-urls.txt" <<'PYURL'
import json, sys
manifest = json.load(open(sys.argv[1]))
for name in ('agent', 'runtime'):
    print(f"https://github.com/banodoco/astrid-{name}/archive/{manifest[name]['sha']}.tar.gz")
PYURL
# These copies are restored by the trap. This machine is a disposable hosted runner.
sudo cp -p /etc/hosts "$case_dir/hosts.original"
sudo cp -p /etc/ssl/cert.pem "$case_dir/cert.original"
cleanup() {
  original_exit=$?
  trap - EXIT
  set +e
  cleanup_failed=0
  cp "$case_dir/fixture.log" "$evidence/fixture.log" 2>/dev/null
  cp "$HOME/Library/Logs/Astrid Runtime/install.log" "$evidence/install.log" 2>/dev/null
  cp "$HOME/Library/Logs/Astrid Runtime/runtime-service.log" "$evidence/runtime-service.log" 2>/dev/null
  if [ -d "$case_dir/program/logs" ]; then cp -R "$case_dir/program/logs" "$evidence/job-logs"; fi
  python3 - "$case_dir/program" "$evidence" <<'PYSNAPSHOT'
import json, pathlib, sys
root, output = map(pathlib.Path, sys.argv[1:])
try:
    current = (root / 'current').read_text().strip()
    install = json.loads((root / 'install.json').read_text())
    job = install.get('update_job', {})
    journal = json.loads((root / 'update-transaction.json').read_text()) if (root / 'update-transaction.json').exists() else {}
    snapshot = {'current': current, 'job': {k: job.get(k) for k in ('operation_id','state','transaction_id','target_release','platform','launch_status','cleanup_status','result')},
                'journal': {k: journal.get(k) for k in ('operation_id','step','update_job','transaction_kind')}}
    (output / 'pre-cleanup-snapshot.json').write_text(json.dumps(snapshot, indent=2) + '\n')
except (OSError, ValueError):
    pass
PYSNAPSHOT
  if [ -f "$case_dir/program/install.json" ]; then
    sh "$case_dir/install" --uninstall --root "$case_dir/program" --manifest "$case_dir/manifest.json" > "$evidence/uninstall.log" 2>&1
    uninstall_exit=$?
    [ "$uninstall_exit" -eq 0 ] || cleanup_failed=1
    printf 'uninstall_exit=%s\n' "$uninstall_exit" > "$evidence/cleanup.txt"
    if [ -f "$case_dir/data/qualification-sentinel.txt" ]; then shasum -a 256 "$case_dir/data/qualification-sentinel.txt" >> "$evidence/cleanup.txt"; fi
    launchctl print "gui/$(id -u)/haus.astrid.runtime" >/dev/null 2>&1; service_exit=$?
    printf 'runtime_launchd_print_exit=%s\n' "$service_exit" >> "$evidence/cleanup.txt"
    [ "$service_exit" -ne 0 ] || cleanup_failed=1
    if [ -f "$evidence/accepted.json" ]; then
      operation=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["response"]["operation_id"])' "$evidence/accepted.json")
      launchctl print "gui/$(id -u)/astrid-update-$operation" >/dev/null 2>&1; worker_exit=$?
      printf 'worker_launchd_print_exit=%s\n' "$worker_exit" >> "$evidence/cleanup.txt"
      [ "$worker_exit" -ne 0 ] || cleanup_failed=1
    fi
    if [ -f "$evidence/baseline.json" ]; then
      expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sentinel_sha256"])' "$evidence/baseline.json")
      actual=$(shasum -a 256 "$case_dir/data/qualification-sentinel.txt" | awk '{print $1}')
      [ "$expected" = "$actual" ] || cleanup_failed=1
    fi
  fi
  if [ -f "$case_dir/server.pid" ]; then sudo kill "$(cat "$case_dir/server.pid")"; fi
  sudo cp -p "$case_dir/hosts.original" /etc/hosts
  sudo cp -p "$case_dir/cert.original" /etc/ssl/cert.pem
  [ "$cleanup_failed" -eq 0 ] || original_exit=1
  exit "$original_exit"
}
trap cleanup EXIT
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$case_dir/ca.key" -out "$case_dir/ca.pem" -days 1 -subj '/CN=Astrid disposable CI CA' -addext 'basicConstraints=critical,CA:TRUE' > /dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -keyout "$case_dir/leaf.key" -out "$case_dir/leaf.csr" -subj '/CN=astrid.haus' > /dev/null 2>&1
printf 'subjectAltName=DNS:astrid.haus\nextendedKeyUsage=serverAuth\n' > "$case_dir/leaf.ext"
openssl x509 -req -in "$case_dir/leaf.csr" -CA "$case_dir/ca.pem" -CAkey "$case_dir/ca.key" -CAcreateserial -out "$case_dir/leaf.pem" -days 1 -extfile "$case_dir/leaf.ext" > /dev/null 2>&1
printf '\n127.0.0.1 astrid.haus\n' | sudo tee -a /etc/hosts >/dev/null
sudo tee -a /etc/ssl/cert.pem < "$case_dir/ca.pem" >/dev/null
sudo dscacheutil -flushcache || true
sudo /usr/bin/python3 "$scripts/server.py" "$case_dir" > "$case_dir/fixture.log" 2>&1 &
for i in {1..30}; do [ -f "$case_dir/server.pid" ] && break; sleep 1; done
[ -f "$case_dir/server.pid" ]
# Both checks use ordinary hostname and chain verification, with the test CA trusted only on this runner.
curl --cacert /etc/ssl/cert.pem --fail --silent --show-error https://astrid.haus/install-manifest.json -o "$case_dir/check.json"
cmp "$case_dir/check.json" "$case_dir/manifest.json"
sh "$case_dir/install" --root "$case_dir/program" --data-root "$case_dir/data" --manifest "$case_dir/baseline.json" > "$evidence/baseline-install.out" 2>&1
python3 "$scripts/lifecycle.py" "$case_dir" "$evidence"
