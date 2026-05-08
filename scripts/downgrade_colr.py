"""Downgrade a font's COLR table from version 1 (paint graph) to version 0
(flat layered glyphs with palette colors).

COLR v0 is what most JVM/Java2D-based renderers (PHPStorm, IntelliJ, etc.)
support. We lose gradients, transforms, and compositing, but keep enough
shape + color to make emoji recognizable.

The walk:
  - PaintColrLayers      → recurse into each child layer
  - PaintGlyph           → fix `current_glyph`, recurse into child paint to find color
  - PaintSolid           → emit (current_glyph, palette_index)
  - PaintLinear/Radial/Sweep gradient → pick the dominant stop's palette index
  - PaintColrGlyph       → recurse into the referenced base glyph's paint
  - PaintTransform/etc.  → recurse, drop the transform
  - PaintComposite       → recurse backdrop then source
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fontTools.colorLib.builder import buildCOLR
from fontTools.ttLib import TTFont


# Paint format ids per OpenType COLRv1 spec.
PAINT_COLR_LAYERS = 1
PAINT_SOLID = (2, 3)                # 2 = Solid, 3 = VarSolid
PAINT_LINEAR_GRADIENT = (4, 5)
PAINT_RADIAL_GRADIENT = (6, 7)
PAINT_SWEEP_GRADIENT = (8, 9)
PAINT_GLYPH = 10
PAINT_COLR_GLYPH = 11
PAINT_TRANSFORM_FAMILY = set(range(12, 22))  # Transform, Translate, Scale*, Rotate*, Skew* (and Var variants)
PAINT_COMPOSITE = 22


def _dominant_palette_index(color_line) -> int | None:
    """Pick a representative palette index from a gradient's color stops.

    We use the middle stop. It's a heuristic — gradients can use multiple palette
    entries, but in practice picking one works well enough for emoji.
    """
    stops = color_line.ColorStop
    if not stops:
        return None
    stop = stops[len(stops) // 2]
    return stop.PaletteIndex


def _build_glyph_lookup(colr_table) -> dict[str, object]:
    """Map base glyph name → its top-level Paint, for resolving PaintColrGlyph refs."""
    return {
        rec.BaseGlyph: rec.Paint
        for rec in colr_table.BaseGlyphList.BaseGlyphPaintRecord
    }


def _flatten(
    paint,
    layer_list,
    glyph_lookup,
    out: list[tuple[str, int]],
    current_glyph: str | None,
    visited: set[str],
) -> None:
    fmt = paint.Format

    if fmt == PAINT_COLR_LAYERS:
        first = paint.FirstLayerIndex
        for i in range(first, first + paint.NumLayers):
            _flatten(layer_list[i], layer_list, glyph_lookup, out, current_glyph, visited)
        return

    if fmt in PAINT_SOLID:
        if current_glyph is not None:
            out.append((current_glyph, int(paint.PaletteIndex)))
        return

    if fmt in PAINT_LINEAR_GRADIENT or fmt in PAINT_RADIAL_GRADIENT or fmt in PAINT_SWEEP_GRADIENT:
        if current_glyph is not None:
            idx = _dominant_palette_index(paint.ColorLine)
            if idx is not None:
                out.append((current_glyph, int(idx)))
        return

    if fmt == PAINT_GLYPH:
        _flatten(paint.Paint, layer_list, glyph_lookup, out, paint.Glyph, visited)
        return

    if fmt == PAINT_COLR_GLYPH:
        ref = paint.Glyph
        if ref in visited:
            return
        target = glyph_lookup.get(ref)
        if target is None:
            return
        visited.add(ref)
        try:
            _flatten(target, layer_list, glyph_lookup, out, current_glyph, visited)
        finally:
            visited.discard(ref)
        return

    if fmt in PAINT_TRANSFORM_FAMILY:
        _flatten(paint.Paint, layer_list, glyph_lookup, out, current_glyph, visited)
        return

    if fmt == PAINT_COMPOSITE:
        _flatten(paint.BackdropPaint, layer_list, glyph_lookup, out, current_glyph, visited)
        _flatten(paint.SourcePaint, layer_list, glyph_lookup, out, current_glyph, visited)
        return

    # Unknown format — silently skip rather than corrupt the build.


def _dedupe_preserve_order(layers: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Drop duplicate (glyph, palette) pairs, preserving first occurrence."""
    seen: set[tuple[str, int]] = set()
    out: list[tuple[str, int]] = []
    for pair in layers:
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return out


