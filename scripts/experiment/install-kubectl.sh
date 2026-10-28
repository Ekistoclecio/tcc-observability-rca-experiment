#!/usr/bin/env bash
set -euo pipefail

version="v1.35.8"
case "$(uname -m)" in
  x86_64) arch=amd64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo "unsupported kubectl host architecture: $(uname -m)" >&2; exit 2 ;;
esac

tool_dir="${KUBECTL_TOOL_DIR:-${TMPDIR:-/tmp}/tcc-tools/kubectl-${version}}"
mkdir -p "${tool_dir}"
url="https://dl.k8s.io/release/${version}/bin/linux/${arch}/kubectl"
curl -fsSL "${url}" -o "${tool_dir}/kubectl"
curl -fsSL "${url}.sha256" -o "${tool_dir}/kubectl.sha256"
printf '%s  %s\n' "$(cat "${tool_dir}/kubectl.sha256")" "${tool_dir}/kubectl" | sha256sum --check
chmod 0755 "${tool_dir}/kubectl"
printf '%s\n' "${tool_dir}"
