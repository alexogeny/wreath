#!/usr/bin/env bash
# Build the pinned QUIC/TLS stack auditwheel will bundle into wreath-http3.

set -euo pipefail

prefix=${WREATH_HTTP3_PREFIX:-/opt/wreath-http3}
work=${WREATH_HTTP3_BUILD:-/opt/wreath-http3-build}
license_dir=${WREATH_HTTP3_LICENSE_DIR:-}
jobs=$(getconf _NPROCESSORS_ONLN)

nghttp3_version=1.8.0
ngtcp2_version=1.25.0

mkdir -p "$prefix" "$work"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
WREATH_OPENSSL_PREFIX="$prefix" \
WREATH_OPENSSL_BUILD="$work" \
WREATH_OPENSSL_LICENSE_DIR="$license_dir" \
  bash "$script_dir/build_openssl_wheel_dep.sh"

cd "$work"
curl --fail --location --retry 4 --output "nghttp3-$nghttp3_version.tar.xz" \
  "https://github.com/ngtcp2/nghttp3/releases/download/v$nghttp3_version/nghttp3-$nghttp3_version.tar.xz"
tar xf "nghttp3-$nghttp3_version.tar.xz"
cd "nghttp3-$nghttp3_version"
./configure --prefix="$prefix" --enable-lib-only
make -j"$jobs"
make install

cd "$work"
curl --fail --location --retry 4 --output "ngtcp2-$ngtcp2_version.tar.xz" \
  "https://github.com/ngtcp2/ngtcp2/releases/download/v$ngtcp2_version/ngtcp2-$ngtcp2_version.tar.xz"
tar xf "ngtcp2-$ngtcp2_version.tar.xz"
cd "ngtcp2-$ngtcp2_version"
PKG_CONFIG_PATH="$prefix/lib/pkgconfig" \
  ./configure --prefix="$prefix" --enable-lib-only --with-openssl
make -j"$jobs"
make install

PKG_CONFIG_PATH="$prefix/lib/pkgconfig" pkg-config --exists \
  libngtcp2 libngtcp2_crypto_ossl libnghttp3

if [[ -n "$license_dir" ]]; then
  mkdir -p "$license_dir"
  cp "$work/nghttp3-$nghttp3_version/COPYING" "$license_dir/NGHTTP3-MIT.txt"
  cp "$work/ngtcp2-$ngtcp2_version/COPYING" "$license_dir/NGTCP2-MIT.txt"
fi
