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

`up` ends with a menu where you choose what the sandbox may reach (below).
Then open https://astrid.haus, preferably in a separate browser profile. It
reaches the sandbox's door through the same `127.0.0.1:17340` as a normal
install.

## What you get

| | Normal install | In OpenShell |
|---|---|---|
| Kernel | yours | its own (libkrun microVM) |
| Your files, `~/.ssh`, signed-in `claude`/`codex` | readable and writable | not present |
| Network while installing | anything | read-only GET to the installer's hosts: astral.sh, GitHub, nodejs.org, johnvansickle.com, PyPI, npm |
| Network afterwards | anything | the mode and services you pick (below) |
| Your machine's localhost and LAN (ComfyUI, databases) | reachable | blocked in every mode |
| API keys | in Astrid's store | stay in OpenShell; the sandbox sees a placeholder |

Every network rule sets `enforcement: enforce`. OpenShell's default,
`audit`, logs a violation and then allows it. `astrid-openshell.sh check`
uses OpenShell's prover to prove the live policy allows nothing beyond your
saved mode and services. Then it probes from inside the sandbox: example.com,
astrid.haus, the host's loopback (8188, 8000, the gateway), cloud metadata,
private LAN addresses and an upload to GitHub must all fail, and in `dev` mode
PyPI must work.

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

## Choosing what the sandbox may reach

```sh
scripts/openshell/astrid-openshell.sh services        # menu: switch services on/off, change mode, apply
scripts/openshell/astrid-openshell.sh services list
scripts/openshell/astrid-openshell.sh services add huggingface codex
scripts/openshell/astrid-openshell.sh services mode dev
```

**Modes.**

- `locked` (the default): no network except the services you turn on.
- `dev`: also read-only downloads from PyPI, npm, GitHub (including
  `git clone`) and Hugging Face. Uploads and pushes are refused by OpenShell's
  request inspection.

**Services.**

| Service | What it allows | Key |
|---|---|---|
| ChatGPT plan via Astrid's agent | Astrid's own agent (omp, a Pi derivative) may reach chatgpt.com and auth.openai.com, from Bun only. Sign in with `astrid-openshell.sh login chatgpt`: in the agent, `/login` → "ChatGPT Plus/Pro (Codex, headless/device)" (enable device code login under ChatGPT > Settings > Security first). | sign-in tokens live inside the sandbox (`~/.omp/agent/agent.db`); `logout chatgpt` to remove |
| ChatGPT plan via Codex CLI | installs the official Codex CLI (pinned, matching Astrid's Codex adapter). Only Codex may reach chatgpt.com and auth.openai.com; Bun may fetch the adapter from npm. Sign in with `astrid-openshell.sh login codex` (device code; enable it under ChatGPT > Settings > Security first). | sign-in tokens live inside the sandbox; `logout codex` to remove |
| ComfyUI on this machine | Astrid's local generation (`vibecomfy.run` with `comfy_url`) on your running ComfyUI: core API only on the port you give (queue, upload, history, view, `/ws`); ComfyUI-Manager, settings and userdata writes, and custom-node routes are refused. A relay inside the sandbox serves it as `127.0.0.1:<port>`, the only form Astrid accepts. ComfyUI's output folder is shared read-only (see below). | none |
| Hugging Face | read-only model and dataset downloads (huggingface.co and its file CDNs) | optional `HF_TOKEN`, kept outside the sandbox |
| OpenAI / Anthropic / OpenRouter API key | Astrid's own agent (Bun) to that API only | kept outside the sandbox |

The menu asks for a key when you switch a service on (hidden input), or reads
it from the service's environment variable. OpenShell stores it in the
gateway. Inside the sandbox the variable holds a placeholder
(`openshell:resolve:env:...`), and OpenShell's proxy swaps in the real key
only for that service's host and binaries.

The selection is saved per sandbox, so you never edit a policy by hand. To
add a service to the catalog, add an entry to `scripts/openshell/catalog.json`.
`compose_policy.py` builds every policy and provider profile from it.
`lockdown` is the emergency switch: mode `locked`, every service off, every
provider detached.

## Using your own ComfyUI

Astrid's local generation attaches to a ComfyUI you already run, and it
collects results by reading ComfyUI's output folder from disk. So the service
needs two things: ComfyUI's port, and that folder shared into the sandbox.

1. Allow folder shares once: `ASTRID_OPENSHELL_HOST_MOUNTS=1 scripts/openshell/install-openshell.sh`
   (this restarts the gateway).
2. Switch on "ComfyUI on this machine" in the menu, or with `services add comfyui`.
   It asks for the port, then finds the output folder the way Astrid's own scan
   does: `--output-directory`, otherwise `<ComfyUI>/output`. If that folder is a
   symlink, its real path is shared.
3. Run `astrid-openshell.sh rebuild DIR`. The shared folder is fixed when the
   sandbox is created, so the sandbox is recreated. Saved keys are re-attached;
   sign-ins inside the sandbox have to be redone.

The folder is shared read-only at the path ComfyUI reports, so Astrid's
`comfy_output_dir` checks pass inside the sandbox. Two cautions:

- **The API is the boundary.** Whatever a loaded custom node can do when a
  workflow is queued, the agent can do. Prefer a second ComfyUI instance for
  Astrid: same models, its own input/output/temp/user folders, no
  ComfyUI-Manager, and only the custom nodes you trust (`--disable-all-custom-nodes
  --whitelist-custom-nodes ...`).
- **The share relaxes isolation.** OpenShell calls host folder shares an "unsafe
  operator override". libkrun does not confine a compromised guest kernel to the
  shared folder: it may reach other files the gateway's user can read. Turn
  shares on only if you accept that.

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
- **Codex's own sandbox can't run inside OpenShell** (bubblewrap needs user
  namespaces, which OpenShell denies), so the setup sets
  `sandbox_mode = "danger-full-access"` in the sandbox's `~/.codex/config.toml`.
  The OpenShell VM is the boundary.
- **Missing hosts.** If a download fails, `astrid-openshell.sh logs | grep
  DENIED` names the blocked host and binary. Hugging Face's file CDN, for
  example, recently moved Xet-backed files to `us.aws.cdn.hf.co`.
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
