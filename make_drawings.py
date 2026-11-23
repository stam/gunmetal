"""Generate per-wall elevation drawings (PDF + DXF) from a Saninet design XML.

Usage: .venv/bin/python make_drawings.py design.xml out/
"""
import math
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import ezdxf
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Circle, Polygon, Rectangle

# --------------------------------------------------------------------------
# Catalogue knowledge (names fetched from the Saninet content service)
# --------------------------------------------------------------------------
NAMES = {
    "UNIB0102004": "Deur (naar buiten, rechts)",
    "XXXX999910002": "Raam",
    "XXXX999912001": "Haakse wand / ombouw",
    "XXXX999912002": "Voorzetwand",
    "INK_1257953": "Onderkast 120 (INK 45, houten keerlijst)",
    "INK_0207061": "Wastafel 120 dubbel, 2 kraangaten (INK Aspekt)",
    "INK_1105364": "Spiegelkast 120 dubbel gespiegeld (INK SPK2)",
    "UNIB0801002": "Douchedeur 890 x 2000",
    "UNIB0802004": "Zijwand glas (douche)",
    "IVY_CSB1BB": "Doucheset thermostatisch (IVY Bond)",
    "IVY_610010": "Wastafelmengkraan 1-gats (IVY Bond)",
    "IVY_650040": "Reserverolhouder, 2 rollen (IVY)",
    "IVY_650035": "Toiletrolhouder (IVY)",
    "ULMOU0101001": "Wandcloset",
    "ULMOU0101009": "Closetzitting",
    "UNIB1501004": "Plafondspot",
    "ULMOU0910005": "Plant (decoratie)",
}
OPENINGS = {"UNIB0102004", "XXXX999910002"}
BUILT = {"XXXX999912001", "XXXX999912002"}
LIGHTS = {"UNIB1501004"}
SKIP = {"ULMOU0910005"}  # decoration
# For these items Saninet stores the TOP edge as "distance to floor"
# (verified visually in the 3D viewer: cabinet hangs under basin, toilet bowl
# under its seat). For everything else it is the bottom edge.
TOP_ANCHORED = {"INK_1257953", "INK_0207061", "ULMOU0101001"}

# Shower set: Saninet's height is the thermostat/connection point, not the bottom.
# Bottom offset estimated from the 3D view (thermostat ~31% up the rail) - indicative.
CONNECTION_OFFSET = {"IVY_CSB1BB": 0.315}

INCLUDE_BAND = 250  # mm: items whose nearest face is within this of a wall show on its elevation


@dataclass
class Item:
    code: str
    name: str
    w: float
    h: float
    d: float
    z0: float
    corners: list  # plan footprint, 4 (x, y)
    kind: str
    nr: int = 0
    conn: float = None  # connection height (for items anchored at their connection point)

    @property
    def z1(self):
        return self.z0 + self.h


@dataclass
class Wall:
    letter: str
    title: str
    origin: tuple  # left corner as seen from inside the room
    u: tuple       # direction to the right
    n: tuple       # inward normal
    length: float
    tile: str = ""
    items: list = field(default_factory=list)


def vec(s):
    return [float(t) for t in s.split("|")]


