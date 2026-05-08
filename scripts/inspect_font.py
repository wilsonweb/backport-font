"""Inspect a font file and report its name table, color tables, and glyph stats."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fontTools.ttLib import TTFont


COLOR_TABLES = ("COLR", "CPAL", "CBDT", "CBLC", "SVG ", "sbix")


def inspect(font_path: Path) -> None:
    font = TTFont(str(font_path))

    print(f"File: {font_path}")
    print(f"Size: {font_path.stat().st_size:,} bytes")
    print()

    print("=== name table ===")
    name = font["name"]
    seen = set()
    for rec in name.names:
        key = (rec.nameID, rec.platformID, rec.platEncID, rec.langID)
        if key in seen:
            continue
        seen.add(key)
        try:
            value = rec.toUnicode()
        except Exception:
            value = repr(bytes(rec.string))
        print(f"  id={rec.nameID:<3} plat={rec.platformID} enc={rec.platEncID} lang={rec.langID} : {value!r}")

    print()
    print("=== color tables present ===")
    present = [t for t in COLOR_TABLES if t in font]
    if not present:
        print("  (none)")
    for t in present:
        line = f"  {t}"
        if t == "COLR":
            line += f"  version={font['COLR'].version}"
        print(line)

    print()
    print("=== glyphs ===")
    glyph_order = font.getGlyphOrder()
    print(f"  count={len(glyph_order)}")
    print(f"  sample={glyph_order[:8]}")

    if "COLR" in font and font["COLR"].version == 1:
        colr = font["COLR"].table
        if hasattr(colr, "BaseGlyphList") and colr.BaseGlyphList:
            n = colr.BaseGlyphList.BaseGlyphCount
            print(f"  COLRv1 base glyph paint records={n}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("font", type=Path, help="Path to font file")
    args = p.parse_args()
    if not args.font.exists():
        print(f"error: {args.font} does not exist", file=sys.stderr)
        return 2
    inspect(args.font)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
