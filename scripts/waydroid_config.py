#!/usr/bin/env python3
"""Apply or restore the small Waydroid config changes used by this project."""

from __future__ import annotations

import argparse
import configparser
import json
from pathlib import Path


ROOT_WORKAROUND = {
    ("properties", "ro.build.selinux"): "1",
    ("properties", "ro.build.tags"): "release-keys",
    ("properties", "ro.product.build.tags"): "release-keys",
    ("properties", "ro.vendor.build.tags"): "release-keys",
    ("properties", "ro.odm.build.tags"): "release-keys",
}
HIRES_OVERLAY = {("waydroid", "mount_overlays"): "True"}
ALLOWED_CHANGES = ROOT_WORKAROUND | HIRES_OVERLAY


def load_config(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    with path.open("r", encoding="utf-8") as config_file:
        parser.read_file(config_file)
    return parser


def write_config(parser: configparser.ConfigParser, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as config_file:
        parser.write(config_file, space_around_delimiters=True)


def state_key(section: str, key: str) -> str:
    return f"{section}.{key}"


def validate_state(state: dict) -> None:
    if state.get("version") != 1 or not isinstance(state.get("values"), dict):
        raise ValueError("Unsupported or damaged state file")
    for item in state["values"].values():
        section = item.get("section")
        key = item.get("key")
        applied = item.get("applied")
        expected = ALLOWED_CHANGES.get((section, key))
        if expected is None or applied != expected:
            raise ValueError(f"Unexpected state entry: {section}.{key}")


def apply_changes(args: argparse.Namespace) -> int:
    parser = load_config(args.config)
    if args.state.exists():
        state = json.loads(args.state.read_text(encoding="utf-8"))
    else:
        state = {"version": 1, "values": {}}
    validate_state(state)

    changes = {}
    if args.root_workaround:
        changes.update(ROOT_WORKAROUND)
    if args.hires:
        changes.update(HIRES_OVERLAY)
    if not changes:
        raise ValueError("No Waydroid config change was selected")

    values = state.setdefault("values", {})
    for (section, key), new_value in changes.items():
        identity = state_key(section, key)
        if identity not in values:
            present = parser.has_section(section) and parser.has_option(section, key)
            values[identity] = {
                "section": section,
                "key": key,
                "present": present,
                "value": parser.get(section, key) if present else None,
                "applied": new_value,
            }
        if not parser.has_section(section):
            parser.add_section(section)
        parser.set(section, key, new_value)

    write_config(parser, args.output)
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


def restore_changes(args: argparse.Namespace) -> int:
    if not args.state.is_file():
        raise FileNotFoundError(f"State file not found: {args.state}")
    state = json.loads(args.state.read_text(encoding="utf-8"))
    validate_state(state)
    parser = load_config(args.config)

    for item in state.get("values", {}).values():
        section = item["section"]
        key = item["key"]
        applied = item["applied"]
        current = (
            parser.get(section, key)
            if parser.has_section(section) and parser.has_option(section, key)
            else None
        )
        if current != applied:
            print(f"Leaving user-changed value alone: {section}.{key}")
            continue
        if item["present"]:
            if not parser.has_section(section):
                parser.add_section(section)
            parser.set(section, key, item["value"])
        else:
            parser.remove_option(section, key)

    write_config(parser, args.output)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--config", type=Path, required=True)
    apply_parser.add_argument("--output", type=Path, required=True)
    apply_parser.add_argument("--state", type=Path, required=True)
    apply_parser.add_argument("--root-workaround", action="store_true")
    apply_parser.add_argument("--hires", action="store_true")

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--config", type=Path, required=True)
    restore_parser.add_argument("--output", type=Path, required=True)
    restore_parser.add_argument("--state", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "apply":
            return apply_changes(args)
        return restore_changes(args)
    except (configparser.Error, FileNotFoundError, OSError, ValueError) as error:
        print(f"Waydroid config was not changed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
