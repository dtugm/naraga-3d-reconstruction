"""The `cascade_mesh` model: watertight LOD2 CityJSON from BO + RS + DSM + DTM.

Service glue around reconstruction_3d.lod2: re-check the inputs the contract
says services MUST re-check, download them into a per-job temp dir, run the
CPU-bound pipeline in a worker thread while heartbeating, and PUT the result to
the presigned upload URL.

Roof structure (RS) arrives in Reconstruction3dInputs.roof_structure (contract
3.0.0 replaced remote_sensing with it).

One gap remains: `cityjson` is a DatasetFormat since 3.0.0 but is not among this
service's output_formats, so the CityJSON is uploaded to output_upload_urls[0]
under that slot's format and naraga-converter turns it into glTF / 3D Tiles;
result_summary stays None rather than claiming formats we did not generate.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

ReportProgress = Callable[[int], Awaitable[None]]

ROOF_STRUCTURE_KEY = "roof_structure"
VECTOR_FORMATS = ("geojson",)
RASTER_FORMATS = ("geotiff", "cog")

# (input key, accepted formats, local file name)
_INPUTS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("building_outline", VECTOR_FORMATS, "bo.geojson"),
    (ROOF_STRUCTURE_KEY, VECTOR_FORMATS, "rs.geojson"),
    ("dsm", RASTER_FORMATS, "dsm.tif"),
    ("dtm", RASTER_FORMATS, "dtm.tif"),
)

_CHUNK = 1 << 20

# Above this share of buildings failing validation the tile is judged unusable and
# the job fails instead of completing with a hollowed-out model. Tunable: the product
# threshold is still open (quality reporting, team issue list).
MAX_SKIPPED_FRACTION = 0.5


def validate_request(request: Any) -> None:
    """Raise ValueError unless the request can run through cascade_mesh.

    The gateway enforces elevation_source -> required inputs, but the contract
    says services MUST re-check on the InternalJobRequest they receive.
    """
    if str(request.params.elevation_source) != "dtm_dsm":
        raise ValueError("cascade_mesh requires params.elevation_source == 'dtm_dsm'")
    datasets = request.input_datasets
    if "point_cloud" in datasets:
        raise ValueError("cascade_mesh (dtm_dsm) forbids a point_cloud input")
    missing = [key for key, _, _ in _INPUTS if key not in datasets]
    if missing:
        raise ValueError(f"cascade_mesh is missing input(s): {', '.join(missing)}")
    for key, formats, _ in _INPUTS:
        fmt = str(datasets[key].dataset_format)
        if fmt not in formats:
            raise ValueError(
                f"input '{key}' is {fmt}; cascade_mesh accepts {', '.join(formats)} here"
            )
    if not request.output_upload_urls:
        raise ValueError("no output_upload_urls to write the CityJSON to")


def _check_skipped(n_skipped: int, n_buildings: int) -> None:
    """Fail the job when too many buildings were left out to call the result a model."""
    if n_buildings and n_skipped / n_buildings > MAX_SKIPPED_FRACTION:
        raise RuntimeError(
            f"{n_skipped} of {n_buildings} buildings failed validation; "
            "the inputs (roof structure vs DSM/DTM) likely disagree"
        )


async def _download(client: httpx.AsyncClient, key: str, url: str, dest: Path) -> None:
    """Stream one signed URL to disk. The URL is a credential: never log or echo it."""
    try:
        async with client.stream("GET", url) as response:
            if response.status_code >= 300:
                raise RuntimeError(f"download of input '{key}' failed: HTTP {response.status_code}")
            with dest.open("wb") as stream:
                async for chunk in response.aiter_bytes(_CHUNK):
                    stream.write(chunk)
    except httpx.HTTPError as exc:
        # `from None`: httpx messages embed the signed URL.
        raise RuntimeError(f"download of input '{key}' failed: {type(exc).__name__}") from None


async def _file_chunks(path: Path) -> AsyncIterator[bytes]:
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            yield chunk


async def _upload(client: httpx.AsyncClient, url: str, path: Path) -> None:
    """PUT the output to its presigned URL. Explicit Content-Length: S3-style
    presigned PUTs reject chunked transfer encoding."""
    headers = {"Content-Type": "application/json", "Content-Length": str(path.stat().st_size)}
    try:
        response = await client.put(url, content=_file_chunks(path), headers=headers)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"output upload failed: {type(exc).__name__}") from None
    if response.status_code >= 300:
        raise RuntimeError(f"output upload failed: HTTP {response.status_code}")


async def _with_heartbeat[T](
    work: Awaitable[T],
    report_progress: ReportProgress,
    progress: Callable[[], int],
    interval: float,
) -> T:
    """Await `work`, calling report_progress(progress()) at least every `interval` s.

    The contract's heartbeat has to keep flowing while one long download or one
    large tile is being processed, not just between steps.

    asyncio.wait() does not cancel what it waits on, so when the job is cancelled or
    times out, `work` is cancelled here explicitly. Otherwise an in-flight upload would
    still complete a PUT for a job the gateway already considers dead. (A to_thread
    worker cannot be interrupted this way; run() stops it with a threading.Event.)
    """
    task = asyncio.ensure_future(work)
    # Consume the outcome so a cancelled or failed task is never reported as unretrieved.
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if done:
                return task.result()
            await report_progress(progress())
    finally:
        if not task.done():
            task.cancel()


async def run(
    request: Any, report_progress: ReportProgress, client: httpx.AsyncClient
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Run one cascade_mesh job. Returns (output dataset drafts, result_summary).

    Credit accounting stays in jobs.py.
    """
    from .lod2 import LOD2Params, generate_lod2  # lazy: needs the `geo` extra

    validate_request(request)
    interval = max(0.05, min(request.heartbeat_interval_seconds / 2, 5.0))
    upload = request.output_upload_urls[0]
    cancel = threading.Event()
    pipeline_pct = [0]  # written by the worker thread, read by the heartbeat

    with tempfile.TemporaryDirectory(prefix="lod2-", ignore_cleanup_errors=True) as tmp:
        workdir = Path(tmp)
        paths = {key: workdir / name for key, _, name in _INPUTS}
        output = workdir / "lod2.city.json"

        downloads = asyncio.gather(
            *(
                _download(client, key, request.input_datasets[key].signed_url, paths[key])
                for key, _, _ in _INPUTS
            )
        )
        await _with_heartbeat(downloads, report_progress, lambda: 5, interval)
        await report_progress(10)

        params = LOD2Params(
            input_building=str(paths["building_outline"]),
            input_roof=str(paths[ROOF_STRUCTURE_KEY]),
            input_dsm=str(paths["dsm"]),
            input_dtm=str(paths["dtm"]),
            output_file=str(output),
            # Never ship a solid that failed validation: nothing downstream can tell,
            # since result_summary carries no quality signal yet.
            on_invalid="skip",
        )

        def on_progress(_message: str, percent: int) -> None:
            pipeline_pct[0] = percent

        try:
            report = await _with_heartbeat(
                asyncio.to_thread(generate_lod2, params, on_progress, cancel.is_set),
                report_progress,
                lambda: 10 + int(pipeline_pct[0] * 0.8),  # pipeline spans 10..88
                interval,
            )
        finally:
            # Cancelled or timed out: stop the worker thread between buildings
            # instead of letting it run on after the job is gone.
            cancel.set()

        log.info("job %s: LOD2 %s", request.job_id, report.summary)
        _check_skipped(report.n_skipped, report.n_buildings_in)
        if report.n_skipped:
            log.warning(
                "job %s: %d of %d buildings failed validation and were left out",
                request.job_id,
                report.n_skipped,
                report.n_buildings_in,
            )

        await report_progress(90)
        await _with_heartbeat(
            _upload(client, upload.url, output), report_progress, lambda: 90, interval
        )
        size_bytes = output.stat().st_size

    if report.crs_epsg is None:  # generate_lod2 sets it or raises; never guess a CRS
        raise RuntimeError("LOD2 report carries no CRS")
    crs = f"EPSG:{report.crs_epsg}"
    draft: dict[str, Any] = {
        "name": "lod2-buildings",
        # `cityjson` is not in this service's output_formats, so the upload slot's
        # format is the only contract-valid value. naraga-converter produces the
        # real glTF/3D Tiles.
        "dataset_format": str(upload.output_format),
        "dataset_role": "mesh",
        "storage_key": upload.storage_key,  # under request.output_prefix
        "size_bytes": size_bytes,
        "crs": crs,
        "bbox": None,
    }
    return [draft], None
