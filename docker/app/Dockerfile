# crawler2 development/application image.
# Dependencies come from the pinned exports of uv.lock (requirements*.txt);
# regenerate them with `make requirements` after changing pyproject.toml.
FROM docker.io/library/python:3.12.11-slim-bookworm AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /src
COPY contracts/ contracts/
COPY requirements.txt requirements-dev.txt ./
ARG REQUIREMENTS=requirements-dev.txt
RUN pip install -r "${REQUIREMENTS}"

COPY pyproject.toml README.md ./
COPY crawler2/ crawler2/
RUN pip install --no-deps .

FROM docker.io/library/python:3.12.11-slim-bookworm AS runtime

ARG APP_UID=10001
RUN groupadd --gid "${APP_UID}" crawler2 \
 && useradd --no-log-init --uid "${APP_UID}" --gid crawler2 --home-dir /app --shell /usr/sbin/nologin crawler2 \
 && install -d -o crawler2 -g crawler2 /app /var/lib/crawler2/scratch

COPY --from=build /opt/venv /opt/venv
COPY --chown=crawler2:crawler2 pyproject.toml /app/
COPY --chown=crawler2:crawler2 tests/ /app/tests/

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CRAWLER2_SCRATCH_DIR=/var/lib/crawler2/scratch

WORKDIR /app
USER crawler2

HEALTHCHECK --interval=30s --timeout=20s --start-period=30s --retries=3 CMD ["crawler2-check"]
CMD ["sh", "-c", "crawler2-check --wait 180 && exec sleep infinity"]
