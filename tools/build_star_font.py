"""Builds fonts/EustaceMono-Regular.ttf: Cascadia Mono plus the 16 pixel-star glyphs Eustace animates.

    python -m pip install fonttools
    python tools/build_star_font.py path/to/CascadiaMono-Regular.ttf

The base font is the static CascadiaMono-Regular.ttf from a Cascadia Code release
(https://github.com/microsoft/cascadia-code/releases, ttf/static/ in the zip). It is licensed under the SIL
Open Font License 1.1 with the reserved name "Cascadia Code", so the result gets its own family name and
ships with fonts/OFL.txt.

Run this only when the star art changes. Eustace itself never needs fontTools: the built font is committed
and installed by "eustace --install-font".

To change the art, edit STAR below. Every glyph is generated from it:
  glyph = STAR_BASE + width_index * len(STAR_HEIGHTS) + height_index
Those three constants are repeated in cli.py; tests/test_star.py fails if the two copies disagree or if the
committed font does not match them.
"""
import os
import sys

# One pixel per character, '#' filled. Must be left-right symmetric (the spin relies on it) and 7 wide.
STAR = (
    "...#...",
    "...#...",
    "#######",
    ".#####.",
    "..###..",
    ".##.##.",
    ".#...#.",
)
STAR_BASE = 0xE900            # Private Use Area; clear of the Powerline (E0A0-E0D4) and Nerd Font icon ranges
STAR_WIDTHS = (7, 5, 3, 1)    # pixel widths of the spin frames: face on, turning, turning, edge on
STAR_HEIGHTS = (0, 2, 4, 5)   # pixels above the baseline for the bounce frames
STAR_FONT_FAMILY = "Eustace Mono"
STAR_FONT_FILE = "EustaceMono-Regular.ttf"
PIXELS_PER_CELL = 8           # the star's pixel is 1/8 of the cell width
# The static Cascadia Mono files leave the PANOSE classification empty; the variable Cascadia Mono in the same
# release has this one (the fourth number, 9, means "monospaced"). Copied so that Windows classifies Eustace
# Mono the way it classifies that font. tests/test_star.py checks the result on a real console in CI.
PANOSE = (2, 11, 6, 9, 2, 0, 0, 2, 0, 4)


def squeeze(rows: tuple[str, ...], width: int) -> list[str]:
    """The grid narrowed to `width` columns the way an old sprite turns: columns are folded toward the centre
    (a pixel survives if any column folded onto it had one), so the result stays symmetric and keeps its height."""
    full = len(rows[0])
    if width == full:
        return list(rows)
    half, new_half = (full - 1) / 2, (width - 1) / 2
    out = []
    for row in rows:
        cells = ["."] * width
        for c, ch in enumerate(row):
            if ch == "#":
                d = c - half
                # round half up, on the distance from the centre, so both sides fold the same way
                folded = int(abs(d) * new_half / half + 0.5) if half else 0
                cells[int(new_half + (folded if d > 0 else -folded))] = "#"
        out.append("".join(cells))
    return out


def outline(rows: list[str]) -> list[list[tuple[int, int]]]:
    """Closed contours around the filled pixels, in pixel units with y up and (0, 0) at the grid's bottom left.
    Filled areas are traced clockwise (TrueType's outer direction), holes come out counter-clockwise, and
    there are no edges between neighbouring pixels, so nothing can show as a seam when the glyph is drawn."""
    h = len(rows)
    edges = set()
    for r, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch != "#":
                continue
            y = h - 1 - r
            for a, b in (((x, y + 1), (x + 1, y + 1)), ((x + 1, y + 1), (x + 1, y)),
                         ((x + 1, y), (x, y)), ((x, y), (x, y + 1))):
                if (b, a) in edges:
                    edges.remove((b, a))   # shared with the neighbouring pixel: not part of the outline
                else:
                    edges.add((a, b))
    out_of: dict = {}
    for a, b in edges:
        out_of.setdefault(a, []).append(b)
    contours = []
    while edges:
        start = min(edges)
        a, b = start
        points = [a]
        while True:
            edges.remove((a, b))
            out_of[a].remove(b)
            if b == start[0]:
                break
            points.append(b)
            dx, dy = b[0] - a[0], b[1] - a[1]
            # Where two pixels touch only at a corner there are two ways on; turning right keeps them as
            # two simple contours instead of one that crosses itself.
            nxt = min(out_of[b], key=lambda n: {(dy, -dx): 0, (dx, dy): 1, (-dy, dx): 2}[(n[0] - b[0], n[1] - b[1])])
            a, b = b, nxt
        # drop points in the middle of a straight run
        n = len(points)
        contours.append([p for i, p in enumerate(points)
                         if (p[0] - points[i - 1][0]) * (points[(i + 1) % n][1] - p[1])
                         != (p[1] - points[i - 1][1]) * (points[(i + 1) % n][0] - p[0])])
    return contours


