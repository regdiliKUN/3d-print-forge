"""修复管线探针：构造已知缺陷，打印当前行为。用于开发期定位问题。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import analysis, repair  # noqa: E402


def h(m):
    v, f = np.asarray(m.vertices), np.asarray(m.faces)
    return analysis.edge_histogram(f, len(v)), analysis.signed_volume(v, f), len(f), len(v)


def show(name, m):
    hist, vol, nf, nv = h(m)
    print(f"{name:<28} faces={nf:<6} verts={nv:<6} boundary={hist['boundary']:<4} "
          f"nonmanifold={hist['nonmanifold']:<4} vol={vol:>10.2f} watertight={m.is_watertight}")


def open_box(gap=True):
    m = trimesh.creation.box(extents=[20, 20, 20])
    keep = np.ones(len(m.faces), bool)
    keep[[0, 1]] = False  # 去掉一个面的两个三角
    m.update_faces(keep)
    m.remove_unreferenced_vertices()
    return m


def broken_ball():
    m = trimesh.creation.icosphere(subdivisions=3, radius=25)
    keep = np.ones(len(m.faces), bool)
    keep[np.arange(0, len(m.faces), 40)] = False
    m.update_faces(keep)
    m.remove_unreferenced_vertices()
    m.faces = m.faces[:, ::-1]
    scrap = trimesh.creation.box(extents=[3, 3, 3])
    scrap.apply_translation([60, 0, 0])
    return trimesh.util.concatenate([m, scrap])


def sliver_with_body():
    body = trimesh.creation.box(extents=[10, 10, 10])
    v = np.array([[20, 0, 0], [22, 0, 0], [21, 2, 0]], dtype=float)
    f = np.array([[0, 1, 2]])
    tri = trimesh.Trimesh(vertices=v, faces=f, process=False)
    return trimesh.util.concatenate([body, tri])


def duplicate_faces():
    m = trimesh.creation.box(extents=[10, 10, 10])
    return trimesh.Trimesh(vertices=m.vertices.copy(),
                           faces=np.vstack([m.faces, m.faces]), process=False)


def multi_one_inverted():
    a = trimesh.creation.box(extents=[10, 10, 10])
    a.apply_translation([-20, 0, 0])
    b = trimesh.creation.box(extents=[10, 10, 10])
    b.apply_translation([20, 0, 0])
    b.faces = b.faces[:, ::-1]
    return trimesh.util.concatenate([a, b])


def folded_fan():
    # 两个共享顶点、几乎折叠的三角形，构成自交（共享顶点）
    v = np.array([[0, 0, 0], [10, 0, 0], [0, 10, 0], [1, 1, -1]], dtype=float)
    f = np.array([[0, 1, 2], [0, 2, 3]])
    return trimesh.Trimesh(vertices=v, faces=f, process=False)


def main():
    cases = {
        "open_box": open_box(),
        "broken_ball": broken_ball(),
        "sliver": sliver_with_body(),
        "duplicate_faces": duplicate_faces(),
        "multi_one_inverted": multi_one_inverted(),
        "folded_fan": folded_fan(),
    }
    print("== 输入 ==")
    for k, m in cases.items():
        show(k, m)

    print("\n== fill_holes ==")
    for k in ("open_box", "broken_ball", "sliver"):
        m = cases[k]
        try:
            out, info = repair.act_fill_holes(m, {})
            print(f"-- {k}: {info}")
            show(k, out)
            loops = repair.boundary_loops(np.asarray(out.faces), len(out.vertices))
            print(f"   remaining loops={len(loops)} sizes={[len(x) for x in loops][:10]}")
        except Exception as e:
            print(f"-- {k}: EXC {type(e).__name__}: {e}")

    print("\n== fix_normals ==")
    for k in ("broken_ball", "multi_one_inverted", "open_box"):
        m = cases[k]
        out, info = repair.act_fix_normals(m, {})
        print(f"-- {k}: {info}")
        show(k, out)

    print("\n== self intersections ==")
    for k in ("duplicate_faces", "folded_fan", "broken_ball"):
        m = cases[k]
        r = analysis.find_self_intersections(np.asarray(m.vertices), np.asarray(m.faces))
        print(f"-- {k}: count={r['count']} sampled={r.get('sampled')} cand={r.get('candidate_pairs')}")

    print("\n== analyze broken_ball ==")
    rep = analysis.analyze_mesh(cases["broken_ball"], "broken.stl", {"sample_faces": 200})
    for it in rep["items"]:
        print(f"  {it['id']:<20} {it['severity']:<8} {it['message'][:90]}")


if __name__ == "__main__":
    main()