def load(path):
    root = ET.parse(path).getroot()
    b = root.find("bathroom")
    H = float(b.findtext("wallheight"))
    outline = [vec(v.text)[:2] for v in b.find("walloutlines")]
    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    X0, X1, Y0, Y1 = min(xs), max(xs), min(ys), max(ys)
    if len(outline) != 4:
        raise SystemExit("Only rectangular rooms are supported")

    # tiles per wall (placedwall mesh m/p gives which side); just collect tile ids
    tiles = {}
    for t in b.find("MyTiles"):
        tiles[t.findtext("filename")] = f'{t.findtext("x")}x{t.findtext("y")}'

    items = []
    for o in b.find("OBJECT_CATALOG").findall("OBJECT"):
        code = o.findtext("databaseobject/objectid")
        if code in SKIP:
            continue
        w, h, d = (float(o.findtext(k)) for k in ("width", "height", "depth"))
        px, py, pz = vec(o.findtext("transform/p"))
        th = math.radians(vec(o.findtext("transform/r"))[2])
        ax = (math.cos(th), math.sin(th))      # width axis
        dx = (-math.sin(th), math.cos(th))     # depth axis (into room)
        c = []
        for sw, sd in ((-0.5, 0), (0.5, 0), (0.5, 1), (-0.5, 1)):
            c.append((px + ax[0] * w * sw + dx[0] * d * sd, py + ax[1] * w * sw + dx[1] * d * sd))
        z0 = pz - h if code in TOP_ANCHORED else pz
        conn = None
        if code in CONNECTION_OFFSET:
            conn = pz
            z0 = pz - CONNECTION_OFFSET[code] * h
        kind = ("opening" if code in OPENINGS else "built" if code in BUILT
                else "light" if code in LIGHTS else "fixture")
        items.append(Item(code, NAMES.get(code, code), w, h, d, z0, c, kind, conn=conn))

    walls = [
        Wall("A", "Raamwand", (X0, Y1), (1, 0), (0, -1), X1 - X0),
        Wall("B", "Wastafelwand", (X1, Y1), (0, -1), (-1, 0), Y1 - Y0),
        Wall("C", "Deur-/douchewand", (X1, Y0), (-1, 0), (0, 1), X1 - X0),
        Wall("D", "Toiletwand", (X0, Y0), (0, 1), (1, 0), Y1 - Y0),
    ]
    tile_note = {}
    for pw in b.findall("placedwall"):
        m = vec(pw.findtext("mesh/pb/p/m/p"))
        tid = pw.findtext("mesh/pb/p/p/tiles/placedtile/tileID")
        tile_note[(round(m[0]), round(m[2]))] = tid
    # map placed wall start points to our walls
    starts = {("A"): (X0, -Y0), ("B"): (X1, -Y0), ("C"): (X1, -Y1), ("D"): (X0, -Y1)}
    tile_desc = {}
    for i, tid in enumerate(dict.fromkeys(tile_note.values())):
        tile_desc[tid] = f"tegel {'ABCDEF'[i].lower()} 600x600, voeg 3 mm"
    for wl in walls:
        key = tuple(round(v) for v in starts[wl.letter])
        tid = tile_note.get(key)
        # fall back: match by any coordinate equality
        wl.tile = tile_desc.get(tid, "")

    floor_tid = b.findtext("placedfloor/mesh/pb/p/p/tiles/placedtile/tileID")
    floor_tile = tile_desc.get(floor_tid, "600x600")

    nr = 1
    for it in items:
        if it.kind != "light":
            it.nr = nr
            nr += 1
    for wl in walls:
        for it in items:
            if it.kind == "light" or it.code == "ULMOU0101009":
                continue
            pts = it.corners[:2] if it.kind == "opening" else it.corners  # openings: wall face only
            ds = [(cx - wl.origin[0]) * wl.n[0] + (cy - wl.origin[1]) * wl.n[1] for cx, cy in pts]
            if it.kind == "opening" and max(abs(v) for v in ds) > 5:
                continue
            if min(ds) > INCLUDE_BAND:
                continue
            us = [(cx - wl.origin[0]) * wl.u[0] + (cy - wl.origin[1]) * wl.u[1] for cx, cy in pts]
            u0, u1 = max(0, min(us)), min(wl.length, max(us))
            if u1 - u0 < 1:
                continue
            wl.items.append((it, u0, u1, max(0, min(ds))))
    room = dict(X0=X0, X1=X1, Y0=Y0, Y1=Y1, H=H, floor_tile=floor_tile)
    return room, walls, items


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def chain(values, total, min_gap=20):
    vals = sorted(set([0, total] + [round(v) for v in values if 0 <= v <= total]))
    out = [vals[0]]
    for v in vals[1:]:
        if v - out[-1] >= min_gap:
            out.append(v)
        elif v == total:
            out[-1] = v
    return out


STYLE = {
    "opening": dict(ec="#1f5fa8", fc="none", ls="--", lw=0.8),
    "built": dict(ec="#444", fc="#d9d9d9", ls="-", lw=0.8, hatch="////"),
    "fixture": dict(ec="#111", fc="white", ls="-", lw=0.8),
    "light": dict(ec="#b07800", fc="#ffe9a8", ls="-", lw=0.6),
}

SCALE = 20  # 1:20
PREVIEW = None
MM = 1 / 25.4


