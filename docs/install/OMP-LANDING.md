# Land and select the fixed OMP producer

Real Astrid installs use the reviewed `peteromallet/oh-my-pi` fork selected by
`tools.omp.source` in the published install manifest. The local installer default
in `astrid/installer/tools.py` stays `@oh-my-pi/pi-coding-agent@18.12.6` for installs
without a manifest source. That npm fallback lacks the fork's session-store fix,
`omp onboard`, and per-instance identities; it is not the production selection.

The full fork is rebased onto exactly upstream `v18.12.6`
(`45c35e40c46166dc726ccc52f5d27f40f2449f99`). It retains the provider wizard,
`OMP_AGENT_IDENTITY`, JSON faces and `omp identity`, source installation, disabled
self-update, agent launch/background controls, branding, and explicit ACP stores
and legacy history. `astrid --onboard` calls the fork's `omp onboard`;
`astrid --onboard --help` reaches its help without a terminal.

The delivery artifact is `omp-fork-18.12.6.bundle`, advertising
`refs/heads/astrid/18.12.6`. Release owners must push the exact bundled branch to
`peteromallet/oh-my-pi` and verify GitHub serves its full SHA before deploying the
manifest that selects it. This also applies to the selected Agent and Runtime
commits. Astrid never patches the selected producer during install.

## Push the bundled commit

In a clean clone containing upstream `v18.12.6`, inspect and fetch the bundle:

```sh
git bundle verify /path/to/omp-fork-18.12.6.bundle
git bundle list-heads /path/to/omp-fork-18.12.6.bundle
git fetch /path/to/omp-fork-18.12.6.bundle refs/heads/astrid/18.12.6:refs/heads/astrid/18.12.6
git show -s --format='%H%n%T%n%P' astrid/18.12.6
git diff v18.12.6 astrid/18.12.6 -- packages/coding-agent
```

Verify the advertised SHA matches the fetched head, `v18.12.6` is its ancestor,
and the intervening commits preserve the fork's changes. This is the whole fork,
not a single session-store patch. Then push the branch unchanged:

```sh
git push origin astrid/18.12.6:refs/heads/astrid/18.12.6
```

Verify GitHub serves that full SHA before selecting or deploying it. Do not
cherry-pick or amend the delivered commit: the manifest and bundle must resolve
to the same commit ID.

## Select the landed producer

The qualified local fork head is `78f0e0960f471c2c62b943272a48c33366f239fe`. The published manifest's OMP entry
must select exactly this source (the app manifest lives in the separate app
repository and is not edited in this agent worktree):

```json
{
  "tools": {
    "omp": {
      "source": {
        "repo": "https://github.com/peteromallet/oh-my-pi",
        "sha": "78f0e0960f471c2c62b943272a48c33366f239fe"
      }
    }
  }
}
```

`tools.omp.source` is the only authoritative OMP source field. Set it to an object
with `repo: "https://github.com/peteromallet/oh-my-pi"` and `sha` equal to the
full 40-character commit SHA printed above. Keep the app deployment gate closed
until GitHub serves that commit and the manifest selects it.

After deployment, run the update journey and check that conversations list and
reopen after a cold restart, including configured external stores and old OMP12
history. Remove the published-default known-gap note from
`docs/testing/TESTING.md` only after those checks pass. A saved model that no
longer resolves is rejected by 18.12.6; Astrid tells the user to start a new chat
and retains the saved transcript.

## Reproduce the focused native ACP tests

Use Bun **1.3.14**. The frozen lock SHA256 is
`6e330e18ca780dfccd6b6161818c204155f4c2cefce482bfead3e8315406a048`;
the native release is **18.12.6**, reported by `__piNativesBuildVersion()`.
Prepare a scratch HOME and `PI_CODING_AGENT_DIR` before running the CLI. In the
fetched checkout, install and generate the tool views:

```sh
bun install --frozen-lockfile
bun run gen:tool-views
sha256sum bun.lock
```

Install matching native leaves before real CLI tests. The Linux x64 archive is
pinned below; every supported target's URL, SHA256, SRI and filenames are in
`astrid/installer/omp/recipe.json` in the agent repository.

```sh
native_scratch="$(mktemp -d)"
curl -fsSL https://registry.npmjs.org/@oh-my-pi/pi-natives-linux-x64/-/pi-natives-linux-x64-18.12.6.tgz -o "$native_scratch/native.tgz"
printf '%s  %s\n' 16c79fdc9783c7eb08ffd48867771eae792f6c0377a5eb5fc5ebb5e84ac5e931 "$native_scratch/native.tgz" | sha256sum -c -
tar -xzf "$native_scratch/native.tgz" -C "$native_scratch"
mkdir -p packages/natives/native
cp "$native_scratch/package/pi_natives.linux-x64-baseline.node" "$native_scratch/package/pi_natives.linux-x64-modern.node" packages/natives/native/
rm -rf "$native_scratch"
bun test ./packages/coding-agent/test/acp-agent.test.ts ./packages/coding-agent/test/acp-session-dir-cli.test.ts
bun test ./packages/coding-agent/test/acp-*.test.ts
sha256sum bun.lock
```

