# syntax=docker/dockerfile:1.7
# Production image. Same conda-forge base as dev so what you test is what ships.
FROM mambaorg/micromamba:1.5.10-noble

USER root
RUN apt-get update && apt-get install -y --no-install-recommends curl tini \
 && rm -rf /var/lib/apt/lists/*
USER $MAMBA_USER

WORKDIR /app

RUN micromamba install -y -n base -c conda-forge \
      python=3.12 gdal=3.9 pdal=2.8 python-pdal laspy \
 && micromamba clean --all --yes

COPY --chown=$MAMBA_USER:$MAMBA_USER pyproject.toml ./
COPY --chown=$MAMBA_USER:$MAMBA_USER src ./src
# '.[geo]' on purpose: the real run_job imports torch/rasterio/etc., and a prod image
# missing them fails at runtime while dev (which installs the extra) passes. The image
# is large; that is the honest cost of shipping the pipeline's dependencies.
RUN micromamba run -n base python -m pip install --no-cache-dir '.[geo]'

# Durable job state (SQLite) lives here; MOUNT A VOLUME over it in deployment —
# a rescheduled pod loses un-mounted container disk, and with it the callback outbox.
RUN mkdir -p /app/data

ENV PATH=/opt/conda/bin:$PATH PYTHONUNBUFFERED=1
EXPOSE 8084

# tini as PID 1 so SIGTERM reaches uvicorn and in-flight work drains.
ENTRYPOINT ["/usr/bin/tini", "--"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:8084/health || exit 1
CMD ["micromamba", "run", "-n", "base", "uvicorn", "reconstruction_3d.main:app", \
     "--host", "0.0.0.0", "--port", "8084", "--timeout-graceful-shutdown", "30"]