def axes_at(fig, x_mm, y_mm, w_mm, h_mm, xlim0, ylim0):
    """Axes placed in mm on an A4-landscape figure with an exact 1:SCALE mapping."""
    W, Hh = 297, 210
    ax = fig.add_axes([x_mm / W, y_mm / Hh, w_mm / W, h_mm / Hh])
    ax.set_xlim(xlim0, xlim0 + w_mm * SCALE)
    ax.set_ylim(ylim0, ylim0 + h_mm * SCALE)
    ax.set_aspect("equal")
    ax.axis("off")
    return ax


def hdim(ax, x0, x1, y, txt=None, fs=5.5):
    ax.annotate("", (x0, y), (x1, y), arrowprops=dict(arrowstyle="<|-|>", lw=0.4, mutation_scale=4, color="#c00"))
    ax.text((x0 + x1) / 2, y + 25, txt or f"{x1 - x0:.0f}", ha="center", va="bottom", fontsize=fs, color="#c00")


def vdim(ax, y0, y1, x, txt=None, fs=5.5):
    ax.annotate("", (x, y0), (x, y1), arrowprops=dict(arrowstyle="<|-|>", lw=0.4, mutation_scale=4, color="#c00"))
    ax.text(x - 25, (y0 + y1) / 2, txt or f"{y1 - y0:.0f}", ha="right", va="center", rotation=90, fontsize=fs, color="#c00")


def frame(fig, title, subtitle):
    fig.text(12 / 297, 200 / 210, title, fontsize=13, weight="bold", va="top")
    fig.text(12 / 297, 192 / 210, subtitle, fontsize=7.5, va="top", color="#444")
    fig.text(285 / 297, 6 / 210, "Schaal 1:20 op A4 liggend  |  maten in mm  |  bron: Saninet ontwerp",
             fontsize=6, ha="right", color="#666")


def table(fig, rows, x_mm, y_top_mm, cols):
    W, H = 297, 210
    y = y_top_mm
    xs = [x_mm]
    for _, wmm in cols:
        xs.append(xs[-1] + wmm)
    for i, (hdr, _) in enumerate(cols):
        fig.text(xs[i] / W, y / H, hdr, fontsize=5.8, weight="bold", va="top")
    y -= 4.2
    fig.add_artist(plt.Line2D([x_mm / W, xs[-1] / W], [(y + 0.8) / H] * 2, lw=0.4, color="#333"))
    for r in rows:
        for i, v in enumerate(r):
            fig.text(xs[i] / W, y / H, str(v), fontsize=5.6, va="top", wrap=False)
        y -= 4.0
    return y


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
def draw_plan(pdf, room, walls, items):
    fig = plt.figure(figsize=(297 * MM, 210 * MM))
    X0, X1, Y0, Y1 = room["X0"], room["X1"], room["Y0"], room["Y1"]
    frame(fig, "Plattegrond badkamer",
          f"Binnenmaat {X1 - X0:.0f} x {Y1 - Y0:.0f} mm, wandhoogte {room['H']:.0f} mm. "
          f"Vloer: {room['floor_tile']}. Letters A-D verwijzen naar de wandaanzichten.")
    ax = axes_at(fig, 8, 12, (X1 - X0 + 1050) / SCALE, (Y1 - Y0 + 1100) / SCALE, X0 - 450, Y0 - 650)
    ax.add_patch(Rectangle((X0, Y0), X1 - X0, Y1 - Y0, fc="#fafafa", ec="black", lw=1.6))
    for it in sorted(items, key=lambda i: {"built": 0, "fixture": 1, "opening": 2, "light": 3}[i.kind]):
        if it.code == "ULMOU0101009":
            continue
        st = STYLE[it.kind]
        if it.kind == "light":
            cx = sum(c[0] for c in it.corners) / 4
            cy = sum(c[1] for c in it.corners) / 4
            ax.add_patch(Circle((cx, cy), 45, fc=st["fc"], ec=st["ec"], lw=0.6))
            continue
        if it.kind == "opening":
            # draw on the wall line, outward
            p0, p1 = it.corners[0], it.corners[1]
            ax.plot([p0[0], p1[0]], [p0[1], p1[1]], color="white", lw=3.2, solid_capstyle="butt")
            ax.plot([p0[0], p1[0]], [p0[1], p1[1]], color=st["ec"], lw=1.2, ls="--")
            mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
            nx, ny = it.corners[3][0] - it.corners[0][0], it.corners[3][1] - it.corners[0][1]
            L = math.hypot(nx, ny) or 1
            ax.text(mx - nx / L * 140, my - ny / L * 140, str(it.nr), fontsize=6, ha="center", va="center",
                    color=st["ec"], weight="bold")
            continue
        ax.add_patch(Polygon(it.corners, closed=True, fc=st["fc"], ec=st["ec"], ls=st["ls"],
                             lw=st["lw"], hatch=st.get("hatch")))
        cx = sum(c[0] for c in it.corners) / 4
        cy = sum(c[1] for c in it.corners) / 4
        ax.text(cx, cy, str(it.nr), fontsize=5.5, ha="center", va="center",
                bbox=dict(boxstyle="circle,pad=0.15", fc="white", ec="#333", lw=0.3))
    # wall letters
    mid = {"A": ((X0 + X1) * 0.7, Y1 + 260), "B": (X1 + 260, (Y0 + Y1) * 0.5),
           "C": ((X0 + X1) * 0.3, Y0 - 260), "D": (X0 - 260, (Y0 + Y1) * 0.5)}
    for wl in walls:
        x, y = mid[wl.letter]
        ax.text(x, y, wl.letter, fontsize=11, weight="bold", ha="center", va="center",
                bbox=dict(boxstyle="circle,pad=0.25", fc="white", ec="black", lw=0.8))
    hdim(ax, X0, X1, Y0 - 520)
    vdim(ax, Y0, Y1, X1 + 520)
    rows = [(it.nr, it.name[:40], f"{it.w:.0f}x{it.d:.0f}", it.code) for it in items
            if it.kind != "light" and it.code != "ULMOU0101009"]
    rows.append(("o", f"Plafondspot ({sum(1 for i in items if i.kind == 'light')}x)", "", "UNIB1501004"))
    y = table(fig, rows, 205, 178, [("Nr", 5), ("Omschrijving", 51), ("BxD", 14), ("Code", 18)])
    # light positions
    lrows = []
    for it in items:
        if it.kind == "light":
            cx = sum(c[0] for c in it.corners) / 4
            cy = sum(c[1] for c in it.corners) / 4
            lrows.append(("o", f"vanaf wand D: {cx - X0:.0f}", f"vanaf wand A: {Y1 - cy:.0f}", ""))
    fig.text(205 / 297, (y - 4) / 210, "Plafondspots (hart)", fontsize=5.8, weight="bold", va="top")
    table(fig, lrows, 205, y - 8, [("", 5), ("", 30), ("", 30), ("", 1)])
    pdf.savefig(fig)
    if PREVIEW:
        fig.savefig(PREVIEW / f"{fig.texts[0].get_text()[:6].replace(' ', '_')}.png", dpi=170)
    plt.close(fig)


