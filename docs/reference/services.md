# Agent services

The agent declares two services: `pack-host` and `chat`. The Runtime supervises
both generically through the version 1 `contract/schemas/services.json`
contract. The agent supplies structured argv, environment, restart policy,
and service-owned readiness files. The local source installer writes the
declaration and sets the source profile's `service_declaration` to its path.
For another configured source installation, generate the declaration with its
installed Python and Bun, then set that same profile field:

```sh
python -m astrid.services declare --source-checkout /absolute/agent \
  --data-root /absolute/workspace --bun /absolute/bun > /absolute/services.json
```

When moving an existing app bridge, retain its `ASTRID_ACP_CWD`, profile,
command, and optional session directory when generating the declaration.
`--chat-cwd` can set the previous cwd explicitly; cwd participates in OMP's
session store identity. New installations default to `<selected data root>/chat`.
The installer retains explicit cwd, profile and session-directory selections
across updates and kept-workspace reinstalls. When replacing an old managed
source-checkout cwd, it explicitly retains that cwd's original OMP session
directory without moving or resetting the store. The default command remains `astrid`; the declaration prepends the selected
Python's scripts directory to PATH to resolve that installed launcher.

Release owners select the OMP producer with the single `tools.omp.source`
manifest field. It names either an exact npm package and version or a fork
repository and full commit. Real installs select the reviewed `peteromallet/oh-my-pi`
fork. The fallback for installs without a manifest source is
`@oh-my-pi/pi-coding-agent@18.12.6`.
This npm release has the session-folder listing and recovery gap described in the [tester guide](../testing/TESTING.md#known-gaps).
See the [OMP producer landing guide](../install/OMP-LANDING.md) for the owner
handoff, patch and pinning procedure. Astrid does not apply a downstream patch
to the selected producer during an ordinary install.

The pack-host entry is `python -m astrid.core.execution.generic_host --service
--source-checkout ... --support-root ...`. It reads the Runtime worker handoff
from discovery and uses the ordinary bootstrap's credential, source, render,
and boot-manifest checks. It enters the worker loop in the supervised process,
after releasing the bootstrap lock, and publishes the normal readiness and
process identity records. It never starts a detached host.

Chat runs with Bun 1.3.14: `bun astrid/chat/service.mjs`. The declaration selects
its stable cwd, installed `astrid` command, Python, and Runtime discovery file.
The service generates a private ACP credential, binds loopback, and atomically
publishes `chat.json` beside discovery only after binding succeeds. Shutdown
removes only its own registration. Windows file permissions use the Runtime's
shared platform module through the declared Python. The Runtime door forwards
authenticated requests with `X-Astrid-Bridge-Version: v1`. The service always
enables the local-media-only policy.

The TypeScript sources and tests live in `astrid/chat`. Adapter versions,
login-shell PATH discovery, one readiness state per agent, shared sessions,
project associations, model routes, and transcript custody moved from the app
without a chat feature change. Native path APIs accept Windows drive/UNC paths
on Windows, and executable discovery includes `.exe` names.

The committed `service.mjs` bundles Zod 4.3.6 and all JavaScript dependencies;
the wheel also includes its adjacent local-media policy YAML. This is simpler
than building at install time: the installed service needs only Bun and the
Python environment, with no npm install or compiler step. The root package
lock pins build inputs. With those dev dependencies available, run
`bun run build:chat`, then `bun run check:chat-bundle` to check reproducibility.
The bundle compiler is pinned to Bun 1.4.2 (the compiler available in this
workspace); the installed service runs on Bun 1.3.14. CI checks the committed
bundle with the compiler pin, then runs the unit and adapter tests with the
Runtime Bun pin.

Run `bun test ./astrid/chat` for the moved tests. The network-dependent adapter
smoke is `bun run astrid/chat/astrid-local-agent-adapters-smoke.ts`; it sends
ACP `initialize` to each pinned adapter through `bun x --bun`. App development
uses `npm run dev:local` and reads the service registration on every request.
The app launcher no longer owns a chat process or its credential.
