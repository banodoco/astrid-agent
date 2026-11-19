set -euo pipefail

case "$CELL" in
  reference)
    agent=ae929cdae7460c36cfbfd95599019a4c45f42e1c
    runtime=2537fed24385ec1c24f48a812e09af4bb63fb794 ;;
  candidate)
    agent=253456ce240c3d68a3b877faf75a9636d98eb5ee
    runtime=8e8711fd2b3b43fcae680cecd8cec1e7640489cc ;;
  *) exit 2 ;;
esac
root="$GITHUB_WORKSPACE"
code="$root/proof/scripts/ci/fix12_pair"
out="$RUNNER_TEMP/fix12-receipt"
mkdir "$out"
checkout() {
  local repo="$1" sha="$2" dir="$3"
  git init -q "$dir"
  git -C "$dir" remote add origin "https://github.com/$repo.git"
  git -C "$dir" fetch -q --depth=1 --no-tags origin "$sha"
  git -C "$dir" checkout -q --detach FETCH_HEAD
  test "$(git -C "$dir" rev-parse HEAD)" = "$sha"
  test -z "$(git -C "$dir" status --porcelain)"
}
checkout banodoco/astrid-agent "$agent" src
checkout banodoco/astrid-runtime "$runtime" astrid-runtime
checkout banodoco/astrid-pinokio d15c58b5cd1f7e24c1e699340b33961027f7c573 astrid-pinokio
checkout banodoco/VibeComfy c27fe76b79d917fd9c08246660bc77211be64146 vibecomfy-checkout
checkout banodoco/runpod-lifecycle 7f0b025e9391baac9f5c2fb7393617ea6fef0704 runpod-lifecycle-checkout
checkout Comfy-Org/ComfyUI 6bfaacc67c2103481e5f0c84d75257cd0581d86a comfyui-checkout
cd src
python "$code/verify.py" sources "$out"
test "$(python scripts/vibecomfy_pin.py)" = "$(git -C "$ASTRID_VIBECOMFY_CHECKOUT" rev-parse HEAD)"
sudo apt-get update
sudo apt-get install -y ffmpeg fonts-dejavu-core libnss3 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libgbm1 libasound2t64 libxrandr2 libxkbcommon0 libxfixes3 libxcomposite1 libxdamage1 libpango-1.0-0 libcairo2 libcups2
# Apply original resolved versions to the normal Lanes installation commands.
PIP_CONSTRAINT="$code/original.txt" python -m pip install -e '.[dev]'
python -m pip install --no-deps -e "$ASTRID_RUNTIME_CHECKOUT" -e "$ASTRID_RUNTIME_CHECKOUT/packages/python"
PIP_CONSTRAINT="$code/original.txt" python -m pip install -e "$ASTRID_VIBECOMFY_CHECKOUT"
npm ci --prefix remotion
(cd remotion && npx remotion browser ensure)
python "$code/verify.py" inventory "$out"
for dir in "$root/src" "$ASTRID_RUNTIME_CHECKOUT" "$ASTRID_PINOKIO_CHECKOUT" "$ASTRID_VIBECOMFY_CHECKOUT" "$ASTRID_TEST_LIFECYCLE_CHECKOUT" "$ASTRID_TEST_COMFY_SOURCE"; do
  test -z "$(git -C "$dir" status --porcelain)"
done
