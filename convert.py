"""Convert a color font into a renamed, more-compatible variant.

Defaults are tuned for Noto Color Emoji on Fedora Silverblue, but every option
is overridable, so the same tool works for any color font.

Modes:
  rename-only  Just rewrite the family name. Leaves color tables untouched.
  colr-v0      Rename and downgrade COLRv1 → COLRv0 (best for JVM apps like PHPStorm).
  auto         Choose colr-v0 if the input has COLRv1; otherwise rename-only.

Examples:
  # Default: convert system Noto Color Emoji into "Noto Color Emoji Compat".
  python convert.py

  # Convert a different font:
  python convert.py --input /path/to/MyEmoji.ttf --name "My Emoji Compat"
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from fontTools.ttLib import TTFont

# Allow running from project root without a package.
sys.path.insert(0, str(Path(__file__).parent / "scripts"))
from downgrade_colr import downgrade  # noqa: E402
from rename_font import rename_font  # noqa: E402


# Conventional path on Fedora; override with --input for other systems/fonts.
DEFAULT_SOURCE = Path("/usr/share/fonts/google-noto-color-emoji-fonts/Noto-COLRv1.ttf")
DEFAULT_OUTPUT = Path("build/output/NotoColorEmojiCompat.ttf")
DEFAULT_NAME = "Noto Color Emoji Compat"
DEFAULT_WORK_COPY = Path("build/source")


def _stage_input(input_path: Path, work_dir: Path) -> Path:
    """Copy the input into our build dir so we never modify the system file."""
    work_dir.mkdir(parents=True, exist_ok=True)
    staged = work_dir / input_path.name
    if staged.resolve() != input_path.resolve():
        shutil.copy2(input_path, staged)
    return staged


def _detect_mode(input_path: Path) -> str:
    font = TTFont(str(input_path))
    if "COLR" in font and font["COLR"].version == 1:
        return "colr-v0"
    return "rename-only"


def _glyph_count(path: Path) -> int:
    return len(TTFont(str(path)).getGlyphOrder())


def convert(
    input_path: Path,
    output_path: Path,
    new_name: str,
    mode: str,
    verbose: bool = False,
) -> dict:
    if not input_path.exists():
        raise SystemExit(f"error: input font not found: {input_path}")

    if mode == "auto":
        mode = _detect_mode(input_path)
        if verbose:
            print(f"auto-detected mode: {mode}", file=sys.stderr)

    staged = _stage_input(input_path, DEFAULT_WORK_COPY)

    # Step 1: rename, always. Output goes to a temp path if we're also downgrading.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "rename-only":
        renamed = output_path
    else:
        renamed = output_path.with_suffix(".renamed.ttf")
    rename_font(staged, renamed, new_name)

    summary: dict = {
        "input": str(input_path),
        "output": str(output_path),
        "name": new_name,
        "mode": mode,
        "glyph_count": _glyph_count(staged),
    }

    if mode == "colr-v0":
        result = downgrade(renamed, output_path, verbose=verbose)
        summary["base_glyphs_input"] = result["base_glyphs_input"]
        summary["base_glyphs_output"] = result["base_glyphs_output"]
        summary["skipped"] = result["skipped"]
        try:
            renamed.unlink()
        except FileNotFoundError:
            pass
    elif mode == "rename-only":
        pass
    else:
        raise SystemExit(f"error: unknown mode {mode!r}")

    return summary


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", type=Path, default=DEFAULT_SOURCE, help=f"Source font (default: {DEFAULT_SOURCE})")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"Output font (default: {DEFAULT_OUTPUT})")
    p.add_argument("--name", default=DEFAULT_NAME, help=f"New family name (default: {DEFAULT_NAME!r})")
    p.add_argument("--mode", choices=("auto", "rename-only", "colr-v0"), default="auto", help="Conversion mode (default: auto)")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    summary = convert(args.input, args.output, args.name, args.mode, verbose=args.verbose)

    print()
    print("=== conversion complete ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
