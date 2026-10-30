#!/usr/bin/env python3
"""Convert vscode-pokemon GIFs into PNG frames the BUSY Bar can draw.

The bar's firmware decodes PNG but not GIF, and its front panel is 72x16. So
the converter crops every 32x32 (or 64x64) GIF to the pixels it uses. Then
it scales the sprite to at most 16 px tall. It keeps the GIF's frame timing
in `index.json` next to the frames, so the app can replay the same animation.

    python3 tools/convert_sprites.py --source ../vscode-pokemon/media \
        --out pokemon-companion/sprites --gen 1 --sheet tools/review/gen1_sheet.png

Output layout: <out>/gen<N>/<slug>/<color>/<state>_<facing>_<i>.png plus
<out>/index.json. The optional contact sheet puts the original frame next to
every converted frame, enlarged, so a human can judge the conversion.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageSequence

PANEL_H = 16
COLORS = ("default", "shiny")
STATES = ("idle", "walk")
FACINGS = ("right", "left")
RESAMPLE = {"nearest": Image.NEAREST, "box": Image.BOX}
ALPHA_CUTOFF = 128           # the LEDs have no alpha: a pixel is on or off

SHEET_SCALE = 6              # enlargement of converted frames on the contact sheet
SHEET_ORIG_SCALE = 3         # enlargement of the original frame on the sheet
SHEET_ROWS_PER_PAGE = 40
SHEET_LABEL_W = 170
SHEET_PAD = 6


def load_frames(gif_path):
    """All frames of a GIF as (RGBA image, duration in ms)."""
    with Image.open(gif_path) as im:
        return [(frame.convert("RGBA"), int(frame.info.get("duration", 0)))
                for frame in ImageSequence.Iterator(im)]


def union_bbox(images):
    """Smallest box containing every non-transparent pixel of every image."""
    bbox = None
    for im in images:
        b = im.getbbox()
        if b is None:
            continue
        bbox = b if bbox is None else (min(bbox[0], b[0]), min(bbox[1], b[1]),
                                       max(bbox[2], b[2]), max(bbox[3], b[3]))
    return bbox


def data_path(media):
    """The extension's Pokémon table, next to its media folder."""
    return Path(media).parent / "src" / "common" / "pokemon-data.ts"


DATA_ENTRY = re.compile(r"^  (\w+): \{\s*id: (\d+),\s*name: (?:'([^']*)'|\"([^\"]*)\")", re.M)


def load_data(path):
    """{slug: {"number", "name"}} from the extension's pokemon-data.ts."""
    text = Path(path).read_text(encoding="utf-8")
    return {m.group(1): {"number": int(m.group(2)), "name": m.group(3) or m.group(4)}
            for m in DATA_ENTRY.finditer(text)}


def display_name(slug):
    name = slug
    suffix = ""
    for sex in ("female", "male"):
        if name.endswith("_" + sex):
            name, suffix = name[:-len(sex) - 1], " (%s)" % sex.capitalize()
    return " ".join(part.capitalize() for part in name.split("_")) + suffix


def fit(image, bbox, size, resample):
    """Crop `image` to `bbox`, scale to `size`, and make the alpha binary."""
    cropped = image.crop(bbox)
    if resample == Image.NEAREST:
        scaled = cropped.resize(size, resample)
    else:
        # Area filters must average premultiplied colour, or transparent
        # pixels bleed their (meaningless) RGB into the sprite's edges.
        scaled = cropped.convert("RGBa").resize(size, resample).convert("RGBA")
    px = scaled.load()
    for y in range(scaled.height):
        for x in range(scaled.width):
            r, g, b, a = px[x, y]
            px[x, y] = (r, g, b, 255) if a >= ALPHA_CUTOFF else (0, 0, 0, 0)
    return scaled


def _state_sources(src_dir, color):
    """Which GIF feeds each output state, and whether it must be mirrored."""
    sources = {}
    for state in STATES:
        gif = src_dir / f"{color}_{state}_8fps.gif"
        if not gif.is_file():
            return None
        sources[f"{state}_right"] = (gif, False)
        dedicated = src_dir / f"{color}_{state}_left_8fps.gif"
        sources[f"{state}_left"] = (dedicated, False) if dedicated.is_file() else (gif, True)
    return sources


def convert_pokemon(src_dir, out_dir, gen, resample="nearest", data=None):
    """Convert one pokemon folder. Returns its index entry, or None if it has no GIFs."""
    src_dir = Path(src_dir)
    out_dir = Path(out_dir)
    slug = src_dir.name
    filt = RESAMPLE[resample]
    colors = {}
    for color in COLORS:
        sources = _state_sources(src_dir, color)
        if sources is None:
            continue
        frames = {state: load_frames(gif) for state, (gif, _) in sources.items()}
        bbox = union_bbox(im for seq in frames.values() for im, _ in seq)
        bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        scale = min(1.0, PANEL_H / bh)
        size = (max(1, round(bw * scale)), max(1, round(bh * scale)))
        color_dir = out_dir / f"gen{gen}" / slug / color
        color_dir.mkdir(parents=True, exist_ok=True)
        states = {}
        for state, (gif, mirrored) in sources.items():
            names = []
            durations = []
            previous = None
            for im, duration in frames[state]:
                frame = fit(im, bbox, size, filt)
                if frame.tobytes() == previous:
                    # Some GIFs repeat a frame several times in a row. One
                    # longer frame saves the bar a redraw of the same picture.
                    durations[-1] += duration
                    continue
                previous = frame.tobytes()
                if mirrored:
                    frame = frame.transpose(Image.FLIP_LEFT_RIGHT)
                name = f"{state}_{len(names)}.png"
                frame.save(color_dir / name, optimize=True)
                names.append(name)
                durations.append(duration)
            states[state] = {"frames": names, "durations_ms": durations,
                             "source": gif.name, "mirrored": mirrored}
        colors[color] = {"w": size[0], "h": size[1], "source_size": [bw, bh], "states": states}
    if not colors:
        return None
    entry = {"gen": gen, "name": display_name(slug), "colors": colors}
    known = (data or {}).get(slug)
    if known:
        entry["number"] = known["number"]
        if known["name"].isascii():      # the bar's bitmap fonts are ASCII only
            entry["name"] = known["name"]
    return entry