Both lock checks must retain the hash above. The former 17.4.0 producer's
qualification does not qualify this release, GitHub availability, or a deployed
manifest. Native macOS and Windows require separate qualification.

To refresh the build recipe from a reviewed commit, run in the agent checkout:

```sh
python scripts/refresh_omp_recipe.py --producer-repo /path/to/oh-my-pi \
  --candidate astrid/18.12.6 --version 18.12.6 --bun-version 1.3.14 \
  --cache /path/to/native-archives
```

The script emits the installer's build closure only. It verifies native archives
against registry SRI and records their SHA256; it does not generate a downstream
patch or select the user's producer.

## Fork commit ledger

The 25 original commits are accounted for below. The adaptation commit
`a62a012830` connects the preserved behavior to 18.x's relocated TUI,
namespaced auth, and explicit-store cwd contract. Nothing else is squashed.

| Original | Change | Fate / rebased commit |
| --- | --- | --- |
| `ca1411b598` | dev: local omp instance changes + setup docs | Carried: `73be219ef5` |
| `f210fffb4a` | docs(omp-setup): fresh-machine setup guide (clone, natives build, config, creds, arnold) | Carried: `8b77f31a56` |
| `352f0bba06` | fix(agent): trailing message after flags implies --print; value flags consume their argument | Carried: `53838ebbdb` |
| `9713c0162c` | brand: arnold identity across CLI surface | Carried: `92186fa10d` |
| `60f5d6ff72` | chore(onboard-ui): megado artifacts — plan v2 settled, frozen tasklist | Artifacts squashed into `92186fa10d` |
| `4c6ddcadfa` | feat(onboard): omp onboard command — pure-data scan + non-TTY-guarded shell | Carried: `a5f194d0a2` |
| `5d73338d3f` | feat(onboard): wizard scenes — detect-pick + wire/verify on native components | Carried: `9931433d99` |
| `9724dfbb04` | style(onboard): beauty pass — copy sweep to stock wizard tone | Carried: `eb7e31d9f0` |
| `39f8ec4107` | fix(onboard): scan ready requires apiKey; final review evidence | Carried: `1ca8b0614c` |
| `9799a2da43` | Merge remote-tracking branch 'origin/main' | Merge flattened; its second parent `72000acfeb` is an ancestor of v18.12.6 |
| `4f868141f9` | style(onboard): hollow dot for candidates — green filled dot unambiguously marks ready | Carried: `3036f098e4` |
| `50d67f9422` | fix(theme): downgrade to 256-color on Apple Terminal | Dropped: upstream already covers Terminal.app via `packages/tui/src/theme/color.ts` and `packages/tui/test/theme-color-mode.test.ts` (3 passing tests) |
| `9fdf8849e0` | fix(onboard): env-ready requires the env var to actually be set | Carried: `a35fa4f9ec` |
| `fe32e48e34` | style(onboard): 'Choose your LLM provider' h1 + bolded existing-credentials subtitle | Carried: `6d2a5af5c5` |
| `5a3ab535ef` | feat(brand): per-agent ASCII identities — Arnold raised-fist mark in sunset oranges | Carried: `600ec2192e` |
| `1d90185871` | style(brand): restore centered composition of Arnold mark | Carried: `ea57d402da` |
| `fbb97551f1` | feat(brand): glowing eyes — per-cell color overrides for agent marks | Carried: `0be1ab57ba` |
| `05fb7e8f8a` | fix(brand): uniform-width logo grid — per-line centering was breaking composition | Carried: `42dffaf0fa` |
| `48b1a9d8ff` | feat(brand): per-instance identities — OMP_AGENT_IDENTITY env + JSON-defined faces | Carried: `8511d1638c` |
| `f4da3c7be4` | feat(brand): omp identity <name> — one-command per-instance identity scaffold | Carried: `d41ab70923` |
| `1a9cdc086a` | fix(update): disable npm update check in fork; add sync-upstream script | Carried: `7bba57d81b` |
| `e2c44e30b2` | feat(identity): one scaffold creates both halves — identity JSON + agent prompt stub | Carried: `a66070a87e` |
| `c73e346643` | fix(onboard): kill dead space in provider list — cap primary column, model first | Carried: `8ab63eb9bf` |
| `9962a46db1` | fix(install): install this fork from source, not upstream | Carried: `35b4e2aef4` |
| `9e282751c0` | fix(acp): preserve explicit session stores and legacy history | Carried: `3936e21ec1` |
