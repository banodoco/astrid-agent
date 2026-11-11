"""G3 slice 3: the pod-side failure bundle, its redactor, and collection before the reset (H10).

The redactor and collector are the shipped ``failure_bundle.py``. Machine journeys use the
fakes of ``test_machine.py``; the fake pod runs the real collector source in an isolated
interpreter (``pod_collector``), so every bundle below was written by the pod-side code.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from astrid.packs.runpod import failure_bundle as fb
from astrid.packs.runpod import failure_receipt
from astrid.packs.runpod import machine as rm
from tests.packs.runpod import pod_collector
from tests.packs.runpod.test_failure_receipt import _only, _run, receipts as receipts  # noqa: F401  (fixture)
from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401  (fixture)
from tests.packs.runpod.test_machine import machine_case as machine_case  # noqa: F401  (fixture)
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401  (fixture)
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401  (fixture)
from tests.packs.runpod.test_warm_session import (  # noqa: F401  (fixtures)
    lease_selection_case as lease_selection_case, small_lease as small_lease,
)

PROMPT = "a quiet harbour at dawn, fishing boats with red sails, mist rolling over the water"
SHORT = "Mira"
TWO_LINES = "first private line of the story\nsecond private line of the story"
#: Key-shaped strings (RunPod, Hugging Face, OpenAI, AWS, GitHub, JWT, generic high entropy, assignments).
SECRETS = {
    "runpod": "rpa_9ZQXW3K2M7N4B8V1C6X5Z0L9K8J7H6G5F4D3",
    "runpod_legacy": "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q7R8S9T0",
    "hf": "hf_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "openai": "sk-proj-Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe",
    "openai_plain": "sk-4f9a8b7c6d5e4f3a2b1c",
    "aws": "AKIAIOSFODNN7EXAMPLE",
    "github": "ghp_1234567890abcdefghijABCDEFGHIJ123456",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "opaque_bearer": "opaque-bearer-value-7781",
    "basic": "dXNlcjpodW50ZXIyaHVudGVy",
    "entropy": "q8Lr2Vt6Yx0Bn4Mk9Hs3Dg7Fp1Jz5Cw8Ea2Ru6Ti",
    "entropy_assigned": "Mx4Pq7Rt2Wz9Kb3Nd8Lf5Hj1Vc6Gs0Ay7EuQ2",
    "assigned": "topsecretvalue99",
    "json_password": "hunter2hunter2",
    "cookie": "sessionid=c00k1e-v4lue",
}
SECRET_LINES = [
    f"RunPod key {SECRETS['runpod']} and legacy {SECRETS['runpod_legacy']}",
    f"HF_TOKEN={SECRETS['hf']}",
    f"using {SECRETS['hf']} for downloads",
    f"openai {SECRETS['openai']} / {SECRETS['openai_plain']}",
    f"aws {SECRETS['aws']} github {SECRETS['github']}",
    f"Authorization: Bearer {SECRETS['jwt']}",
    f"curl -H 'Authorization: Bearer {SECRETS['opaque_bearer']}' -H \"authorization: Basic {SECRETS['basic']}\"",
    f"token {SECRETS['entropy']} seen",
    f"extra_arg={SECRETS['entropy_assigned']}",
    f"api_key={SECRETS['assigned']} retry",
    f'{{"user": "x", "password": "{SECRETS["json_password"]}"}}',
    f"Cookie: {SECRETS['cookie']}",
]
URLS = [
    "http://example.com/models/a.safetensors",
    "https://user:hunter2@internal.example.org:8443/v1/run?token=abc123&sig=zz",
    "https://bucket.s3.amazonaws.com/x?X-Amz-Signature=0123abcd",
    "s3://private-bucket/key",
    "ws://10.0.0.5:8188/ws?clientId=1",
]
URL_LINES = [f"fetching {url} now" for url in URLS] + [
    "posting to api.runpod.io/graphql (bare host)",
    "listening on 10.20.30.40 and 192.168.1.77:8188",
    "peer 2001:db8:85a3::8a2e:370:7334 and [fe80::1%eth0] and [::1]:8188",
]
#: Fragments that must never survive anywhere (URL hosts, credentials, query tokens, addresses).
URL_FRAGMENTS = ["example.com", "user:hunter2", "internal.example.org", "token=abc123", "amazonaws",
                 "X-Amz-Signature", "private-bucket", "10.0.0.5", "runpod.io", "10.20.30.40", "192.168.1.77",
                 "2001:db8", "fe80::1", "::1"]
POD_ONLY_SECRET = "plainword-secret"  # a pod environment value with no key shape
CONTROLLER_ONLY_SECRET = "ctrl-only-credential-4417"


def _leaks(text: str) -> list[str]:
    """Every planted value that is still visible in ``text`` (case-insensitive for prompts)."""
    found = [name for name, value in SECRETS.items() if value in text]
    found += [fragment for fragment in URL_FRAGMENTS if fragment in text]
    low = text.lower()
    if PROMPT.lower() in low:
        found.append("prompt")
    found += [f"prompt[{i}:{i + 32}]" for i in range(len(PROMPT) - 31) if PROMPT[i:i + 32].lower() in low]
    if SHORT.lower() in low:
        found.append("short prompt")
    found += [line for line in TWO_LINES.split("\n") if line.lower() in low]
    for value in (POD_ONLY_SECRET, CONTROLLER_ONLY_SECRET):
        if value in text:
            found.append(value)
    return found


def _planted_log(extra=()):
    return "\n".join([
        "Starting server", "got prompt",
        f"prompt text: {PROMPT}",
        f"partial echo: {PROMPT[10:52]} (42 chars of it)",
        f"json echo: {json.dumps({'text': PROMPT + chr(10) + 'more'})}",
        f"speaker={SHORT} voice=mira", f"MIRA is the voice",
        f"story: {TWO_LINES.splitlines()[0]}", f"then {TWO_LINES.splitlines()[1]} ended",
        *SECRET_LINES, *URL_LINES,
        f"connecting with {POD_ONLY_SECRET} now", f"controller secret {CONTROLLER_ONLY_SECRET} here",
        "digest sha256:" + "a" * 64,
        *extra,
        "Traceback (most recent call last):",
        '  File "/root/astrid/engines/comfyui/execution.py", line 152, in execute',
        "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB",
    ])


# ----------------------------------------------------------------------------
# The redactor
# ----------------------------------------------------------------------------


def _redactor(**kw):
    return fb.Redactor(prompts=kw.get("prompts", [PROMPT, SHORT, TWO_LINES, "cat"]),
                       secrets=kw.get("secrets", [POD_ONLY_SECRET]))


def test_a_planted_prompt_never_comes_out():
    r = _redactor()
    out = r.text(_planted_log())
    assert _leaks(out) == [CONTROLLER_ONLY_SECRET]  # only the controller knows that one
    # A line with 32+ consecutive prompt characters is dropped whole, not edited.
    assert "partial echo" not in out and "prompt text:" not in out and "json echo" not in out
    assert out.count(fb.DROPPED) >= 3 and r.counts["lines_dropped"] >= 3
    # Shorter matches of 4+ characters are replaced; strings under 4 characters are not matched.
    assert "speaker=<prompt> voice=<prompt>" in out and "<prompt> is the voice" in out
    assert fb.Redactor(prompts=["cat"]).text("a cat sat") == "a cat sat"
    # The cause stays readable.
    assert "CUDA out of memory" in out and "/root/astrid/engines/comfyui/execution.py" in out


def test_a_planted_key_never_comes_out():
    r = _redactor(prompts=[])
    for line in SECRET_LINES + [f"connecting with {POD_ONLY_SECRET} now"]:
        out = r.text(line)
        assert not [name for name, value in SECRETS.items() if value in out], (line, out)
        assert POD_ONLY_SECRET not in out
    assert r.counts["secret"] >= len(SECRET_LINES)
    assert r.text("digest sha256:" + "a" * 64) == "digest sha256:" + "a" * 64  # digests are kept


def test_a_planted_url_or_ip_never_comes_out():
    r = _redactor(prompts=[])
    out = r.text("\n".join(URL_LINES))
    assert not [fragment for fragment in URL_FRAGMENTS if fragment in out], out
    assert r.counts["url"] >= len(URLS) + 1 and r.counts["ip"] >= 4
    # Code that only looks like a host name, and log timestamps, stay.
    assert r.text("self.net(x) at 12:30:45 torchvision.io.read_image") == "self.net(x) at 12:30:45 torchvision.io.read_image"


def test_redaction_runs_before_truncation_so_a_secret_on_the_cap_boundary_is_never_half_visible(tmp_path):
    token = "Zt8Kq3Nw7Rb2Lx6Vp1Hd5Fj9Cs4Gm0Ya8Ue3Io7Tw"
    secret_line = f"Authorization: Bearer {token}"
    filler = [f"step {i:06d} ok" for i in range(4000)]
    # Place the secret where a byte-tail cut at the cap would split it.
    tail = "\n".join(filler[-1500:])
    cap = len(tail.encode()) + len(secret_line) // 2 + 1
    text = "\n".join(filler[:-1500] + [secret_line, tail])
    body, truncated, _ = fb.fit_text(fb.Redactor().text(text), cap)
    assert truncated and len(body.encode()) <= cap
    naive = text.encode()[-cap:].decode()
    assert token[len(token) // 2:] in naive, "a naive cut would have shown half the token"
    assert not any(token[i:i + 6] in body for i in range(len(token) - 5))
    # A line over the 2 KiB line cap is shortened after redaction too.
    long_line = "x " * 1015 + token + " tail " + "y" * 100  # unredacted, the 2 KiB cut lands inside the token
    shortened, _, count = fb.fit_text(fb.Redactor().text(long_line), 64 * 1024)
    assert count == 1 and not any(token[i:i + 6] in shortened for i in range(len(token) - 5))
    # The start of a log window is a partial line: it is dropped unread, never redacted in part.
    log = tmp_path / "big.log"
    log.write_text("lead " + "\n".join(filler) + "\n" + secret_line + "\nlast\n")
    window = len(secret_line) // 2 + len("\nlast\n")  # the window starts inside the secret line
    assert token[-10:] in log.read_bytes()[-window:].decode()
    text, source = fb.read_tail(str(log), window=window)
    assert source == log.stat().st_size and text == "last\n", "the partial first line was dropped unread"


def _pod(tmp_path, *, stderr=None, engine=None, result=None, graph=None, bindings=None):
    """A pod-shaped tree under ``tmp_path`` and the collector request for it."""
    root = tmp_path / "pod"
    run = root / "root/astrid/runs/0123456789abcdef0123456789abcdef"
    job = root / "workspace/.astrid-jobs/fedcba9876543210fedcba9876543210"
    engine_root = root / "root/astrid/engines/comfyui"
    for folder in (run / "in", job, engine_root / "runtime/out/sessions/comfyui", engine_root / ".astrid-engine"):
        folder.mkdir(parents=True, exist_ok=True)
    (run / "in/source.json").write_text(json.dumps(graph if graph is not None else {
        "nodes": [{"id": 1, "type": "CLIPTextEncode", "inputs": [{"name": "clip", "type": "CLIP", "link": 3}],
                   "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING", "links": [4], "slot_index": 0}],
                   "widgets_values": [PROMPT, 7, True]},
                  {"id": 2, "type": "SaveImage", "inputs": [], "widgets_values": [SECRETS["hf"]]}],
        "links": [[3, 5, 0, 1, 0, "CLIP"], [4, 1, 0, 6, 1, "CONDITIONING"]]}))
    (run / "job.json").write_text(json.dumps({
        "bindings": bindings if bindings is not None else {"prompt": PROMPT, "speaker": SHORT, "story": TWO_LINES},
        "inputs": {"seed": 7}, "execution": {"source": "/root/astrid/runs/0123456789abcdef0123456789abcdef/in/source.json"}}))
    # The collector reads the run folder's own copy of the source: point it at this tree.
    body = json.loads((run / "job.json").read_text())
    body["execution"]["source"] = str(run / "in/source.json")
    (run / "job.json").write_text(json.dumps(body))
    (job / "stderr").write_text(stderr if stderr is not None else _planted_log())
    (engine_root / "runtime/out/sessions/comfyui/comfy.log").write_text(engine if engine is not None else _planted_log())
    (run / "result.json").write_text(json.dumps(result if result is not None else {
        "status": "failed", "error": {"code": "node_failed", "message": f"node 1 failed on {URLS[1]} with {PROMPT}"},
        "issues": [{"severity": "error", "code": "input_rejected",
                    "details": {"node_id": "1", "value": SECRETS["openai"], "text": SHORT}}]}))
    (engine_root / ".astrid-engine/install.json").write_text(json.dumps({"status": "failed",
                                                                        "step": "pip", "log": URLS[0]}))
    request = {"name": "0123456789abcdef0123456789abcdef", "failures_root": str(root / "root/astrid/failures"),
               "roots": [str(root / "root/astrid"), str(root / "workspace/.astrid-jobs")], "run_dir": str(run),
               "job_dir": str(job), "engine_root": str(engine_root), "phase": "fetch", "exit_code": 1,
               "include_install": True, "prepared": str(root / "root/astrid/prepared-connection.json")}
    return root, request


def _folder_text(folder: Path) -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(folder.iterdir()) if path.is_file()}


def test_the_pod_folder_holds_the_cause_and_no_planted_value(tmp_path):
    root, request = _pod(tmp_path)
    (root / "root/astrid/prepared-connection.json").write_text(json.dumps(
        {"credential_file": str(root / "root/astrid/cred")}))
    (root / "root/astrid/cred").write_text("file-held-credential-0001\n")
    stderr = (root / "workspace/.astrid-jobs/fedcba9876543210fedcba9876543210/stderr")
    stderr.write_text(stderr.read_text() + "\nusing file-held-credential-0001\n")
    folder, digest, facts = fb.collect(request, environ={"MY_SERVICE_TOKEN": POD_ONLY_SECRET})
    files = _folder_text(Path(folder))
    assert set(files) == {*fb.BUNDLE_FILES, fb.MANIFEST_NAME}
    for name, text in files.items():
        assert _leaks(text) == ([CONTROLLER_ONLY_SECRET] if name.endswith(".txt") else []), name
        assert "file-held-credential-0001" not in text
    assert "CUDA out of memory" in files["error-tail.txt"] and "CUDA out of memory" in files["engine-log-tail.txt"]
    # The workflow comes back as structure only.
    shaped = json.loads(files["workflow.redacted.json"])
    assert shaped["format"] == "litegraph"
    assert shaped["nodes"][0] == {"id": 1, "type": "CLIPTextEncode",
                                  "inputs": [{"name": "clip", "type": "CLIP", "link": 3}],
                                  "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING", "links": [4],
                                               "slot_index": 0}],
                                  "widgets_values": [{"redacted": "string", "len": len(PROMPT)},
                                                     {"redacted": "number"}, {"redacted": "boolean"}]}
    assert shaped["links"] == [[3, 5, 0, 1, 0, "CLIP"], [4, 1, 0, 6, 1, "CONDITIONING"]]
    collector = json.loads(files["collector.json"])
    assert collector["policy"] == fb.POLICY and collector["limits"]["prompt_min_chars"] == 4
    assert collector["limits"]["line_drop_chars"] == 32 and collector["workflow"]["values_replaced"] == 4
    assert collector["redaction"]["secret"] > 0 and collector["redaction"]["lines_dropped"] > 0
    assert collector["exit_code"] == 1 and collector["phase"] == "fetch"
    manifest = (Path(folder) / fb.MANIFEST_NAME).read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == digest
    for name, entry in json.loads(manifest)["files"].items():
        assert hashlib.sha256((Path(folder) / name).read_bytes()).hexdigest() == entry["sha256"]
    assert facts["files"]["error-tail.txt"]["present"] is True


def test_the_caps_hold_for_the_folder_and_for_each_file(tmp_path):
    huge = "\n".join(f"line {i} " + "y" * 120 for i in range(90000))  # ~11 MiB
    issues = [{"severity": "error", "code": "input_rejected", "details": {"text": "z" * 900}} for _ in range(600)]
    graph = {"nodes": [{"id": i, "type": "Note", "widgets_values": ["w" * 300]} for i in range(2000)], "links": []}
    _root, request = _pod(tmp_path, stderr=huge, engine=huge, result={"issues": issues}, graph=graph)
    folder, _digest, facts = fb.collect(request, environ={})
    sizes = {path.name: path.stat().st_size for path in Path(folder).iterdir()}
    assert sum(sizes.values()) <= fb.TOTAL_CAP, sizes
    for name, cap in fb.FILE_CAPS.items():
        assert sizes.get(name, 0) <= cap, name
    assert sizes[fb.MANIFEST_NAME] <= fb.MANIFEST_CAP
    assert facts["files"]["error-tail.txt"]["truncated"] and facts["files"]["engine-log-tail.txt"]["truncated"]
    # The engine tail gives way first: it got what the other files left.
    others = sum(size for name, size in sizes.items() if name not in ("engine-log-tail.txt",))
    assert sizes["engine-log-tail.txt"] <= fb.TOTAL_CAP - others
    # A tail is cut at line boundaries and keeps the newest lines.
    tail = (Path(folder) / "error-tail.txt").read_text()
    assert tail.endswith("line 89999 " + "y" * 120) and tail.splitlines()[0].startswith("line ")


def test_the_pod_keeps_only_its_newest_five_folders(tmp_path):
    root, request = _pod(tmp_path)
    names = [f"{i:032x}" for i in range(7)]
    for number, name in enumerate(names):
        folder, _, _ = fb.collect({**request, "name": name}, environ={})
        os.utime(folder, (1000 + number, 1000 + number))
    kept = sorted(path.name for path in (root / "root/astrid/failures").iterdir())
    assert kept == names[2:]


def test_the_collector_is_standard_library_source_that_runs_isolated(tmp_path):
    _root, request = _pod(tmp_path)
    command = fb.command(request)
    source, decoded = pod_collector.decode(command)
    assert decoded == request
    blob = base64.b64encode(json.dumps(request).encode()).decode()
    done = subprocess.run([sys.executable, "-I", "-S", "-", blob], input=source, capture_output=True,
                          env={"PATH": "/usr/bin:/bin"}, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    line = done.stdout.decode().strip().splitlines()[-1]
    assert line.startswith(fb.MANIFEST_MARK) and len(line) == len(fb.MANIFEST_MARK) + 64
    # A broken request prints only an exception class, never its text.
    bad = base64.b64encode(json.dumps({"name": "../escape"}).encode()).decode()
    done = subprocess.run([sys.executable, "-I", "-", bad], input=source, capture_output=True, timeout=60)
    assert done.returncode == 1 and done.stdout.decode().strip() == f"{fb.COMMAND_MARK}: failed ValueError"


def test_an_over_cap_manifest_is_refused_before_any_file_is_fetched():
    with pytest.raises(rm.BundleOverCap):
        rm._admit_bundle({"engine-log-tail.txt": {"sha256": "0" * 64, "size": fb.FILE_CAPS["engine-log-tail.txt"] + 1}}, 100)
    with pytest.raises(rm.BundleOverCap):
        rm._admit_bundle({"secrets.txt": {"sha256": "0" * 64, "size": 1}}, 100)
    with pytest.raises(rm.BundleOverCap):
        rm._admit_bundle({name: {"sha256": "0" * 64, "size": cap} for name, cap in fb.FILE_CAPS.items()}, 1)
    rm._admit_bundle({"error-tail.txt": {"sha256": "0" * 64, "size": 10}}, 100)


# ----------------------------------------------------------------------------
# Collection on the machine: before the reset, aborted by any outside release
# ----------------------------------------------------------------------------


@pytest.fixture
def pod_bundle(machine_case, tmp_path, monkeypatch):
    """The fake pod runs the real collector; its stderr, engine log and result carry planted values."""
    c = machine_case
    c.order, c.collector_requests = [], []
    c.pod_environ = {"PATH": "/usr/bin:/bin", "MY_SERVICE_TOKEN": POD_ONLY_SECRET}
    c.before_collect = None
    monkeypatch.setenv("ASTRID_FAKE_API_KEY", CONTROLLER_ONLY_SECRET)  # only the controller holds this value
    exec_original, start_original = c.pod.exec, c.pod.job_start
    log_path = c.installation.root + "/runtime/out/sessions/comfyui/comfy.log"

    def exec_(command, timeout=600):
        if pod_collector.is_collector(command):
            c.order.append("collect")
            if c.before_collect is not None:
                c.before_collect()
            return (*pod_collector.run(c.pod.files, command, tmp_path / "pod-run", environ=c.pod_environ,
                                       log=c.collector_requests), "")
        return exec_original(command, timeout)

    def job_start(command, *, job_id):
        started = start_original(command, job_id=job_id)
        if "pod_reset reset" in command:
            c.order.append("reset")
        elif " generate " in command:
            c.order.append("generate")
            root = command.split("--job ", 1)[1].split()[0].rsplit("/", 1)[0].strip("'")
            if c.pod.jobs[job_id]:
                c.pod.files[started["dir"] + "/stderr"] = _planted_log(["engine error tail: ValueError: bad seed"]).encode()
                c.pod.files[log_path] = _planted_log(["pre-reset engine line 1234"]).encode()
                c.pod.files[root + "/result.json"] = json.dumps({"status": "failed", "error": {
                    "code": "node_failed", "message": f"{PROMPT} at {URLS[1]} using {SECRETS['hf']}"}}).encode()
        return started

    c.pod.exec, c.pod.job_start = exec_, job_start
    c.job_with_prompt = lambda n=1: _with_prompt(c.job(n))
    return c


def _with_prompt(job):
    from dataclasses import replace
    return replace(job, inputs={**job.inputs, "workflow_inputs": json.dumps(
        {"filename_prefix": PROMPT, "speaker": SHORT, "story": TWO_LINES})})


def _receipt_files(receipts):
    path = failure_receipt.find_receipt(None, root=receipts)
    return json.loads(path.read_text()), path.parent / "files"


def test_an_execution_failure_collects_the_bundle_before_the_reset(pod_bundle, receipts):
    c = pod_bundle
    c.pod.fail_jobs = {" generate cpu-image "}
    error, timing, _machine = _run(c, receipts, job=c.job_with_prompt())
    assert isinstance(error, rm.MachineError)
    # Collection ran after the job and before the reset that sweeps the run folder and logs.
    assert c.order[-3:] == ["generate", "collect", "reset"], c.order
    request = c.collector_requests[-1]
    assert request["run_dir"].startswith("/root/astrid/runs/") and request["job_dir"].startswith("/workspace/.astrid-jobs/")
    assert request["failures_root"] == rm.FAILURE_ROOT and request["exit_code"] == 1
    receipt, files = _receipt_files(receipts)
    collection = receipt["collection"]
    assert collection["state"] == "collected" and collection["cap_bytes"] == 262144 and collection["local_only"]
    assert len(collection["bundle_manifest_sha256"]) == 64 and 0 < collection["bytes"] <= 262144
    names = {row["name"] for row in receipt["files"]}
    assert {"error-tail.txt", "engine-log-tail.txt", "node-error.json", "workflow.redacted.json",
            "collector.json"} <= names
    assert collection["bytes"] == sum(row["bytes"] for row in receipt["files"])
    for row in receipt["files"]:
        data = (files / row["name"]).read_bytes()
        assert row["path"] == "files/" + row["name"] and len(data) == row["bytes"]
        assert hashlib.sha256(data).hexdigest() == row["sha256"] and len(row["pod_sha256"]) == 64
    # The files hold the pre-reset content: the cause is named without SSH.
    assert "pre-reset engine line 1234" in (files / "engine-log-tail.txt").read_text()
    assert "engine error tail: ValueError: bad seed" in (files / "error-tail.txt").read_text()
    # The reset removed the run and job folders afterwards; the pod keeps its redacted folder.
    reset = c.pod.resets[-1]
    assert not [p for p in c.pod.files if p.startswith(reset["dirs"][0] + "/")]
    pod_folder = collection["pod_folder"]
    pod_texts = {p: data.decode() for p, data in c.pod.files.items() if p.startswith(pod_folder + "/")}
    assert pod_folder.startswith("/root/astrid/failures/") and len(pod_texts) == len(names) + 1
    # Pod side: nothing planted survives but the value only the controller knows.
    for path, text in pod_texts.items():
        assert set(_leaks(text)) <= {CONTROLLER_ONLY_SECRET}, path
    assert any(CONTROLLER_ONLY_SECRET in text for text in pod_texts.values()), "the pod could not know it"
    # Controller side and the final local files: nothing planted survives at all.
    for row in receipt["files"]:
        assert _leaks((files / row["name"]).read_text()) == [], row["name"]
    assert _leaks(json.dumps(receipt)) == []
    redaction = receipt["redaction"]
    assert redaction["applied"] and redaction["policy"] == "astrid.failure-redaction.v1"
    assert redaction["pod"]["lines_dropped"] > 0 and redaction["pod"]["secret"] > 0 and redaction["pod"]["url"] > 0
    assert redaction["local"]["secret"] >= 1, "the controller pass removed its own value"
    assert redaction["rule"]["prompt_min_chars"] == 4 and redaction["rule"]["line_drop_chars"] == 32
    # The pod was safe: it is kept, and the staging copy is gone.
    assert receipt["machine"]["after"] == "kept" and c.deletes == []
    assert not list(Path(c.job().scratch).glob("failure-bundle-*"))
    assert receipt["timing"]["phases"]["failure_collection"]["seconds"] is not None


def test_generate_failure_names_the_cause_from_the_bundle(pod_bundle, tmp_path, monkeypatch, capsys):
    from astrid.core.generation.cli import main as generate

    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    root = tmp_path / "data" / "generation" / "failures"
    c = pod_bundle
    c.pod.fail_jobs = {" generate cpu-image "}
    _run(c, root, job=c.job_with_prompt())
    capsys.readouterr()
    assert generate(["failure", "task"]) == 0
    human = capsys.readouterr().out
    assert "Bundle: collected, " in human and "redacted on the pod and on this machine; local only" in human
    assert "files/error-tail.txt" in human and "Last lines of error-tail.txt:" in human
    assert "CUDA out of memory" in human and _leaks(human) == []


@pytest.mark.parametrize("reason", ["paid_deadline", "explicit", "host_shutdown"])
def test_an_outside_release_aborts_collection_and_wins(pod_bundle, receipts, reason):
    c = pod_bundle
    c.pod.fail_jobs = {" generate cpu-image "}
    releaser = []

    def release_now():
        record = c.read()
        thread = threading.Thread(target=lambda: c.host.release(
            reason=reason, _expected_operation=record["operation_id"], expected_machine_id=record["pod_id"]))
        thread.start()
        releaser.append(thread)
        for _ in range(500):
            if c.read()["lease"]["state"] == "releasing" or c.read()["state"] == "cleanup_complete":
                break
            time.sleep(0.01)

    c.before_collect = release_now
    error, _timing, _machine = _run(c, receipts, job=c.job_with_prompt())
    releaser[0].join(10)
    assert isinstance(error, rm.MachineError) and not releaser[0].is_alive()
    receipt = _only(receipts)
    assert receipt["collection"]["state"] == "aborted_by_release" and receipt["collection"]["reason"] == reason
    assert receipt["files"] == [] and receipt["redaction"]["applied"] is False
    assert c.read()["state"] == "cleanup_complete" and c.deletes == ["pod-1"]
    assert "reset" not in c.order[c.order.index("collect"):], "no reset after an outside release"


def test_the_paid_timer_aborts_collection(pod_bundle, receipts):
    c = pod_bundle
    c.pod.fail_jobs = {" generate cpu-image "}
    c.before_collect = lambda: c.clock.advance(c.read()["lease"]["paid_expires_at"] - c.clock() + 1)
    _run(c, receipts, job=c.job_with_prompt())
    receipt = _only(receipts)
    assert receipt["collection"]["state"] == "aborted_by_release"
    assert receipt["collection"]["reason"] == "paid_deadline"
    assert c.read()["state"] == "cleanup_complete" and c.deletes == ["pod-1"]


def test_a_bundle_digest_mismatch_marks_the_machine_unsafe_and_releases_it(pod_bundle, receipts):
    c = pod_bundle
    c.pod.fail_jobs = {" generate cpu-image "}
    c.pod.corrupt_get = "error-tail.txt"
    _run(c, receipts, job=c.job_with_prompt())
    receipt = _only(receipts)
    assert receipt["collection"]["state"] == "digest_mismatch" and receipt["files"] == []
    assert receipt["machine"]["after"] == "released" and receipt["machine"]["release_reason"] == "unsafe_job_state"
    assert c.deletes == ["pod-1"] and "reset" not in c.order[c.order.index("collect"):]


def test_an_over_cap_bundle_is_not_fetched_and_the_machine_is_kept(pod_bundle, receipts):
    c = pod_bundle
    c.pod.fail_jobs = {" generate cpu-image "}
    gets = []
    original_get = c.pod.get

    def get(remote, local):
        gets.append(remote)
        return original_get(remote, local)

    c.pod.get = get
    original = c.pod.exec

    def oversized(command, timeout=600):
        code, out, err = original(command, timeout)
        if pod_collector.is_collector(command):
            folder = c.collector_requests[-1]["failures_root"] + "/" + c.collector_requests[-1]["name"]
            body = json.dumps({"schema_version": 1, "files": {"engine-log-tail.txt": {
                "sha256": "0" * 64, "size": 10 * 1024 * 1024}}}, sort_keys=True, indent=2) + "\n"
            c.pod.files[folder + "/" + fb.MANIFEST_NAME] = body.encode()
            out = fb.MANIFEST_MARK + hashlib.sha256(body.encode()).hexdigest() + "\n"
        return code, out, err

    c.pod.exec = oversized
    _run(c, receipts, job=c.job_with_prompt())
    receipt = _only(receipts)
    assert receipt["collection"]["state"] == "over_cap" and receipt["files"] == []
    bundle_gets = [path for path in gets if path.startswith(rm.FAILURE_ROOT)]
    assert bundle_gets and all(path.endswith(fb.MANIFEST_NAME) for path in bundle_gets)
    assert receipt["machine"]["after"] == "kept"


def test_an_unreachable_collector_never_changes_release(pod_bundle, receipts):
    c = pod_bundle
    c.pod.fail_jobs = {" generate cpu-image "}
    original = c.pod.exec

    def unreachable(command, timeout=600):
        if pod_collector.is_collector(command):
            raise ConnectionError("agent at https://10.9.9.9/ refused with " + SECRETS["hf"])
        return original(command, timeout)

    c.pod.exec = unreachable
    _run(c, receipts, job=c.job_with_prompt())
    receipt = _only(receipts)
    assert receipt["collection"]["state"] == "unreachable" and receipt["machine"]["after"] == "kept"
    assert _leaks(json.dumps(receipt)) == [] and "10.9.9.9" not in json.dumps(receipt)


def test_a_preparation_failure_collects_first_in_the_claim_handler(pod_bundle, receipts):
    c = pod_bundle
    c.pod.fail_jobs = {"engines install"}
    original_release = c.host.release
    released = []

    def release(**kw):
        released.append(len(c.order))
        return original_release(**kw)

    c.host.release = release
    error, _timing, _machine = _run(c, receipts, job=c.job_with_prompt())
    assert error is not None
    receipt = _only(receipts)
    assert receipt["stage"] == "install" and receipt["phase"] == "claim"
    assert receipt["collection"]["state"] == "collected", receipt["collection"]
    assert "collect" in c.order and released and released[0] > c.order.index("collect"), (c.order, released)
    request = c.collector_requests[-1]
    assert request["name"].startswith("prep-") and request["include_install"] and request["run_dir"] is None
    assert request["job_dir"].startswith("/workspace/.astrid-jobs/")
    names = {row["name"] for row in receipt["files"]}
    assert "error-tail.txt" in names and "collector.json" in names
    assert c.deletes == ["pod-1"]


def test_a_bounded_get_lands_a_file_through_a_spawned_caller_and_an_abort_stops_it(tmp_path):
    """The real bounded transport (spawned caller, deadline, abort) carries the bundle fetch."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from astrid.packs.runpod.remote_placement import RemotePlacementError, _HttpsOps

    body = b'{"schema": "astrid.failure-bundle.v1"}\n'
    seen = []

    class Agent(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(request["path"])
            chunk = body[request["offset"]:]
            reply = json.dumps({"data": base64.b64encode(chunk).decode(), "offset": request["offset"],
                                "total": len(body), "sha256": hashlib.sha256(chunk).hexdigest(),
                                "etag": hashlib.sha256(body).hexdigest()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Agent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        ops = _HttpsOps("pod1", "x" * 32, base_url=f"http://127.0.0.1:{server.server_port}")
        bounded = ops.bounded(time.monotonic() + 30, abort_event=threading.Event())
        landed = tmp_path / "collector.json"
        bounded.get(rm.FAILURE_ROOT + "/abc/collector.json", landed)
        assert landed.read_bytes() == body and seen == [rm.FAILURE_ROOT + "/abc/collector.json"]
        aborted = threading.Event()
        aborted.set()
        with pytest.raises(RemotePlacementError, match="interrupted by exact release"):
            ops.bounded(time.monotonic() + 30, abort_event=aborted).get(rm.FAILURE_ROOT + "/abc/x", tmp_path / "x")
        assert len(seen) == 1
    finally:
        server.shutdown()
        server.server_close()
