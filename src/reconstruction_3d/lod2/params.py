"""Configuration and data structures for watertight LOD2 generation.

Everything the pipeline passes around is declared here exactly once. Ported from
sam-interactive-github/ai/lod_generation_v2/interface.py, minus the CLI-only
selection knobs (only_id / limit / bbox) that make no sense for a service job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import atan, degrees, hypot
from typing import Any

from shapely.geometry import Polygon

# Confidence a plane gets purely from how it was obtained, before the rmse and
# inlier-count penalties in RoofPlane.confidence are applied.
_SOURCE_BASE_CONFIDENCE: dict[str, float] = {
    "fitted": 1.00,  # least-squares fit on DSM pixels inside the segment
    "horizontal_median": 0.50,  # median DSM inside the un-eroded segment
    "horizontal_building": 0.30,  # 75th percentile DSM over the whole footprint
    "fallback_flat": 0.15,  # ground + min_building_height; the last resort
}


@dataclass
class LOD2Params:
    """Inputs and tuning for one LOD2 run.

    Defaults are chosen for ~0.5 m GSD aerial DSM/DTM over dense urban blocks.
    """

    # --- inputs ---
    input_building: str
    input_roof: str
    input_dsm: str
    input_dtm: str | None = None
    output_file: str = ""

    # --- precision / topology ---
    # Planar snapping grid. Must sit well above float64 noise and well below the
    # DSM ground sample distance, and must be an exact multiple of output_scale.
    topology_tolerance: float = 0.01
    output_scale: float = 0.001  # CityJSON transform.scale, in CRS units

    # --- segment reconciliation ---
    min_segment_area: float = 1.0  # m^2; clipped RS parts below this are dropped
    sliver_absorb_area: float = 0.5  # m^2; gap faces below this merge into a neighbour
    sliver_thinness: float = 0.15  # 4*pi*A/P^2; below this a gap is a sliver at any size
    max_faces_per_building: int = 2000  # hard stop against a polygonize blow-up

    # --- plane fitting ---
    erode_distance: float | None = None  # m; None -> 1.0 * DSM pixel size
    min_plane_pixels: int = 10
    trim_sigma: float = 2.5
    trim_iterations: int = 3
    max_plane_rmse: float = 1.0  # m; above this, demote to a horizontal plane
    max_plane_slope_deg: float = 80.0
    min_inlier_fraction: float = 0.5

    # --- heights ---
    ground_statistic: str = "median"  # median | min | p10
    min_building_height: float = 2.0  # m; also the clamp when DTM >= DSM

    # --- output format ---
    # 1.1 is the default because its quantised integers are exactly the
    # representation the topology was built on. 1.0 changes serialisation only.
    cityjson_version: str = "1.1"

    # --- behaviour ---
    id_field: str = "uuid_bgn"
    triangulate_roof: bool = True
    on_invalid: str = "warn"  # warn | strict | skip
    z_disagreement_warn: float = 1.0  # m

    def __post_init__(self) -> None:
        if self.ground_statistic not in ("median", "min", "p10"):
            raise ValueError(
                f"ground_statistic must be median|min|p10, got {self.ground_statistic!r}"
            )
        if self.on_invalid not in ("warn", "strict", "skip"):
            raise ValueError(f"on_invalid must be warn|strict|skip, got {self.on_invalid!r}")
        if self.cityjson_version not in ("1.0", "1.1"):
            raise ValueError(f"cityjson_version must be 1.0 or 1.1, got {self.cityjson_version!r}")
        if self.output_scale <= 0:
            raise ValueError("output_scale must be positive")
        ratio = self.topology_tolerance / self.output_scale
        if abs(ratio - round(ratio)) > 1e-9:
            raise ValueError(
                f"topology_tolerance ({self.topology_tolerance}) must be an exact "
                f"multiple of output_scale ({self.output_scale}); otherwise a snapped "
                "coordinate is not exactly representable as an integer and vertex "
                "identity stops being exact"
            )


@dataclass
class RoofPlane:
    """A roof plane in the form z = a*(x - x0) + b*(y - y0) + c.

    Coordinates are centred on (x0, y0) because fitting raw UTM eastings and
    northings gives a normal matrix with a condition number around 1e13.
    """

    a: float
    b: float
    c: float
    x0: float
    y0: float
    rmse: float
    n_inliers: int
    source: str

    def eval(self, x: float, y: float) -> float:
        return self.a * (x - self.x0) + self.b * (y - self.y0) + self.c

    @property
    def slope_deg(self) -> float:
        return degrees(atan(hypot(self.a, self.b)))

    @property
    def is_fallback(self) -> bool:
        return self.source != "fitted"

    @property
    def confidence(self) -> float:
        """Weight this plane carries when reconciling a shared vertex z.

        Never returns zero: a face always gets some say in the height of its own
        corners, however poorly its plane was determined.
        """
        base = _SOURCE_BASE_CONFIDENCE.get(self.source, 0.15)
        if self.source == "fitted":
            base /= 1.0 + max(self.rmse, 0.0)
            if self.n_inliers < 30:
                base *= 0.5 + 0.5 * (self.n_inliers / 30.0)
        return max(base, 0.05)

    @classmethod
    def horizontal(cls, z: float, source: str, n_inliers: int = 0) -> RoofPlane:
        return cls(a=0.0, b=0.0, c=z, x0=0.0, y0=0.0, rmse=0.0, n_inliers=n_inliers, source=source)


@dataclass
class Face:
    """One planar piece of a building's roof, covering part of the footprint."""

    polygon: Polygon
    segment_index: int | None = None  # None => this face is a gap in the RS cover
    plane: RoofPlane | None = None
    absorbed: bool = False  # a sliver gap that inherited a neighbour's plane

    @property
    def is_gap(self) -> bool:
        return self.segment_index is None


