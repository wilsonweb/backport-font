"""Compare the version of the currently installed source font against the
version we last built from. Reports whether a rebuild is needed."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fontTools.ttLib import TTFont


def font_version(path: Path) -> str | None:
    try:
        font = TTFont(str(path))
    except Exception as e:
        print(f"warning: could not read {path}: {e}", file=sys.stderr)
        return None
    name = font["name"]
    rec = name.getName(5, 3, 1, 0x409) or name.getName(5, 1, 0, 0)
    return rec.toUnicode() if rec else None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, default=Path("/usr/share/fonts/google-noto-color-emoji-fonts/Noto-COLRv1.ttf"))
    p.add_argument("--state", type=Path, default=Path("build/last-built-version.txt"))
    args = p.parse_args()

    if not args.source.exists():
        print(f"source font not found: {args.source}")
        return 2

    current = font_version(args.source)
    last = args.state.read_text().strip() if args.state.exists() else None

    print(f"current source version: {current or '(unknown)'}")
    print(f"last built version:     {last or '(none)'}")

    if current != last:
        print("=> rebuild needed")
        return 1
    print("=> up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
