#!/bin/sh
# Intel macOS diagnostic only. Usage: sh native-crypto-probe.sh /absolute/new/scratch-dir
set -eu
[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = x86_64 ] || { echo 'Requires native Darwin x86_64' >&2; exit 2; }
[ "$#" = 1 ] || { echo 'Pass one new absolute scratch path' >&2; exit 2; }
case "$1" in /*) ;; *) echo 'Path must be absolute' >&2; exit 2;; esac
[ ! -e "$1" ] || { echo 'Scratch path already exists' >&2; exit 2; }
mkdir -p "$1"
D=$(cd "$1" && pwd -P)
mkdir -p "$D/home" "$D/tmp" "$D/wheels" "$D/cache"
export HOME="$D/home" TMPDIR="$D/tmp" UV_CACHE_DIR="$D/cache" UV_PYTHON_INSTALL_DIR="$D/python"
export CARGO_HOME="$D/cargo" MACOSX_DEPLOYMENT_TARGET=15.0
unset RUSTUP_HOME RUSTUP_TOOLCHAIN PYTHONPATH PYTHONHOME UV_INDEX_URL UV_EXTRA_INDEX_URL UV_DEFAULT_INDEX PKG_CONFIG_PATH
fetch() {
    curl --fail --silent --show-error --location "$1" -o "$2"
    printf '%s  %s\n' "$3" "$2" | shasum -a 256 -c -
}
echo '=== Host ==='
date -u; sw_vers; uname -a; xcode-select -p; xcrun clang --version; perl -v | head -2
fetch https://files.pythonhosted.org/packages/12/85/ef4d0f02f469cfad1d73c11bf31ac0a8f24da3c62d7f3305c50a5e51e814/uv-0.11.32-py3-none-macosx_10_12_x86_64.whl "$D/uv.whl" c82954b1bd507e767b1d996c2865ea3eb7d464a479ec0e4d60f0b8e2e07dee45
unzip -q "$D/uv.whl" 'uv-0.11.32.data/scripts/uv' -d "$D"
UV="$D/uv-0.11.32.data/scripts/uv"
chmod u+x "$UV"
"$UV" --version
"$UV" -v python install 3.11.13 --no-bin
"$UV" venv --python 3.11.13 "$D/venv"
PY="$D/venv/bin/python"
"$PY" -VV
"$PY" -c 'import platform,sys,sysconfig; print(sys.executable, platform.platform(), platform.machine(), sysconfig.get_platform())'
shasum -a 256 "$("$UV" python find 3.11.13)"
fetch https://static.rust-lang.org/dist/2025-09-18/rust-1.90.0-x86_64-apple-darwin.tar.xz "$D/rust.tar.xz" da1ddcec07101c533a4521f294b9da6f439b861f6eb2b7064c817128529ab84b
tar -xJf "$D/rust.tar.xz" -C "$D"
"$D/rust-1.90.0-x86_64-apple-darwin/install.sh" --prefix="$D/rust" --disable-ldconfig
export PATH="$D/rust/bin:/usr/bin:/bin:/usr/sbin:/sbin"
rustc -Vv; cargo -Vv; command -v cargo
fetch https://www.openssl.org/source/openssl-3.5.4.tar.gz "$D/openssl.tar.gz" 967311f84955316969bdb1d8d4b983718ef42338639c621ec4c34fddef355e99
tar -xzf "$D/openssl.tar.gz" -C "$D"
(cd "$D/openssl-3.5.4" && ./Configure darwin64-x86_64-cc no-shared --prefix="$D/openssl" --openssldir="$D/openssl/ssl" && make -j2 && make install_sw)
export OPENSSL_DIR="$D/openssl" OPENSSL_STATIC=1
"$D/openssl/bin/openssl" version -a
fetch https://files.pythonhosted.org/packages/43/b6/79c881410a3b1c187f7eb3d407aecae646c6a4433d630d72200359015e83/maturin-1.14.1-py3-none-macosx_10_12_x86_64.whl "$D/wheels/maturin-1.14.1-py3-none-macosx_10_12_x86_64.whl" f3306078070c1508fd715b9116070cbcaff5959024272a9f1e6f5cb29768b86c
fetch https://files.pythonhosted.org/packages/70/d2/16d99a0c4948febc0ebd133a13b2f688ff7f8cb04da971e1128872ce0c03/cffi-2.1.1-cp311-cp311-macosx_10_15_x86_64.whl "$D/wheels/cffi-2.1.1-cp311-cp311-macosx_10_15_x86_64.whl" c8d2c9fd1f2d16f780d15127abb050d13d1a76c03a4bd87d7e4980e45e511e12
fetch https://files.pythonhosted.org/packages/0c/c3/44f3fbbfa403ea2a7c779186dc20772604442dde72947e7d01069cbe98e3/pycparser-3.0-py3-none-any.whl "$D/wheels/pycparser-3.0-py3-none-any.whl" b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992
fetch https://files.pythonhosted.org/packages/a3/dc/17031897dae0efacfea57dfd3a82fdd2a2aeb58e0ff71b77b87e44edc772/setuptools-80.9.0-py3-none-any.whl "$D/wheels/setuptools-80.9.0-py3-none-any.whl" 062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922
fetch https://files.pythonhosted.org/packages/de/41/6cbdcf9142d00fe82836fbb51e503e58088575cf7a0fe1dbff6695bf0840/cryptography-50.0.0.tar.gz "$D/wheels/cryptography-50.0.0.tar.gz" eeac2acb5a20ed25e0ad6d1df9891a520b78b404266b6d11778f25d5d691a6c9
cat > "$D/build-constraints.txt" <<'CONSTRAINTS'
maturin==1.14.1
cffi==2.1.1
pycparser==3.0
setuptools==80.9.0
CONSTRAINTS
cat > "$D/cffi.txt" <<'REQUIREMENTS'
cffi==2.1.1 --hash=sha256:c8d2c9fd1f2d16f780d15127abb050d13d1a76c03a4bd87d7e4980e45e511e12
pycparser==3.0 --hash=sha256:b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992
REQUIREMENTS
printf '%s\n' 'cryptography==50.0.0 --hash=sha256:eeac2acb5a20ed25e0ad6d1df9891a520b78b404266b6d11778f25d5d691a6c9' > "$D/crypto.txt"
export UV_NO_INDEX=1 UV_FIND_LINKS="$D/wheels" UV_BUILD_CONSTRAINT="$D/build-constraints.txt"
"$UV" pip install --python "$PY" --require-hashes --no-deps -r "$D/cffi.txt"
"$UV" -v pip install --python "$PY" --require-hashes --no-deps -r "$D/crypto.txt"
"$PY" -c 'import cryptography; from cryptography.hazmat.backends.openssl.backend import backend; print("cryptography",cryptography.__version__,"OpenSSL",backend.openssl_version_text())'
echo 'Native cryptography probe passed; this is not an R0 lifecycle pass.'