def glyph_names() -> list[str]:
    return [f"eustace.star.w{w}.h{h}" for w in STAR_WIDTHS for h in STAR_HEIGHTS]


def build(base_path: str, out_path: str) -> None:
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import TTFont

    if any(row != row[::-1] for row in STAR) or len({len(row) for row in STAR}) != 1:
        raise SystemExit("STAR must be rectangular and left-right symmetric.")
    font = TTFont(base_path, recalcTimestamp=False)
    advances = {adv for adv, _ in font["hmtx"].metrics.values() if adv}
    if len(advances) != 1 or not font["post"].isFixedPitch:
        raise SystemExit("The base font is not monospaced.")
    cell = advances.pop()
    px = cell // PIXELS_PER_CELL
    top = (max(STAR_HEIGHTS) + len(STAR)) * px
    if top > font["hhea"].ascent:
        raise SystemExit(f"The highest bounce frame ({top}) would be cut off above the ascender ({font['hhea'].ascent}).")
    taken = [cp for cp in font.getBestCmap() if STAR_BASE <= cp < STAR_BASE + len(glyph_names())]
    if taken:
        raise SystemExit(f"The base font already uses {', '.join(hex(c) for c in taken)}.")

    names = glyph_names()
    i = 0
    for w in STAR_WIDTHS:
        rows = squeeze(STAR, w)
        left = (cell - w * px) // 2
        for h in STAR_HEIGHTS:
            pen = TTGlyphPen(None)
            for contour in outline(rows):
                pts = [(left + x * px, (h + y) * px) for x, y in contour]
                pen.moveTo(pts[0])
                for p in pts[1:]:
                    pen.lineTo(p)
                pen.closePath()
            name = names[i]
            font["glyf"][name] = pen.glyph()   # also appends the name to the glyph order
            font["hmtx"][name] = (cell, left + min(x for c in outline(rows) for x, _ in c) * px)
            for table in font["cmap"].tables:
                if table.isUnicode():
                    table.cmap[STAR_BASE + i] = name
            i += 1
    font["OS/2"].ulUnicodeRange2 |= 1 << (60 - 32)   # Private Use Area
    for field, value in zip(("bFamilyType", "bSerifStyle", "bWeight", "bProportion", "bContrast", "bStrokeVariation",
                             "bArmStyle", "bLetterForm", "bMidline", "bXHeight"), PANOSE):
        setattr(font["OS/2"].panose, field, value)

    base_family = font["name"].getDebugName(1)
    base_version = font["name"].getDebugName(5)
    ps_name = STAR_FONT_FILE.rsplit(".", 1)[0]
    for name_id, value in (
        (1, STAR_FONT_FAMILY), (2, "Regular"), (4, STAR_FONT_FAMILY), (6, ps_name),
        (3, f"{ps_name};Eustace;derived from {base_family}"),
        (5, f"{base_version}; Eustace star glyphs U+{STAR_BASE:04X}-U+{STAR_BASE + len(names) - 1:04X}"),
        (10, f"{base_family} with sixteen pixel-star glyphs added in the Private Use Area for the Eustace CLI. "
             "Not a Microsoft release."),
        (13, "This Font Software is licensed under the SIL Open Font License, Version 1.1."),
    ):
        font["name"].setName(value, name_id, 3, 1, 0x409)
        font["name"].setName(value, name_id, 1, 0, 0)
    for stale in (16, 17, 21, 22):   # typographic family names would still say Cascadia
        font["name"].removeNames(nameID=stale)
    if "DSIG" in font:
        del font["DSIG"]             # the signature covers the original bytes only
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    font.save(out_path)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target = os.path.join(here, "fonts", STAR_FONT_FILE)
    build(sys.argv[1], target)
    print(f"Wrote {target}")
