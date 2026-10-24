"""Build OpenShell policies and provider profiles from catalog.json (stdlib only).

Used by astrid-openshell.sh; run with `python3 -I`.

  compose_policy.py install                          install-time policy (JSON)
  compose_policy.py run MODE [SERVICE ...]           run-time policy (JSON)
  compose_policy.py boundary MODE [SERVICE ...]      run-time policy + provider rules, for the prover
  compose_policy.py profile SERVICE                  OpenShell provider profile (YAML)
  compose_policy.py menu                             tab-separated lines for the menu
  compose_policy.py field SERVICE KEY                one service field ("" when absent)

A SERVICE written as `name+cred` means its credential provider is attached.
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


def endpoint(spec: dict) -> dict:
    out = {"host": spec["host"], "port": spec.get("port", 443)}
    if spec.get("tcp"):
        out["protocol"] = "tcp"
        return out
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
    return out


def parse(services: list[str]) -> tuple[list[str], set[str]]:
    names, with_credential = [], set()
    for item in services:
        name, _, flag = item.partition("+")
        if name not in CATALOG["services"]:
            raise SystemExit(f"unknown service {name!r}")
        names.append(name)
        if flag == "cred":
            with_credential.add(name)
    return names, with_credential


def run_policy(mode: str, services: list[str]) -> dict:
    if mode not in CATALOG["modes"]:
        raise SystemExit(f"unknown mode {mode!r}")
    names, with_credential = parse(services)
    wanted = list(CATALOG["modes"][mode]["rules"])
    for name in names:
        wanted += CATALOG["services"][name].get("rules", [])
    dropped = {CATALOG["services"][n]["credential"].get("replaces_rule") for n in with_credential}
    policy = json.loads(json.dumps(BASE))
    rules = {}
    for rule in dict.fromkeys(wanted):
        if rule in dropped:
            continue
        spec = CATALOG["rules"][rule]
        rules[rule] = {"name": rule, "endpoints": [endpoint(e) for e in spec["endpoints"]],
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
    command, *args = argv
    if command == "install":
        policy = json.loads(json.dumps(BASE))
        policy["network_policies"] = {"astrid_install": {
            "name": "astrid-install",
            "endpoints": [endpoint({"host": h, "access": "read-only",
                                    "allow_encoded_slash": h == "registry.npmjs.org"}) for h in INSTALL_HOSTS],
            "binaries": [{"path": p} for p in INSTALL_BINARIES]}}
        json.dump(policy, sys.stdout, indent=2)
    elif command == "run":
        json.dump(run_policy(args[0], args[1:]), sys.stdout, indent=2)
    elif command == "boundary":
        policy = run_policy(args[0], args[1:])
        _, with_credential = parse(args[1:])
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
