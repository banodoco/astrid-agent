import hashlib
import json
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).parents[1] / "scripts" / "journey_pinokio_open.mjs"


HARNESS = r"""
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const [script, app, scenario] = process.argv.slice(2);
let unhandled = [];
process.on('unhandledRejection', (error) => unhandled.push(String(error)));
const health = {status:'ok', protocol:'workspace.v1', identity:{release:14, managed_by:'pinokio'}, runtime_instance_id:'runtime-1', schema_digest:'schema-1'};
let captured = false, connected = false, fallbackCalls = 0;
const response = {url:()=> 'http://127.0.0.1:17340/api/runtime/v1/health', ok:()=>true, json:async()=>{
  captured = true;
  if (['expired', 'unavailable', 'wrong-release'].includes(scenario) || (scenario === 'fast-only' && connected)) {
    throw new Error('No resource with given identifier found');
  }
  if (scenario === 'parse-error') throw new Error('invalid json');
  return health;
}};
let fallbackUrl = null, fallbackOptions = null, timeoutMs = null;
const originalTimeout = AbortSignal.timeout.bind(AbortSignal);
AbortSignal.timeout = (ms) => { timeoutMs = ms; return originalTimeout(ms); };
globalThis.fetch = async (url, options) => {
  fallbackUrl = url; fallbackOptions = options;
  if (scenario === 'unavailable') return {ok:false, status:503};
  return {ok:true, status:200, json:async()=>scenario === 'wrong-release'
    ? {...health, identity:{...health.identity, release:13}} : health};
};
const page = {
 route: async()=>{},
 waitForResponse: (predicate, opts)=> {
   if (opts.timeout !== 60000 || !predicate(response)) throw new Error('health response did not match');
   return Promise.resolve(response);
 },
 goto: async()=>({ok:()=>true}),
 locator:()=>({waitFor:async()=>{await new Promise(r=>setTimeout(r,30)); connected = true;}}),
 evaluate: async (fn, arg)=> {
   if (typeof arg === 'string') { fallbackCalls++; return fn(arg); }
   const manifest='manifest'; return {build:{app:'app-sha'}, manifest};
 },
};
const modDir=path.join(app,'node_modules','@playwright','test'); fs.mkdirSync(modDir,{recursive:true});
fs.writeFileSync(path.join(modDir,'package.json'), JSON.stringify({main:'index.cjs'}));
fs.writeFileSync(path.join(modDir,'index.cjs'),`exports.chromium = {launch:async()=>({newContext:async()=>({newPage:async()=>globalThis.__page}),close:async()=>{}})};`);
globalThis.__page=page;
const oldArgv=process.argv;
process.argv=[oldArgv[0],script,'/tmp/pinokio-dist','https://astrid.haus/?connect=1',path.join(app,'result.json'),'app-sha','MANIFEST_SHA',app];
let outcome='ok';
try { await import(pathToFileURL(script)); } catch(e) { outcome=String(e); }
await new Promise(r=>setTimeout(r,80));
console.log(JSON.stringify({outcome,unhandled,captured,fallbackCalls,fallbackUrl,fallbackCache:fallbackOptions?.cache,fallbackTimeoutMs:timeoutMs,output:fs.existsSync(path.join(app,'result.json'))}));
"""


def run_driver(tmp_path, scenario):
    app = tmp_path / "app"
    app.mkdir()
    (app / "package.json").write_text("{}")
    manifest_sha = hashlib.sha256(b"manifest").hexdigest()
    harness = HARNESS.replace("MANIFEST_SHA", manifest_sha)
    harness_path = tmp_path / "harness.mjs"
    harness_path.write_text(harness)
    result = subprocess.run(
        ["node", str(harness_path), str(SCRIPT), str(app), scenario],
        capture_output=True, text=True, timeout=8,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_expired_health_body_falls_back_and_verifies_identity(tmp_path):
    result = run_driver(tmp_path, "expired")
    assert result["outcome"] == "ok"
    assert result["fallbackCalls"] == 1
    assert result["fallbackUrl"] == "http://127.0.0.1:17340/api/runtime/v1/health"
    assert result["fallbackCache"] == "no-store"
    assert result["fallbackTimeoutMs"] == 10000
    assert result["output"] is True
    assert result["unhandled"] == []


def test_parse_error_is_handled_during_connection_wait(tmp_path):
    result = run_driver(tmp_path, "parse-error")
    assert "invalid json" in result["outcome"]
    assert result["unhandled"] == []


def test_consumes_original_body_before_waiting_on_the_connection_badge(tmp_path):
    result = run_driver(tmp_path, "fast-only")
    assert result["outcome"] == "ok"
    assert result["fallbackCalls"] == 0
    assert result["output"] is True


def test_unavailable_fallback_fails_without_output(tmp_path):
    result = run_driver(tmp_path, "unavailable")
    assert "fallback failed" in result["outcome"]
    assert result["output"] is False


def test_fallback_identity_must_match_exact_release(tmp_path):
    result = run_driver(tmp_path, "wrong-release")
    assert "wrong live Runtime identity" in result["outcome"]
    assert result["fallbackCalls"] == 1
    assert result["output"] is False
