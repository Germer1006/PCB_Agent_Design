"""Recheck and package an existing verified project without regenerating its PCB."""
import argparse
import json
from pathlib import Path
import shutil
import sys

from delivery import package_delivery
from kicad_io import _find_python
from release_check import evaluate
from verify import find_kicad_cli


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--kicad-cli", type=Path)
    parser.add_argument("--review", type=Path, help="Actual completed artifact-bound review")
    parser.add_argument("--target", choices=("draft", "prototype"), default="draft")
    args = parser.parse_args()
    try:
        directory = args.directory.resolve(strict=True)
        cli = find_kicad_cli(args.kicad_cli)
        if cli is None:
            raise RuntimeError("Native KiCad CLI required for package recheck")
        if args.review:
            source = args.review.resolve(strict=True)
            destination = directory / "release_review.json"
            if source != destination:
                shutil.copyfile(source, destination)
        result = package_delivery(directory, cli=cli, design={"_python": str(_find_python(cli))})
        gate = evaluate(directory)
        print(json.dumps({"delivery": str(directory / "delivery.zip"), "roundtrip": result,
                          "release": gate}, ensure_ascii=False, indent=2))
        return 0 if args.target == "draft" or gate["prototype_ready"] else 2
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Packaging failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
