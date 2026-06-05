# Backport Font

Convert a modern color font into a renamed, JVM-compatible variant.

The default configuration converts the system **Noto Color Emoji** (which uses
COLR v1) into **Noto Color Emoji Compat** (COLR v0), so PHPStorm, IntelliJ, and
other JetBrains IDEs on Fedora Silverblue can render color emoji. Every option
is overridable, so the same tool works for any color font.

## TLDR

Just copy any .ttf (or .otf) file like the one found at `dist/NotoColorEmojiCompat.ttf` into:

Your user only: `~/.local/share/fonts/`
All users (needs sudo): `/usr/local/share/fonts/`

`fc-cache -f`

## What it does

1. Copies the source font into `build/source/` (never modifies the system file).
2. Rewrites the `name` table so the output advertises a new family name.
3. (When the source uses COLR v1) flattens the v1 paint graph into a v0 layer
   list — solid layers from gradients are picked at the dominant stop,
   affine transforms are baked into the layer outlines (so flips, scales, and
   translations are preserved, not lost), composites degrade to their
   content-bearing child, and palette references are preserved verbatim.
   Adjacent same-color layers are merged so the whole font fits COLR v0's
   layer budget without dropping anything.

## Why

PHPStorm (and other JetBrains apps using JetBrains Runtime) ship a Java2D
font path that doesn't render COLR v1 — gradients and paint graphs aren't
supported. COLR v0 (a flat list of layered glyphs colored by palette index)
is widely supported. The tool produces a font that's visually similar enough
for emoji rendering while staying within COLR v0.

The renaming step is needed because Java2D and fontconfig will otherwise
prefer the system's COLR v1 font and fall back to colorless rendering.

## Setup

Run on Fedora Silverblue. A toolbox is recommended but not required, as long
as Python 3.11+ is available.

```sh
make setup     # creates .venv and installs fonttools, brotli, lxml
```

## Build & install

```sh
make build     # produces build/output/NotoColorEmojiCompat.ttf
make install   # copies to ~/.local/share/fonts/ and runs fc-cache
```

A pre-built copy is committed at `dist/NotoColorEmojiCompat.ttf`, so users
who don't want to run the toolchain can install directly:

```sh
cp dist/NotoColorEmojiCompat.ttf ~/.local/share/fonts/
fc-cache -f ~/.local/share/fonts/
```

After install, point JetBrains at the new family. In PHPStorm:

- **Settings → Editor → Font → Fallback font** → `Noto Color Emoji Compat`
- Restart PHPStorm so the JVM rebuilds its font cache.

If emoji still don't render, JetBrains Runtime may also need a refresh:
delete `~/.cache/JetBrains/<product>/fontcache` and restart.

## Use with a different font

Every default is overridable:

```sh
# Convert a custom font:
make build INPUT=/path/to/MyEmoji.ttf NAME="My Emoji Compat" \
           OUTPUT=build/output/MyEmojiCompat.ttf

# Or call convert.py directly:
.venv/bin/python convert.py --input MyEmoji.ttf --name "My Emoji Compat" \
                            --output out.ttf --mode colr-v0
```

Modes:

- `auto` (default) — pick `colr-v0` if the input has COLR v1, otherwise `rename-only`.
- `rename-only` — just rewrite the name table.
- `colr-v0` — also downgrade COLR v1 → v0.

## Update workflow

When Fedora pushes a new `google-noto-color-emoji-fonts` package:

```sh
make check     # compare installed source vs. last-built version
make install   # rebuild and reinstall if needed
```

## Layout

```
convert.py                # main entry point
scripts/
  inspect_font.py         # dump name, color tables, glyph stats
  rename_font.py          # rewrite name table
  downgrade_colr.py       # COLR v1 → v0
  check_upstream.py       # compare versions
dist/
  NotoColorEmojiCompat.ttf  # pre-built release artifact (OFL 1.1)
  OFL.txt                 # license bundled with the font
build/                    # gitignored scratch dir for builds
docs/
  source-font-info.txt    # snapshot of the input font's structure
LICENSE                   # MIT — applies to source code only
OFL.txt                   # SIL OFL 1.1 — applies to all .ttf files
```

## Known limitations

- Gradients are flattened to a single dominant color — output will look
  flatter than the original.
- Blend/clip composites (e.g. soft-light shading, src-in masks) can't be
  represented in COLR v0, so the overlay is dropped and the underlying
  content is kept. The emoji is recognizable but loses subtle shading.
- Affine transforms are baked into per-layer outlines by generating
  transformed copies of the source glyphs. This grows the glyph count
  (which is also a uint16, capped at 65535) but keeps placement correct.
- Adjacent same-color layers are merged losslessly to stay within COLR v0's
  65535-layer-record budget; truncation only kicks in if a font still
  overflows after merging (Noto does not).

## Credits

This project is a derivative of [Noto Color Emoji](https://github.com/googlefonts/noto-emoji)
by Google LLC. All glyph artwork, palette data, and outline data in the
output font come from the upstream Noto Color Emoji release shipped in the
Fedora `google-noto-color-emoji-fonts` package. This tool only rewrites the
font's `name` table and rebuilds its `COLR` table at version 0.

- Upstream font:    https://github.com/googlefonts/noto-emoji
- OFL 1.1 license:  https://openfontlicense.org/
- OFL 1.1 FAQ:      https://scripts.sil.org/OFL-FAQ_web

## License

This repository contains two distinct kinds of artifacts under different
licenses:

- **Source code** (Python scripts, Makefile, documentation) — [MIT](LICENSE).
- **Font files** (`dist/*.ttf` and any `.ttf` produced by `make build`) —
  [SIL Open Font License 1.1](OFL.txt). This is required: the output font is
  a derivative of Noto Color Emoji, which is OFL-licensed. The output font
  preserves Google's original copyright and OFL notices in its `name` table
  (ids 0, 7, 13, 14).

If you redistribute the `.ttf` (e.g. bundle it inside another project), you
must include `OFL.txt` alongside it, as required by OFL clause 2.

## Trademark notice

"Noto" is a trademark of Google LLC. The output font's name retains "Noto"
because the SIL OFL 1.1 license terms attached to the upstream font do not
declare any Reserved Font Names — see clause 3 of OFL and confirm against
the [upstream LICENSE](https://github.com/googlefonts/noto-emoji/blob/main/LICENSE).
This does not grant trademark rights.

**This project is not affiliated with, endorsed by, or sponsored by Google.**
It is an independent compatibility shim built for use with PHPStorm and other
JVM-based applications.
