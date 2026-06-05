"""Downgrade a font's COLR table from version 1 (paint graph) to version 0
(flat layered glyphs with palette colors).

COLR v0 is what most JVM/Java2D-based renderers (PHPStorm, IntelliJ, etc.)
support. COLR v0 can only stack opaque, solid-filled glyph outlines, so we lose
gradients and blending — but we preserve shape, color, and *placement* so emoji
stay recognizable.

The walk flattens the paint graph into an ordered (glyph, palette_index) layer
list per base glyph:
  - PaintColrLayers      → recurse into each child layer, bottom to top
  - PaintGlyph           → set `current_glyph`; capture the transform in effect
  - PaintSolid           → emit (current_glyph, palette_index)
  - PaintLinear/Radial/Sweep gradient → pick the dominant stop's palette index
  - PaintColrGlyph       → recurse into the referenced base glyph's paint
  - PaintTransform/etc.  → accumulate the affine; it is BAKED into the emitted
                           glyph's outline (a transformed copy is generated)
                           instead of being dropped
  - PaintComposite       → recurse the content-bearing child(ren) per the
                           composite mode (see _COMPOSITE_RECURSE); blend modes
                           keep the backdrop and drop the source overlay

Two lossless post-passes keep us within COLR v0's limits:
  - adjacent same-color layers are merged into one composite glyph
  - identical generated glyphs (same outline+transform, or same run) are shared
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fontTools.colorLib.builder import buildCOLR
from fontTools.misc.transform import Identity, Transform
from fontTools.pens.roundingPen import RoundingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_l_y_f import ARGS_ARE_XY_VALUES, Glyph, GlyphComponent


# Paint format ids per OpenType COLRv1 spec.
PAINT_COLR_LAYERS = 1
PAINT_SOLID = (2, 3)                # 2 = Solid, 3 = VarSolid
PAINT_LINEAR_GRADIENT = (4, 5)
PAINT_RADIAL_GRADIENT = (6, 7)
PAINT_SWEEP_GRADIENT = (8, 9)
PAINT_GLYPH = 10
PAINT_COLR_GLYPH = 11
PAINT_TRANSFORM_FAMILY = set(range(12, 32))  # Transform, Translate, Scale*, Rotate*, Skew* (and Var variants), formats 12–31
PAINT_COMPOSITE = 32  # PaintComposite is the final paint format, not 22


# CompositeMode -> child paints to recurse, in bottom-to-top paint order.
#
# COLR v0 can only stack opaque solid-filled glyphs, so we can't reproduce
# clipping or blending. We degrade each composite to the child(ren) that carry
# the actual emoji content:
#   - "over" modes paint both, in the right order.
#   - clip modes (SRC_IN/DEST_IN/...) keep the shape-bearing child.
#   - blend modes (PLUS/SOFT_LIGHT/MULTIPLY/...) keep the backdrop content and
#     drop the source overlay — painting that overlay as an opaque solid would
#     smother the emoji underneath.
# Values are CompositeMode ints per the OpenType spec.
_COMPOSITE_RECURSE: dict[int, tuple[str, ...]] = {
    0: (),                                   # CLEAR
    1: ("SourcePaint",),                     # SRC
    2: ("BackdropPaint",),                   # DEST
    3: ("BackdropPaint", "SourcePaint"),     # SRC_OVER
    4: ("SourcePaint", "BackdropPaint"),     # DEST_OVER
    5: ("BackdropPaint",),                   # SRC_IN   (source clipped to backdrop shape)
    6: ("BackdropPaint",),                   # DEST_IN
    7: ("SourcePaint",),                     # SRC_OUT
    8: ("BackdropPaint",),                   # DEST_OUT
    9: ("BackdropPaint", "SourcePaint"),     # SRC_ATOP
    10: ("SourcePaint", "BackdropPaint"),    # DEST_ATOP
    11: ("BackdropPaint", "SourcePaint"),    # XOR
}
# Blend modes (CompositeMode >= 12) fall through to backdrop-only.
_COMPOSITE_DEFAULT: tuple[str, ...] = ("BackdropPaint",)


def _paint_to_transform(paint) -> Optional[Transform]:
    """Convert a COLRv1 transform paint into a fontTools Transform.

    COLR's Affine2x3 (xx, yx, xy, yy, dx, dy) maps a point as
    x' = xx·x + xy·y + dx ; y' = yx·x + yy·y + dy, which is exactly
    fontTools' Transform(xx, yx, xy, yy, dx, dy).

    Returns None for transform formats we don't model (none of which occur in
    Noto), so the caller leaves the transform out rather than guessing.
    """
    fmt = paint.Format
    if fmt in (12, 13):  # PaintTransform / PaintVarTransform
        t = paint.Transform
        return Transform(t.xx, t.yx, t.xy, t.yy, t.dx, t.dy)
    if fmt in (14, 15):  # PaintTranslate
        return Identity.translate(paint.dx, paint.dy)
    if fmt in (16, 17):  # PaintScale
        return Identity.scale(paint.scaleX, paint.scaleY)
    if fmt in (18, 19):  # PaintScaleAroundCenter
        return (Identity.translate(paint.centerX, paint.centerY)
                .scale(paint.scaleX, paint.scaleY)
                .translate(-paint.centerX, -paint.centerY))
    if fmt in (20, 21):  # PaintScaleUniform
        return Identity.scale(paint.scaleX, paint.scaleX)
    if fmt in (22, 23):  # PaintScaleUniformAroundCenter
        return (Identity.translate(paint.centerX, paint.centerY)
                .scale(paint.scaleX, paint.scaleX)
                .translate(-paint.centerX, -paint.centerY))
    return None


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


def _make_composite_glyph(component_glyphs: list[str]) -> Glyph:
    """Build a composite glyf entry that overlays `component_glyphs` at the origin."""
    g = Glyph()
    g.numberOfContours = -1
    g.components = []
    for name in component_glyphs:
        comp = GlyphComponent()
        comp.glyphName = name
        comp.x = 0
        comp.y = 0
        comp.flags = ARGS_ARE_XY_VALUES
        g.components.append(comp)
    return g


def _transform_key(t: Transform) -> tuple:
    """Normalize a transform for caching baked glyphs.

    Rounded only enough to absorb floating-point noise from matrix composition
    (sub-micro-unit), so distinct transforms always get distinct outlines and the
    output is deterministic — we don't merge visibly different placements.
    """
    return tuple(round(v, 6) for v in t)


_IDENTITY_KEY = _transform_key(Identity)


class _GlyphFactory:
    """Creates and de-dupes the extra glyphs we synthesize while flattening:
    transformed copies of layer outlines, and composites of merged runs.

    All new glyphs are registered in the font's glyf table immediately (which
    also adds them to the glyph order); metrics are assigned by finalize().
    """

    def __init__(self, font: TTFont):
        self.font = font
        self.glyf = font["glyf"]
        self.hmtx = font["hmtx"]
        self.vmtx = font["vmtx"] if "vmtx" in font else None
        # Glyph set for the *original* outlines — all transforms bake from these.
        self.glyphset = font.getGlyphSet()
        self._existing = set(font.getGlyphOrder())
        self._counter = 0
        self._xform_cache: dict[tuple, str] = {}   # (glyph, transform_key) -> name
        self._comp_cache: dict[tuple, str] = {}     # component tuple -> name
        # (new_name, advance_reference_glyph) in creation order; transformed
        # glyphs precede composites that may reference them.
        self._created: list[tuple[str, str]] = []

    def _new_name(self) -> str:
        self._counter += 1
        name = f"colrlayer{self._counter:05d}"
        while name in self._existing:
            self._counter += 1
            name = f"colrlayer{self._counter:05d}"
        self._existing.add(name)
        return name

    def transformed(self, glyph_name: str, transform: Transform) -> str:
        """Return a glyph whose outline is `glyph_name` baked through `transform`.

        Identity transforms reuse the original glyph; everything else gets a
        cached, generated copy so the layer renders in the right place/size/flip.
        """
        key = _transform_key(transform)
        if key == _IDENTITY_KEY:
            return glyph_name
        cache_key = (glyph_name, key)
        name = self._xform_cache.get(cache_key)
        if name is not None:
            return name
        ttpen = TTGlyphPen(self.glyphset)
        # source outline -> apply affine -> round to integer grid -> record
        self.glyphset[glyph_name].draw(TransformPen(RoundingPen(ttpen), transform))
        name = self._new_name()
        self.glyf[name] = ttpen.glyph()
        self._xform_cache[cache_key] = name
        self._created.append((name, glyph_name))
        return name

    def composite(self, component_names: list[str]) -> str:
        """Return one glyph that overlays `component_names` (>=2) at the origin."""
        key = tuple(component_names)
        name = self._comp_cache.get(key)
        if name is not None:
            return name
        name = self._new_name()
        self.glyf[name] = _make_composite_glyph(component_names)
        self._comp_cache[key] = name
        self._created.append((name, component_names[0]))
        return name

    def finalize(self) -> int:
        """Recalculate bounds and assign metrics for every generated glyph."""
        for name, advance_ref in self._created:
            g = self.glyf[name]
            g.recalcBounds(self.glyf)
            self.hmtx[name] = (self.hmtx[advance_ref][0], g.xMin)
            if self.vmtx is not None:
                self.vmtx[name] = (self.vmtx[advance_ref][0], g.yMax)
        return len(self._created)


@dataclass
class _Ctx:
    layer_list: list
    glyph_lookup: dict
    factory: _GlyphFactory
    transformed: int = field(default=0)  # count of layers emitted under a transform


def _flatten(
    paint,
    ctx: _Ctx,
    out: list[tuple[str, int]],
    current_glyph: str | None,
    glyph_ctm: Transform,
    ctm: Transform,
    visited: set[str],
) -> None:
    fmt = paint.Format

    if fmt == PAINT_COLR_LAYERS:
        first = paint.FirstLayerIndex
        for i in range(first, first + paint.NumLayers):
            _flatten(ctx.layer_list[i], ctx, out, current_glyph, glyph_ctm, ctm, visited)
        return

    if fmt in PAINT_SOLID:
        if current_glyph is not None:
            out.append((ctx.factory.transformed(current_glyph, glyph_ctm), int(paint.PaletteIndex)))
        return

    if fmt in PAINT_LINEAR_GRADIENT or fmt in PAINT_RADIAL_GRADIENT or fmt in PAINT_SWEEP_GRADIENT:
        if current_glyph is not None:
            idx = _dominant_palette_index(paint.ColorLine)
            if idx is not None:
                out.append((ctx.factory.transformed(current_glyph, glyph_ctm), int(idx)))
        return

    if fmt == PAINT_GLYPH:
        # The outline of paint.Glyph is transformed by everything accumulated so
        # far; capture that as its glyph_ctm. Keep ctm flowing for nested glyphs.
        _flatten(paint.Paint, ctx, out, paint.Glyph, ctm, ctm, visited)
        return

    if fmt == PAINT_COLR_GLYPH:
        ref = paint.Glyph
        if ref in visited:
            return
        target = ctx.glyph_lookup.get(ref)
        if target is None:
            return
        visited.add(ref)
        try:
            _flatten(target, ctx, out, current_glyph, glyph_ctm, ctm, visited)
        finally:
            visited.discard(ref)
        return

    if fmt in PAINT_TRANSFORM_FAMILY:
        t = _paint_to_transform(paint)
        new_ctm = ctm.transform(t) if t is not None else ctm
        _flatten(paint.Paint, ctx, out, current_glyph, glyph_ctm, new_ctm, visited)
        return

    if fmt == PAINT_COMPOSITE:
        children = _COMPOSITE_RECURSE.get(int(paint.CompositeMode), _COMPOSITE_DEFAULT)
        for attr in children:
            _flatten(getattr(paint, attr), ctx, out, current_glyph, glyph_ctm, ctm, visited)
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


def _flatten_base_record(rec, ctx: _Ctx) -> list[tuple[str, int]]:
    """Flatten one BaseGlyphPaintRecord into a deduped layer list."""
    raw: list[tuple[str, int]] = []
    _flatten(rec.Paint, ctx, raw, None, Identity, Identity, {rec.BaseGlyph})
    return _dedupe_preserve_order(raw)


def _merge_consecutive_layers(
    flat: dict[str, list[tuple[str, int]]], factory: _GlyphFactory
) -> dict[str, list[tuple[str, int]]]:
    """Collapse runs of consecutive same-color layers into composite glyphs.

    A COLR v0 layer is one (glyph, palette_index) pair, so several adjacent
    layers that share a palette index can be replaced by a single layer whose
    glyph is the union of their outlines. Because the run is contiguous and
    monochrome, the union renders identically — this is lossless and frees up
    layer-record budget. Identical runs are shared across base glyphs.
    """
    out: dict[str, list[tuple[str, int]]] = {}
    for base, layers in flat.items():
        groups: list[tuple[list[str], int]] = []
        for glyph, color in layers:
            if groups and groups[-1][1] == color:
                groups[-1][0].append(glyph)
            else:
                groups.append(([glyph], color))

        merged: list[tuple[str, int]] = []
        for glyphs, color in groups:
            if len(glyphs) == 1:
                merged.append((glyphs[0], color))
            else:
                merged.append((factory.composite(glyphs), color))
        out[base] = merged
    return out


# COLR v0 records every layer in a single global array whose length (and the
# per-base firstLayerIndex into it) are uint16 — so the whole font must fit in
# 65535 layer records. We first try to fit losslessly by merging adjacent
# same-color layers; only if that still overflows do we truncate per-glyph.
COLR_V0_GLOBAL_LAYER_LIMIT = 65535
DEFAULT_PER_GLYPH_LAYER_CAP = 64


def downgrade(input_path: Path, output_path: Path, verbose: bool = False, layer_cap: int = DEFAULT_PER_GLYPH_LAYER_CAP) -> dict:
    font = TTFont(str(input_path))

    if "COLR" not in font:
        raise SystemExit("error: input font has no COLR table — nothing to downgrade")

    colr = font["COLR"]
    if colr.version == 0:
        raise SystemExit("error: input font is already COLR v0; nothing to do")

    table = colr.table
    factory = _GlyphFactory(font)
    ctx = _Ctx(
        layer_list=table.LayerList.Paint if table.LayerList else [],
        glyph_lookup=_build_glyph_lookup(table),
        factory=factory,
    )

    flat: dict[str, list[tuple[str, int]]] = {}
    skipped = 0
    for rec in table.BaseGlyphList.BaseGlyphPaintRecord:
        layers = _flatten_base_record(rec, ctx)
        if not layers:
            skipped += 1
            continue
        flat[rec.BaseGlyph] = layers

    # Lossless pass: merge adjacent same-color layers into composite glyphs.
    flat = _merge_consecutive_layers(flat, factory)
    generated = factory.finalize()
    total_layers = sum(len(v) for v in flat.values())

    capped = 0
    if total_layers > COLR_V0_GLOBAL_LAYER_LIMIT:
        # Last resort: still over budget, so truncate the topmost layers of the
        # busiest glyphs. Tighten the per-glyph cap progressively until we fit.
        new_cap = layer_cap
        while total_layers > COLR_V0_GLOBAL_LAYER_LIMIT and new_cap > 1:
            new_cap = max(1, new_cap - 2)
            for k in flat:
                if len(flat[k]) > new_cap:
                    flat[k] = flat[k][:new_cap]
                    capped += 1
            total_layers = sum(len(v) for v in flat.values())
        if verbose:
            print(f"tightened per-glyph cap to {new_cap} to fit COLR v0 limit", file=sys.stderr)

    if verbose:
        print(
            f"base_glyphs={len(flat)} skipped={skipped} generated_glyphs={generated} "
            f"capped={capped} total_layers={total_layers} numGlyphs={font['maxp'].numGlyphs}",
            file=sys.stderr,
        )

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
        "generated_glyphs": generated,
        "total_layers": total_layers,
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
