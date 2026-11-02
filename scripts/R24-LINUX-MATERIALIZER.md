# R-24 Linux runner diagnostic materializer

`scripts/r24-linux-materialize.mjs` is a standalone Linux runner hook for a
closed M4-4a **plan**, not an executor config, dispatch input, trusted receipt,
or qualification result. It calls only `createLinuxProviderSurface()` and
`prepareM4Harness('M4-4a')`; it does not call the native engine or run a
journey. It has no connection to `journey.yml` or to
`m4_4a_executor_configs.linux`. The workflow's initial input check accepts
arbitrary objects, but the published backend executor rejects both the plan and
its diagnostic wrapper because neither supplies `candidate_checkouts` and the
other required executor bindings. Never place either object in that input.

On a Linux runner with Node.js 18 or newer and Git, independently check out
clean, distinct, non-nested roots at the pinned commits. The provider checkout
is separate from the published Pinokio candidate. Make an existing output
directory outside every checkout, and invoke:

```sh
node agent/scripts/r24-linux-materialize.mjs /absolute/path/request.json
```

The request is one JSON object with exactly these fields; all paths must be
absolute real paths to checkout top-levels or the existing output directory:

```json
{
  "providerCheckout": "/runner/provider",
  "checkouts": {
    "app": "/runner/app",
    "agent": "/runner/candidate-agent",
    "runtime": "/runner/runtime",
    "pinokio": "/runner/pinokio"
  },
  "harness": "M4-4a",
  "outputDirectory": "/runner/diagnostic-output"
}
```

Provider repository is `banodoco/astrid-pinokio`, candidate lane
`lane/M-PIN/R-24-R5`, immutable commit
`603298861a359a04642143aea11e60dbd030505e`. Candidate commits are App
`5c95abb00525e1a77d00c32042b6453d9f14f4e7`, Agent
`e425903f7a9fa5b82973281d15054c0716b46e2d`, Runtime
`5453366458f7b4ee874c94d15ae7952e3e82d3c3`, and Pinokio
`2f085c5fc0cde192ac4a022065883c85dcccd0de`. The hook checks clean HEADs and
SHA-256 pins for the provider facade and its five imported modules before
loading it. It checks the provider interface and the exact M4-4a plan, including
all dependency reasons. It does not authenticate the runner, provide a
protected collector, or bind candidate working bytes as trusted evidence.

On success stdout contains the result path, and the hook creates exclusively
`<outputDirectory>/r24-linux-m4-4a-diagnostic-plan.json`. It refuses overwrite.
The JSON has exactly `kind`, `version`, `qualification`, `state`,
`executorConfig`, `provider`, `candidate`, and `plan`. `kind` is
`r24-linux-runner-diagnostic-plan`, `version` is 1, `qualification` is
`closed`, `state` is `not-run`, and `executorConfig` is false. `provider`
contains `repository`, `ref`, `checkout`, `sourceSha256`; `candidate` contains
`tuple`, `checkouts`; `plan` is the unchanged provider object with `kind`,
`interface`, `interfaceSha256`, `harness`, `title`, `state`, `qualification`,
`await`, `externalAwait`, `producers`. Both await arrays preserve R-21 and
R-36, including their exact provider reasons. Any rejected input, source or
result exits nonzero without creating a new result. The plan's interface digest
is an interface identifier, not a verifier receipt.

A canonical backend dispatch still requires independently trusted R-36
collector and verifier, approved profiles, immutable bindings and provenance,
plus the approved R-21 old-launcher stopped-path integration. The executor also
requires complete owner-bound platform, candidate file, launcher, action,
observation, adapter and negative-control inputs, and the three-OS config
contract; a diagnostic plan supplies none of them. Provider source is a lane
candidate and is not the published Pinokio integration SHA. Do not dispatch a
protected journey on the strength of this hook.
