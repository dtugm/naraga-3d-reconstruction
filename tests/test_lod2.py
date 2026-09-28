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
