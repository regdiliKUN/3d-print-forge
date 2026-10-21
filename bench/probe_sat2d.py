import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
from app import analysis

T1 = np.array([[[0,0,0],[10,0,0],[0,10,0]]], float)
T2 = np.array([[[20,20,0],[30,20,0],[20,30,0]]], float)
n1 = np.array([[0,0,1.0]])
print("direct", analysis._coplanar_overlap_2d(T1, T2, n1, 1e-7))

u = np.array([[1.0,0,0]])
v = np.array([[0,1.0,0]])
P1 = np.stack([np.einsum("kij,kj->ki", T1, u), np.einsum("kij,kj->ki", T1, v)], axis=2)
P2 = np.stack([np.einsum("kij,kj->ki", T2, u), np.einsum("kij,kj->ki", T2, v)], axis=2)
P = np.concatenate([P1,P2], axis=1)
print("P", P)
edges = np.stack([P[:, (i + 1) % 3] - P[:, i] for i in range(3)]
                 + [P[:, 3 + (i + 1) % 3] - P[:, 3 + i] for i in range(3)], axis=1)
axes = np.stack([-edges[:, :, 1], edges[:, :, 0]], axis=2)
alen = np.linalg.norm(axes, axis=2)
axes = axes / np.where(alen > 1e-12, alen, 1.0)[:, :, None]
proj = np.einsum("kij,kij->ki", P, axes)
print("edges", edges)
print("axes", axes)
print("proj", proj)
print("lo1 hi1", proj[:, :3].min(1), proj[:, :3].max(1))
print("lo2 hi2", proj[:, 3:].min(1), proj[:, 3:].max(1))
print("overlap", np.minimum(proj[:, :3].max(1), proj[:, 3:].max(1)) - np.maximum(proj[:, :3].min(1), proj[:, 3:].min(1)))