def draw_elevation(pdf, room, wl):
    H = room["H"]
    fig = plt.figure(figsize=(297 * MM, 210 * MM))
    frame(fig, f"Wand {wl.letter} - {wl.title}",
          f"Aanzicht vanuit de badkamer. Breedte {wl.length:.0f} mm, hoogte {H:.0f} mm. "
          f"Wandtegels: {wl.tile or 'zie ontwerp'}.  Links = vanaf linkerhoek, Onder/Boven = vanaf vloer, Afst. = afstand tot de wand.")
    ax = axes_at(fig, 8, 22, (wl.length + 900) / SCALE, 160, -250, -650)
    ax.add_patch(Rectangle((0, 0), wl.length, H, fc="#fbfbf7", ec="black", lw=1.4))
    ax.plot([-120, wl.length + 120], [0, 0], color="black", lw=1.8)
    order = {"built": 0, "opening": 1, "fixture": 2}
    for it, u0, u1, dist in sorted(wl.items, key=lambda t: (order[t[0].kind], -t[3])):
        st = STYLE[it.kind]
        ax.add_patch(Rectangle((u0, it.z0), u1 - u0, it.h, fc=st["fc"], ec=st["ec"], ls=st["ls"],
                               lw=st["lw"], hatch=st.get("hatch"), alpha=0.95))
    for it, u0, u1, dist in wl.items:
        if it.conn is not None:
            ax.plot([u0 - 60, u1 + 60], [it.conn, it.conn], color="#c00", lw=0.7, ls="-.")
            ax.text(u1 + 70, it.conn, f"aansluiting {it.conn:.0f}", fontsize=5, color="#c00", va="center")
    # numbers on top
    for it, u0, u1, dist in wl.items:
        ax.text((u0 + u1) / 2, it.z0 + it.h / 2, str(it.nr), fontsize=5.5, ha="center", va="center",
                bbox=dict(boxstyle="circle,pad=0.15", fc="white", ec="#333", lw=0.3), zorder=5)
    # dimension chains (ignore tiny items)
    big = [t for t in wl.items if (t[2] - t[1]) >= 120 or t[0].kind == "opening"]
    hs = chain([v for t in big for v in (t[1], t[2])], wl.length)
    for a, b in zip(hs, hs[1:]):
        hdim(ax, a, b, -220, fs=5)
    hdim(ax, 0, wl.length, -470)
    vs = chain([v for t in big for v in ((t[0].z0, t[0].z1) if t[0].conn is None else (t[0].conn,))], H)
    for a, b in zip(vs, vs[1:]):
        vdim(ax, a, b, wl.length + 300, fs=5)
    vdim(ax, 0, H, wl.length + 560)
    # corner labels
    ax.text(0, H + 80, "linkerhoek", fontsize=5.5, color="#666")
    rows = []
    for it, u0, u1, dist in sorted(wl.items, key=lambda t: (t[1], t[0].z0)):
        if it.conn is not None:
            rows.append((it.nr, it.name[:42], f"{u1 - u0:.0f}x{it.h:.0f}", f"{u0:.0f}", f"aansl. {it.conn:.0f}",
                         "", f"{dist:.0f}"))
        else:
            rows.append((it.nr, it.name[:42], f"{u1 - u0:.0f}x{it.h:.0f}", f"{u0:.0f}", f"{it.z0:.0f}",
                         f"{it.z1:.0f}", f"{dist:.0f}"))
    table(fig, rows, 197, 178, [("Nr", 5), ("Omschrijving", 44), ("BxH", 13), ("Links", 9), ("Onder", 9),
                               ("Boven", 9), ("Afst.", 9)])
    fig.text(197 / 297, 40 / 210,
             "Let op: onderkant onderkast, wastafel en closet afgeleid\nuit Saninet (bovenkant-maat). "
             "Doucheset: alleen de aansluit-\nhoogte is exact, de getekende stang is indicatief.\n"
             "Controleer leidingwerk (afvoer, aansluitingen)\nmet de installateur.",
             fontsize=5.5, color="#a40", va="top")
    pdf.savefig(fig)
    if PREVIEW:
        fig.savefig(PREVIEW / f"{fig.texts[0].get_text()[:6].replace(' ', '_')}.png", dpi=170)
    plt.close(fig)


