"""D40 custody runner. Stage A is synthetic; run-original requires reviewed bytes.

preflight-identity runs Stage B metadata checks and stops before child launch.
The profile callback observes the real method's return without replacing it.
Observation errors fail custody separately, after pytest, and never replace a
product return or exception. A profile return of None is deliberately ambiguous
(normal None or exception unwind); it is never presented as an exception/value.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

ORCH = Path("/tmp/claude-0/-home-user-repo/7dc61be1-7066-52e0-a5d0-94f90e61430a/scratchpad/orch")
CHILD = Path("/home/user/work/bench-store/entries/child-3b5cb98c4f2dfc20ce8747f68bf2e1124ff47b9dd5f04dabaada3a22278fdfc9")
REMOTION = Path("/home/user/work/runtimes/agent-remotion-v3-819acf-1110")
NODE = Path("/home/user/work/toolchain/node-v20.19.4-linux-x64/bin/node")
BASELINE = ORCH / "workers/agt-F-D40-integrated-installed-0520/evidence/d40-installed-original-positive.json"
TEST = "tests/stage1/test_zero_shim_execution_pack_deletion.py"
FUNCTION = "test_generic_host_runs_external_python_orchestrator_from_admitted_pack"
HELPER = "tests/helpers/external_orchestrator_owner.py"
PREFIXES = ("astrid", "astrid_runtime", "runtime_protocol", "astrid_runtime_client")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def retain(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def retain_json(path, value):
    retain(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def retain_self(evidence):
    data = Path(__file__).read_bytes()
    retain(evidence / "observer.py", data)
    retain(evidence / "observer.sha256", (sha(data) + "  observer.py\n").encode())
    return sha(data)


def origins(prefixes=PREFIXES):
    result = {}
    for name, module in tuple(sys.modules.items()):
        if not any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
            continue
        attributes = vars(module)
        filename = attributes.get("__file__")
        paths = attributes.get("__path__", [])
        result[name] = {
            "file": filename,
            "realpath": str(Path(filename).resolve()) if filename else None,
            "sha256": sha(Path(filename).read_bytes()) if filename else None,
            "path": list(paths),
        }
    return result


class Observation:
    def __init__(self, code, evidence, origin_reader=origins):
        self.code = code
        self.origin_reader = origin_reader
        self.errors = []
        self.calls = 0
        self.values = 0
        self.fd = os.open(evidence / "direct-observations.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)

    def emit(self, record):
        data = (json.dumps(record, sort_keys=True) + "\n").encode()
        while data:
            written = os.write(self.fd, data)
            if written <= 0:
                raise OSError("observer write made no progress")
            data = data[written:]
        os.fsync(self.fd)

    def failure(self, exc):
        message = f"D40 OBSERVER FAILURE: {type(exc).__name__}: {exc}"
        self.errors.append(message)
        try:
            os.write(2, (message + "\n").encode())
        except OSError:
            pass  # The enclosing runner also records errors and fails custody.

    def profile(self, frame, event, arg):
        if frame.f_code is not self.code or event not in {"call", "return"}:
            return
        try:
            record = {"event": event, "code_file": frame.f_code.co_filename,
                      "code_name": frame.f_code.co_qualname}
            if event == "call":
                self.calls += 1
                count = self.code.co_argcount + self.code.co_kwonlyargcount
                record["argument_ids"] = {name: id(frame.f_locals[name])
                                          for name in self.code.co_varnames[:count]}
            else:
                record["object_id"] = id(arg)
                if arg is None:
                    record["disposition"] = "None return or exception unwind; no direct value"
                else:
                    # The installed method returns this concrete container. Do
                    # not call properties, repr, iterators or arbitrary mappings.
                    if type(arg) is not SimpleNamespace:
                        raise TypeError("expected concrete SimpleNamespace result")
                    outputs = vars(arg)["outputs"]
                    if type(outputs) is not dict or type(outputs["imported_from"]) is not str:
                        raise TypeError("expected concrete outputs dict and imported_from str")
                    record["imported_from"] = outputs["imported_from"]
                    record["outputs_id"] = id(outputs)
                    record["disposition"] = "direct actual return.outputs.imported_from"
                    self.values += 1
            self.emit(record)
            # Persist the direct value first, even if origin collection fails.
            self.emit({"event": event + "-origins", "modules": self.origin_reader()})
        except Exception as exc:  # noqa: BLE001 - diagnostics must not change the product outcome
            self.failure(exc)

    def start(self):
        if sys.getprofile() is not None:
            raise RuntimeError("existing profile hook: refuse to replace it")
        sys.setprofile(self.profile)

    def stop(self):
        sys.setprofile(None)

    def close(self):
        os.close(self.fd)


def source_identity(root, evidence):
    data = (root / TEST).read_bytes()
    helper = (root / HELPER).read_bytes()
    retain(evidence / "original-test.py", data)
    retain(evidence / "original-helper.py", helper)
    node = next(n for n in ast.parse(data).body if isinstance(n, ast.FunctionDef) and n.name == FUNCTION)
    # 0520 hashes the source slice without a final LF and includes AST locations.
    body = "\n".join(data.decode().splitlines()[node.lineno - 1:node.end_lineno])
    assertions = [n for n in ast.walk(node) if isinstance(n, ast.Assert)]
    identity = {
        "file_sha256": sha(data), "helper_sha256": sha(helper),
        "function_source_sha256": sha(body.encode()),
        "function_ast_sha256": sha(ast.dump(node, include_attributes=True).encode()),
        "assertion_count": len(assertions),
        "assertion_ast_sha256": [sha(ast.dump(n, include_attributes=True).encode()) for n in assertions],
    }
    return identity


def custody(root, evidence):
    """Read files only. Baseline fields are expectations, never new observations."""
    baseline_bytes = BASELINE.read_bytes()
    if sha(baseline_bytes) != "77bddf5b34c107a53a518823e4faf42a6d373646636a1931596454d069af495f":
        raise RuntimeError("immutable 0520 baseline changed")
    baseline = json.loads(baseline_bytes)
    retain(evidence / "0520-baseline.json", baseline_bytes)
    identity = source_identity(root, evidence)
    for key, value in baseline["original_body_and_assertion_hashes"]["pre_body_copy"].items():
        if identity[key] != value:
            raise RuntimeError(f"original identity changed: {key}")
    expected_helper = subprocess.check_output(["git", "show", "2007cf7aaf:" + HELPER], cwd=root)
    if sha(expected_helper) != identity["helper_sha256"]:
        raise RuntimeError("2007 helper changed")
    files = {}

    def capture(path, expected=None):
        data = path.read_bytes()
        actual = sha(data)
        if expected is not None and actual != expected:
            raise RuntimeError(f"custody hash mismatch: {path}")
        if path.is_symlink():
            raise RuntimeError(f"custody file is a symlink: {path}")
        files[str(path)] = {"sha256": actual, "realpath": str(path.resolve()),
                            "is_symlink": path.is_symlink()}
        return data

    for name in (".bench-owner.json", ".astrid-source-commit", ".astrid-runtime-source-commit",
                 ".astrid-runtime-client-identity", ".astrid-distribution-metadata", ".astrid-import-origins"):
        retain(evidence / name.removeprefix("."), capture(CHILD / name))
    owner = json.loads((CHILD / ".bench-owner.json").read_bytes())
    if owner != baseline["child"]["owner"]:
        raise RuntimeError("installed child owner identity changed")
    for name, key in ((".astrid-source-commit", "agent_sha_marker"),
                      (".astrid-runtime-source-commit", "runtime_sha_marker")):
        if (CHILD / name).read_text().strip() != baseline["child"][key]:
            raise RuntimeError(f"installed marker changed: {name}")
    retain(evidence / "provision-receipt.txt", capture(Path(baseline["child"]["provision_receipt"]),
                                                     baseline["child"]["provision_receipt_sha256"]))
    site = CHILD / "lib/python3.13/site-packages"
    for entry in baseline["installed_distribution_urls"]:
        distribution = entry["distribution"].replace("-", "_")
        metadata = list(site.glob(distribution + "-*.dist-info/direct_url.json"))
        if len(metadata) != 1:
            raise RuntimeError(f"ambiguous installed metadata: {distribution}")
        direct = json.loads(capture(metadata[0]))
        if direct != entry["direct_url"]:
            raise RuntimeError("installed direct URL changed or editable install")
        retain_json(evidence / (distribution + "-direct_url.json"), direct)
    for relative, expected in owner["identity"]["client_tuple"].items():
        capture(site / relative.removeprefix("packages/python/"), expected)
    for item in baseline["runtime_fixture_origins_and_hashes"].values():
        capture(Path(item["path"]), item["expected_sha256"])
    for relative, entry in baseline["relevant_production_source_equality"].items():
        capture(root / relative, entry["worktree"])
        capture(site / relative, entry["worktree"])
    for key in ("agent_source_realpath", "runtime_source_realpath"):
        path = Path(baseline["child"][key])
        if path.is_symlink() or path.resolve() != path:
            raise RuntimeError(f"retained source snapshot is a symlink: {path}")
    capture(REMOTION / "package-lock.json", baseline["profile"]["package_lock_sha256"])
    capture(REMOTION / "artifacts/banodoco-timeline-composition-635f4de06fda22a85fc2e4cfc764d0153a4c9f40.tgz",
            baseline["profile"]["composition_archive_expected_sha256"])
    retain_json(evidence / "custody.json", {"files": files, "original": identity})
    return files


def pytest_child(root, evidence, expected_sha):
    if not sys.flags.isolated or sha(Path(__file__).read_bytes()) != expected_sha:
        raise RuntimeError("child must execute isolated, reviewed, retained observer bytes")
    import pytest

    from astrid.core.execution.generic_host import GenericPackHost

    # Test support only; never insert a production checkout in sys.path.
    if "tests" in sys.modules:
        raise RuntimeError("unexpected preloaded tests package")
    spec = importlib.util.spec_from_file_location("tests", root / "tests/__init__.py",
                                                submodule_search_locations=[str(root / "tests")])
    module = importlib.util.module_from_spec(spec)
    sys.modules["tests"] = module
    spec.loader.exec_module(module)
    method = GenericPackHost.invoke_capability
    observer = Observation(method.__code__, evidence)

    class Plugin:
        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_call(self, item):
            observer.start()
            try:
                return (yield)
            finally:
                observer.stop()

        def pytest_runtest_logreport(self, report):
            try:
                observer.emit({"event": "pytest-report", "nodeid": report.nodeid,
                               "when": report.when, "outcome": report.outcome,
                               "duration": report.duration})
            except Exception as exc:  # noqa: BLE001 - preserve pytest's test outcome
                observer.failure(exc)

    args = ["-q", "--import-mode=importlib", "-oaddopts=", "-p", "no:cacheprovider",
            "-o", "tmp_path_retention_policy=all",
            "--timeout=30", "--tb=short", "--rootdir=" + str(root),
            "--basetemp=" + str(Path(os.environ["TMPDIR"]) / "d40-body"),
            "--junitxml=" + str(evidence / "junit.xml"), str(root / TEST) + "::" + FUNCTION]
    retain_json(evidence / "pytest-command.json", args)
    retain_json(evidence / "origins-before-pytest.json", origins())
    status = int(pytest.main(args, plugins=[Plugin()]))
    try:
        after = origins()
        retain_json(evidence / "origins-after-pytest.json", after)
    except Exception as exc:  # noqa: BLE001 - preserve the completed pytest exit
        observer.failure(exc)
        after = {}
    installed_site = CHILD / "lib/python3.13/site-packages"
    for name, entry in after.items():
        if entry["realpath"] and not Path(entry["realpath"]).is_relative_to(installed_site):
            observer.errors.append(f"production origin escaped installed site: {name}")
    if GenericPackHost.invoke_capability is not method:
        observer.errors.append("production method identity changed")
    if observer.calls != 1 or observer.values != 1:
        observer.errors.append(f"expected one direct invocation/value; got {observer.calls}/{observer.values}")
    observer.close()
    retain_json(evidence / "pytest-result.json", {"pytest_exit": status, "observer_errors": observer.errors,
                                               "calls": observer.calls, "direct_values": observer.values})
    return status if status else (2 if observer.errors else 0)


def run_original(root, evidence, reviewed_sha, *, prelaunch_only=False):
    digest = retain_self(evidence)
    if digest != reviewed_sha:
        raise RuntimeError("reviewed harness SHA does not match: no original execution")
    before = custody(root, evidence)
    if subprocess.check_output([str(NODE), "--version"], text=True).strip() != "v20.19.4":
        raise RuntimeError("Node profile changed")
    scratch = Path(tempfile.mkdtemp(prefix="d40-", dir=os.environ["TMPDIR"]))
    env = {"HOME": os.environ["HOME"], "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(scratch),
           "PATH": str(NODE.parent) + ":/usr/bin:/bin", "ASTRID_HOST_EXECUTION_PROFILE": "installed",
           "ASTRID_NODE_EXECUTABLE": str(NODE), "ASTRID_REMOTION_PROJECT_DIR": str(REMOTION),
           "ASTRID_TEST_NODE_MODULES": str(REMOTION / "node_modules")}
    command = [str(CHILD / "bin/python"), "-I", str(evidence / "observer.py"), "_pytest",
               "--root", str(root), "--evidence", str(evidence), "--reviewed-source-sha256", digest]
    retain_json(evidence / "command.json", {"argv": command, "env": env, "cwd": str(root)})
    retain(evidence / "command.sh", ("env -i " + shlex.join([k + "=" + v for k, v in env.items()]
                                                          + command) + "\n").encode())
    if prelaunch_only:
        retain_json(evidence / "identity-preflight.json", {
            "status": "PASS", "observer_sha256": digest, "root": str(root),
            "custody_files_checked": len(before), "launch_intercepted_before_Popen": True,
        })
        return 0
    with (evidence / "stdout.txt").open("xb") as stdout, (evidence / "stderr.txt").open("xb") as stderr:
        process = subprocess.Popen(command, cwd=root, env=env, stdout=stdout, stderr=stderr)
        try:
            status = process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            status = process.wait()
        stdout.flush()
        stderr.flush()
        os.fsync(stdout.fileno())
        os.fsync(stderr.fileno())
    retain_json(evidence / "exit.json", {"returncode": status})
    changed = [path for path, entry in before.items() if sha(Path(path).read_bytes()) != entry["sha256"]]
    original = json.loads((evidence / "custody.json").read_bytes())["original"]
    if sha((root / TEST).read_bytes()) != original["file_sha256"]:
        changed.append(TEST)
    if sha((root / HELPER).read_bytes()) != original["helper_sha256"]:
        changed.append(HELPER)
    retain_json(evidence / "custody-after.json", {"changed_files": changed,
                "test_sha256": sha((root / TEST).read_bytes()), "helper_sha256": sha((root / HELPER).read_bytes())})
    return status if status else (2 if changed else 0)


def preflight(root, evidence):
    digest = retain_self(evidence)
    scratch = Path(tempfile.mkdtemp(prefix="d40-synthetic-", dir=os.environ["TMPDIR"]))
    snapshots = {p: sha(p.read_bytes()) for folder in ("astrid", "tests") for p in (root / folder).rglob("*.py")}
    checks = []

    def exercise(label, origin_reader, break_output=False):
        folder = scratch / label
        folder.mkdir()
        sentinel = object()
        request = {"sentinel": sentinel}
        outputs = {"imported_from": "synthetic-direct-value"}
        result = SimpleNamespace(outputs=outputs)
        error = ValueError("synthetic actual exception")
        seen = []

        def target(arg, *, fail=False):
            seen.append((arg, fail))
            if fail:
                raise error
            return result

        before = target
        observer = Observation(target.__code__, folder, origin_reader)
        if break_output:
            observer.close()
        # Before, during and after profiling: exact args, result and exception.
        for observed in (False, True, False):
            if observed:
                observer.start()
            try:
                with redirect_stdout(io.StringIO()):
                    assert target(request) is result
                    try:
                        target(request, fail=True)
                    except ValueError as caught:
                        assert caught is error
                    else:
                        raise AssertionError("exception was lost")
            finally:
                if observed:
                    observer.stop()
            assert target is before and result.outputs is outputs
            assert request == {"sentinel": sentinel} and outputs == {"imported_from": "synthetic-direct-value"}
        assert all(arg is request for arg, _ in seen) and [v for _, v in seen] == [False, True] * 3
        assert sys.getprofile() is None
        if not break_output:
            observer.close()
        records = [json.loads(line) for line in (folder / "direct-observations.jsonl").read_text().splitlines()]
        if label == "normal":
            assert not observer.errors and observer.calls == 2 and observer.values == 1
            returned = next(r for r in records if "imported_from" in r)
            assert returned["imported_from"] == outputs["imported_from"] and returned["object_id"] == id(result)
            assert returned["outputs_id"] == id(outputs)
            logged_origins = [r["modules"] for r in records if r["event"] in {"call-origins", "return-origins"}]
            assert len(logged_origins) == 4
            for mapping in logged_origins:
                assert mapping["__main__"]["realpath"] == str(Path(__file__).resolve())
                assert mapping["__main__"]["sha256"] == digest
            assert any(r.get("argument_ids", {}).get("arg") == id(request) for r in records)
        else:
            assert observer.errors
            if label == "origin-failure":
                assert next(r for r in records if "imported_from" in r)["imported_from"] == outputs["imported_from"]
        retain_json(evidence / (label + ".json"), {"records": records, "errors": observer.errors})
        checks.append(label + ": same args, value, exception and object identities before/during/after")

    exercise("normal", lambda: origins(("__main__",)))
    exercise("durable-write-failure", origins, break_output=True)

    def broken_origins():
        raise OSError("synthetic origin read failure")

    exercise("origin-failure", broken_origins)
    # A non-container result must be rejected diagnostically without iterating it.
    consumed = []

    def iterator_target():
        def values():
            consumed.append(True)
            yield "never consumed"
        return values()

    folder = scratch / "iterator"
    folder.mkdir()
    observer = Observation(iterator_target.__code__, folder)
    observer.start()
    try:
        returned = iterator_target()
        assert iter(returned) is returned and not consumed
    finally:
        observer.stop()
        observer.close()
    assert observer.errors and not consumed
    checks.append("unsupported iterator result: unchanged, never consumed; custody failure visible")
    folder = scratch / "existing-profile"
    folder.mkdir()
    observer = Observation(iterator_target.__code__, folder)

    def existing_profile(frame, event, arg):
        pass

    sys.setprofile(existing_profile)
    try:
        try:
            observer.start()
        except RuntimeError:
            assert sys.getprofile() is existing_profile
        else:
            raise AssertionError("existing profile was replaced")
    finally:
        sys.setprofile(None)
        observer.close()
    checks.append("existing profile hook: refused before execution, original hook preserved")
    try:
        Observation(iterator_target.__code__, scratch / "missing-directory")
    except FileNotFoundError:
        assert sys.getprofile() is None and not consumed
    else:
        raise AssertionError("unwritable initial output must refuse before execution")
    checks.append("initial durable-output open failure: stops before hook or target execution")
    assert snapshots == {p: sha(p.read_bytes()) for p in snapshots}
    assert not origins(), "synthetic preflight must not import production modules"
    retain_json(evidence / "preflight.json", {"stage": "A", "status": "PASS", "observer_sha256": digest,
                "command": sys.orig_argv, "python": sys.executable, "sys_prefix": sys.prefix,
                "isolated": bool(sys.flags.isolated), "root": str(root), "checks": checks,
                "production_python_files_unchanged": len(snapshots),
                "production_modules_imported": [], "original_body_runs": 0,
                "complete_installed_custody": "UNVERIFIED; Stage B is not authorized by Stage A"})
    print(json.dumps({"stage": "A", "status": "PASS", "observer_sha256": digest, "checks": checks}, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "preflight-identity", "run-original", "_pytest"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--reviewed-source-sha256")
    args = parser.parse_args()
    root, evidence = args.root.resolve(), args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    if args.mode == "preflight":
        return preflight(root, evidence)
    if not args.reviewed_source_sha256:
        parser.error("Stage B requires independent review and --reviewed-source-sha256")
    if args.mode == "_pytest":
        return pytest_child(root, evidence, args.reviewed_source_sha256)
    return run_original(root, evidence, args.reviewed_source_sha256,
                        prelaunch_only=args.mode == "preflight-identity")


if __name__ == "__main__":
    raise SystemExit(main())
