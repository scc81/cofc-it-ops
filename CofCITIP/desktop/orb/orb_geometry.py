"""
orb_geometry.py — procedural faceted icosahedron mesh for the presence orb
==========================================================================
CofCITIP — JARVIS Phase 3 desktop UI (Session C)

Qt Quick 3D ships no icosahedron primitive (only #Sphere/#Cube/#Cone/...), and a
#Sphere is exactly the "smooth-sphere-with-extra-triangles" the design doc says
to avoid. So the orb mesh is generated here as a QQuick3DGeometry and exposed to
QML (via @QmlElement) as `IcosahedronGeometry`, usable as:

    import JarvisOrb
    Model { geometry: IcosahedronGeometry { subdivisions: 1; radius: 90 } }

Faceting is the whole point: the buffer is a NON-indexed triangle list where the
three vertices of every face carry that face's FLAT normal (vertices are NOT
shared between faces). That gives genuine per-facet flat shading — each triangle
reads as a distinct facet — instead of the smooth interpolated normals you'd get
from an indexed sphere.

`subdivisions` controls geodesic detail:
  0 -> 20 faces  (the raw icosahedron — very crude)
  1 -> 80 faces  (DEFAULT — clearly faceted, still obviously low-poly)
  2 -> 320 faces (starts to read smooth; past the "deliberately faceted" intent)
Level 1 is the default: distinctly faceted without looking like a chopped sphere.

Cheap to build (a few hundred triangles) and built once per property change — it
never competes with Ollama for the A2000's 6GB at runtime.
"""

from __future__ import annotations

import math
import struct

from PySide6.QtCore import Property, Signal
from PySide6.QtGui import QVector3D
from PySide6.QtQml import QmlElement
from PySide6.QtQuick3D import QQuick3DGeometry

# QML registration target: `import JarvisOrb` exposes the @QmlElement types below.
QML_IMPORT_NAME = "JarvisOrb"
QML_IMPORT_MAJOR_VERSION = 1


# ── icosahedron generation (pure math, no Qt) ──────────────────────────────────
def _icosahedron() -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """12 vertices / 20 faces of a regular icosahedron (unit-ish, normalised later)."""
    t = (1.0 + math.sqrt(5.0)) / 2.0
    verts = [
        (-1,  t,  0), (1,  t,  0), (-1, -t,  0), (1, -t,  0),
        (0, -1,  t), (0,  1,  t), (0, -1, -t), (0,  1, -t),
        (t,  0, -1), (t,  0,  1), (-t,  0, -1), (-t,  0,  1),
    ]
    faces = [
        (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
        (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
        (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
        (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1),
    ]
    return verts, faces


def _subdivide(verts, faces):
    """One geodesic subdivision: each triangle -> 4, midpoints cached per edge so
    adjacent faces share the same new vertex position (the projection to the sphere
    happens at build time, not here)."""
    verts = list(verts)
    mid_cache: dict[tuple[int, int], int] = {}

    def midpoint(i: int, j: int) -> int:
        key = (i, j) if i < j else (j, i)
        cached = mid_cache.get(key)
        if cached is not None:
            return cached
        vi, vj = verts[i], verts[j]
        verts.append(((vi[0] + vj[0]) / 2.0, (vi[1] + vj[1]) / 2.0, (vi[2] + vj[2]) / 2.0))
        idx = len(verts) - 1
        mid_cache[key] = idx
        return idx

    new_faces = []
    for (a, b, c) in faces:
        ab, bc, ca = midpoint(a, b), midpoint(b, c), midpoint(c, a)
        new_faces += [(a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)]
    return verts, new_faces


def _norm(v):
    length = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
    return (v[0] / length, v[1] / length, v[2] / length)


def _scale(v, s):
    return (v[0] * s, v[1] * s, v[2] * s)


def _face_normal(a, b, c):
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    return (nx / length, ny / length, nz / length)


@QmlElement
class IcosahedronGeometry(QQuick3DGeometry):
    """A faceted (flat-normal) geodesic icosahedron, rebuilt when radius or
    subdivisions change."""

    radiusChanged = Signal()
    subdivisionsChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._radius = 90.0
        self._subdivisions = 1
        self._rebuild()

    # ── radius property ──
    def _get_radius(self) -> float:
        return self._radius

    def _set_radius(self, r: float) -> None:
        r = float(r)
        if r != self._radius:
            self._radius = r
            self._rebuild()
            self.radiusChanged.emit()

    radius = Property(float, _get_radius, _set_radius, notify=radiusChanged)

    # ── subdivisions property ──
    def _get_subdivisions(self) -> int:
        return self._subdivisions

    def _set_subdivisions(self, s: int) -> None:
        s = max(0, int(s))
        if s != self._subdivisions:
            self._subdivisions = s
            self._rebuild()
            self.subdivisionsChanged.emit()

    subdivisions = Property(int, _get_subdivisions, _set_subdivisions, notify=subdivisionsChanged)

    # ── mesh build ──
    def _rebuild(self) -> None:
        verts, faces = _icosahedron()
        for _ in range(self._subdivisions):
            verts, faces = _subdivide(verts, faces)
        # project every vertex onto the sphere of the requested radius
        sphere = [_scale(_norm(v), self._radius) for v in verts]

        # NON-indexed: emit each face's 3 vertices with that face's flat normal so
        # facets stay hard-edged. Layout per vertex: pos.xyz(12B) + normal.xyz(12B).
        data = bytearray()
        min_b = [1e30, 1e30, 1e30]
        max_b = [-1e30, -1e30, -1e30]
        for (ia, ib, ic) in faces:
            pa, pb, pc = sphere[ia], sphere[ib], sphere[ic]
            n = _face_normal(pa, pb, pc)
            for p in (pa, pb, pc):
                data += struct.pack("<6f", p[0], p[1], p[2], n[0], n[1], n[2])
                for i in range(3):
                    if p[i] < min_b[i]:
                        min_b[i] = p[i]
                    if p[i] > max_b[i]:
                        max_b[i] = p[i]

        self.clear()
        self.setVertexData(bytes(data))
        self.setStride(6 * 4)  # 6 floats * 4 bytes
        self.setPrimitiveType(QQuick3DGeometry.PrimitiveType.Triangles)
        self.addAttribute(
            QQuick3DGeometry.Attribute.Semantic.PositionSemantic, 0,
            QQuick3DGeometry.Attribute.ComponentType.F32Type,
        )
        self.addAttribute(
            QQuick3DGeometry.Attribute.Semantic.NormalSemantic, 3 * 4,
            QQuick3DGeometry.Attribute.ComponentType.F32Type,
        )
        self.setBounds(QVector3D(*min_b), QVector3D(*max_b))
        self.update()
