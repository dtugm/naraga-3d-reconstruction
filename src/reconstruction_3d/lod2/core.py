"""The LOD2 pipeline orchestrator. Synchronous and CPU-bound: call it via
asyncio.to_thread, never directly on the event loop.

Per building:

    subdivide footprint by RS segments  (partition)
      -> plane fit per face             (plane_fit)
      -> one z per shared vertex        (reconcile)
      -> triangulate roof               (triangulate)
      -> assemble shell                 (solid)
      -> verify                         (validate)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

from shapely.geometry import Polygon

from . import io_vector
from .cityjson import CityJSONBuilder
from .grid import Grid, VertexPool
from .params import BuildingReport, LOD2Params, QAReport
from .partition import partition_footprint, roof_union
from .plane_fit import building_reference_z, ground_from_dsm, plane_for_face
from .raster import RasterView
from .reconcile import reconcile
from .solid import SolidError, build_solid
from .validate import check_quantisation, check_shell

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int], None]
CancelCheck = Callable[[], bool]


class LOD2Error(RuntimeError):
    """The run could not proceed at all."""


class LOD2Cancelled(RuntimeError):
    """should_cancel() returned True; the run stopped between buildings."""


def _noop_progress(message: str, percent: int) -> None:
    pass


def _never() -> bool:
    return False


def _clean_attributes(row: Any) -> dict[str, object]:
    """Source attributes, JSON-safe."""
    attributes: dict[str, object] = {}
    for key, value in row.items():
        if key == "geometry":
            continue
        if value is None or isinstance(value, str | bool | int | float):
            attributes[str(key)] = value
        elif hasattr(value, "item"):  # numpy scalar
            attributes[str(key)] = value.item()
        else:
            attributes[str(key)] = str(value)
    return attributes


def generate_lod2(
    params: LOD2Params,
    progress_cb: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> QAReport:
    """Build watertight LOD2 solids and write them as CityJSON to params.output_file.

    progress_cb receives percentages in 0..98; the caller owns the rest.
    should_cancel is polled between buildings so a cancelled or timed-out job
    stops burning CPU instead of running on in its worker thread.
    """
    emit = progress_cb or _noop_progress
    cancelled = should_cancel or _never
    started = time.time()
    report = QAReport(output_file=params.output_file)

    emit("Reading vector inputs", 2)
    buildings = io_vector.load_buildings(params)
    roofs = io_vector.load_roofs(params)
    report.n_buildings_in = len(buildings)

    dsm = RasterView(params.input_dsm)
    dtm = RasterView(params.input_dtm) if params.input_dtm else None
    try:
        rasters = {"DSM": dsm.epsg} | ({"DTM": dtm.epsg} if dtm else {})
        epsg = io_vector.check_crs_agreement(buildings, roofs, rasters)
        report.crs_epsg = epsg

        emit("Matching roof segments to buildings", 8)
        mapping = io_vector.assign_roofs_to_buildings(buildings, roofs)

        # The grid's translate has to come from the extent up front, before any
        # vertex is quantised, or the integers overflow int32 on UTM northings.
        minx, miny, _, _ = buildings.total_bounds
        grid = Grid.from_min_corner(
            float(minx), float(miny), 0.0, params.output_scale, params.topology_tolerance
        )
        pool = VertexPool()
        builder = CityJSONBuilder(grid, epsg)

        erode_distance = params.erode_distance if params.erode_distance is not None else dsm.gsd
        log.info(
            "grid translate=%s scale=%s | DSM gsd=%.3f erode=%.3f",
            grid.translate,
            grid.scale,
            dsm.gsd,
            erode_distance,
        )

        total = len(buildings)
        emit(f"Building {total} solids", 10)
        stride = max(1, total // 80)

        for position in range(total):
            if cancelled():
                raise LOD2Cancelled("LOD2 generation cancelled")
            row = buildings.iloc[position]
            segments = [roofs.geometry.iloc[j] for j in mapping.get(position, [])]
            record = _build_one(
                object_id=str(row[params.id_field]),
                footprint=row.geometry,
                segments=segments,
                attributes=_clean_attributes(row),
                dsm=dsm,
                dtm=dtm,
                grid=grid,
                pool=pool,
                builder=builder,
                params=params,
                erode_distance=erode_distance,
            )
            report.buildings.append(record)

            if record.problems and not record.watertight:
                report.n_not_watertight += 1
            if record.roof_source == "fallback_flat":
                report.n_fallback_flat += 1
            if record.ground_clamped:
                report.n_ground_clamped += 1

            if position % stride == 0:
                emit(
                    f"Building solids ({position + 1}/{total})",
                    10 + int(80 * position / max(1, total)),
                )

        report.n_buildings_out = len(builder)
        report.n_skipped = report.n_buildings_in - report.n_buildings_out
        report.n_vertices = len(pool)

        for problem in check_quantisation(pool, grid):
            log.error("quantisation: %s", problem)

        if not len(builder):
            raise LOD2Error("no buildings could be built")

        # A cancel after the last building must not write: the caller may already have
        # removed the directory output_file points into.
        if cancelled():
            raise LOD2Cancelled("LOD2 generation cancelled")

        if params.output_file:
            emit(f"Writing CityJSON {params.cityjson_version}", 95)
            builder.write(params.output_file, pool, params.cityjson_version)
    finally:
        dsm.close()
        if dtm is not None:
            dtm.close()

    report.elapsed_seconds = time.time() - started
    emit(f"Finished: {report.summary}", 98)
    return report


def _build_one(
    object_id: str,
    footprint: Polygon,
    segments: Sequence[Polygon],
    attributes: dict[str, object],
    dsm: RasterView,
    dtm: RasterView | None,
    grid: Grid,
    pool: VertexPool,
    builder: CityJSONBuilder,
    params: LOD2Params,
    erode_distance: float,
) -> BuildingReport:
    """One building, start to finish. Never raises (unless strict); records instead.

    A building that is not written leaves no vertices behind in the shared pool.
    """
    record = BuildingReport(object_id=object_id)
    mark = len(pool)

    try:
        faces, method = partition_footprint(footprint, segments, params, grid)
        record.n_roof_faces = len(faces)
        record.n_gap_faces = sum(1 for f in faces if f.is_gap)
        if method in ("no_segments", "fallback"):
            record.roof_source = "fallback_flat"

        ground: float | None = None
        if dtm is not None:
            ground = dtm.statistic(footprint, params.ground_statistic)
        if ground is None:
            # DTM hole or footprint outside its extent. Never fall back to an absolute
            # 0 m: the walls would reach sea level and still pass the volume check.
            ground = ground_from_dsm(footprint, dsm)
            if ground is None:
                raise SolidError("no ground height: DTM and the DSM around the footprint are empty")
            record.ground_source = "dsm_surroundings"
            log.warning(
                "%s: no DTM under the footprint; ground taken from the DSM around it", object_id
            )
        ground_seed = ground
        reference_z = building_reference_z(footprint, dsm)

        for face in faces:
            face.plane = plane_for_face(
                face.polygon, dsm, params, erode_distance, reference_z, ground_seed
            )
        planes = [f.plane for f in faces if f.plane is not None]
        record.n_fallback_planes = sum(1 for p in planes if p.is_fallback)
        record.max_plane_rmse = max((p.rmse for p in planes), default=0.0)

        z_top, table = reconcile(faces, grid)
        if not z_top:
            raise SolidError("no roof vertices were produced")
        worst, where = table.worst_disagreement()
        record.max_z_disagreement = worst
        if worst > params.z_disagreement_warn and where is not None:
            x, y = grid.xy_to_world(where)
            log.warning(
                "%s: roof planes disagree by %.2f m at (%.2f, %.2f)", object_id, worst, x, y
            )

        roof_min = min(z_top.values())
        roof_max = max(z_top.values())

        # The DTM under a footprint routinely comes out above the DSM at building
        # edges. Clamping keeps the height positive; many clamps mean a bad DTM.
        z_ground = ground_seed
        if z_ground >= roof_min - 1e-6:
            z_ground = roof_min - params.min_building_height
            record.ground_clamped = True

        record.ground_z = z_ground
        record.roof_min_z = roof_min
        record.roof_max_z = roof_max
        record.height = roof_max - z_ground

        shell, semantics = build_solid(
            faces, roof_union(faces), z_top, z_ground, grid, pool, params
        )

        problems, volume = check_shell(
            shell,
            pool,
            grid,
            footprint_area=float(footprint.area),
            height=max(roof_max - z_ground, 1e-6),
        )
        if not object_id:
            problems.append("empty CityObject id")
        record.volume = volume
        record.problems = problems
        record.watertight = not problems

        if problems:
            summary = "; ".join(problems[:3])
            if params.on_invalid == "strict":
                raise SolidError(summary)
            log.warning("%s: %s", object_id, summary)
            if params.on_invalid == "skip":
                pool.rollback(mark)
                return record

        builder.add_building(object_id, attributes, shell, semantics)
        return record

    except SolidError as exc:
        pool.rollback(mark)
        if params.on_invalid == "strict":
            raise
        record.watertight = False
        record.problems.append(str(exc))
        log.warning("%s skipped: %s", object_id, exc)
        return record
    except Exception as exc:  # one bad building must not lose the tile
        pool.rollback(mark)
        if params.on_invalid == "strict":
            raise
        record.watertight = False
        record.problems.append(f"{type(exc).__name__}: {exc}")
        log.exception("%s failed", object_id)
        return record
