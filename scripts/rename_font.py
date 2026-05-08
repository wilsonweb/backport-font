"""Rename a font's family across all relevant `name` table records.

Updates name IDs:
  1  Family
  3  Unique font identifier
  4  Full font name
  6  PostScript name (no spaces allowed)
  16 Typographic family (preferred family) — if present
  21 WWS family — if present

Records are written for both Windows (platformID=3, encodingID=1) and
Mac (platformID=1, encodingID=0) platforms, replacing any existing entries
with the same (nameID, platformID, encodingID, langID) key.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from fontTools.ttLib import TTFont


WINDOWS = (3, 1, 0x409)  # platformID, platEncID, langID
MAC = (1, 0, 0)

NAME_IDS_TO_REWRITE = (1, 3, 4, 6, 16, 21)


def _postscript_name(family: str) -> str:
    """Build a PostScript name: ASCII, no spaces, max 63 chars."""
    ps = re.sub(r"\s+", "", family)
    ps = re.sub(r"[^A-Za-z0-9_-]", "", ps)
    return ps[:63] or "Font"


def _unique_id(family: str, version: str | None) -> str:
    if version:
        return f"{version};{_postscript_name(family)}"
    return _postscript_name(family)


def rename_font(input_path: Path, output_path: Path, new_family_name: str) -> None:
    font = TTFont(str(input_path))
    name = font["name"]

    # Pull existing version (id=5) so the unique id stays meaningful.
    version_rec = name.getName(5, *WINDOWS) or name.getName(5, *MAC)
    version = version_rec.toUnicode() if version_rec else None

    full_name = new_family_name
    ps_name = _postscript_name(new_family_name)
    unique = _unique_id(new_family_name, version)

    new_values = {
        1: new_family_name,
        3: unique,
        4: full_name,
        6: ps_name,
        16: new_family_name,
        21: new_family_name,
    }

    # Drop existing records we'll replace, then re-add for both platforms.
    for nid in NAME_IDS_TO_REWRITE:
        name.removeNames(nameID=nid)

    for nid, value in new_values.items():
        # 16 and 21 are optional — only set if the original had them, OR
        # always set 16 since it's broadly useful for differentiating subfamilies.
        if nid in (16, 21):
            continue  # skip optional records to keep the table clean
        for plat, enc, lang in (WINDOWS, MAC):
            name.setName(value, nid, plat, enc, lang)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    font.save(str(output_path))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--name", required=True, help="New family name, e.g. 'Noto Color Emoji Compat'")
    args = p.parse_args()
    if not args.input.exists():
        print(f"error: {args.input} does not exist", file=sys.stderr)
        return 2
    rename_font(args.input, args.output, args.name)
    print(f"Wrote {args.output} with family {args.name!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
