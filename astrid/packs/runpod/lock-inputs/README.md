# Linux lock reproduction

The installed source and both platform recipes now pin
`6bfaacc67c2103481e5f0c84d75257cd0581d86a`, dated
2026-09-19T18:44:02-04:00 (2026-09-19 UTC). In a scratch clone of
https://github.com/Comfy-Org/ComfyUI.git, these searches each identify it as
the first introduction after the previous pin:

```sh
git log --ancestry-path --reverse -S QwenImage21Cache ee71d5c4993f29086b27fde1629a945ae48425bf..master -- comfy_extras/nodes_qwen.py
git log --ancestry-path --reverse -S TextEncodeQwenImage21 ee71d5c4993f29086b27fde1629a945ae48425bf..master -- comfy_extras/nodes_qwen.py
```

Its parent `3c80da7f87ee359b2d06f107cb3c0797079dfbbb` has neither name.
The previous pin is an ancestor. `comfyui_version.py` still declares
`0.36.0`, satisfying the unchanged H3 version requirement.

The original lock pinned newer requirements (frontend 1.57.4, templates
0.11.100, docs 0.5.15) than installed source `ee71d5c4` declares
(1.52.7, 0.11.62, 0.5.11). As established by the manager's ruling, no
master commit matches that original lock exactly: it was compiled against
a different source than the one installed. This re-lock makes the source
and lock agree.

## Inputs

Reconstructed from the original lock's direct `# via -r` annotations:

- `requirements.in`: hatchling 1.33.0 and the three unchanged final wheel URLs.
- `pins-source-build-policy.in`: argbind 0.3.9, openai-whisper 20250625,
  randomname 0.2.1 and setuptools 84.0.0, retaining the original exact
  artifact URLs and hashes. This file is both a resolver input and a build
  constraint.

`requirements.in` additionally retains `opencv-python-headless==5.0.0.93`.
The previous lock annotated it as coming from `requirements.source.txt`,
which is not the pinned upstream source. The existing engine recipe test
and environment probe require it; making it explicit preserves that
contract when replacing the old snapshot with real upstream requirements.

Use clean scratch checkouts at the following immutable revisions. Fetch
from the repositories in `../pins.json`, then `git checkout --detach` each
revision. The local dependency checkout was verified clean at its pin.

| Input path | Revision |
| --- | --- |
| `/tmp/c-comfy-sources/comfyui/requirements.txt` | 6bfaacc67c2103481e5f0c84d75257cd0581d86a |
| `/home/user/wt/c-dl-vc2/pyproject.toml` | 88cff6d5f19795f54f8fcf87bb47e93fc0534cbc |
| `/tmp/c-comfy-sources/videohelpersuite/requirements.txt` | 4ee72c065db22c9d96c2427954dc69e7b908444b |
| `/tmp/c-comfy-sources/comfyui-hunyuanvideo-foley/requirements.txt` | b94f7aca877ff2be5bc264b057abee3dac682c1b |
| `/tmp/c-comfy-sources/comfyui-qwentts/requirements.txt` | d8122a8ba835b65fd65c113d2b273b1ad1579293 |

## Executed command

Run from the repository root using uv 0.11.32. The existing output lock
supplies uv's version preferences for unaffected packages. The scratch
cache override is necessary because this machine's default cache is read-only.
No cutoff change was needed. One initial invocation failed to create the
cache; the first successful resolution showed the missing retained OpenCV
input, which was restored before the final successful command below.

```sh
mkdir -p .tmp-tests
TMPDIR=$PWD/.tmp-tests UV_CACHE_DIR=/tmp/comfy-bump-uv-cache \
uv --no-config pip compile \
  --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
  --no-sources --generate-hashes --only-binary :all: \
  --no-binary openai-whisper --no-binary argbind --no-binary randomname \
  --build-constraints astrid/packs/runpod/lock-inputs/pins-source-build-policy.in \
  --exclude-newer 2026-10-31T00:00:00Z \
  -o astrid/packs/runpod/requirements.linux-cu130.lock \
  /tmp/c-comfy-sources/comfyui/requirements.txt \
  /home/user/wt/c-dl-vc2/pyproject.toml \
  /tmp/c-comfy-sources/videohelpersuite/requirements.txt \
  /tmp/c-comfy-sources/comfyui-hunyuanvideo-foley/requirements.txt \
  /tmp/c-comfy-sources/comfyui-qwentts/requirements.txt \
  astrid/packs/runpod/lock-inputs/requirements.in \
  astrid/packs/runpod/lock-inputs/pins-source-build-policy.in
```

Refresh the packaged lock digest in `recipe.linux-cu130.json`, then run
`python scripts/vibecomfy_pin.py --source comfyui <revision>` to refresh
pin-manifest and recipe digests. The legacy default interface is preserved.

## Resolved package changes

| Package | Before | After |
| --- | --- | --- |
| comfy-aimdo | 0.6.0 | 0.5.5 |
| comfy-kitchen | 0.2.40 | 0.2.35 |
| comfyui-embedded-docs | 0.5.15 | 0.5.11 |
| comfyui-frontend-package | 1.57.4 | 1.53.6 |
| comfyui-workflow-templates | 0.11.100 | 0.11.62 |
| comfyui-workflow-templates-core | 0.3.391 | 0.3.350 |
| comfyui-workflow-templates-json | 0.1.126 | 0.1.85 |
| comfyui-workflow-templates-media-assets-01 | 0.1.48 | 0.1.46 |
| comfyui-workflow-templates-media-assets-02 | 0.1.26 | removed |

All other resolved packages and the three final wheel URLs/hashes are
unchanged. The lock contains 183 distributions (previously 184).

## Windows

The Windows recipe uses `resolve-once` with `packaged_lock: null`; there is
no packaged Windows lock to regenerate. Its source revision is updated to
the same commit. Explicit kitchen/aimdo constraints move from 0.2.40/0.6.0
to 0.2.35/0.5.5 in both inputs and recipe. On the next resolution, the source
requirements select frontend 1.53.6, templates 0.11.62, and docs 0.5.11,
with the corresponding template dependencies listed above. Other unconstrained
Windows transitive selections remain dependent on the resolver and index;
no Windows execution or complete Windows resolution is claimed here.
