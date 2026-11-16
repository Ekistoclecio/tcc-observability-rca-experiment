#!/usr/bin/env python3
"""Read the config digest from a Docker image-save archive on stdin."""

from __future__ import annotations

import hashlib
import json
import sys
import tarfile
from typing import Any, BinaryIO


def read_image_archive(stream: BinaryIO) -> dict[str, Any]:
    small_files: dict[str, bytes] = {}
    compatibility_manifest: list[dict[str, Any]] | None = None
    oci_index: dict[str, Any] | None = None

    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            if not member.isfile():
                continue
            if member.size > 1024 * 1024:
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            content = extracted.read()
            if member.name == "manifest.json":
                compatibility_manifest = json.loads(content)
            elif member.name == "index.json":
                oci_index = json.loads(content)
            else:
                small_files[member.name] = content

    if not compatibility_manifest or not oci_index:
        raise ValueError("Docker save archive is missing manifest.json or index.json")
    config_path = compatibility_manifest[0].get("Config")
    config_bytes = small_files.get(config_path)
    if not config_path or config_bytes is None:
        raise ValueError("Docker save archive does not contain the image config blob")

    config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    expected_config_digest = "sha256:" + config_path.rsplit("/", 1)[-1].removesuffix(".json")
    if config_digest != expected_config_digest:
        raise ValueError("image config bytes do not match the config digest in manifest.json")

    manifests = oci_index.get("manifests") or []
    if not manifests:
        raise ValueError("OCI index does not reference an image manifest")
    return {
        "config_digest": config_digest,
        "index_manifest_digest": manifests[0].get("digest"),
    }


def main() -> int:
    try:
        print(json.dumps(read_image_archive(sys.stdin.buffer), sort_keys=True))
        return 0
    except (OSError, tarfile.TarError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
