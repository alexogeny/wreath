#!/usr/bin/env bash
# Build the pinned OpenSSL shared libraries that auditwheel will bundle.

set -euo pipefail

prefix=${WREATH_OPENSSL_PREFIX:-/opt/wreath-openssl}
work=${WREATH_OPENSSL_BUILD:-/opt/wreath-openssl-build}
license_dir=${WREATH_OPENSSL_LICENSE_DIR:-}
jobs=$(getconf _NPROCESSORS_ONLN)
version=3.5.2

mkdir -p "$prefix" "$work"
cd "$work"
curl --fail --location --retry 4 --output "openssl-$version.tar.gz" \
  "https://github.com/openssl/openssl/releases/download/openssl-$version/openssl-$version.tar.gz"
tar xf "openssl-$version.tar.gz"
cd "openssl-$version"
./Configure --prefix="$prefix" --libdir=lib shared no-apps no-docs no-tests
make -j"$jobs"
make install_sw

if [[ -n "$license_dir" ]]; then
  mkdir -p "$license_dir"
  cp LICENSE.txt "$license_dir/OPENSSL-APACHE-2.0.txt"
fi
