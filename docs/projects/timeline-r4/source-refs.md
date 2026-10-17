# Timeline R4 handover source refs

These refs were pushed normally, without force, on 2026-10-17. They are separate recovery branches. Do not infer that the commits are merged, qualified, or accepted.

| Repository | Remote | Branch | Full commit | Recorded role |
|---|---|---|---|---|
| Astrid | `https://github.com/peteromallet/Astrid-agent.git` | `codex/handover/timeline-r4-20261017` | `2b60097e3df642ff0985c3f6646c41672b2321ce` | Current rolling-package SDK source return; candidate-specific official task validation/readback and A admission remained pending. |
| Astrid | same | `codex/handover/timeline-r4-astrid-source-143-20261017` | `84b5b8848b9ecc199d0a0c684c20b8cfb2d41e71` | Separate source-state projection. |
| Astrid | same | `codex/handover/timeline-r4-a110-astrid-sdk-20261017` | `0d0ac5a173595ffe48edecd387e81eb88493c0ff` | Separate A110 source return; public acceptance criteria remain unmet. |
| App | `https://github.com/banodoco/reigh-app.git` | `codex/handover/timeline-r4-20261017` | `4b722961bc44bf51dda9aa0878c3307baca18ca9` | Current rolling-package App source return; still unqualified. |
| App | same | `codex/handover/timeline-r4-g-background-fix-20261017` | `aaf1f876e8e891cd840edec1ad57df8aba7d5c7d` | Separate background behavior correction; legacy/native parity decision remains open. |
| App | same | `codex/handover/timeline-r4-a110-app-20261017` | `b509be34e2c53be1f4e694c703e4ab5803e6ffc6` | Separate A110 App base. |
| Runtime | `https://github.com/banodoco/banodoco-workspace-runtime.git` | `codex/handover/timeline-r4-20261017` | `6bfe23c0c624a0ba7ebd2653628823ef5b4e4674` | Shared Runtime source pin. |
| Runtime | same | `codex/handover/timeline-r4-a110-runtime-20261017` | `4de15fe20c8c60029abd17c98bb09271a6942501` | Separate A110 private Runtime source return. |
| Timeline Composition | `https://github.com/peteromallet/timeline-composition.git` | `codex/handover/timeline-r4-20261017` | `5947abc0f5d6c0f5ca20bb478521e010e76be039` | Current T source pin; repository is a public fork of `AstridPseudon/timeline-composition`. |
| Timeline Composition | same | `codex/handover/timeline-r4-a110-native-hold-20261017` | `dfc14e43451979198e5821175edf2b4fdb8d9375` | Separate A110 T source return; original public/history/replay criteria remain unmet. |

The current admitted compatible-source join originally used SDK `8b4289dda77175e229d94b3a987dacd01cf976b4`, App `9207d111d527044d5a95f79d4f240c5c93466793`, T `5947abc0f5d6c0f5ca20bb478521e010e76be039`, and shared Runtime `6bfe23c0c624a0ba7ebd2653628823ef5b4e4674`. Its admission is source-join evidence, not product acceptance. The newer SDK/App source returns above are also unqualified. Consult the private handover packet for full provenance, genuine failures, pending work, and additional branch/recovery bundles.

Verify a remote ref with:

```sh
git ls-remote --heads <remote-url> <branch>
```
