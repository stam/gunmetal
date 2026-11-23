"""Build the scene textures, colour-matched to the reference photo (target-colours.jpg).

* Tiles: the tile images embedded in the Saninet design (MyTiles/icon) give the vein pattern;
  their colour is remapped (per channel, in linear light) to the white-balanced photo sample.
* Wood: cropped straight from the photo (shadow-free part of the sample), white-balanced and
  mirrored into a seamless tile.
* The photo is white-balanced on the white paper label on the light tile (assumed albedo 0.85).

Usage: .venv/bin/python make_textures.py design.xml target-colours.jpg web/textures
"""
import binascii
import io
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image

# Regions in the photo, in full-resolution pixels (x0, y0, x1, y1).
REGIONS = {
    "paper": (2840, 70, 3230, 510),      # white label, used for white balance
    "light": (230, 230, 2540, 1390),     # light tile
    "taupe": (2660, 2660, 3350, 3460),   # taupe tile (right of the faucet)
    "wood": (20, 2000, 980, 4120),       # wood sample, left of the faucet shadow
    "gunmetal": (2310, 2310, 2400, 3000),  # faucet body (brushed, mid-tones)
}
PAPER_ALBEDO = 0.85


def srgb_to_lin(c):
    c = np.asarray(c, dtype=np.float64) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def lin_to_srgb(c):
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055) * 255.0


def region(img, key):
    x0, y0, x1, y1 = REGIONS[key]
    return img[y0:y1, x0:x1].reshape(-1, 3)


def hexc(lin):
    s = lin_to_srgb(lin).round().astype(int)
    return "#%02x%02x%02x" % tuple(s)


def recolor(tex, target_lin):
    """Scale texture per channel (linear) so its median hits the target albedo."""
    lin = srgb_to_lin(tex)
    med = np.median(lin.reshape(-1, 3), axis=0)
    return lin_to_srgb(lin * (target_lin / med)).astype(np.uint8)


def add_grout(tex, tiles_x, tiles_y, px, grout_lin):
    out = srgb_to_lin(tex)
    h, w, _ = out.shape
    g = np.asarray(grout_lin)
    for i in range(tiles_x):
        x = round(i * w / tiles_x)
        out[:, max(0, x - px // 2):x + (px + 1) // 2] = g
    out[:, w - px // 2:] = g
    for j in range(tiles_y):
        y = round(j * h / tiles_y)
        out[max(0, y - px // 2):y + (px + 1) // 2, :] = g
    out[h - px // 2:, :] = g
    return lin_to_srgb(out).astype(np.uint8)


def main():
    design, photo, out = (Path(a) for a in (sys.argv[1:4] if len(sys.argv) > 3
                                            else ("design.xml", "target-colours.jpg", "web/textures")))
    out.mkdir(parents=True, exist_ok=True)
    img = np.asarray(Image.open(photo).convert("RGB"))

    paper = srgb_to_lin(np.percentile(region(img, "paper"), 90, axis=0))
    wb = PAPER_ALBEDO / paper  # per-channel gain photo-linear -> albedo
    albedo = {k: srgb_to_lin(np.median(region(img, k), axis=0)) * wb for k in ("light", "taupe", "wood", "gunmetal")}

    # Tile images from the design
    b = ET.parse(design).getroot().find("bathroom")
    icons = {t.findtext("filename"): Image.open(io.BytesIO(binascii.unhexlify(t.findtext("icon")))).convert("RGB")
             for t in b.find("MyTiles")}
    walls = {pw.findtext("name"): pw.findtext(".//tileID") for pw in b.findall("placedwall")}
    floor = b.findtext("placedfloor//tileID")
    taupe_id = floor
    light_id = next(t for t in icons if t != taupe_id)

    grout_px = 5  # 3 mm on a 600 mm tile at 1024 px
    tiles = {}
    for name, tid, tx in (("light", light_id, None), ("taupe", taupe_id, None)):
        im = icons[tid]
        tx = max(1, round(im.width / im.height))  # 1024x512 icon = two 600x600 tiles side by side
        arr = recolor(np.asarray(im), albedo[name])
        grout = albedo[name] * 0.78
        arr = add_grout(arr, tx, 1, grout_px, grout)
        Image.fromarray(arr).save(out / f"tile_{name}.jpg", quality=92)
        tiles[name] = dict(id=tid, tiles_x=tx, size_mm=[600 * tx, 600])

    # Wood: crop, white balance, mirror into a seamless 2x2 tile, downsize
    x0, y0, x1, y1 = REGIONS["wood"]
    wood = srgb_to_lin(img[y0:y1, x0:x1]) * wb
    wood = lin_to_srgb(wood).astype(np.uint8)
    wood = np.concatenate([wood, wood[:, ::-1]], axis=1)
    wood = np.concatenate([wood, wood[::-1]], axis=0)
    w_img = Image.fromarray(wood).resize((1024, 2048), Image.LANCZOS)
    w_img = w_img.transpose(Image.ROTATE_90)  # grain horizontal (u along the cabinet width)
    w_img.save(out / "wood.jpg", quality=90)
    # Photo sample is ~180 mm wide x ~400 mm tall -> mirrored tile covers 2x that.
    sample_mm = [(x1 - x0) / (y1 - y0) * 400.0, 400.0]

    palette = {
        "tile_light": hexc(albedo["light"]),
        "tile_taupe": hexc(albedo["taupe"]),
        "wood": hexc(albedo["wood"]),
        "gunmetal": hexc(albedo["gunmetal"]),
        "wood_tile_mm": [2 * sample_mm[1], 2 * sample_mm[0]],  # after the 90 degree rotation
        "tiles": tiles,
        "walls": {k: ("taupe" if v == taupe_id else "light") for k, v in walls.items()},
        "floor": "taupe",
    }
    (out / "palette.json").write_text(json.dumps(palette, indent=1))
    print(json.dumps(palette, indent=1))


if __name__ == "__main__":
    main()
