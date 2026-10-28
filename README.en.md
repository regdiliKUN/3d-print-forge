# 3D Print Forge

An all-in-one local web toolkit that takes an STL from import to a print-ready part. Step-by-step wizard, seven-point mesh inspection, automated repair, hollowing with drain holes, support generation, parameter advice, and batch processing — all in one browser UI.

> Chinese documentation: [README.md](README.md)

## Quick start

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe run.py          # → http://127.0.0.1:8000
```

Or with Docker:

```bash
docker compose up --build                # → http://127.0.0.1:8000
```

Drag an STL into the browser window. The original file is never modified — every operation works on a copy.

## Six-step workflow

| Step | What it does | Why it has to happen here |
|---|---|---|
| Import | Creates a working copy | Everything downstream stays revertible |
| Inspect | Seven-point health check | See all problems before fixing any |
| Repair | Fill holes / fix normals / remove debris | Boolean ops require closed manifolds |
| Process | Decimate / hollow / drill / supports / orient | Orient before drilling, or holes end up on the side |
| Advise | Material / layer height / infill / temperature | Every value is derived from geometry, with reasoning |
| Export | STL / 3MF / OBJ / PLY / config / report / log | Configs become reusable templates |

## Inspection and repair

| Check | Basis | Repair |
|---|---|---|
| Watertightness | Every edge shared by exactly two faces | `fill_holes` — boundary loops + ear clipping |
| Normals | Signed volume sign, degenerate face count | `fix_normals` — per-connected-component |
| Wall thickness | Ray cast along face normals to the far side | `thicken` — voxel dilation |
| Non-manifold edges | Edges shared by 3+ faces | `merge_vertices` |
| Self-intersection | 11-axis SAT + near-coplanar exclusion | `resolve_self_intersection` — voxel remesh |
| Isolated shells | Connected components by shared vertices | `remove_small` — keeps cavities |
| Units | Bounding box magnitude + 25.4 multiple match | `rescale` by inferred factor |

## Visualisation

| Feature | How | Use |
|---|---|---|
| Thickness heatmap | Click "壁厚热力" | Red→green over 0–1.5 mm; spots that will under-cure |
| Overhang heatmap | Click "悬垂热力" | Red→green over 0–90°; red needs supports |
| Slice layer preview | Drag the layer slider | Real cross-section contours and area per layer |
| Section profile | Click "扫描 40 层" | Cross-section area vs height; sudden narrowing = weak point |
| Cross-section | Drag the clip slider | Real-time cutaway of internal cavities |
| Part colouring | Click "部件分色" | Each connected component in a distinct colour |
| Normal arrows | Click "法线" | Arrows pointing inward mean flipped normals |
| Problem markers | Click "标出问题" | Red = self-intersection, orange = holes, purple = debris |
| Support preview | Click "支撑预览" | Orange lines show exactly where supports will land |
| Quality score | Automatic after inspection | 0–100 plus A/B/C/D grade with per-item bars |
| Measurement | Click "测量" then two points | Distance and XYZ deltas |

## Performance

Benchmarked on a 20,480-face mesh (`tests/bench.py`):

| Stage | Time | Memory |
|---|---|---|
| Binary STL load | 24 ms | 1.2 MB |
| ASCII STL load | 85 ms | 0.5 MB |
| Inspection (7 checks, defaults) | **684 ms** | 2.8 MB |
| Thickness, 6000 samples (uncapped) | 20,995 ms | 20.1 MB |
| Thickness, 6000 samples (capped 12 mm) | **1,303 ms** | 1.7 MB |
| Hollow 2 mm | 882 ms | 5.5 MB |

Thickness scanning originally used `trimesh`'s ray, which builds a full R×F AABB matrix — 6000 × 20480 is 120M float64s, **4.7 GB and 46 seconds**. Replaced with an rtree candidate lookup + Möller–Trumbore plus ray-length capping: 1.3 s / 20 MB.

## Notable implementation traps

These are real bugs hit during development, not theory:

- **`trimesh.repair.fill_holes` fails silently.** Returns `False` and does nothing on most broken meshes, even with `mapbox-earcut` installed. Hole filling is implemented from scratch: boundary edges → directed loops → Newell normal → projected ear clipping → orientation matched to neighbouring faces.
- **Filling a hole does not guarantee consistent winding.** On non-planar holes these are different properties, and inconsistent winding makes `is_volume` false, which makes manifold booleans refuse the input. A BFS orientation pass runs after every fill.
- **marching cubes has no guaranteed orientation.** Cavity normals are decided by the sign of the computed volume.
- **A mesh that fills its own bounding box breaks marching cubes** (thin plates, cubes). One layer of empty voxels is padded around the grid.
- **SAT alone over-reports self-intersection.** Triangles are zero-thickness; coplanar neighbours always overlap in the normal direction. Near-coplanar pairs are excluded.
- **A cached rtree index keyed by `id(verts)` is a landmine.** `id()` is a memory address, and CPython reuses addresses after GC — the cache returned another model's index and every ray missed. The cache now stores a reference and verifies identity.
- **Support sampling by face centroid under-samples low-poly meshes.** A 40×40 overhang made of two triangles produced two support columns. Sampling is now area-weighted with barycentric placement inside triangles.
- **"Is there solid below" probes must offset along the normal.** Offsetting straight down burrows inside the solid and the ray hits the face it started from.
- **Vertex colours must use `min` aggregation for thickness.** A thin plate's side faces cast rays across the full plate width; averaging washes 0.6 mm out to 24 mm and the heatmap goes all green.

## API

```
POST /api/upload                        Import (multipart, multiple files)
GET  /api/projects                      List projects
PATCH /api/projects/{id}                Rename / favourite / tag
POST /api/projects/{id}/analyze         Inspect        → job
POST /api/projects/{id}/repair          Repair         → job
POST /api/projects/{id}/process         Process        → job
POST /api/projects/{id}/split           Split          → job
POST /api/projects/{id}/pack            Nest on plate  → job
POST /api/projects/{id}/boolean         Union / difference / intersection
POST /api/projects/{id}/merge           Merge meshes
POST /api/projects/{id}/rotate          Rotate by axis
POST /api/projects/{id}/revert          Roll back to a history entry
POST /api/projects/{id}/checkout        Point working copy at a file
GET  /api/projects/{id}/mesh            Preview mesh (base64 arrays)
GET  /api/projects/{id}/heatmap         Scalar field (thickness/overhang/components)
GET  /api/projects/{id}/section         Cross-section at a height
GET  /api/projects/{id}/section-profile Area vs height over N layers
GET  /api/projects/{id}/issues          Problem locations for 3D markers
POST /api/projects/{id}/preview-supports  Support segments without generating them
GET  /api/projects/{id}/diff            Compare original vs current
GET  /api/projects/{id}/advice          Parameter recommendations
GET  /api/projects/{id}/report.html     Self-contained HTML report
GET  /api/projects/{id}/download        ?kind=stl|3mf|obj|ply|config|slicer|report|log
POST /api/batch                         Batch process
GET  /api/batch/report.csv              Batch results as CSV
GET  /api/batch/export.zip              Bundle models into a zip
GET  /api/jobs                          Job list
POST /api/jobs/{id}/pause|resume|cancel
GET  /api/system/resources              Live CPU / memory / GPU
GET  /api/workflow                      Wizard steps and parameter docs
GET  /api/logs                          Recent logs with trace ids
```

### Environment variables

```bash
TPF_MAX_WORKERS=4          # concurrent jobs
TPF_THREAD_WORKERS=8       # threads per job
TPF_MEMORY_LIMIT_MB=8192   # process memory ceiling
TPF_CHUNK_FACES=200000     # streaming load chunk size
TPF_WALL_WARN=0.8          # wall thickness warning line (mm)
TPF_OVERHANG_DEG=45        # overhang threshold
```

Every response carries an `X-Trace-Id` header; the same id appears in `data/logs/app.log`.

## Tests

```bash
.venv/Scripts/python.exe -m pytest tests/ -q     # unit tests
.venv/Scripts/python.exe tests/e2e.py            # end-to-end over HTTP
.venv/Scripts/python.exe tests/ui_features.py    # browser smoke (Playwright)
.venv/Scripts/python.exe tests/bench.py          # performance benchmark
```

CI runs on GitHub Actions across Python 3.11 and 3.12: unit tests, end-to-end, and a service smoke start.

## Known limitations

- Ear clipping assumes the hole boundary is roughly planar. Badly twisted holes produce poor patches — fix those in the modelling tool.
- Voxel operations (hollow, thicken, remesh) slightly round sharp edges; precision is bounded by the voxel pitch.
- Self-intersection uses KD-tree local candidates, so it is an approximation in theory — in practice self-intersections are always local.
- Supports are straight frustums; no tree supports or contact-point refinement. Complex parts still need tuning in the slicer.
- The 3D preview loads Three.js from a CDN; offline environments degrade to a notice, everything else keeps working.
