"""Shared-vertex height reconciliation -- the crux of the algorithm.

A vertex where several roof faces meet gets a different z from each incident
plane. For the shell to close there must be exactly ONE z per planimetric
(x, y), so those candidates are collapsed into a weighted mean of the incident
planes evaluated at that vertex.

The decisive property is that it never moves x or y, so the 2D partition -- and
therefore the whole shell topology -- stays exactly what partition.py built.
Intersecting adjacent planes to recover true ridges was rejected: at a 3-plane
vertex the solution MOVES the vertex planimetrically, which invalidates the
partition and forces a re-node loop with no termination guarantee. The cost of
averaging is sub-decimetre ridge error on asymmetric junctions; on a symmetric
gable it is exact. See the reference module's "phase 2" note for the upgrade
(shared-edge ridge snapping) that is deliberately not built yet.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence

from .grid import Grid, PlanKey
from .params import Face

log = logging.getLogger(__name__)


class ZTable:
    """Accumulates candidate heights per planimetric key, then collapses them."""

    __slots__ = ("_numerator", "_denominator", "_lowest", "_highest")

    def __init__(self) -> None:
        self._numerator: dict[PlanKey, float] = {}
        self._denominator: dict[PlanKey, float] = {}
        self._lowest: dict[PlanKey, float] = {}
        self._highest: dict[PlanKey, float] = {}

    def add(self, key: PlanKey, z: float, weight: float) -> None:
        weight = max(float(weight), 1e-9)
        self._numerator[key] = self._numerator.get(key, 0.0) + weight * z
        self._denominator[key] = self._denominator.get(key, 0.0) + weight
        if key in self._lowest:
            self._lowest[key] = min(self._lowest[key], z)
            self._highest[key] = max(self._highest[key], z)
        else:
            self._lowest[key] = z
            self._highest[key] = z

    def resolve(self) -> dict[PlanKey, float]:
        return {key: self._numerator[key] / self._denominator[key] for key in self._numerator}

    def worst_disagreement(self) -> tuple[float, PlanKey | None]:
        """The single most contested vertex -- whether RS and DSM agree, where they agree least."""
        worst = 0.0
        where: PlanKey | None = None
        for key in self._lowest:
            spread = self._highest[key] - self._lowest[key]
            if spread > worst:
                worst = spread
                where = key
        return worst, where

    def __len__(self) -> int:
        return len(self._numerator)


def face_weight(face: Face) -> float:
    """How much say a face gets in the height of its own corners.

    sqrt(area), not area: linear weighting lets a 300 m2 main roof swamp a 6 m2
    dormer at their shared corner. Scaled by plane confidence so a fallback face
    does not get equal standing against a well-fitted one.
    """
    if face.plane is None:
        raise ValueError("face has no plane; fit planes before reconciling")
    return math.sqrt(max(float(face.polygon.area), 1e-9)) * face.plane.confidence


def reconcile(faces: Sequence[Face], grid: Grid) -> tuple[dict[PlanKey, float], ZTable]:
    """Collapse every face's ring vertices to one height per planimetric key.

    Each plane is evaluated at the *quantised* vertex position, so two faces
    holding coordinates that differ in the last bits do not produce two heights
    before any real plane disagreement. Built per building: vertices shared by
    adjacent buildings are deliberately NOT merged -- they are independent solids.
    """
    table = ZTable()
    for face in faces:
        weight = face_weight(face)
        plane = face.plane
        assert plane is not None  # face_weight already raised otherwise
        for ring in [face.polygon.exterior, *face.polygon.interiors]:
            keys = grid.ring_keys(ring.coords)
            if keys is None:
                continue
            for key in keys:
                x, y = grid.xy_to_world(key)
                table.add(key, plane.eval(x, y), weight)
    return table.resolve(), table
