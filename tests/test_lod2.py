"""The LOD2 pipeline against a synthetic gable whose answers are known in closed form.

Catches the failures that are otherwise invisible until someone looks at a model:
orientation flips, off-by-one wall indexing, and reconciliation weighting changes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("geopandas")  # needs the `geo` extra: uv sync --extra geo

from reconstruction_3d.lod2 import LOD2Cancelled, LOD2Params, generate_lod2  # noqa: E402

from .lod2_fixtures import EAVE_Z, EPSG, EXPECTED_VOLUME, RIDGE_Z, write_gable_inputs  # noqa: E402


def _params(paths: dict[str, Path], out: Path) -> LOD2Params:
    return LOD2Params(
        input_building=str(paths["bo"]),
        input_roof=str(paths["rs"]),
        input_dsm=str(paths["dsm"]),
        input_dtm=str(paths["dtm"]),
        output_file=str(out),
    )


def test_gable_matches_closed_form(tmp_path: Path) -> None:
    paths = write_gable_inputs(tmp_path)
    out = tmp_path / "gable.city.json"
    progress: list[int] = []

    report = generate_lod2(_params(paths, out), lambda _msg, pct: progress.append(pct))

    assert report.n_buildings_out == 1
    assert report.crs_epsg == EPSG
    record = report.buildings[0]
    assert record.watertight, record.problems
    assert record.problems == []
    assert record.roof_max_z == pytest.approx(RIDGE_Z, abs=0.05)
    assert record.roof_min_z == pytest.approx(EAVE_Z, abs=0.05)
    assert record.ground_z == pytest.approx(0.0, abs=0.05)
    assert record.volume == pytest.approx(EXPECTED_VOLUME, rel=0.01)
    assert progress == sorted(progress) and progress[-1] == 98


def test_writes_cityjson_11_solid(tmp_path: Path) -> None:
    paths = write_gable_inputs(tmp_path)
    out = tmp_path / "gable.city.json"
    generate_lod2(_params(paths, out))

    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["type"] == "CityJSON"
    assert doc["version"] == "1.1"
    assert "transform" in doc
    assert doc["metadata"]["referenceSystem"].endswith(f"/EPSG/0/{EPSG}")
    (building,) = doc["CityObjects"].values()
    assert building["type"] == "Building"
    geometry = building["geometry"][0]
    assert geometry["type"] == "Solid"
    assert geometry["lod"] == "2"
    surfaces = {s["type"] for s in geometry["semantics"]["surfaces"]}
    assert surfaces == {"RoofSurface", "GroundSurface", "WallSurface"}


def test_inputs_are_never_modified(tmp_path: Path) -> None:
    paths = write_gable_inputs(tmp_path)
    before = {role: p.read_bytes() for role, p in paths.items()}
    generate_lod2(_params(paths, tmp_path / "out.json"))
    assert {role: p.read_bytes() for role, p in paths.items()} == before


def test_should_cancel_stops_the_run(tmp_path: Path) -> None:
    paths = write_gable_inputs(tmp_path)
    out = tmp_path / "out.json"
    with pytest.raises(LOD2Cancelled):
        generate_lod2(_params(paths, out), should_cancel=lambda: True)
    assert not out.exists()


def test_cancel_after_the_last_building_writes_nothing(tmp_path: Path) -> None:
    """The cancel flag is re-checked before the write, not only between buildings."""
    paths = write_gable_inputs(tmp_path)
    out = tmp_path / "out.json"
    calls = {"n": 0}

    def cancel_after_loop() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # False for the only building, True before the write

    with pytest.raises(LOD2Cancelled):
        generate_lod2(_params(paths, out), should_cancel=cancel_after_loop)
    assert not out.exists()


def test_missing_dtm_takes_ground_from_surrounding_dsm(tmp_path: Path) -> None:
    """A DTM hole must not put the ground at an absolute 0 m."""
    import numpy as np
    import rasterio

    paths = write_gable_inputs(tmp_path)
    with rasterio.open(paths["dtm"]) as src:
        profile = src.profile
    profile.update(nodata=-32767.0)
    with rasterio.open(paths["dtm"], "w", **profile) as dst:
        dst.write(np.full((profile["height"], profile["width"]), -32767.0, "float32"), 1)
    with rasterio.open(paths["dsm"]) as src:
        dsm = src.read(1)
        dsm_profile = src.profile
    dsm[dsm == 0.0] = 3.0  # the terrain around the house now sits at 3 m
    with rasterio.open(paths["dsm"], "w", **dsm_profile) as dst:
        dst.write(dsm, 1)

    report = generate_lod2(_params(paths, tmp_path / "out.json"))

    record = report.buildings[0]
    assert record.watertight, record.problems
    assert record.ground_source == "dsm_surroundings"
    assert record.ground_z == pytest.approx(3.0, abs=0.05)
    # Box from 3 m to the eaves plus the same triangular prism above them.
    assert record.volume == pytest.approx(EXPECTED_VOLUME - 20 * 10 * 3.0, rel=0.01)


def test_unique_ids_never_collide() -> None:
    from reconstruction_3d.lod2.io_vector import _unique_id

    used: set[str] = set()
    ids = [_unique_id(v, used) for v in ["a_1", "a", "a", None, ""]]
    assert ids[:3] == ["a_1", "a", "a_2"]
    assert len(set(ids)) == len(ids)


def test_crs_check_rejects_missing_and_geographic() -> None:
    import geopandas as gpd
    from shapely.geometry import box

    from reconstruction_3d.lod2.io_vector import check_crs_agreement

    def layer(crs: str) -> gpd.GeoDataFrame:
        return gpd.GeoDataFrame(geometry=[box(0, 0, 1, 1)], crs=crs)

    utm = layer(f"EPSG:{EPSG}")
    assert check_crs_agreement(utm, utm, {"DSM": EPSG, "DTM": EPSG}) == EPSG
    with pytest.raises(ValueError, match="no EPSG code for: DSM"):
        check_crs_agreement(utm, utm, {"DSM": None})
    with pytest.raises(ValueError, match="different reference systems"):
        check_crs_agreement(utm, utm, {"DSM": 32749})
    wgs84 = layer("EPSG:4326")
    with pytest.raises(ValueError, match="geographic"):
        check_crs_agreement(wgs84, wgs84, {"DSM": 4326})


def test_ear_clip_never_emits_zero_area_triangles() -> None:
    from reconstruction_3d.lod2.triangulate import _cross, ear_clip

    rings = [
        [(0, 0), (5, 0), (10, 0), (10, 10), (0, 10)],  # T-vertex on an edge
        [(0, 0), (3, 0), (6, 0), (10, 0), (10, 10), (5, 10), (0, 10)],
        [(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)],  # L-shape
    ]
    for ring in rings:
        triangles = ear_clip(ring)
        assert triangles is not None
        assert all(_cross(a, b, c) != 0 for a, b, c in triangles)
        assert {v for t in triangles for v in t} == set(ring)  # no vertex dropped
    assert ear_clip([(0, 0), (5, 0), (10, 0)]) is None  # collinear: nothing to clip


def test_disconnected_outline_still_closes() -> None:
    """Roof faces outside the kept outline part must not open the shell."""
    from shapely.geometry import box
    from shapely.geometry.polygon import orient

    from reconstruction_3d.lod2.grid import Grid, VertexPool
    from reconstruction_3d.lod2.params import Face, RoofPlane
    from reconstruction_3d.lod2.partition import roof_union
    from reconstruction_3d.lod2.reconcile import reconcile
    from reconstruction_3d.lod2.solid import build_solid
    from reconstruction_3d.lod2.validate import half_edge_problems

    params = LOD2Params(input_building="", input_roof="", input_dsm="")
    grid = Grid.from_min_corner(0.0, 0.0, 0.0, params.output_scale, params.topology_tolerance)
    faces = [
        Face(orient(box(0, 0, 10, 10), 1.0), 0, RoofPlane.horizontal(10.0, "fitted", 100)),
        Face(orient(box(20, 0, 22, 2), 1.0), 1, RoofPlane.horizontal(8.0, "fitted", 100)),
    ]
    z_top, _ = reconcile(faces, grid)

    shell, _ = build_solid(faces, roof_union(faces), z_top, 0.0, grid, VertexPool(), params)

    assert half_edge_problems(shell) == []


def test_vertex_pool_rollback_forgets_only_new_vertices() -> None:
    from reconstruction_3d.lod2.grid import VertexPool

    pool = VertexPool()
    kept = pool.index_of((1, 1, 1))
    mark = len(pool)
    assert pool.index_of((1, 1, 1)) == kept  # reused, not appended
    pool.index_of((2, 2, 2))
    pool.rollback(mark)
    assert len(pool) == 1
    assert pool.index_of((2, 2, 2)) == 1  # re-added cleanly after the rollback
