"""Tests for the COLR v1 -> v0 downgrade.

Run directly (no framework needed):

    .venv/bin/python tests/test_downgrade.py

These guard the paint-graph walk against the bug where PaintComposite
(format 32) was misnumbered as 22 and silently dropped, deleting every
emoji whose paint graph used a composite (soft-light shading, src-in clips).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from fontTools.ttLib import TTFont  # noqa: E402

from downgrade_colr import (  # noqa: E402
    _build_glyph_lookup,
    _flatten_base_record,
    _Ctx,
    _GlyphFactory,
    PAINT_COMPOSITE,
)

SOURCE = Path("/usr/share/fonts/google-noto-color-emoji-fonts/Noto-COLRv1.ttf")


def _make_ctx(font):
    table = font["COLR"].table
    return _Ctx(
        layer_list=table.LayerList.Paint if table.LayerList else [],
        glyph_lookup=_build_glyph_lookup(table),
        factory=_GlyphFactory(font),
    )


def test_composite_format_constant():
    # Per OpenType, PaintComposite is the final paint format (32), not 22.
    assert PAINT_COMPOSITE == 32, f"PaintComposite must be 32, got {PAINT_COMPOSITE}"


def test_no_base_glyph_dropped_to_empty():
    """Every base glyph that has a paint graph must flatten to >=1 layer.

    Before the fix, 262 glyphs whose root paint was a composite flattened to
    nothing and were skipped entirely (the missing emoji in the screenshot).
    """
    if not SOURCE.exists():
        print(f"SKIP test_no_base_glyph_dropped_to_empty (no source font at {SOURCE})")
        return
    font = TTFont(str(SOURCE))
    ctx = _make_ctx(font)
    empty = [
        rec.BaseGlyph
        for rec in font["COLR"].table.BaseGlyphList.BaseGlyphPaintRecord
        if not _flatten_base_record(rec, ctx)
    ]
    assert not empty, f"{len(empty)} base glyphs flattened to no layers, e.g. {empty[:10]}"


def test_composite_root_glyph_keeps_backdrop_content():
    """A glyph whose root paint is a soft-light composite must keep its
    backdrop content (the actual emoji), not collapse to empty."""
    if not SOURCE.exists():
        print("SKIP test_composite_root_glyph_keeps_backdrop_content (no source font)")
        return
    font = TTFont(str(SOURCE))
    ctx = _make_ctx(font)
    composite_roots = [
        rec
        for rec in font["COLR"].table.BaseGlyphList.BaseGlyphPaintRecord
        if rec.Paint.Format == 32
    ]
    assert composite_roots, "expected at least one composite-rooted base glyph in source"
    for rec in composite_roots:
        layers = _flatten_base_record(rec, ctx)
        assert layers, f"composite-rooted glyph {rec.BaseGlyph} flattened to nothing"


def test_transform_baking_is_geometric():
    """A dropped transform is the main cause of misplaced emoji parts. Verify the
    factory bakes transforms into the outline: translation shifts it, scaling
    resizes it, and a negative scale flips it — and identity reuses the original."""
    if not SOURCE.exists():
        print("SKIP test_transform_baking_is_geometric (no source font)")
        return
    from fontTools.misc.transform import Identity

    from downgrade_colr import _GlyphFactory

    font = TTFont(str(SOURCE))
    glyf = font["glyf"]
    g0 = "glyph22744"
    base = glyf[g0]
    base.recalcBounds(glyf)

    fac = _GlyphFactory(font)
    moved = fac.transformed(g0, Identity.translate(0, -225))
    scaled = fac.transformed(g0, Identity.scale(2, 1))
    flipped = fac.transformed(g0, Identity.scale(-1, 1))
    fac.finalize()

    m, s, fl = glyf[moved], glyf[scaled], glyf[flipped]
    assert (m.yMin, m.yMax) == (base.yMin - 225, base.yMax - 225), "translation not baked"
    assert (s.xMin, s.xMax) == (base.xMin * 2, base.xMax * 2), "scale not baked"
    assert (fl.xMin, fl.xMax) == (-base.xMax, -base.xMin), "horizontal flip not baked"
    assert fac.transformed(g0, Identity) == g0, "identity must reuse the original glyph"


def test_full_build_is_lossless_and_fits():
    """End-to-end: the real font downgrades with nothing skipped, nothing
    truncated, under the COLR v0 ceiling, and every merged composite renders
    identically to the run of layers it replaced."""
    if not SOURCE.exists():
        print("SKIP test_full_build_is_lossless_and_fits (no source font)")
        return

    import tempfile
    from fontTools.pens.recordingPen import DecomposingRecordingPen

    from downgrade_colr import COLR_V0_GLOBAL_LAYER_LIMIT, downgrade

    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "out.ttf"
        summary = downgrade(SOURCE, out)
        assert summary["skipped"] == 0, summary
        assert summary["total_layers"] <= COLR_V0_GLOBAL_LAYER_LIMIT, summary
        assert summary["generated_glyphs"] > 0, "expected glyphs to be generated"

        font = TTFont(str(out))
        assert font["COLR"].version == 0
        assert "CPAL" in font
        total = sum(len(v) for v in font["COLR"].ColorLayers.values())
        assert total <= COLR_V0_GLOBAL_LAYER_LIMIT, total
        assert font["maxp"].numGlyphs <= 65535, font["maxp"].numGlyphs

        # Every composite must be geometrically identical to its component run.
        glyf = font["glyf"]
        gs = font.getGlyphSet()
        composites = [n for n in font.getGlyphOrder() if glyf[n].isComposite()]
        assert composites, "expected composite glyphs from merging"
        for name in composites:
            got = DecomposingRecordingPen(gs)
            gs[name].draw(got)
            ref = DecomposingRecordingPen(gs)
            for c in glyf[name].components:
                gs[c.glyphName].draw(ref)
            assert got.value == ref.value, f"composite {name} does not match its run"


def main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
