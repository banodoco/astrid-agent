#!/bin/bash
# Prepare the approved probe's native build inputs outside the driver's isolated HOME.
set -euo pipefail
[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = x86_64 ]
D="$RUNNER_TEMP/v03-intel-bootstrap"
[ ! -e "$D" ]
mkdir -p "$D/cargo"
fetch() {
  curl --fail --silent --show-error --location "$1" -o "$2"
  printf '%s  %s\n' "$3" "$2" | shasum -a 256 -c -
}
echo '=== runner ==='
date -u; sw_vers; uname -a; xcode-select -p; xcrun clang --version
printf 'RUN_ID=%s ATTEMPT=%s JOB=%s RUNNER_OS=%s RUNNER_ARCH=%s ImageOS=%s ImageVersion=%s\n' "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" "$GITHUB_JOB" "$RUNNER_OS" "$RUNNER_ARCH" "${ImageOS-}" "${ImageVersion-}"
fetch https://static.rust-lang.org/dist/2025-09-18/rust-1.90.0-x86_64-apple-darwin.tar.xz "$D/rust.tar.xz" da1ddcec07101c533a4521f294b9da6f439b861f6eb2b7064c817128529ab84b
tar -xJf "$D/rust.tar.xz" -C "$D"
"$D/rust-1.90.0-x86_64-apple-darwin/install.sh" --prefix="$D/rust" --disable-ldconfig
fetch https://www.openssl.org/source/openssl-3.5.4.tar.gz "$D/openssl.tar.gz" 967311f84955316969bdb1d8d4b983718ef42338639c621ec4c34fddef355e99
tar -xzf "$D/openssl.tar.gz" -C "$D"
(cd "$D/openssl-3.5.4" && ./Configure darwin64-x86_64-cc no-shared --prefix="$D/openssl" --openssldir="$D/openssl/ssl" && make -j2 && make install_sw)
cat > "$D/build-constraints.txt" <<'CONSTRAINTS'
maturin==1.14.1
cffi==2.1.1
pycparser==3.0
setuptools==80.9.0
CONSTRAINTS
"$D/rust/bin/rustc" -Vv
"$D/rust/bin/cargo" -Vv
"$D/openssl/bin/openssl" version -a
shasum -a 256 "$D/rust/bin/rustc" "$D/rust/bin/cargo" "$D/openssl/bin/openssl" "$D/openssl/lib/libssl.a" "$D/openssl/lib/libcrypto.a" "$D/build-constraints.txt"
printf '%s\n' "$D/rust/bin" >> "$GITHUB_PATH"
{
  printf 'CARGO_HOME=%s\n' "$D/cargo"
  printf 'OPENSSL_DIR=%s\n' "$D/openssl"
  printf 'OPENSSL_STATIC=1\n'
  printf 'UV_BUILD_CONSTRAINT=%s\n' "$D/build-constraints.txt"
} >> "$GITHUB_ENV"
