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
v = np.asarray(m.vertices); f = np.asarray(m.faces)
h = analysis.edge_histogram(f, len(v))
print("before", h)
loops = repair.boundary_loops(f, len(v))
print("loops", len(loops), "sizes", [len(x) for x in loops], "sum", sum(len(x) for x in loops))
from collections import Counter
ec = Counter()
for ring in loops:
    for i in range(len(ring)):
        a, b = int(ring[i]), int(ring[(i + 1) % len(ring)])
        ec[(min(a, b), max(a, b))] += 1
print("loop edges repeated:", [(k, n) for k, n in ec.items() if n > 1])
print("unique loop edges", len(ec), "expected boundary", h["boundary"])
# check loops touching / duplicate vertices
for i, ring in enumerate(loops):
    if len(set(ring.tolist())) != len(ring):
        print("loop", i, "repeats vertex", ring.tolist())
# check pairs of loops sharing vertices
for i in range(len(loops)):
    for j in range(i+1, len(loops)):
        common = set(loops[i].tolist()) & set(loops[j].tolist())
        if common:
            print("loops", i, j, "share", common)
