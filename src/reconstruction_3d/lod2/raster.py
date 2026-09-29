"""Windowed DSM/DTM access.

Only the pixels under the polygon being asked about are read, so a large tile
never materialises the whole raster. Returned coordinates are pixel centres --
sampling at corners biases every fitted plane by half a pixel.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
import numpy.typing as npt
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import Window
from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)

FloatArray = npt.NDArray[np.float64]
Sample = tuple[FloatArray, FloatArray, FloatArray]

# Anything at or below -32767 is void, in addition to the declared nodata:
# the sample DSMs carry that sentinel without declaring it.
_VOID_AT_OR_BELOW = -32767.0


def _empty() -> Sample:
    return (
        np.empty(0, dtype=np.float64),
        np.empty(0, dtype=np.float64),
        np.empty(0, dtype=np.float64),
    )


class RasterView:
    """Read-only view over one single-band raster."""

    def __init__(self, path: str, band: int = 1) -> None:
        self.path = path
        self.band = band
        self._ds: Any = rasterio.open(path)
        self._nodata: float | None = self._ds.nodata

    def close(self) -> None:
        if self._ds is not None and not self._ds.closed:
            self._ds.close()

    def __enter__(self) -> RasterView:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def epsg(self) -> int | None:
        if not self._ds.crs:
            return None
        code: int | None = self._ds.crs.to_epsg()
        return code

    @property
    def gsd(self) -> float:
        """Ground sample distance, taken as the larger pixel dimension."""
        return float(max(abs(self._ds.transform.a), abs(self._ds.transform.e)))

    def _window_for(self, bounds: tuple[float, float, float, float], pad: int = 1) -> Any:
        """Pixel window covering `bounds`, padded and clipped to the dataset, or None.

        Computed straight from the inverse affine rather than through
        rasterio.windows helpers, whose rounding keywords have shifted between releases.
        """
        minx, miny, maxx, maxy = bounds
        inv = ~self._ds.transform
        corners = [inv @ pt for pt in ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy))]
        cols = [c for c, _ in corners]
        rows = [r for _, r in corners]

        col_off = max(int(math.floor(min(cols))) - pad, 0)
        row_off = max(int(math.floor(min(rows))) - pad, 0)
        col_end = min(int(math.ceil(max(cols))) + pad, self._ds.width)
        row_end = min(int(math.ceil(max(rows))) + pad, self._ds.height)

        if col_end <= col_off or row_end <= row_off:
            return None
        return Window(col_off, row_off, col_end - col_off, row_end - row_off)

    def sample_polygon(self, poly: BaseGeometry | None, all_touched: bool = False) -> Sample:
        """Return (xs, ys, zs) for every valid pixel centre inside `poly`.

        all_touched=False keeps pixels whose centre falls inside, which is right
        for plane fitting. True is for coarse statistics over small polygons.
        """
        if poly is None or poly.is_empty:
            return _empty()

        win = self._window_for(poly.bounds)
        if win is None:
            return _empty()

        data: FloatArray = self._ds.read(self.band, window=win).astype(np.float64)
        transform = self._ds.window_transform(win)

        inside = geometry_mask(
            [poly],
            out_shape=data.shape,
            transform=transform,
            invert=True,
            all_touched=all_touched,
        )
        valid = inside & np.isfinite(data) & (data > _VOID_AT_OR_BELOW)
        if self._nodata is not None and np.isfinite(self._nodata):
            valid &= data != self._nodata

        rows, cols = np.nonzero(valid)
        if rows.size == 0:
            return _empty()

        cc = cols + 0.5
        rr = rows + 0.5
        xs: FloatArray = transform.c + transform.a * cc + transform.b * rr
        ys: FloatArray = transform.f + transform.d * cc + transform.e * rr
        return xs, ys, data[rows, cols]

    def value_at(self, x: float, y: float) -> float | None:
        """Single-pixel lookup, or None if outside the raster or void."""
        col_f, row_f = (~self._ds.transform) @ (x, y)
        col, row = int(math.floor(col_f)), int(math.floor(row_f))
        if not (0 <= col < self._ds.width and 0 <= row < self._ds.height):
            return None
        value = float(self._ds.read(self.band, window=Window(col, row, 1, 1))[0, 0])
        if not math.isfinite(value) or value <= _VOID_AT_OR_BELOW:
            return None
        if self._nodata is not None and value == self._nodata:
            return None
        return value

    def statistic(
        self,
        poly: BaseGeometry,
        stat: str = "median",
        all_touched: bool = True,
    ) -> float | None:
        """Robust summary of the raster inside `poly`, or None if nothing valid."""
        _, _, zs = self.sample_polygon(poly, all_touched=all_touched)
        if zs.size == 0:
            point = poly.representative_point()
            return self.value_at(point.x, point.y)

        if stat == "median":
            return float(np.median(zs))
        if stat == "min":
            return float(np.min(zs))
        if stat == "max":
            return float(np.max(zs))
        if stat.startswith("p"):
            return float(np.percentile(zs, float(stat[1:])))
        raise ValueError(f"unknown statistic {stat!r}")
