"""D9/LS-9: actual client against a private loopback HTTP stand-in, not real ComfyUI."""
from __future__ import annotations
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPException
import pytest
from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient

@pytest.fixture
def private_comfy():
    state = dict(running=[], pending=["ours", "other"], calls=[], queue_override=None,
                 promote_on_delete=False, drop_delete_ack=False, lock=threading.Lock())
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_args): pass
        def reply(self, payload, code=200):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            with state["lock"]:
                state["calls"].append(("GET", self.path, None))
                if self.path != "/queue":
                    self.reply({"unexpected": self.path}, 404)
                    return
                queue = state["queue_override"]
                if queue is None:
                    queue = {"queue_running": [[i,p] for i,p in enumerate(state["running"])],
                             "queue_pending": [[i,p] for i,p in enumerate(state["pending"])]}
                self.reply(queue)
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            with state["lock"]:
                state["calls"].append(("POST", self.path, body))
                if self.path == "/queue":
                    if state["promote_on_delete"]:
                        state["pending"] = [p for p in state["pending"] if p != "ours"]
                        state["running"] = ["ours"]
                    state["pending"] = [p for p in state["pending"] if p not in body.get("delete", [])]
                    if state["drop_delete_ack"]:
                        self.close_connection = True
                        return
                self.reply({})
    server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    try: yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

def client(state, kind="attached", prompt_id="ours"):
    value = ComfyTargetClient(ComfyTarget(state["url"],kind))
    if prompt_id is not None: value.witness_prompt(prompt_id)
    return value

def assert_no_global_control(state):
    assert not any(path in {"/interrupt","/api/interrupt","/api/free","/free"} for _,path,_ in state["calls"])
    assert not any(body and "clear" in body for _,_,body in state["calls"])

@pytest.mark.parametrize("kind",["attached","managed"])
def test_running_owned_job_is_refused_not_interrupted(private_comfy,kind):
    state=private_comfy; state["running"]=["ours"]
    with pytest.raises(ValueError,match="running.*global interrupt") as error:
        client(state,kind).cancel_prompt("ours")
    assert getattr(error.value,"code",None)=="running_cancellation_unsupported"
    assert getattr(error.value,"prompt_id",None)=="ours"
    assert state["running"]==["ours"] and state["pending"]==["other"]
    assert_no_global_control(state)

@pytest.mark.parametrize("kind",["attached","managed"])
def test_pending_owned_job_is_deleted_without_touching_other_work(private_comfy,kind):
    state=private_comfy; state["running"]=["foreign"]
    client(state,kind).cancel_prompt("ours")
    assert state["pending"]==["other"] and state["running"]==["foreign"]
    assert state["calls"]==[("POST","/queue",{"delete":["ours"]}),("GET","/queue",None)]
    assert_no_global_control(state)

def test_pending_to_running_race_is_refused(private_comfy):
    state=private_comfy; state["promote_on_delete"]=True
    with pytest.raises(ValueError,match="running.*global interrupt"): client(state).cancel_prompt("ours")
    assert state["running"]==["ours"] and state["pending"]==["other"]
    assert_no_global_control(state)

def test_finished_job_is_not_resubmitted_or_interrupted(private_comfy):
    state=private_comfy; state["pending"]=["other"]; state["running"]=["foreign"]
    client(state).cancel_prompt("ours")
    assert state["pending"]==["other"] and state["running"]==["foreign"]
    assert_no_global_control(state)
    assert not any(path=="/prompt" for _,path,_ in state["calls"])

@pytest.mark.parametrize("requested",["other","guessed"])
def test_unwitnessed_job_is_refused_before_network(private_comfy,requested):
    with pytest.raises(ValueError,match="witnessed prompt acceptance"): client(private_comfy).cancel_prompt(requested)
    assert private_comfy["calls"]==[]

@pytest.mark.parametrize("requested",["","../ours","ours?x=1",None])
def test_invalid_identity_is_refused_before_network(private_comfy,requested):
    with pytest.raises(ValueError,match="unsafe"): client(private_comfy).cancel_prompt(requested)
    assert private_comfy["calls"]==[]

@pytest.mark.parametrize("queue",[{}, {"queue_running":{}}, {"queue_running":[[0,None]]}, {"queue_running":[[]]}])
def test_malformed_queue_is_not_success_or_permission_to_interrupt(private_comfy,queue):
    private_comfy["queue_override"]=queue
    with pytest.raises(ValueError,match="running prompt identities"): client(private_comfy).cancel_prompt("ours")
    assert_no_global_control(private_comfy)

@pytest.mark.parametrize("payload",[{}, {"prompt_id":"ours"}, {"prompt_id":"other"}])
def test_attached_raw_interrupt_is_refused_even_with_owned_prompt(private_comfy,payload):
    with pytest.raises(ValueError): client(private_comfy).request_json("/interrupt",payload)
    assert private_comfy["calls"]==[]

def test_repeat_cancellation_of_pending_job_is_safe(private_comfy):
    value=client(private_comfy); value.cancel_prompt("ours"); value.cancel_prompt("ours")
    assert private_comfy["pending"]==["other"]
    assert_no_global_control(private_comfy)

def test_separate_callers_cannot_cancel_each_others_jobs(private_comfy):
    state=private_comfy; first,second=client(state),client(state,prompt_id="other")
    with pytest.raises(ValueError,match="witnessed"): first.cancel_prompt("other")
    with pytest.raises(ValueError,match="witnessed"): second.cancel_prompt("ours")
    assert state["calls"]==[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda pair: pair[0].cancel_prompt(pair[1]),[(first,"ours"),(second,"other")]))
    assert state["pending"]==[]
    assert_no_global_control(state)

def test_lost_delete_ack_does_not_retry_or_interrupt(private_comfy):
    private_comfy["drop_delete_ack"]=True
    with pytest.raises((ValueError, HTTPException)): client(private_comfy).cancel_prompt("ours")
    assert private_comfy["calls"]==[("POST","/queue",{"delete":["ours"]})]
    assert_no_global_control(private_comfy)

def test_private_journal_cleanup_propagates_running_refusal(private_comfy,tmp_path):
    from astrid.core.generation.backends.vibecomfy_local_server import cleanup_prompts
    from astrid.core.generation.vibecomfy_runtime_root import vibecomfy_runtime_root
    root=tmp_path/"attempt"; root.mkdir()
    journal=vibecomfy_runtime_root(root)/"out/runs/our-run/attempt.json"; journal.parent.mkdir(parents=True)
    body={"queue_acceptance":{"status":"accepted","prompt_id":"ours"}}
    journal.write_text(json.dumps(body)); private_comfy["running"]=["ours"]
    with pytest.raises(ValueError,match="running.*global interrupt"):
        cleanup_prompts(private_comfy["url"],root,confirm=True)
    assert json.loads(journal.read_text())==body
    assert_no_global_control(private_comfy)
