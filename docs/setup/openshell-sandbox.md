# Run Astrid in an OpenShell sandbox (Linux)

Astrid's agent can run shell commands, and its chat bridge approves tool
calls automatically (see [SECURITY.md](../../SECURITY.md): packs run with your
full user permissions). If you would rather not give it your account, run it
inside an [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) microVM.
`scripts/openshell/` does the whole setup without sudo:

```sh
scripts/openshell/install-openshell.sh       # once: OpenShell 0.1.5, user-local, telemetry off
scripts/openshell/astrid-openshell.sh fetch   # saves install.sh + manifest, prints hashes and pins
# read the two files, then:
scripts/openshell/astrid-openshell.sh up ~/.local/share/astrid-openshell/installers/<timestamp>
```

Then open https://astrid.haus, preferably in a separate browser profile. It
reaches the sandbox's door through the same `127.0.0.1:17340` as a normal
install.

## What you get

| | Normal install | In OpenShell |
|---|---|---|
| Kernel | yours | its own (libkrun microVM) |
| Your files, `~/.ssh`, signed-in `claude`/`codex` | readable and writable | not present |
| Network while installing | anything | read-only GET to the installer's hosts: astral.sh, GitHub, nodejs.org, johnvansickle.com, PyPI, npm |
| Network afterwards | anything | nothing; an attached LLM API only, and only from Astrid's Bun |
| Your machine's localhost and LAN (ComfyUI, databases) | reachable | blocked |
| LLM key | in Astrid's store | stays in OpenShell; the sandbox sees a placeholder |

Every network rule sets `enforcement: enforce`. OpenShell's default,
`audit`, logs a violation and then allows it. `astrid-openshell.sh check`
proves the live policy stays inside `policies/boundary.yaml` with OpenShell's
prover, then probes example.com, GitHub, astrid.haus, the host's loopback
(8188, 8000, the gateway), cloud metadata and private LAN addresses from inside
the sandbox. Every probe must fail.

What it does not do: the hosted page and the agent can still do anything
*inside* the sandbox, including spending whatever LLM key is attached. Use a
throwaway key with a spend cap.

## Requirements

- Linux x86_64 with `/dev/kvm` usable by your user (the `kvm` group).
- `curl`, `tar`, `sha256sum`, `python3`, `git`, and e2fsprogs (`mke2fs`,
  `debugfs`, `e2fsck`) on the host.
- About 4.5 GB of disk: 0.4 GB for OpenShell, about 1 GB of image cache, and
  about 3 GB per installed sandbox (measured with Astrid e2cac213). Set
  `ASTRID_OPENSHELL_HOME` to put everything on a bigger drive.

## LLM key

```sh
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY / OPENROUTER_API_KEY
scripts/openshell/astrid-openshell.sh provider anthropic
```

This imports `providers/astrid-anthropic.yaml` and stores the key in the
OpenShell gateway, not in the sandbox. Inside the sandbox the variable holds
`openshell:resolve:env:ANTHROPIC_API_KEY`. OpenShell's proxy substitutes the
real key only on requests from Astrid's Bun binary to `api.anthropic.com`.

## Day to day

```sh
astrid-openshell.sh status          # sandbox phase and forwards
astrid-openshell.sh stop            # stop the VM, keep its disk
astrid-openshell.sh resume && astrid-openshell.sh start && astrid-openshell.sh door
astrid-openshell.sh shell           # a shell inside the sandbox
astrid-openshell.sh logs            # OpenShell's log, including every denied connection
astrid-openshell.sh destroy         # delete the sandbox (asks first)
```

The OpenShell gateway does not start at login. After a reboot, run
`$ASTRID_OPENSHELL_HOME/start-gateway.sh`.

## Testing an unreleased agent or Runtime commit

`install DIR` honours the manifest's `repo` field
([local-source-install.md](local-source-install.md)). The script clones each
named local repository into the sandbox as a bare repository and rewrites
`repo` to point at that copy.

## Notes and gotchas

- **Image.** OpenShell's default image (`nvcr.io/nvidia/base/ubuntu:24.04`)
  has no curl or wget, so the installer can't start. The script uses
  `buildpack-deps:noble-scm`, pinned by digest. Override it with
  `ASTRID_SANDBOX_IMAGE`.
- **No systemd in the sandbox.** The installer falls back to a detached
  Runtime. Re-running the install line would re-fetch its sources, which the
  run-time policy blocks, so `start` launches Runtime the way that fallback
  does.
- **No pidfds in the sandbox.** OpenShell's seccomp filter denies
  `pidfd_open` and `pidfd_send_signal`, and Runtime only signals processes
  through pidfds. So Runtime can't be stopped from inside: to restart it, run
  `stop`, then `resume`, `start` and `door`.
- **Runtime needs O_PATH directory walks.** Under Landlock, the sandbox can
  traverse `/` but not read it. Runtime versions without the
  `runtime_protocol/dirfd.py` ancestor-walk fix stop at startup with
  `[Errno 13] Permission denied: '/'`.
- **GPU.** OpenShell's microVM GPU support is whole-card VFIO passthrough with a
  root gateway. That is no use on a one-GPU machine, so this setup has no GPU.
  Keep ComfyUI and other GPU work on the host or on a cloud backend.
- **Ports.** On the microVM driver, `host.openshell.internal` is the host's
  `127.0.0.1`. Never add a rule for it unless you mean to give the sandbox
  that service.
- **Host folders.** OpenShell calls host bind mounts an "unsafe operator
  override". The gateway config written here disables them. Move files in with
  `astrid-openshell.sh shell` / `openshell sandbox upload` instead.

## macOS and Windows: UNTESTED

Nobody has run these steps on macOS or Windows yet. Reports welcome.

- **macOS (Apple Silicon).** OpenShell supports a Hypervisor.framework microVM
  and ships a Homebrew package. Install that instead of `install-openshell.sh`,
  then set `ASTRID_OPENSHELL_GATEWAY` to its gateway name and use
  `astrid-openshell.sh` as above. There is no GPU path into the sandbox. Run
  ComfyUI natively and use cloud backends.
- **Windows.** Astrid doesn't support Windows yet, and OpenShell calls WSL 2
  experimental. The likely path is this Linux setup inside WSL 2 with nested
  virtualisation (`/dev/kvm`). Whether the browser on Windows reaches
  `127.0.0.1:17340` inside WSL depends on WSL's networking mode.
