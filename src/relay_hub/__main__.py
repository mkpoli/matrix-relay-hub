"""Command-line entry point for static topology checks."""

import argparse
import sys

from .topology import ValidationError, load_topology


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m relay_hub")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="validate a topology inventory")
    check.add_argument("file", metavar="FILE", help="UTF-8 TOML inventory")
    args = parser.parse_args(argv)
    try:
        load_topology(args.file)
    except ValidationError as exc:
        for error in exc.errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print("Topology is valid (static inventory only; connectivity not checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
