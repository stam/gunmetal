"""Convert a Saninet design XML into web/scene.json for the three.js viewer.

Coordinates: Saninet design space is millimetres, x right, y (negative) into the plan, z up.
three.js space is metres, Y up: three = (x, z, -y) / 1000.

Usage: .venv/bin/python make_scene.py design.xml web/scene.json
"""
import json
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def vec(s):
    return [float(t) for t in s.split("|")]


def to3(x, y, z):
    return [round(x / 1000, 5), round(z / 1000, 5), round(-y / 1000, 5)]


# Material for each Saninet shader code (the design's <shader> per slot).
SHADER_MATERIAL = {
    "UNIB0001": "paint_white",
    "UNIB1973": "wood",
    "UNIB1269": "mineral_white",
    "UNIB2642": "ceramic_white",
    "UNIB0046": "paint_white",
    "ULMA_METAL_Gunmetal": "gunmetal",
    "IVY_GM": "gunmetal",
    "UNIB0275": "glass",
    "UNIB2662": "glass",
    "ULMA01WhiteGlos": "ceramic_white",
    "ULMA0005": "plant_pot",
    "ULMA0021": "plant_leaf",
}
# Fallback material per mesh slot name.
SLOT_MATERIAL = {
    "Body": "paint_white", "Front": "paint_white", "White": "paint_white",
    "Chrome": "steel", "aluminium": "aluminium", "Grey": "grey_matte", "Grijs": "grey_matte",
    "Black": "black_matte", "Zwart": "black_matte", "Glass": "glass", "Sideglas": "glass",
    "Lightglas": "light_diffuser", "Mirror": "mirror", "Ceramic": "ceramic_white",
}
# Product-specific overrides (code -> slot -> material).
OVERRIDES = {
    "INK_1257953": {"Chrome": "gunmetal", "Grey": "grey_matte"},  # handle-less front, dark profiles
    "IVY_610010": {"Black": "black_matte"},
    "INK_1105364": {"Body": "wood"},
    "UNIB1501004": {"Body": "steel", "Chrome": "steel"},
}
OPENINGS = {"UNIB0102004", "XXXX999910002"}
LIGHTS = {"UNIB1501004"}
# Face order of Saninet built elements (verified against which faces touch walls):
# bottom, top, back (wall side), +x side, front (room side), -x side
BOX_FACES = ["bottom", "top", "back", "px", "front", "nx"]


def main():
    src, dst = (Path(a) for a in (sys.argv[1:3] if len(sys.argv) > 2 else ("design.xml", "web/scene.json")))
    b = ET.parse(src).getroot().find("bathroom")
    H = float(b.findtext("wallheight"))
    pts = [vec(v.text)[:2] for v in b.find("walloutlines")]
    X0, X1 = min(p[0] for p in pts), max(p[0] for p in pts)
    Y0, Y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    taupe = b.findtext("placedfloor//tileID")
    finish = lambda tid: "tile_taupe" if tid == taupe else ("tile_light" if tid and tid.startswith("TETO") else "plaster")

    # Walls. Saninet placedwall m/p is (x, ?, -y); match each to a room side.
    sides = {
        "A": dict(a=(X0, Y1), b=(X1, Y1), n=(0, -1)),   # window wall (y = 0)
        "B": dict(a=(X1, Y1), b=(X1, Y0), n=(-1, 0)),   # vanity wall (x = max)
        "C": dict(a=(X1, Y0), b=(X0, Y0), n=(0, 1)),    # door / shower wall
        "D": dict(a=(X0, Y0), b=(X0, Y1), n=(1, 0)),    # toilet wall (x = 0)
    }
    wall_finish = {}
    for pw in b.findall("placedwall"):
        vb = [vec(v.text) for v in pw.find("mesh/vb")]
        length = max(v[0] for v in vb) - min(v[0] for v in vb)
        mx, _, mz = vec(pw.findtext("mesh/pb/p/m/p"))
        along_x = abs(length - (X1 - X0)) < 1
        key = ("A" if abs(mz) < 1 else "C") if along_x else ("B" if abs(mx - X1) < 1 else "D")
        wall_finish[key] = finish(pw.findtext(".//tileID"))

    items, boxes, holes, lights = [], [], {k: [] for k in sides}, []
    for o in b.find("OBJECT_CATALOG").findall("OBJECT"):
        code = o.findtext("databaseobject/objectid")
        w, h, d = (float(o.findtext(k)) for k in ("width", "height", "depth"))
        px, py, pz = vec(o.findtext("transform/p"))
        rz = vec(o.findtext("transform/r"))[2]
        sx, sy, sz = vec(o.findtext("transform/s"))
        th = math.radians(rz)
        ax = (math.cos(th), math.sin(th))
        dx = (-math.sin(th), math.cos(th))
        shaders = {c.tag: c.text for c in o.find("shader")}

        if code in OPENINGS:
            ww = w  # XML size is final; transform/s scales the model to it
            for k, s in sides.items():
                # opening lies on this wall if its origin is on the wall line
                dist = (px - s["a"][0]) * s["n"][0] + (py - s["a"][1]) * s["n"][1]
                if abs(dist) < 5:
                    ux, uy = s["b"][0] - s["a"][0], s["b"][1] - s["a"][1]
                    L = math.hypot(ux, uy)
                    u = ((px - s["a"][0]) * ux + (py - s["a"][1]) * uy) / L
                    holes[k].append(dict(code=code, u0=round(u - ww / 2), u1=round(u + ww / 2),
                                         z0=round(pz), z1=round(pz + h)))
        if code.startswith("XXXX"):
            if code == "XXXX999910002":
                continue  # window: modelled procedurally from its wall hole
            faces = [p.findtext("p/tiles/placedtile/tileID") for p in o.find("mesh/pb").findall("p")]
            cx = px + dx[0] * d / 2
            cy = py + dx[1] * d / 2
            boxes.append(dict(code=code, center=to3(cx, cy, pz + h / 2), size=[w / 1000, h / 1000, d / 1000],
                              rotY=round(th, 5), faces={BOX_FACES[i]: finish(t) for i, t in enumerate(faces)}))
            continue

        mats = {}
        for slot in ("Body", "Front", "Ceramic"):
            if slot in shaders and shaders[slot] in SHADER_MATERIAL:
                mats[slot] = SHADER_MATERIAL[shaders[slot]]
        mats.update(OVERRIDES.get(code, {}))
        item = dict(code=code, pos=to3(px, py, pz), rotY=round(th, 5), scale=[sx, sz, sy], materials=mats)
        items.append(item)
        if code in LIGHTS:
            lights.append(to3(px, py, pz))

    walls = []
    for k, s in sides.items():
        walls.append(dict(id=k, a=to3(*s["a"], 0), b=to3(*s["b"], 0), normal=[s["n"][0], 0, -s["n"][1]],
                          finish=wall_finish.get(k, "tile_light"), holes=holes[k]))
    scene = dict(
        room=dict(size=[(X1 - X0) / 1000, H / 1000, (Y1 - Y0) / 1000], min=to3(X0, Y1, 0)),
        floor="tile_taupe", ceiling="plaster",
        walls=walls, boxes=boxes, items=items, downlights=lights,
        slotMaterials=SLOT_MATERIAL,
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(scene, indent=1))
    print(json.dumps(dict(walls=walls, boxes=boxes), indent=1)[:3000])


if __name__ == "__main__":
    main()