# --------------------------------------------------------------------------
# DXF (1 unit = 1 mm, all views side by side in modelspace)
# --------------------------------------------------------------------------
def write_dxf(path, room, walls, items):
    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    for name, color in (("WAND", 7), ("BOUWKUNDIG", 8), ("SANITAIR", 5), ("OPENING", 4),
                        ("VERLICHTING", 2), ("MATEN", 1), ("TEKST", 7)):
        doc.layers.add(name, color=color)
    msp = doc.modelspace()
    dimstyle = "EZDXF"
    ds = doc.dimstyles.get(dimstyle)
    ds.dxf.dimtxt = 60
    ds.dxf.dimasz = 40
    ds.dxf.dimexe = 30
    ds.dxf.dimexo = 20
    ds.dxf.dimlfac = 1   # drawing units are mm
    ds.dxf.dimdec = 0
    ds.dxf.dimpost = ""
    ds.dxf.dimgap = 15

    def rect(x, y, w, h, layer):
        msp.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True, dxfattribs={"layer": layer})

    def text(s, x, y, hgt=60, layer="TEKST", align="MIDDLE_CENTER"):
        t = msp.add_text(s, height=hgt, dxfattribs={"layer": layer})
        t.set_placement((x, y), align=getattr(ezdxf.enums.TextEntityAlignment, align))

    layer_of = {"built": "BOUWKUNDIG", "fixture": "SANITAIR", "opening": "OPENING", "light": "VERLICHTING"}
    X0, X1, Y0, Y1, H = room["X0"], room["X1"], room["Y0"], room["Y1"], room["H"]

    # plan at origin
    ox, oy = 0, 0
    rect(X0, Y0, X1 - X0, Y1 - Y0, "WAND")
    text("PLATTEGROND", (X0 + X1) / 2, Y1 + 700, 120)
    for it in items:
        if it.code == "ULMOU0101009":
            continue
        if it.kind == "light":
            cx = sum(c[0] for c in it.corners) / 4
            cy = sum(c[1] for c in it.corners) / 4
            msp.add_circle((cx, cy), 45, dxfattribs={"layer": "VERLICHTING"})
            continue
        pts = it.corners[:2] if it.kind == "opening" else it.corners
        msp.add_lwpolyline(pts, close=it.kind != "opening", dxfattribs={"layer": layer_of[it.kind]})
        cx = sum(c[0] for c in it.corners) / 4
        cy = sum(c[1] for c in it.corners) / 4
        text(str(it.nr), cx, cy, 50)
    msp.add_linear_dim(base=(X0, Y0 - 400), p1=(X0, Y0), p2=(X1, Y0), dimstyle=dimstyle,
                       dxfattribs={"layer": "MATEN"}).render()
    msp.add_linear_dim(base=(X1 + 400, Y0), p1=(X1, Y0), p2=(X1, Y1), angle=90, dimstyle=dimstyle,
                       dxfattribs={"layer": "MATEN"}).render()
    mid = {"A": ((X0 + X1) / 2, Y1 + 250), "B": (X1 + 250, (Y0 + Y1) / 2),
           "C": ((X0 + X1) / 2, Y0 - 250), "D": (X0 - 250, (Y0 + Y1) / 2)}
    for wl in walls:
        text(wl.letter, *mid[wl.letter], 150)

    # elevations to the right of the plan
    ex = X1 + 1500
    for wl in walls:
        rect(ex, 0, wl.length, H, "WAND")
        text(f"WAND {wl.letter} - {wl.title.upper()}", ex + wl.length / 2, H + 300, 120)
        for it, u0, u1, dist in wl.items:
            rect(ex + u0, it.z0, u1 - u0, it.h, layer_of[it.kind])
            text(str(it.nr), ex + (u0 + u1) / 2, it.z0 + it.h / 2, 50)
        big = [t for t in wl.items if (t[2] - t[1]) >= 120 or t[0].kind == "opening"]
        hs = chain([v for t in big for v in (t[1], t[2])], wl.length)
        for a, b in zip(hs, hs[1:]):
            msp.add_linear_dim(base=(ex, -250), p1=(ex + a, 0), p2=(ex + b, 0), dimstyle=dimstyle,
                               dxfattribs={"layer": "MATEN"}).render()
        msp.add_linear_dim(base=(ex, -550), p1=(ex, 0), p2=(ex + wl.length, 0), dimstyle=dimstyle,
                           dxfattribs={"layer": "MATEN"}).render()
        vs = chain([v for t in big for v in ((t[0].z0, t[0].z1) if t[0].conn is None else (t[0].conn,))], H)
        for a, b in zip(vs, vs[1:]):
            msp.add_linear_dim(base=(ex + wl.length + 250, 0), p1=(ex + wl.length, a), p2=(ex + wl.length, b),
                               angle=90, dimstyle=dimstyle, dxfattribs={"layer": "MATEN"}).render()
        msp.add_linear_dim(base=(ex + wl.length + 550, 0), p1=(ex + wl.length, 0), p2=(ex + wl.length, H),
                           angle=90, dimstyle=dimstyle, dxfattribs={"layer": "MATEN"}).render()
        # legend under the elevation
        y = -900
        for it, u0, u1, dist in sorted(wl.items, key=lambda t: (t[1], t[0].z0)):
            text(f"{it.nr}. {it.name}  {u1 - u0:.0f}x{it.h:.0f}  links {u0:.0f}  onder {it.z0:.0f}  "
                 f"uit wand {dist:.0f}", ex, y, 45, align="LEFT")
            y -= 90
        ex += wl.length + 1800
    doc.saveas(path)


def main():
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "design.xml")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "output")
    out.mkdir(exist_ok=True)
    global PREVIEW
    PREVIEW = out / "preview"
    PREVIEW.mkdir(exist_ok=True)
    room, walls, items = load(src)
    with PdfPages(out / "badkamer_wandaanzichten.pdf") as pdf:
        draw_plan(pdf, room, walls, items)
        for wl in walls:
            draw_elevation(pdf, room, wl)
    write_dxf(out / "badkamer_wandaanzichten.dxf", room, walls, items)
    for wl in walls:
        print(wl.letter, wl.title, wl.tile, [(t[0].nr, t[0].code, round(t[1]), round(t[2]), round(t[0].z0)) for t in wl.items])


if __name__ == "__main__":
    main()
