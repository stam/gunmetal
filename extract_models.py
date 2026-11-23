"""Extract the product models used in a Saninet design from the viewer's Unity asset bundles.

The Saninet viewer streams products as UnityFS asset bundles. Each bundle holds a family of
products; the root GameObject is named after the product code (e.g. IVY_610010). This script
walks the root's hierarchy, bakes all transforms, converts Unity's left-handed Y-up frame to
glTF's right-handed Y-up frame, and writes one GLB per product code.

Meshes are named after their material slot ("Body", "Front", "Chrome", "Glass", ...), which is
what the three.js scene uses to assign materials.

Usage: .venv/bin/python extract_models.py design.xml assets/bundles web/models
"""
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import trimesh
import UnityPy
from UnityPy.helpers.MeshHelper import MeshHandler


def quat_to_mat(q):
    x, y, z, w = q.x, q.y, q.z, q.w
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def local_matrix(tr):
    p, q, s = tr.m_LocalPosition, tr.m_LocalRotation, tr.m_LocalScale
    m = np.eye(4)
    m[:3, :3] = quat_to_mat(q) @ np.diag([s.x, s.y, s.z])
    m[:3, 3] = [p.x, p.y, p.z]
    return m


def slot_of(name):
    """'_Body_2' -> 'Body', '_aluminium_1' -> 'aluminium', '_Body_002' -> 'Body'."""
    n = name.lstrip("_")
    parts = n.split("_")
    while len(parts) > 1 and parts[-1].isdigit():
        parts.pop()
    return "_".join(parts)


def mesh_geometry(mesh):
    h = MeshHandler(mesh)
    h.process()
    v = np.asarray(h.m_Vertices, dtype=np.float64).reshape(-1, 3)
    n = np.asarray(h.m_Normals, dtype=np.float64).reshape(-1, 3) if h.m_Normals else None
    uv = np.asarray(h.m_UV0, dtype=np.float64).reshape(-1, 2) if h.m_UV0 else None
    tris = h.get_triangles()  # list per submesh of (a, b, c)
    return v, n, uv, tris


def collect(env, objs, go, parent_m, out, path):
    tr = go.m_Transform.read()
    m = parent_m @ local_matrix(tr)
    if not go.m_IsActive:
        return
    mf = mr = None
    for c in go.m_Component:
        ptr = c.component if hasattr(c, "component") else c[1]
        r = objs.get(ptr.path_id)
        if r is None:
            continue
        if r.type.name == "MeshFilter":
            mf = r.read()
        elif r.type.name == "MeshRenderer":
            mr = r.read()
    if mf is not None and mr is not None and mr.m_Enabled:
        mesh = mf.m_Mesh.read()
        v, n, uv, tris = mesh_geometry(mesh)
        for si, sub in enumerate(tris):
            if not len(sub):
                continue
            out.append(dict(name=slot_of(go.m_Name), layer=path, m=m, v=v, n=n, uv=uv,
                            f=np.asarray(sub, dtype=np.int64).reshape(-1, 3)))
    for ch in tr.m_Children:
        cg = ch.read().m_GameObject.read()
        name = cg.m_Name
        collect(env, objs, cg, m, out, name if name.startswith("MeshContainer") else path)


# Raw mesh data is Z-up, front towards -Y, mirrored (FBX import into left-handed Unity).
# Saninet's design frame for a product is: x = width (centred), +y = depth into the room,
# z = up, i.e. design = (x, -y, z) of raw. three.js/glTF is Y-up: three = (x, z, -y) of design.
# Combined raw -> glTF: (x, y, z) -> (x, z, y); a reflection, so triangle winding flips.
FLIP = np.array([[1.0, 0, 0, 0], [0, 0, 1, 0], [0, 1, 0, 0], [0, 0, 0, 1]])


def to_trimesh(part):
    m = FLIP @ part["m"]
    v = (np.c_[part["v"], np.ones(len(part["v"]))] @ m.T)[:, :3]
    f = part["f"]
    used = np.unique(f)
    remap = -np.ones(len(v), dtype=np.int64)
    remap[used] = np.arange(len(used))
    f = remap[f][:, ::-1]  # mirroring flips winding
    kw = {}
    if part["uv"] is not None:
        kw["visual"] = trimesh.visual.TextureVisuals(uv=part["uv"][used])
    tm = trimesh.Trimesh(vertices=v[used], faces=f, process=False, **kw)
    if part["n"] is not None:
        nm = np.linalg.inv(m[:3, :3]).T
        nn = part["n"][used] @ nm.T
        nn /= np.linalg.norm(nn, axis=1, keepdims=True) + 1e-12
        tm.vertex_normals = nn
    return tm


def main():
    design, bundles, out = (Path(a) for a in (sys.argv[1:4] if len(sys.argv) > 3
                                              else ("design.xml", "assets/bundles", "web/models")))
    out.mkdir(parents=True, exist_ok=True)
    root = ET.parse(design).getroot()
    codes = {o.findtext("databaseobject/objectid")
             for o in root.find("bathroom/OBJECT_CATALOG").findall("OBJECT")}
    found = {}
    for f in sorted(bundles.iterdir()):
        env = UnityPy.load(str(f))
        objs = {o.path_id: o for o in env.objects}
        for o in env.objects:
            if o.type.name != "GameObject" or o.peek_name() not in codes:
                continue
            go = o.read()
            if go.m_Transform.read().m_Father.path_id:
                continue
            parts = []
            # Skip the root's own transform (an inconsistent Unity axis fix); raw data is Z-up.
            for ch in go.m_Transform.read().m_Children:
                cg = ch.read().m_GameObject.read()
                collect(env, objs, cg, np.eye(4), parts, cg.m_Name)
            scene = trimesh.Scene()
            for i, p in enumerate(parts):
                scene.add_geometry(to_trimesh(p), node_name=f"{p['name']}__{i}", geom_name=f"{p['name']}__{i}")
            scene.export(out / f"{go.m_Name}.glb")
            lo, hi = scene.bounds
            found[go.m_Name] = dict(bundle=f.name, slots=sorted({p["name"] for p in parts}),
                                    min=lo.round(4).tolist(), max=hi.round(4).tolist())
    (out / "index.json").write_text(json.dumps(found, indent=1))
    for k, v in sorted(found.items()):
        size = (np.array(v["max"]) - np.array(v["min"])) * 1000
        print(f"{k:14s} size(mm) w{size[0]:6.0f} h{size[1]:6.0f} d{size[2]:6.0f}  "
              f"min {v['min']} max {v['max']}  {v['slots']}")
    missing = codes - set(found) - {c for c in codes if c.startswith("XXXX")}
    if missing:
        print("missing:", sorted(missing))


if __name__ == "__main__":
    main()
