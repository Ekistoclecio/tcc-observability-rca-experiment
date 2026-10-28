#!/usr/bin/env bash
set -euo pipefail

version="v0.33.0"
case "$(uname -m)" in
  x86_64) arch=amd64 ;;
  *) echo "this pinned Kind installer currently supports x86_64 hosts; found $(uname -m)" >&2; exit 2 ;;
esac

tool_dir="${KIND_TOOL_DIR:-${TMPDIR:-/tmp}/tcc-tools/kind-${version}}"
mkdir -p "${tool_dir}"
curl -fsSL "https://kind.sigs.k8s.io/dl/${version}/kind-linux-${arch}" -o "${tool_dir}/kind"
printf '%s  %s\n' \
  aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d \
  "${tool_dir}/kind" | sha256sum --check >&2
chmod 0755 "${tool_dir}/kind"
actual="$("${tool_dir}/kind" version | sed -n '1s/^kind \(v[^ ]*\).*/\1/p')"
if [[ "${actual}" != "${version}" ]]; then
  echo "Expected Kind ${version}, found ${actual:-unknown}." >&2
  exit 3
fi
printf '%s\n' "${tool_dir}/kind"