def convert(source, out, gens, resample="nearest", only=None, sheet=None, data=None):
    """Convert every pokemon of the given generations. Writes index.json and returns the index."""
    source = Path(source)
    out = Path(out)
    index = {}
    for gen in gens:
        gen_dir = source / f"gen{gen}"
        for src_dir in sorted(p for p in gen_dir.iterdir() if p.is_dir()):
            if only and src_dir.name not in only:
                continue
            entry = convert_pokemon(src_dir, out, gen, resample, data)
            if entry is not None:
                index[src_dir.name] = entry
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_text(json.dumps(index, indent=1, sort_keys=True) + "\n")
    if sheet is not None:
        write_sheet(source, out, index, Path(sheet))
    return index


# --- review contact sheet ---------------------------------------------------


def _on_black(im, scale):
    bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
    bg.alpha_composite(im)
    return bg.convert("RGB").resize((im.width * scale, im.height * scale), Image.NEAREST)


def _load_png(path):
    with Image.open(path) as im:
        return im.convert("RGBA")


def _sheet_rows(source, out, index):
    """Per pokemon+colour: label, the original idle and walk first frames, converted frames."""
    for slug, entry in index.items():
        for color, info in entry["colors"].items():
            src_dir = source / f"gen{entry['gen']}" / slug
            color_dir = out / f"gen{entry['gen']}" / slug / color
            originals = [load_frames(src_dir / f"{color}_{state}_8fps.gif")[0][0] for state in STATES]
            cells = [_load_png(color_dir / f) for state in info["states"].values() for f in state["frames"]]
            label = f"{slug} {color}\n{info['source_size'][0]}x{info['source_size'][1]} -> {info['w']}x{info['h']}"
            yield label, originals, cells


def write_sheet(source, out, index, path):
    """Contact sheet pages: original frame beside every converted frame, enlarged."""
    rows = list(_sheet_rows(source, out, index))
    pages = [rows[i:i + SHEET_ROWS_PER_PAGE] for i in range(0, len(rows), SHEET_ROWS_PER_PAGE)] or [[]]
    for page_no, page in enumerate(pages, 1):
        cell_w = max([c.width for _, _, cells in page for c in cells] + [PANEL_H]) * SHEET_SCALE + SHEET_PAD
        row_h = max([o.height * SHEET_ORIG_SCALE for _, os, _ in page for o in os] + [PANEL_H * SHEET_SCALE]) + SHEET_PAD
        max_cells = max([len(cells) for _, _, cells in page] + [1])
        orig_w = max([o.width * SHEET_ORIG_SCALE for _, os, _ in page for o in os] + [1]) + SHEET_PAD
        width = SHEET_LABEL_W + orig_w * len(STATES) + cell_w * max_cells + SHEET_PAD
        img = Image.new("RGB", (width, row_h * len(page) + SHEET_PAD), (40, 40, 40))
        draw = ImageDraw.Draw(img)
        for r, (label, originals, cells) in enumerate(page):
            y = SHEET_PAD + r * row_h
            draw.multiline_text((SHEET_PAD, y), label, fill=(230, 230, 230))
            x = SHEET_LABEL_W
            for o in originals:
                img.paste(_on_black(o, SHEET_ORIG_SCALE), (x, y + row_h - SHEET_PAD - o.height * SHEET_ORIG_SCALE))
                x += orig_w
            for c in cells:
                img.paste(_on_black(c, SHEET_SCALE), (x, y + (PANEL_H - c.height) * SHEET_SCALE))
                x += cell_w
        target = path if page_no == 1 else path.with_name(f"{path.stem}-p{page_no}{path.suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        img.save(target)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--source", required=True, help="path to vscode-pokemon/media")
    p.add_argument("--out", required=True, help="output sprites directory")
    p.add_argument("--gen", type=int, action="append", required=True, help="generation to convert (repeatable)")
    p.add_argument("--resample", choices=sorted(RESAMPLE), default="nearest")
    p.add_argument("--sheet", help="write a review contact sheet PNG here (paged with -pN suffixes)")
    p.add_argument("--only", action="append", help="limit to these slugs (repeatable)")
    p.add_argument("--data", help="the extension's pokemon-data.ts for numbers and names "
                   "(default: next to --source)")
    args = p.parse_args()
    try:
        data = load_data(args.data or data_path(args.source))
    except OSError as exc:
        sys.exit("cannot read the extension's Pokémon data (--data): %s" % exc)
    index = convert(args.source, args.out, args.gen, args.resample, args.only, args.sheet, data)
    frames = sum(len(s["frames"]) for e in index.values() for c in e["colors"].values() for s in c["states"].values())
    print(f"converted {len(index)} pokemon, {frames} frames -> {args.out}")


if __name__ == "__main__":
    main()
