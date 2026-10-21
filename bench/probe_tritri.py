import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
from app import analysis

def tri(a, b, c):
    return np.array([[a, b, c]], dtype=float)

cases = []
# 1 相同三角形
cases.append(("identical", tri([0,0,0],[10,0,0],[0,10,0]), tri([0,0,0],[10,0,0],[0,10,0]), True))
# 2 反向重复
cases.append(("opposite", tri([0,0,0],[10,0,0],[0,10,0]), tri([0,0,0],[0,10,0],[10,0,0]), True))
# 3 共面部分重叠
cases.append(("coplanar_overlap", tri([0,0,0],[10,0,0],[0,10,0]), tri([1,1,0],[11,1,0],[1,11,0]), True))
# 4 共面相邻（共享边）
cases.append(("coplanar_adjacent", tri([0,0,0],[10,0,0],[0,10,0]), tri([10,0,0],[0,0,0],[-10,0,0]), False))
# 5 平行不共面
cases.append(("parallel_offset", tri([0,0,0],[10,0,0],[0,10,0]), tri([0,0,5],[10,0,5],[0,10,5]), False))
# 6 真穿插
cases.append(("crossing", tri([0,0,0],[10,0,0],[0,10,0]), tri([2,2,-5],[2,2,5],[8,2,0]), True))
# 7 不相交
cases.append(("apart", tri([0,0,0],[10,0,0],[0,10,0]), tri([20,20,0],[30,20,0],[20,30,0]), False))
# 8 共享一个顶点，折叠穿过
cases.append(("shared_vertex_cross", tri([0,0,0],[10,0,0],[0,10,0]), tri([0,0,0],[5,-5,3],[5,5,-3]), True))
# 9 共享一个顶点，只在顶点接触
cases.append(("shared_vertex_touch", tri([0,0,0],[10,0,0],[0,10,0]), tri([0,0,0],[-10,0,0],[0,-10,0]), False))
# 10 退化三角形
cases.append(("degenerate", tri([0,0,0],[1,0,0],[2,0,0]), tri([0,0,0],[0,1,0],[0,0,1]), False))

for name, a, b, expect in cases:
    hit, cop = analysis.tri_tri_overlap(a, b, tol=1e-7)
    ok = "OK " if bool(hit[0]) == expect else "FAIL"
    print(f"{ok} {name:<22} hit={bool(hit[0])} cop={bool(cop[0])} expect={expect}")
