"""Node's real inherited-listener API under no child network grants.

Transport representation only: no Runtime task, fake renderer or output proof.
"""

import json
import os
import subprocess
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import custody_broker as cb
from astrid.core.execution import generic_host as gh
from astrid.core.execution.remotion_closure import RemotionClosureBinding
from astrid.core.execution.renderer_transport import RetainedRendererTransport
from tests.core.execution import test_confinement_launch_integration as confinement
from tests.core.execution import test_remotion_closure_binding as closure_tests
from tests.core.execution.test_inventory_load_commands import run_assertions_with_retained_owner

launch = confinement.launch
roots = closure_tests.roots


def test_node_adopts_owner_retained_listener_without_child_bind(launch, roots, tmp_path, request):
    if os.environ.get("ASTRID_TEST_RETAINED_OWNER_CHILD") != "1":
        run_assertions_with_retained_owner(tmp_path, request.node.nodeid, ("node",))
        return
    assert cb._COMMAND_INVENTORY_FD in os.environ
    policy, *_ = launch
    bindings = gh.ExecutableAdmission(
        "/Users/peteromalley/.nvm/versions/node/v20.19.4/bin:/usr/bin:/bin"
    ).require(("node",))
    directory = policy.attempt_root / ".astrid-executables"
    directory.mkdir(mode=0o700)
    for binding in bindings:
        (directory / binding.name).symlink_to(binding.executable.resolved)
    policy = replace(
        policy,
        executable_bindings=bindings,
        executable_directory=directory,
        bootstrap_files=(*policy.bootstrap_files, Path("/System/Library/OpenSSL/openssl.cnf")),
    )
    closure = RemotionClosureBinding.admit(**roots[3])
    state = {"active": True}

    def guard():
        if not state["active"]:
            raise ValueError("expired owner")

    bridge = SimpleNamespace(_guard=guard, digest="1" * 64, source_digest="2" * 64)
    transport = RetainedRendererTransport(bridge=bridge, closure=closure, policy=policy)
    policy = replace(policy, renderer_transport=transport)
    owner = transport.socket
    address = transport.address
    script = policy.private_write_root / "consume.cjs"
    script.write_text("""
const http=require('node:http'),net=require('node:net'),fs=require('node:fs');
const server=http.createServer((q,r)=>{r.end('retained-endpoint');server.close();});
const selected=JSON.parse(process.env.ASTRID_TEST_ENDPOINT);
for(const [label,host,port] of [['fresh127','127.0.0.1',0],['same127','127.0.0.1',selected],['wildcard','0.0.0.0',selected],['ipv6','::1',selected]]){
 const forbidden=net.createServer();
 forbidden.on('error',e=>console.log(JSON.stringify({event:'new-bind-denied',label,code:e.code,pid:process.pid})));
 forbidden.listen(port,host,()=>{console.log(JSON.stringify({event:'UNEXPECTED-new-bind',label}));forbidden.close();});
}
for(const [label,host,port] of [['other-port','127.0.0.1',selected===65535?65534:selected+1],['other-address','127.0.0.2',selected],['external','1.1.1.1',80]]){
 const connection=net.connect(port,host);
 connection.on('error',e=>console.log(JSON.stringify({event:'connect-denied',label,code:e.code,pid:process.pid})));
 connection.on('connect',()=>{console.log(JSON.stringify({event:'UNEXPECTED-connect',label}));connection.end();});
}

server.on('error',e=>{console.log(JSON.stringify({event:'consume-error',code:e.code,pid:process.pid}));process.exit(2)});
server.on('listening',()=>console.log(JSON.stringify({event:'retained-listening',address:server.address(),inode:fs.fstatSync(0).ino,pid:process.pid})));
server.listen({fd:0});
setTimeout(()=>server.close(()=>process.exit(0)),2000).unref();
""")
    plan = gh._confined_launch_plan(
        [str(bindings[0].executable.resolved), str(script)], policy=policy
    )
    process = subprocess.Popen(
        plan.argv,
        env={**dict(plan.environment), "ASTRID_TEST_ENDPOINT": str(address[1])},
        cwd=plan.cwd,
        stdin=transport.consume(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    observations = []
    try:
        while True:
            line = process.stdout.readline()
            assert line, process.stderr.read()
            row = json.loads(line)
            observations.append(row)
            if row["event"] in ("retained-listening", "consume-error"):
                break
        assert row["event"] == "retained-listening", observations
        assert row["address"]["address"] == "127.0.0.1" and row["address"]["port"] == address[1], (
            observations
        )
        assert row["inode"] == os.fstat(owner.fileno()).st_ino, observations
        connection = HTTPConnection(*address, timeout=1)
        connection.request("GET", "/")
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        assert response.status == 200 and payload == b"retained-endpoint"
        stdout, stderr = process.communicate(timeout=4)
        observations.extend(json.loads(s) for s in stdout.splitlines() if s)
        assert process.returncode == 0, (observations, stderr)
        print(json.dumps({"raw_transport_controls": observations}), flush=True)
        denied = {
            r.get("label"): r
            for r in observations
            if r["event"] in ("new-bind-denied", "connect-denied")
        }
        assert set(denied) == {
            "fresh127",
            "same127",
            "wildcard",
            "ipv6",
            "other-port",
            "other-address",
            "external",
        }, observations
        assert all(r["code"] in ("EPERM", "EACCES") for r in denied.values()), observations
        with pytest.raises(ValueError, match="replayed"):
            transport.consume()
        with pytest.raises(ValueError, match="attempt changed"):
            transport.validate(replace(policy, claim_binding=(("task", "another"),)))
        state["active"] = False
        with pytest.raises(ValueError, match="expired owner"):
            transport.validate(policy)
        print(
            json.dumps(
                {
                    "owner_pid": os.getpid(),
                    "child_pid": process.pid,
                    "endpoint": address,
                    "owner_inode": os.fstat(owner.fileno()).st_ino,
                    "observations": observations,
                    "wait_exit": process.returncode,
                    "bytes": len(payload),
                }
            ),
            flush=True,
        )
    finally:
        transport.close()
        # This bounded fixture self-closes; no PID adoption or manual signal.
        process.wait(timeout=4)
