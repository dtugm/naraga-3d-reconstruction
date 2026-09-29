"""Self-checks that run on every building, by default.

The half-edge check is the one that matters: it is a complete proof that the
shell is a closed, consistently-oriented 2-manifold. Everything else is a
supporting argument.
"""

from __future__ import annotations

import collections
import logging
from collections.abc import Mapping, Sequence

from .grid import Grid, VertexPool

log = logging.getLogger(__name__)

MAX_REPORTED = 5

Shell = Sequence[Sequence[Sequence[int]]]
Point3 = tuple[float, float, float]


def _det3(a: Point3, b: Point3, c: Point3) -> float:
    """a . (b x c)"""
    return (
        a[0] * (b[1] * c[2] - b[2] * c[1])
        - a[1] * (b[0] * c[2] - b[2] * c[0])
        + a[2] * (b[0] * c[1] - b[1] * c[0])
    )


def half_edge_problems(shell: Shell) -> list[str]:
    """Prove the shell is a closed, consistently-oriented 2-manifold.

    Over every ring of every face, every directed edge must occur exactly once
    (a repeat is non-manifold or mis-oriented), and every edge (a, b) must have
    exactly one twin (b, a) (a missing twin is a hole). Being topological, this
    validates courtyards, genus-1 buildings and triangulated faces uniformly.
    """
    problems: list[str] = []
    edges: collections.Counter[tuple[int, int]] = collections.Counter()

    for face_index, face in enumerate(shell):
        for ring_index, ring in enumerate(face):
            distinct = len(set(ring))
            if len(ring) < 3 or distinct < 3:
                problems.append(
                    f"face {face_index} ring {ring_index} is degenerate "
                    f"({len(ring)} vertices, {distinct} distinct)"
                )
                continue
            for i in range(len(ring)):
                a = ring[i]
                b = ring[(i + 1) % len(ring)]
                if a == b:
                    problems.append(f"face {face_index} ring {ring_index} repeats vertex {a}")
                    continue
                edges[(a, b)] += 1

    repeated = [edge for edge, count in edges.items() if count > 1]
    if repeated:
        problems.append(
            f"{len(repeated)} directed edges occur more than once "
            f"(inconsistent orientation or non-manifold), e.g. {repeated[:MAX_REPORTED]}"
        )

    unpaired = [edge for edge in edges if (edge[1], edge[0]) not in edges]
    if unpaired:
        problems.append(
            f"{len(unpaired)} edges have no reverse twin -- the shell is open, "
            f"e.g. {unpaired[:MAX_REPORTED]}"
        )

    return problems


def signed_volume(shell: Shell, coords: Mapping[int, Point3]) -> float:
    """Volume from the divergence theorem. Outward normals give a positive sign.

    Catches the one failure the half-edge check cannot see: a shell that is
    uniformly inside out (reversing every face preserves the twin property).
    """
    total = 0.0
    for face in shell:
        for ring in face:
            if len(ring) < 3:
                continue
            p0 = coords[ring[0]]
            for i in range(1, len(ring) - 1):
                total += _det3(p0, coords[ring[i]], coords[ring[i + 1]])
    return total / 6.0


def check_shell(
    shell: Shell,
    pool: VertexPool,
    grid: Grid,
    footprint_area: float = 0.0,
    height: float = 0.0,
) -> tuple[list[str], float]:
    """Run every check on one building's shell. Returns (problems, volume)."""
    problems = half_edge_problems(shell)

    n_vertices = len(pool)
    bad_index = [
        index
        for face in shell
        for ring in face
        for index in ring
        if index < 0 or index >= n_vertices
    ]
    if bad_index:
        problems.append(f"{len(bad_index)} vertex indices are out of range")
        return problems, 0.0

    # Only the vertices this shell references: the pool is shared across the file.
    coords = {
        index: grid.to_world(pool.key_at(index))
        for face in shell
        for ring in face
        for index in ring
    }
    volume = signed_volume(shell, coords)

    if volume <= 0.0:
        problems.append(f"signed volume is {volume:.3f}; the shell is inside out or collapsed")
    elif footprint_area > 0 and height > 0:
        # A prism of the same footprint and height is the natural yardstick.
        ratio = volume / (footprint_area * height)
        if not 0.4 <= ratio <= 2.0:
            problems.append(
                f"volume {volume:.1f} m3 is {ratio:.2f}x the footprint prism, "
                "outside the plausible band"
            )

    return problems, volume


def check_quantisation(pool: VertexPool, grid: Grid, sample: int = 64) -> list[str]:
    """Round-trip a sample of vertices through the transform."""
    problems: list[str] = []
    count = len(pool)
    if count == 0:
        return ["no vertices were emitted"]
    step = max(1, count // sample)
    limit = grid.scale / 2 + 1e-9
    for index in range(0, count, step):
        key = pool.key_at(index)
        world = grid.to_world(key)
        for axis, value in enumerate(world):
            back = (value - grid.translate[axis]) / grid.scale
            if abs(back - key[axis]) > limit:
                problems.append(f"vertex {index} fails the quantisation round trip")
                break
    return problems
