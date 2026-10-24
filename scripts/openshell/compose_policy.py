"""Build OpenShell policies and provider profiles from catalog.json (stdlib only).

Used by astrid-openshell.sh; run with `python3 -I`.

  compose_policy.py [--mount PATH]... install        install-time policy (JSON)
  compose_policy.py run MODE [SERVICE ...]           run-time policy (JSON)
  compose_policy.py boundary MODE [SERVICE ...]      run-time policy + provider rules, for the prover
  compose_policy.py profile SERVICE                  OpenShell provider profile (YAML)
  compose_policy.py menu                             tab-separated lines for the menu
  compose_policy.py field SERVICE KEY                one service field ("" when absent)

A SERVICE written as `name+cred` means its credential provider is attached;
`name@PARAM` passes a parameter (the port for `comfyui`). Every --mount PATH is
added read-only to the filesystem policy (it must match in every policy for a
sandbox, since the filesystem policy is fixed when the sandbox starts).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CATALOG = json.loads((Path(__file__).with_name("catalog.json")).read_text(encoding="utf-8"))

# Fixed when a sandbox starts, so every policy for it must carry the same values.
BASE = {
    "version": 1,
    "filesystem_policy": {
        "include_workdir": True,
        "read_only": ["/bin", "/usr", "/lib", "/proc", "/dev/urandom", "/etc", "/var/log"],
        "read_write": ["/tmp", "/dev/null"],
    },
    "landlock": {"compatibility": "hard_requirement"},
}

# What the pinned installer fetches (see docs/setup/openshell-sandbox.md).
INSTALL_HOSTS = [
    "astral.sh", "releases.astral.sh", "github.com", "codeload.github.com",
    "objects.githubusercontent.com", "release-assets.githubusercontent.com", "nodejs.org",
    "johnvansickle.com", "pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
]
INSTALL_BINARIES = ["/usr/bin/curl", *CATALOG["binaries"]["astrid_tools"]]


def binaries(groups: list[str]) -> list[dict]:
    paths: list[str] = []
    for group in groups:
        for path in CATALOG["binaries"][group]:
            if path not in paths:
                paths.append(path)
    return [{"path": path} for path in paths]


# ComfyUI's core HTTP API (server.py), served with and without an /api prefix.
# Everything else on the server is refused: ComfyUI-Manager (installs code),
# settings and userdata writes, and routes that custom nodes add.
COMFY_GET = ["/system_stats", "/object_info", "/object_info/*", "/history", "/history/*", "/view",
             "/view_metadata/*", "/queue", "/prompt", "/features", "/embeddings", "/extensions",
             "/models", "/models/*", "/internal/folder_paths", "/userdata", "/userdata/*"]
COMFY_POST = ["/prompt", "/upload/image", "/upload/mask", "/interrupt", "/queue", "/history", "/free"]
COMFY_JOBS = [("GET", "/api/jobs"), ("GET", "/api/jobs/*"), ("POST", "/api/jobs/cancel"),
              ("POST", "/api/jobs/*/cancel")]


def comfy_endpoints(host: str, port: int) -> list[dict]:
    rules = []
    for method, paths in (("GET", COMFY_GET), ("POST", COMFY_POST)):
        for path in paths:
            rules.append({"allow": {"method": method, "path": path}})
            rules.append({"allow": {"method": method, "path": "/api" + path}})
    rules += [{"allow": {"method": m, "path": p}} for m, p in COMFY_JOBS]
    api = {"host": host, "port": port, "protocol": "rest", "enforcement": "enforce", "rules": rules,
           # Saved workflows are addressed as /userdata/workflows%2F<name>.json.
           "allow_encoded_slash": True}
    sockets = [{"host": host, "port": port, "path": path, "protocol": "websocket",
                "enforcement": "enforce", "access": "read-write"} for path in ("/ws", "/api/ws")]
    return [api, *sockets]


def endpoints(spec: dict, param: str | None = None) -> list[dict]:
    port = spec.get("port", 443)
    if port == "{port}":
        if not param or not param.isdigit() or not 1 <= int(param) <= 65535:
            raise SystemExit(f"{spec['host']} needs a port (service@PORT)")
        port = int(param)
    if spec.get("comfy_api"):
        return comfy_endpoints(spec["host"], port)
    out = {"host": spec["host"], "port": port}
    if spec.get("tcp"):
        out["protocol"] = "tcp"
        return [out]
    out.update(protocol="rest", enforcement="enforce")
    if spec.get("git_read"):
        # Clone and fetch need POST .../git-upload-pack; pushes (git-receive-pack) stay blocked.
        out["rules"] = [
            {"allow": {"method": "GET", "path": "/**"}},
            {"allow": {"method": "HEAD", "path": "/**"}},
            {"allow": {"method": "POST", "path": "/*/*/git-upload-pack"}},
        ]
    else:
        out["access"] = spec["access"]
    if spec.get("allow_encoded_slash"):
        out["allow_encoded_slash"] = True
    return [out]


def endpoint(spec: dict) -> dict:
    return endpoints(spec)[0]


def parse(services: list[str]) -> tuple[list[str], set[str], dict[str, str]]:
    """`name`, `name+cred`, `name@param`; returns names, credentialed names, params."""
    names, with_credential, params = [], set(), {}
    for item in services:
        item, _, flag = item.partition("+")
        name, _, param = item.partition("@")
        if name not in CATALOG["services"]:
            raise SystemExit(f"unknown service {name!r}")
        names.append(name)
        if flag == "cred":
            with_credential.add(name)
        if param:
            params[name] = param
    return names, with_credential, params


def base_policy(mounts: list[str]) -> dict:
    policy = json.loads(json.dumps(BASE))
    for path in mounts:
        if not path.startswith("/") or ".." in path.split("/"):
            raise SystemExit(f"mount target must be an absolute path: {path!r}")
        policy["filesystem_policy"]["read_only"].append(path)
    return policy


def run_policy(mode: str, services: list[str], mounts: list[str]) -> dict:
    if mode not in CATALOG["modes"]:
        raise SystemExit(f"unknown mode {mode!r}")
    names, with_credential, params = parse(services)
    wanted = [(rule, None) for rule in CATALOG["modes"][mode]["rules"]]
    for name in names:
        wanted += [(rule, params.get(name)) for rule in CATALOG["services"][name].get("rules", [])]
    dropped = {CATALOG["services"][n]["credential"].get("replaces_rule") for n in with_credential}
    policy = base_policy(mounts)
    rules = {}
    for rule, param in dict.fromkeys(wanted):
        if rule in dropped:
            continue
        spec = CATALOG["rules"][rule]
        rules[rule] = {"name": rule,
                       "endpoints": [e for spec_e in spec["endpoints"] for e in endpoints(spec_e, param)],
                       "binaries": binaries(spec["binaries"])}
    if rules:
        policy["network_policies"] = rules
    return policy


def provider_rule(name: str) -> dict:
    cred = CATALOG["services"][name]["credential"]
    return {"name": f"provider_{name}",
            "endpoints": [endpoint({"host": cred["host"], "access": cred["access"]})],
            "binaries": binaries(cred["binaries"])}


def main(argv: list[str]) -> None:
    mounts = []
    while argv[:1] == ["--mount"]:
        mounts.append(argv[1])
        argv = argv[2:]
    command, *args = argv
    if command == "install":
        policy = base_policy(mounts)
        policy["network_policies"] = {"astrid_install": {
            "name": "astrid-install",
            "endpoints": [endpoint({"host": h, "access": "read-only",
                                    "allow_encoded_slash": h == "registry.npmjs.org"}) for h in INSTALL_HOSTS],
            "binaries": [{"path": p} for p in INSTALL_BINARIES]}}
        json.dump(policy, sys.stdout, indent=2)
    elif command == "run":
        json.dump(run_policy(args[0], args[1:], mounts), sys.stdout, indent=2)
    elif command == "boundary":
        policy = run_policy(args[0], args[1:], mounts)
        _, with_credential, _ = parse(args[1:])
        for name in sorted(with_credential):
            policy.setdefault("network_policies", {})[f"provider_{name}"] = provider_rule(name)
        json.dump(policy, sys.stdout, indent=2)
    elif command == "profile":
        name = args[0]
        service = CATALOG["services"][name]
        cred = service["credential"]
        rule = provider_rule(name)
        lines = [
            f"# Generated from catalog.json by compose_policy.py: {service['title']}.",
            f"id: astrid-{name}",
            f"display_name: {service['title']} (Astrid sandbox)",
            f"description: {service['summary']}",
            "category: inference" if cred["access"] == "read-write" else "category: other",
            "credentials:",
            "  - name: api_key",
            f"    description: {cred['prompt']}",
            f"    env_vars: [{cred['env']}]",
            "    required: true",
            f"    auth_style: {cred['auth_style']}",
            f"    header_name: {cred['header_name']}",
            "discovery:",
            "  credentials: [api_key]",
            "endpoints:",
        ]
        for e in rule["endpoints"]:
            lines += [f"  - host: {e['host']}", f"    port: {e['port']}", "    protocol: rest",
                      f"    access: {e['access']}", "    enforcement: enforce"]
        lines.append("binaries:")
        lines += [f"  - {b['path']}" for b in rule["binaries"]]
        print("\n".join(lines))
    elif command == "menu":
        for mode, spec in CATALOG["modes"].items():
            print("\t".join(["mode", mode, spec["title"], spec["summary"]]))
        for name, spec in CATALOG["services"].items():
            print("\t".join(["service", name, spec["title"], spec["summary"]]))
    elif command == "field":
        name, key = args
        service = CATALOG["services"][name]
        cred = service.get("credential", {})
        value = service.get(key, cred.get(key.removeprefix("credential_"), "") if key.startswith("credential_") else "")
        print("1" if value is True else "" if value is False else value)
    else:
        raise SystemExit(f"unknown command {command!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