@dataclass
class BuildingReport:
    """Per-building quality record."""

    object_id: str
    n_roof_faces: int = 0
    n_gap_faces: int = 0
    n_fallback_planes: int = 0
    max_plane_rmse: float = 0.0
    max_z_disagreement: float = 0.0
    ground_z: float = 0.0
    roof_min_z: float = 0.0
    roof_max_z: float = 0.0
    height: float = 0.0
    volume: float = 0.0
    roof_source: str = "segmented"  # segmented | fallback_flat
    ground_clamped: bool = False
    watertight: bool = True
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "n_roof_faces": self.n_roof_faces,
            "n_gap_faces": self.n_gap_faces,
            "n_fallback_planes": self.n_fallback_planes,
            "max_plane_rmse": round(self.max_plane_rmse, 4),
            "max_z_disagreement": round(self.max_z_disagreement, 4),
            "ground_z": round(self.ground_z, 3),
            "roof_min_z": round(self.roof_min_z, 3),
            "roof_max_z": round(self.roof_max_z, 3),
            "height": round(self.height, 3),
            "volume": round(self.volume, 3),
            "roof_source": self.roof_source,
            "ground_clamped": self.ground_clamped,
            "watertight": self.watertight,
            "problems": self.problems,
        }


@dataclass
class QAReport:
    """Run-level summary returned by core.generate_lod2."""

    output_file: str = ""
    crs_epsg: int | None = None
    n_buildings_in: int = 0
    n_buildings_out: int = 0
    n_skipped: int = 0
    n_not_watertight: int = 0
    n_fallback_flat: int = 0
    n_ground_clamped: int = 0
    n_vertices: int = 0
    elapsed_seconds: float = 0.0
    buildings: list[BuildingReport] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"{self.n_buildings_out}/{self.n_buildings_in} buildings, "
            f"{self.n_vertices} vertices, "
            f"{self.n_not_watertight} not watertight, "
            f"{self.n_fallback_flat} flat fallback, "
            f"{self.n_skipped} skipped"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_file": self.output_file,
            "crs_epsg": self.crs_epsg,
            "n_buildings_in": self.n_buildings_in,
            "n_buildings_out": self.n_buildings_out,
            "n_skipped": self.n_skipped,
            "n_not_watertight": self.n_not_watertight,
            "n_fallback_flat": self.n_fallback_flat,
            "n_ground_clamped": self.n_ground_clamped,
            "n_vertices": self.n_vertices,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "buildings": [b.to_dict() for b in self.buildings],
        }