# COLR v0 stores firstLayerIndex as uint16, so total layers across all base
# glyphs must fit in 65535. We cap per-glyph layers (after dedup) to keep
# the global total safely under that.
COLR_V0_GLOBAL_LAYER_LIMIT = 65535
DEFAULT_PER_GLYPH_LAYER_CAP = 32


def downgrade(input_path: Path, output_path: Path, verbose: bool = False, layer_cap: int = DEFAULT_PER_GLYPH_LAYER_CAP) -> dict:
    font = TTFont(str(input_path))

    if "COLR" not in font:
        raise SystemExit("error: input font has no COLR table — nothing to downgrade")

    colr = font["COLR"]
    if colr.version == 0:
        raise SystemExit("error: input font is already COLR v0; nothing to do")

    table = colr.table
    layer_list = table.LayerList.Paint if table.LayerList else []
    glyph_lookup = _build_glyph_lookup(table)

    flat: dict[str, list[tuple[str, int]]] = {}
    skipped = 0
    capped = 0
    for rec in table.BaseGlyphList.BaseGlyphPaintRecord:
        raw: list[tuple[str, int]] = []
        _flatten(rec.Paint, layer_list, glyph_lookup, raw, current_glyph=None, visited={rec.BaseGlyph})
        layers = _dedupe_preserve_order(raw)
        if not layers:
            skipped += 1
            continue
        if len(layers) > layer_cap:
            layers = layers[:layer_cap]
            capped += 1
        flat[rec.BaseGlyph] = layers

    total_layers = sum(len(v) for v in flat.values())
    if total_layers > COLR_V0_GLOBAL_LAYER_LIMIT:
        # Tighten the cap progressively until we fit.
        new_cap = layer_cap
        while total_layers > COLR_V0_GLOBAL_LAYER_LIMIT and new_cap > 1:
            new_cap = max(1, new_cap - 2)
            for k in flat:
                if len(flat[k]) > new_cap:
                    flat[k] = flat[k][:new_cap]
            total_layers = sum(len(v) for v in flat.values())
        if verbose:
            print(f"tightened per-glyph cap to {new_cap} to fit COLR v0 limit", file=sys.stderr)

    if verbose:
        print(f"base_glyphs={len(flat)} skipped={skipped} capped={capped} total_layers={total_layers}", file=sys.stderr)
        for name, layers in list(flat.items())[:3]:
            print(f"  {name}: {layers[:6]}{' …' if len(layers) > 6 else ''}", file=sys.stderr)

    new_colr = buildCOLR(flat)
    # buildCOLR may produce v0 automatically when given list-of-tuples,
    # but assert here to fail loudly if the structure changes upstream.
    if new_colr.version != 0:
        raise SystemExit(f"error: built COLR is version {new_colr.version}, expected 0")

    font["COLR"] = new_colr
    # CPAL is preserved automatically — it lives on the font, not on COLR.

    output_path.parent.mkdir(parents=True, exist_ok=True)
    font.save(str(output_path))

    return {
        "input": str(input_path),
        "output": str(output_path),
        "base_glyphs_input": len(table.BaseGlyphList.BaseGlyphPaintRecord),
        "base_glyphs_output": len(flat),
        "skipped": skipped,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    if not args.input.exists():
        print(f"error: {args.input} does not exist", file=sys.stderr)
        return 2
    summary = downgrade(args.input, args.output, verbose=args.verbose)
    for k, v in summary.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
