#!/usr/bin/env python3
"""Build a checksum-guarded 32-bit/192 kHz Waydroid audio overlay."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


FILES = {
    "lib/hw/audio.primary.waydroid.so": {
        "source_sha256": "cac012cfc7d8b1852635afdf34d160f7b9524e927fba3f17c9424b1b2b639cd2",
        "patched_sha256": "8dfce910cf5ab8d9c09d41931929f9c44d5d7dbfc39209bea43dc485c97aa181",
        "edits": {
            0x0894: ("80bb0000", "00ee0200"),
            0x1D4C: ("02000000", "0a000000"),
            0x1D59: ("80bb0000", "00ee0200"),
            0x1DAA: ("80bb0000", "00ee0200"),
            0x1DB1: ("01000000", "03000000"),
            0x1DCE: ("80bb0000", "00ee0200"),
            0x1DD3: ("02", "0a"),
        },
    },
    "lib64/hw/audio.primary.waydroid.so": {
        "source_sha256": "f44860755c9674289322cf1394477a1c840be312dc795a85b932ae98d487072d",
        "patched_sha256": "1359f72b9a54a364f334f12e1c1851c78743a0865ec506cf2bfa9a1e2ab3e740",
        "edits": {
            0x0D84: ("80bb0000", "00ee0200"),
            0x2185: ("02000000", "0a000000"),
            0x219A: ("80bb0000", "00ee0200"),
            0x21ED: ("80bb0000", "00ee0200"),
            0x21FC: ("01000000", "03000000"),
            0x221A: ("80bb0000", "00ee0200"),
            0x2220: ("02000000", "0a000000"),
        },
    },
}

POLICY_PATH = "etc/audio_policy_configuration.xml"
POLICY_SOURCE_SHA256 = (
    "b649d92f3fd2d3a08630da296d5c9ddac39d6978780ff268446623f959585c7b"
)
POLICY_PATCHED_SHA256 = (
    "10c5585054a1dd3bc0aa622947f63639b9a3eff68b58a0934d58a5102fd3defa"
)
POLICY_OLD = (
    b'format="AUDIO_FORMAT_PCM_16_BIT"\n'
    b'                             samplingRates="48000" '
    b'channelMasks="AUDIO_CHANNEL_OUT_STEREO"'
)
POLICY_NEW = (
    b'format="AUDIO_FORMAT_PCM_32_BIT"\n'
    b'                             samplingRates="192000" '
    b'channelMasks="AUDIO_CHANNEL_OUT_STEREO"'
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract(vendor_image: Path, relative: str) -> bytes:
    result = subprocess.run(
        ["debugfs", "-R", f"cat /{relative}", str(vendor_image)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"Could not read /{relative}: {message}")
    return result.stdout


def patch_binary(relative: str, source: bytes) -> bytes:
    spec = FILES[relative]
    actual_hash = sha256(source)
    if actual_hash != spec["source_sha256"]:
        raise ValueError(
            f"Unsupported {relative}: expected {spec['source_sha256']}, "
            f"found {actual_hash}"
        )

    data = bytearray(source)
    for offset, (expected_hex, replacement_hex) in spec["edits"].items():
        expected = bytes.fromhex(expected_hex)
        replacement = bytes.fromhex(replacement_hex)
        if len(expected) != len(replacement):
            raise ValueError(f"Patch length mismatch at 0x{offset:x}")
        actual = bytes(data[offset : offset + len(expected)])
        if actual != expected:
            raise ValueError(
                f"Unexpected bytes at 0x{offset:x} in {relative}: "
                f"expected {expected.hex()}, found {actual.hex()}"
            )
        data[offset : offset + len(expected)] = replacement

    patched = bytes(data)
    actual_patched_hash = sha256(patched)
    if actual_patched_hash != spec["patched_sha256"]:
        raise ValueError(
            f"Patched checksum mismatch for {relative}: {actual_patched_hash}"
        )
    return patched


def patch_policy(source: bytes) -> bytes:
    actual_hash = sha256(source)
    if actual_hash != POLICY_SOURCE_SHA256:
        raise ValueError(
            f"Unsupported {POLICY_PATH}: expected {POLICY_SOURCE_SHA256}, "
            f"found {actual_hash}"
        )
    if source.count(POLICY_OLD) != 1:
        raise ValueError("Could not find the expected primary output profile")
    patched = source.replace(POLICY_OLD, POLICY_NEW, 1)
    actual_patched_hash = sha256(patched)
    if actual_patched_hash != POLICY_PATCHED_SHA256:
        raise ValueError(
            f"Patched checksum mismatch for {POLICY_PATH}: {actual_patched_hash}"
        )
    return patched


def build(vendor_image: Path, output: Path) -> dict[str, dict[str, str]]:
    if not vendor_image.is_file():
        raise FileNotFoundError(f"Waydroid vendor image not found: {vendor_image}")

    patched_files: dict[str, bytes] = {}
    for relative in FILES:
        patched_files[relative] = patch_binary(
            relative, extract(vendor_image, relative)
        )
    patched_files[POLICY_PATH] = patch_policy(extract(vendor_image, POLICY_PATH))

    manifest = {}
    for relative, data in patched_files.items():
        destination = output / "vendor" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        destination.chmod(0o644)
        manifest[relative] = {"sha256": sha256(data)}

    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vendor-image",
        type=Path,
        default=Path("/var/lib/waydroid/images/vendor.img"),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifest = build(args.vendor_image, args.output)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        print(f"Refusing to build the overlay: {error}")
        return 1
    for relative, details in manifest.items():
        print(f"{relative}: {details['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
