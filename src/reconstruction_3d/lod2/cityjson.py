"""CityJSON 1.1 document builder.

1.1 makes `transform` mandatory, so vertices are stored as integers -- and those
integers are the mechanism that makes vertex identity exact, which is what makes
a watertight shell possible (see grid.py). metadata.referenceSystem uses the OGC
URL form, which cjio accepts (it rejects the 1.0-era URN form).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Sequence
from typing import Any

from .grid import Grid, VertexPool

log = logging.getLogger(__name__)

# Order matters: these indices are what solid.ROOF_SURFACE and friends mean.
SURFACES: list[dict[str, str]] = [
    {"type": "RoofSurface"},
    {"type": "GroundSurface"},
    {"type": "WallSurface"},
]


class CityJSONBuilder:
    """Accumulates buildings into one CityJSON 1.1 document."""

    def __init__(self, grid: Grid, epsg: int | None) -> None:
        self.grid = grid
        # A fresh dict per builder, never a copy of a module-level template.
        self._city_objects: dict[str, Any] = {}
        self._epsg = epsg

    def add_building(
        self,
        object_id: str,
        attributes: dict[str, Any],
        shell: Sequence[Sequence[Sequence[int]]],
        semantics: Sequence[int],
    ) -> None:
        if object_id in self._city_objects:
            raise ValueError(f"duplicate CityObject id {object_id!r}")
        self._city_objects[object_id] = {
            "type": "Building",
            "attributes": attributes,
            "geometry": [
                {
                    "type": "Solid",
                    # boundaries[shell][face][ring][vertex]; one exterior shell.
                    "boundaries": [list(shell)],
                    "semantics": {
                        "surfaces": SURFACES,
                        # Nested once per shell, as a Solid requires.
                        "values": [list(semantics)],
                    },
                    # CityJSON 1.1 types lod as a string, unlike 1.0's integer.
                    "lod": "2",
                }
            ],
        }

    def __len__(self) -> int:
        return len(self._city_objects)

    def document(self, pool: VertexPool) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "type": "CityJSON",
            "version": "1.1",
            "transform": self.grid.as_cityjson_transform(),
            "CityObjects": self._city_objects,
            "vertices": pool.as_int_list(),
        }
        metadata: dict[str, Any] = {}
        if self._epsg is not None:
            metadata["referenceSystem"] = f"http://www.opengis.net/def/crs/EPSG/0/{self._epsg}"
        if len(pool):
            metadata["geographicalExtent"] = pool.bbox_world(self.grid)
        if metadata:
            doc["metadata"] = metadata
        return doc

    def write(self, path: str, pool: VertexPool, version: str = "1.1") -> str:
        """Serialise atomically (temp file + os.replace): a truncated file that
        still parses is worse than no file.

        The target directory must already exist. It belongs to the caller (in the
        service, a per-job temp dir), and recreating it after the caller removed it
        would leak the whole output into /tmp.
        """
        doc = self.document(pool)
        if version == "1.0":
            doc = downgrade_to_10(doc)
        elif version != "1.1":
            raise ValueError(f"unsupported CityJSON version {version!r}")
        directory = os.path.dirname(os.path.abspath(path)) or "."

        handle, temporary = tempfile.mkstemp(suffix=".json", prefix=".lod2-", dir=directory)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(doc, stream, separators=(",", ":"))
            os.replace(temporary, path)
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise

        log.info(
            "wrote %d buildings and %d vertices (CityJSON %s)",
            len(self._city_objects),
            len(pool),
            version,
        )
        return path


def downgrade_to_10(doc: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a 1.1 document in the 1.0 serialisation, for readers that reject 1.1.

    Geometry is unchanged -- watertightness lives in the vertex INDICES. Vertices
    become floats, `transform` disappears, referenceSystem reverts to the URN
    form, and `lod` reverts from a string to a number.
    """
    transform = doc.get("transform")
    if transform is None:
        return dict(doc)

    sx, sy, sz = transform["scale"]
    tx, ty, tz = transform["translate"]

    out: dict[str, Any] = {
        "type": "CityJSON",
        "version": "1.0",
        "CityObjects": {},
        "vertices": [
            [round(v[0] * sx + tx, 3), round(v[1] * sy + ty, 3), round(v[2] * sz + tz, 3)]
            for v in doc["vertices"]
        ],
    }

    metadata = dict(doc.get("metadata", {}))
    match = re.search(r"/(\d+)$", metadata.get("referenceSystem", ""))
    if match:
        metadata["referenceSystem"] = f"urn:ogc:def:crs:EPSG::{match.group(1)}"
    if metadata:
        out["metadata"] = metadata

    for object_id, city_object in doc["CityObjects"].items():
        geometries = []
        for geometry in city_object.get("geometry", []):
            downgraded = dict(geometry)
            lod = downgraded.get("lod")
            if isinstance(lod, str):
                downgraded["lod"] = int(lod) if lod.isdigit() else float(lod)
            geometries.append(downgraded)
        out["CityObjects"][object_id] = {**city_object, "geometry": geometries}

    return out
