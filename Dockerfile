# Python 3.12 matches the version every CI workflow qualifies against.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependency metadata first, so edits to source do not invalidate the wheel cache.
COPY pyproject.toml requirements.lock ./
COPY app/__init__.py app/__init__.py

RUN pip install --no-cache-dir '.[test,postgresql,marketdata]' \
    && pip check

COPY . .

# Install again so console entry points resolve against the full source tree.
RUN pip install --no-cache-dir --no-deps -e .

# The runtime exposes no long-lived server process: the executables in this image
# are the qualification and operations tools under tools/. Routing orders requires
# release authority, which an image cannot grant.
RUN useradd --create-home --uid 10001 astra && chown -R astra:astra /app
USER astra

CMD ["python", "tools/system_status.py", "--show"]
