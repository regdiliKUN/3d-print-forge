import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import trimesh
from app import analysis, repair

m = trimesh.creation.icosphere(subdivisions=3, radius=25)
keep = np.ones(len(m.faces), bool)
keep[np.arange(0, len(m.faces), 40)] = False
m.update_faces(keep)
m.remove_unreferenced_vertices()
m.faces = m.faces[:, ::-1]

out, info = repair.act_fill_holes(m, {})
v = np.asarray(out.vertices); f = np.asarray(out.faces)
e = np.vstack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
es = np.sort(e, axis=1)
key = es[:, 0].astype(np.int64) * len(v) + es[:, 1]
uniq, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
bad = uniq[cnt > 2]
print("nonmanifold edges:", len(bad), "counts", cnt[cnt > 2])
for b in bad:
    a0, b0 = divmod(int(b), len(v))
    print(" edge", a0, b0, "verts", v[a0], v[b0])
    fs = np.flatnonzero(key == b) % len(f)
    print("  faces", fs, f[fs])
    for fi in fs:
        print("   ", f[fi], "centroid", v[f[fi]].mean(axis=0))

# duplicate faces
from collections import Counter
c = Counter(tuple(sorted(x)) for x in f.tolist())
dups = {k: n for k, n in c.items() if n > 1}
print("duplicate faces:", len(dups), list(dups.items())[:5])
