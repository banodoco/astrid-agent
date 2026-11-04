# Linux ComfyUI requirements lock provenance

P-44-r5 resolved the retained requirements snapshot from ComfyUI commit
`423cbd20511becb1f16122ab518e92e58bfa03d9`. The exact source snapshot below
is 529 bytes with SHA-256 `5dd8c78436644e0980c5696605d122b4f4b0db51fc459a2cd41ce32c73047530`.
It used uv 0.11.32, Python 3.12.10, the Linux x86_64 manylinux 2.28 target,
and cutoff `2026-10-31T00:00:00Z`. The additional OpenCV input satisfies
the existing environment probe; the three direct wheels match the recipe.

`requirements.source.txt`:

```text
comfyui-frontend-package==1.57.4
comfyui-workflow-templates==0.11.100
comfyui-embedded-docs==0.5.15
torch
torchsde
torchvision
numpy>=1.25.0
einops
transformers>=4.50.3
tokenizers>=0.13.3
sentencepiece
safetensors>=0.4.2
aiohttp>=3.11.8
yarl>=1.18.0
pyyaml
Pillow>=10.1.0
scipy
tqdm
psutil
alembic
SQLAlchemy>=2.0.0
filelock
av>=17.0.0
comfy-aimdo==0.6.0
comfy-kitchen==0.2.40
requests
simpleeval>=1.0.0
blake3

#non essential dependencies:
kornia>=0.7.1
spandrel
pydantic~=2.0
pydantic-settings~=2.0
PyOpenGL>=3.1.8
comfy-angle
```

`requirements.resolution.in`:

```text
-r requirements.source.txt
opencv-python-headless==5.0.0.93
torch @ https://download.pytorch.org/whl/cu130/torch-2.10.0%2Bcu130-cp312-cp312-manylinux_2_28_x86_64.whl#sha256=858f0cbcc78d726fea9499eb3464faa98392fa093845a3262209bd226b7844d6
torchvision @ https://download.pytorch.org/whl/cu130/torchvision-0.25.0%2Bcu130-cp312-cp312-manylinux_2_28_x86_64.whl#sha256=c2f5e38f0cd57a2796e4503c0f13365deba01dbc167ef820f0beec7ca96f5f2e
torchaudio @ https://download.pytorch.org/whl/cu130/torchaudio-2.10.0%2Bcu130-cp312-cp312-manylinux_2_28_x86_64.whl#sha256=ed573e7319ba9a6e137b4895eddf4c9fcd424bb4e10a629267d64b55acc57fbb
```

Resolver invocation (its output path is the original job path):

```sh
uv pip compile --no-config --system-certs --python-version 3.12 --python-platform x86_64-manylinux_2_28 --no-sources --generate-hashes --only-binary :all: --exclude-newer 2026-10-31T00:00:00Z requirements.resolution.in -o /workspace/jobs/P-44-comfyui-lock-closure-r5/out/requirements.lock
```

The lock is the P-44-r5 resolver output. The selected wheelhouse was hashed
in full and checked with an offline `pip install --dry-run --require-hashes`;
the wheelhouse itself was not retained in the delivered evidence. Regeneration
may require fetching large CUDA archives and can depend on index availability.
